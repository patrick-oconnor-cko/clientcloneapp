#!/usr/bin/env python3
"""
Clone apply — walk a plan, resolve id placeholders, execute (or simulate) each call.

SAFETY MODEL
  - dry_run defaults to True. In dry-run NOTHING leaves the process: no sockets are
    opened. Placeholders resolve against synthetic ids so the substitution mechanism is
    exercised end to end without writing.
  - A resolved path or body still containing `{{` is a hard failure. The step is not
    sent, and the run stops.
  - Every step is journalled — request, response, elapsed, error — so a partial run can
    be inspected and either resumed or cleaned up. There is no API rollback.

Deliberately self-contained: standard library only, no shared modules.
"""
import json, os, re, time, pathlib, urllib.request, urllib.error, hashlib

PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_\-]+)\}\}")

# CAT ids are <prefix>_<26 lowercase alphanumerics>. Synthetic dry-run ids mimic that
# shape so any length/pattern validation downstream is genuinely exercised.
def synth_id(source_id, seq):
    prefix = source_id.split("_", 1)[0] if "_" in source_id else "id"
    h = hashlib.sha256(f"{source_id}:{seq}".encode()).hexdigest()
    body = ("dry" + h)[:26]
    return f"{prefix}_{body}"


def substitute(value, id_map):
    """Recursively replace {{source_id}} with the mapped new id.

    Unmapped placeholders are left intact so the caller can detect them.
    """
    if isinstance(value, str):
        def rep(m):
            return id_map.get(m.group(1), m.group(0))
        return PLACEHOLDER.sub(rep, value)
    if isinstance(value, dict):
        return {k: substitute(v, id_map) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, id_map) for v in value]
    return value


def unresolved(*objs):
    found = set()
    for o in objs:
        found |= set(PLACEHOLDER.findall(json.dumps(o)))
    return sorted(found)


# Verbs a plan may use. GET is needed to resolve ids CAT mints on the target that no
# create step returns (the entity's vault account). PUT is allowed for enabling a service.
# DELETE is deliberately absent: removing things is clone_cleanup.py's job, behind its own
# confirm gate, and a plan must never be able to destroy anything.
ALLOWED_METHODS = {"GET", "POST", "PUT"}

# Where a step's bearer comes from. Every step is a CAT call unless it says otherwise. A
# step that targets the Checkout sandbox API declares auth="sandbox_secret" (or
# "sandbox_public" for the few tokenisation endpoints that take a public key) and is then
# sent with that key instead of the CAT token, against SANDBOX_API_BASE unless the step
# names its own base. The keys are typed into the page per session — never stored, never
# journalled — and a sandbox step with no key is BLOCKED, never sent with the CAT token.
SANDBOX_API_BASE = "https://api.sandbox.checkout.com"
AUTH_MODES = {"cat", "sandbox_secret", "sandbox_public"}
SANDBOX_KEY_PREFIX = {"sandbox_secret": "sk_sbox_", "sandbox_public": "pk_sbox_"}
SANDBOX_KEY_LABEL = {"sandbox_secret": "Sandbox Secret Key",
                     "sandbox_public": "Sandbox Public Key"}


def bearer_for(step, token, sandbox_keys):
    """Return (bearer, base_override, error) for a step's auth mode.

    error is set — and bearer is None — when the step cannot be authenticated as declared;
    the caller blocks the step. base_override is the sandbox API host for sandbox modes
    and None for CAT.
    """
    mode = step.get("auth") or "cat"
    if mode not in AUTH_MODES:
        return None, None, f"unknown auth mode {mode!r}"
    if mode == "cat":
        return token, None, None
    key = (sandbox_keys or {}).get(mode) or ""
    if not key:
        return None, None, (f"this step calls the sandbox API and needs the "
                            f"{SANDBOX_KEY_LABEL[mode]} — supply it in the side panel")
    if not key.startswith(SANDBOX_KEY_PREFIX[mode]):
        return None, None, (f"{SANDBOX_KEY_LABEL[mode]} does not look like a sandbox key "
                            f"(expected {SANDBOX_KEY_PREFIX[mode]}…) — refused")
    return key, SANDBOX_API_BASE, None


def _send(base, token, method, path, body=None, timeout=30):
    method = (method or "POST").upper()
    if method not in ALLOWED_METHODS:
        return {}, 0, f"method {method} is not permitted in a plan"
    data = None if (method == "GET" or body is None) else json.dumps(body).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}"), r.status, None
    except urllib.error.HTTPError as e:
        return {}, e.code, e.read().decode()[:400]
    except Exception as e:
        return {}, 0, str(e)


# ---------------------------------------------------------------- verification
#
# Some source configuration must not be recreated, only checked: a read of the clone
# after the fact, compared with what the source had, with differences recorded as
# run-time flags for the post-run report. A step declares this with
#   verify = {"compare": <name below>, "expected": [...]}
# and clone_capture guarantees `expected` is already normalised. Nothing here writes.

def _normalise_payout_route(item):
    """Same reduction as clone_capture.normalise_payout_route — duplicated because the
    modules are deliberately self-contained. Keep the two in step."""
    if not isinstance(item, dict):
        return None
    def pick(*keys):
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    country = pick("country_iso3_code", "country_value", "country_code", "country")
    currency = pick("currency_code", "currency_value", "currency", "currency_label")
    schemes = item.get("schemes_label") or item.get("schemes") or ""
    if isinstance(schemes, list):
        schemes = ", ".join(str(s) for s in schemes)
    if not country or not currency:
        return None
    return {"country": country.upper(), "currency": currency.upper(),
            "schemes": str(schemes).strip(),
            "label": f"{pick('country_label') or country.upper()} / {currency.upper()}"}


def verify_payout_routes(expected, resp):
    """Compare the source's enabled payout corridors with the clone's.

    A corridor is (country, currency). A source corridor absent on the clone is flagged
    `payout_route_missing_on_clone` — it cannot be created by this tool and has to be
    raised with the Payouts team. Same corridor but different schemes is flagged
    separately, because the destination will exist but may not support the same
    networks. Extra corridors on the clone are noted, not flagged: provisioned
    capability the clone happens to have is not a fidelity gap.

    Returns (flags, summary).
    """
    # HAL list, bare {"data": [...]}, or a bare list — the endpoint declares no schema.
    if isinstance(resp, list):
        items = resp
    elif isinstance(resp, dict):
        items = ((resp.get("_embedded") or {}).get("data"))
        if items is None:
            items = resp.get("data")
    else:
        items = None
    have = {}
    for it in (items or []):
        n = _normalise_payout_route(it)
        if n:
            have[(n["country"], n["currency"])] = n
    flags = []
    for exp in expected or []:
        key = (exp["country"], exp["currency"])
        if key not in have:
            flags.append({
                "code": "payout_route_missing_on_clone",
                "message": f"payout corridor {exp['label']} is enabled on the source but "
                           f"not on the clone — this is provisioned capability and cannot "
                           f"be created here; raise with the Payouts team",
                "kind": "payout_route", "country": exp["country"],
                "currency": exp["currency"], "schemes": exp.get("schemes"),
                "action": "not_created"})
        elif (exp.get("schemes") or "") != (have[key].get("schemes") or ""):
            flags.append({
                "code": "payout_route_schemes_differ",
                "message": f"payout corridor {exp['label']} exists on the clone but with "
                           f"different schemes — source: {exp.get('schemes') or '-'}; "
                           f"clone: {have[key].get('schemes') or '-'}",
                "kind": "payout_route", "country": exp["country"],
                "currency": exp["currency"], "source_schemes": exp.get("schemes"),
                "clone_schemes": have[key].get("schemes"), "action": "carried"})
    extra = sorted(set(have) - {(e["country"], e["currency"]) for e in expected or []})
    summary = {"expected": len(expected or []), "found_on_clone": len(have),
               "missing": sum(1 for f in flags if f["code"] == "payout_route_missing_on_clone"),
               "schemes_differ": sum(1 for f in flags if f["code"] == "payout_route_schemes_differ"),
               "extra_on_clone": [f"{c}/{cur}" for c, cur in extra]}
    return flags, summary


def verify_compass_settings(expected, resp):
    """Did the Compass settings POST actually take?

    A 2xx on the POST is not proof: the support site says a client's display currency
    "cannot be changed" once set, so if the clone came up with a default, the source's
    value may have been silently ignored. Flags a differing display_currency and any
    conversion currencies the source has that the clone lacks. Extra conversion
    currencies on the clone are noted, not flagged.

    Returns (flags, summary).
    """
    resp = resp if isinstance(resp, dict) else {}
    expected = expected or {}
    flags = []
    want_disp = (expected.get("display_currency") or "").upper() or None
    have_disp = (resp.get("display_currency") or "").upper() or None
    if want_disp and have_disp != want_disp:
        flags.append({
            "code": "display_currency_differs",
            "message": f"Compass display_currency is {have_disp or 'unset'} on the clone "
                       f"but {want_disp} on the source — CAT may not allow it to change "
                       f"once set; raise with the Dashboard/Compass owners",
            "kind": "client_compass_settings", "source": want_disp,
            "clone": have_disp, "action": "carried"})
    want_conv = {str(c).upper() for c in (expected.get("conversion_currencies") or [])}
    have_conv = {str(c).upper() for c in (resp.get("conversion_currencies") or [])}
    missing = sorted(want_conv - have_conv)
    if missing:
        flags.append({
            "code": "conversion_currencies_differ",
            "message": f"Compass conversion_currencies on the clone lack "
                       f"{', '.join(missing)}, which the source has",
            "kind": "client_compass_settings", "missing": missing,
            "extra_on_clone": sorted(have_conv - want_conv), "action": "carried"})
    summary = {"display_currency": {"source": want_disp, "clone": have_disp},
               "conversion_currencies": {"expected": len(want_conv),
                                         "found_on_clone": len(have_conv),
                                         "missing": len(missing),
                                         "extra_on_clone": sorted(have_conv - want_conv)}}
    return flags, summary


def _network_token_form_values(form):
    """Same flattening as clone_capture.network_token_form_values — duplicated because
    the modules are deliberately self-contained. Keep the two in step."""
    if not isinstance(form, dict) or not isinstance(form.get("schema"), dict):
        return None
    fields = form["schema"].get("form_fields")
    if not isinstance(fields, list):
        return None
    vals = {}
    def walk(items):
        for f in items or []:
            if not isinstance(f, dict):
                continue
            sec = f.get("form_section")
            if isinstance(sec, dict):
                walk(sec.get("form_fields")); continue
            name = f.get("name")
            if not name or f.get("display_style") == "plainText":
                continue
            if "value" in f:
                vals[name] = f["value"]
    walk(fields)
    return vals or None


def verify_network_tokens(expected, resp):
    """Did the network-tokens POST take? Re-reads the form and compares the switches.

    Scheme onboarding may still be PENDING on the clone — that shows in plainText status
    lines, which are skipped, so it is not reported as a difference. Only the operator-set
    values are compared: nt_state, provisioning_state, default_provision_mode, and the
    enabled_visa / enabled_mastercard switches.

    Returns (flags, summary).
    """
    have = _network_token_form_values(resp) or {}
    expected = expected or {}
    # The portal's UPDATE form spells the scheme switches enabled_*; CAT's CREATE-shaped
    # form spells them onboard_*. Accept either, so a shape difference is not reported as
    # a configuration difference.
    pairs = {"nt_state": ("nt_state",),
             "provisioning_state": ("provisioning_state",),
             "default_provision_mode": ("default_provision_mode",),
             "enabled_visa": ("scheme_configuration.enabled_visa",
                              "scheme_configuration.onboard_visa"),
             "enabled_mastercard": ("scheme_configuration.enabled_mastercard",
                                    "scheme_configuration.onboard_mastercard")}
    differ = {}
    for k, names in pairs.items():
        want = expected.get(k)
        if want is None:
            continue
        got = next((have[n] for n in names if have.get(n) is not None), None)
        if got != want:
            differ[k] = {"source": want, "clone": got}
    flags = []
    if not have:
        flags.append({"code": "network_tokens_differ",
                      "message": "network tokens: the clone's form could not be read back, "
                                 "so the configuration cannot be confirmed",
                      "kind": "client_network_tokens", "differences": differ,
                      "action": "carried"})
    elif differ and all(v["clone"] is None for v in differ.values()) \
            and len(differ) == sum(1 for k in pairs if expected.get(k) is not None):
        # Every compared value is absent: the create returned 2xx but nothing reached the
        # store this read consults. Seen live — CAT's POST persisted into CAT's own store
        # while the NT portal, where the configuration actually lives, stayed blank.
        flags.append({"code": "network_tokens_not_persisted",
                      "message": "network tokens: the create returned 2xx but the "
                                 "configuration is absent where it was read back — "
                                 "nothing persisted in the store that matters. CAT's "
                                 "/network-tokens and the NT portal are separate stores; "
                                 "the write must target the portal.",
                      "kind": "client_network_tokens", "differences": differ,
                      "action": "not_created"})
    elif differ:
        desc = "; ".join(f"{k}: source={v['source']!r} clone={v['clone']!r}"
                         for k, v in differ.items())
        flags.append({"code": "network_tokens_differ",
                      "message": f"network tokens on the clone differ from what was "
                                 f"POSTed — {desc}",
                      "kind": "client_network_tokens", "differences": differ,
                      "action": "carried"})
    summary = {"compared": sorted(k for k in pairs if expected.get(k) is not None),
               "differences": differ, "form_read": bool(have)}
    return flags, summary


VERIFIERS = {"payout_routes": verify_payout_routes,
             "compass_settings": verify_compass_settings,
             "network_tokens": verify_network_tokens}


def dig(obj, dotted):
    """Read a dotted path out of a response. Returns None if any hop is missing."""
    cur = obj
    for part in (dotted or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class Journal:
    """Crash-safe append-only journal.

    Written one JSON line per step, flushed immediately. A live run that dies mid-way
    still leaves a complete record of what was created — which matters because CAT
    offers no rollback, so this file is the only basis for cleanup or resume.
    """
    def __init__(self, path=None, header=None):
        self.path = path
        self.fh = None
        if path:
            p = pathlib.Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.fh = p.open("a", encoding="utf-8")
            self._write({"_header": header or {}})

    def _write(self, obj):
        if self.fh:
            self.fh.write(json.dumps(obj) + "\n")
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def append(self, entry):
        self._write(entry)

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def apply_plan(plan, base=None, token=None, dry_run=True, stop_on_error=True,
               pace_seconds=0.35, manual_values=None, journal_path=None,
               sandbox_keys=None, on_step=None):
    """Execute or simulate a clone plan.

    on_step: optional callable(entry, total_steps), invoked once per step as its journal
    entry is written — the same entry, same order, same moment. This is how the page shows
    live progress; a failing callback is swallowed so it can never affect the run.

    dry_run=True (the default) opens no sockets at all.

    manual_values: {"<step_seq>.<field>": value} — merged into a step's body before
    sending. This is how write-only fields (which capture cannot read) get supplied.

    sandbox_keys: {"sandbox_secret": "sk_sbox_…", "sandbox_public": "pk_sbox_…"} — the
    Checkout sandbox API keys typed into the page, used ONLY by steps whose auth names
    them (see bearer_for). Optional; a step that needs one and has none is blocked.

    journal_path: for live runs, where to append the crash-safe journal. Strongly
    recommended: without it, a mid-run failure leaves created objects unrecorded.

    Returns a run document: id_map, journal, counts, created objects, and any problems.
    """
    if not dry_run and (not base or not token):
        raise ValueError("base and token are required for a real (non-dry-run) apply")

    id_map, journal, problems = {}, [], []
    created_objs = []
    # Run-time findings from verify steps — the apply-side half of the post-run report.
    # Plan-time flags live in plan["flags"]; these are what could only be learned by
    # reading the clone after it existed.
    run_flags = []
    created, failed, skipped = 0, 0, 0
    manual_values = manual_values or {}
    t_start = time.time()
    jrn = Journal(journal_path, {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": dry_run, "steps": len(plan.get("steps", [])),
        "source_client": (plan.get("source") or {}).get("client_id"),
        "target_client_name": (plan.get("target") or {}).get("client_name"),
        # Plan-time findings travel WITH the run. Without these, a run that completed
        # 86/86 could hide a feature the plan silently skipped (it did: network tokens),
        # and the journal — the only durable record — would show nothing wrong.
        "plan_flags": plan.get("flags") or [],
        "plan_skipped": plan.get("skipped") or [],
        "plan_counts": plan.get("counts") or {},
        # WHICH keys were supplied, never their values — the journal is a durable file.
        "sandbox_keys_supplied": sorted(k for k, v in (sandbox_keys or {}).items() if v)})
    total_steps = len(plan.get("steps", []))

    def record(entry):
        """Journal an entry (in-memory + crash-safe file) and tell the progress listener."""
        journal.append(entry); jrn.append(entry)
        if on_step:
            try:
                on_step(entry, total_steps)
            except Exception:
                pass

    for step in plan.get("steps", []):
        seq, kind = step["seq"], step["kind"]
        body = dict(step.get("body") or {})

        # merge any hand-supplied values for this step
        for key, val in manual_values.items():
            s, _, field = key.partition(".")
            if s.isdigit() and int(s) == seq and field:
                body[field] = val

        path = substitute(step["path"], id_map)
        body = substitute(body, id_map)

        miss = unresolved(path, body)
        entry = {"seq": seq, "kind": kind, "op": step.get("op"),
                 "method": step["method"], "path": path}
        # Which credential this step is sent with. Resolved before the dry-run branch so
        # a dry run shows a sandbox step blocked for a missing key — that is the point of
        # a dry run — instead of it surfacing live.
        bearer, auth_base, auth_err = bearer_for(step, token, sandbox_keys)
        if (step.get("auth") or "cat") != "cat":
            entry["auth"] = step["auth"]

        if miss:
            entry.update(status="blocked", error=f"unresolved placeholders: {', '.join(miss)}")
            problems.append(f"step {seq} ({kind}): unresolved {', '.join(miss)}")
        elif auth_err:
            entry.update(status="blocked", error=auth_err)
            problems.append(f"step {seq} ({kind}): {auth_err}")
        if miss or auth_err:
            record(entry); failed += 1
            if stop_on_error:
                skipped = len(plan["steps"]) - seq
                break
            continue

        if dry_run:
            # No socket is opened. Mint a synthetic id only where the step actually
            # provides one, so later steps can resolve their references.
            entry.update(status="dry-run", body=body, elapsed_ms=0)
            if step.get("verify"):
                # Nothing to compare against without a response; say what would happen.
                entry["would_verify"] = {
                    "compare": step["verify"].get("compare"),
                    "expected": len(step["verify"].get("expected") or [])}
            if step.get("provides"):
                new_id = synth_id(step["provides"], seq)
                id_map[step["provides"]] = new_id
                entry["would_create"] = new_id
            record(entry); created += 1
            continue

        t0 = time.time()
        # Some ids are minted by CAT on the target asynchronously (the entity's vault
        # account appears ~10s after the client is created), so a step may declare a
        # retry rather than failing the whole run on a race it can wait out.
        rr = step.get("retry") or {}
        attempts = max(1, int(rr.get("attempts", 1)))
        delay = float(rr.get("delay_seconds", 2))
        method = step.get("method") or "POST"
        for attempt in range(1, attempts + 1):
            # A step may name its own base: the network-tokens portal is a different host
            # from CAT, and it — not CAT — is where that configuration is read from. A
            # sandbox-API step gets the sandbox host and its key from bearer_for.
            resp, code, err = _send(step.get("base") or auth_base or base, bearer, method,
                                    path, None if method.upper() == "GET" else body)
            ok = 200 <= code < 300
            # a lookup that succeeds but has not been provisioned yet is not done
            if ok and step.get("provides"):
                got = dig(resp, step["provides_from"]) if step.get("provides_from") \
                    else resp.get("id")
                ok = got is not None
            if ok or attempt == attempts:
                break
            time.sleep(delay)
        if attempts > 1:
            entry["attempts"] = attempt
        entry.update(status=code, body=body, elapsed_ms=int((time.time() - t0) * 1000))
        if step.get("base"):
            entry["base"] = step["base"]
        # For steps that mint no id, the response is the only evidence of what CAT did
        # with the body. A 201 that persisted nowhere useful looked identical to success
        # until this was journalled.
        if not step.get("provides") and resp not in (None, {}):
            entry["response_excerpt"] = json.dumps(resp)[:1500]
        writes = method.upper() != "GET"
        if 200 <= code < 300:
            new_id = dig(resp, step["provides_from"]) if step.get("provides_from") \
                else resp.get("id")
            entry["created_id" if writes else "resolved_id"] = new_id
            if step.get("provides") and new_id:
                id_map[step["provides"]] = new_id
            elif step.get("provides"):
                # A lookup that resolved nothing is a hard failure, not a warning: every
                # downstream step referencing it would block anyway, and stopping here
                # names the real cause instead of surfacing an unresolved placeholder.
                entry["error"] = (f"{'created but response had no id' if writes else 'lookup returned no value at ' + str(step.get('provides_from'))}"
                                  f"; downstream steps that reference it cannot resolve")
                problems.append(f"step {seq} ({kind}): {entry['error']}")
                failed += 1
                record(entry)
                if stop_on_error:
                    skipped = len(plan["steps"]) - seq
                    break
                continue
            created += 1
            # A verify step compares the clone with the source now that the clone
            # exists. Differences are flags, never failures — the run continues.
            v = step.get("verify")
            if v:
                fn = VERIFIERS.get(v.get("compare"))
                if fn is None:
                    entry["verify_error"] = f"unknown verifier {v.get('compare')!r}"
                    problems.append(f"step {seq} ({kind}): {entry['verify_error']}")
                else:
                    vflags, vsummary = fn(v.get("expected") or [], resp)
                    for f in vflags:
                        f.setdefault("seq", seq); f.setdefault("entity", step.get("entity"))
                    entry["verify"] = vsummary
                    if vflags:
                        entry["flags"] = vflags
                    run_flags.extend(vflags)
            # Only real writes belong here — created_objects is what clone_cleanup
            # reverses, and a GET created nothing to reverse.
            if writes:
                created_objs.append({"seq": seq, "kind": kind, "label": step.get("label"),
                                     "new_id": new_id, "path": path})
        else:
            entry["error"] = err
            failed += 1
            if step.get("optional"):
                # An OPTIONAL step is an enhancement nothing else depends on (network
                # tokens, say). Its failure is a finding for the report, not a reason to
                # abandon the remaining steps and leave a partial clone.
                f = {"code": "optional_step_failed", "kind": kind, "seq": seq,
                     "entity": step.get("entity"), "http": code,
                     "error": (err or "")[:400], "action": "not_created",
                     "message": f"step {seq} ({kind}) failed with HTTP {code} and was "
                                f"skipped — it is optional, so the run continued: "
                                f"{(err or '')[:160]}"}
                entry["flags"] = [f]
                run_flags.append(f)
                record(entry)
                continue
            problems.append(f"step {seq} ({kind}): HTTP {code} {(err or '')[:120]}")
            record(entry)
            if stop_on_error:
                skipped = len(plan["steps"]) - seq
                break
            continue
        record(entry)
        time.sleep(pace_seconds)

    jrn.append({"_footer": {"created": created, "failed": failed,
                            "not_attempted": skipped,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}})
    jrn.close()
    return {
        "dry_run": dry_run,
        "journal_path": journal_path,
        "created_objects": created_objs,
        "plan_version": plan.get("plan_version"),
        "source_client": (plan.get("source") or {}).get("client_id"),
        "target_client_name": (plan.get("target") or {}).get("client_name"),
        "counts": {"steps": len(plan.get("steps", [])), "created": created,
                   "failed": failed, "not_attempted": skipped},
        "id_map": id_map,
        "journal": journal,
        "problems": problems,
        "flags": run_flags,
        "elapsed_seconds": round(time.time() - t_start, 2),
    }


def render_run(run, verbose=False):
    """Human-readable summary of a run — used for dry-run review."""
    L = [f"{'DRY RUN' if run['dry_run'] else 'LIVE RUN'} · "
         f"{run['counts']['created']}/{run['counts']['steps']} steps · "
         f"{run['counts']['failed']} failed · {run['elapsed_seconds']}s", ""]
    for e in run["journal"]:
        mark = {"dry-run": "·", "blocked": "!"}.get(e.get("status"), "")
        if isinstance(e.get("status"), int):
            mark = "+" if 200 <= e["status"] < 300 else "!"
        L.append(f" {mark} {e['seq']:>3} {e['kind']:<22} {e['method']} {e['path']}")
        if e.get("would_create"): L.append(f"       -> {e['would_create']}")
        if e.get("created_id"):   L.append(f"       -> {e['created_id']}")
        if e.get("error"):        L.append(f"       !! {e['error']}")
        if verbose and e.get("body"):
            for line in json.dumps(e["body"], indent=2).splitlines():
                L.append("       " + line)
    if run["problems"]:
        L += ["", "problems:"] + [f"  - {p}" for p in run["problems"]]
    return "\n".join(L)
