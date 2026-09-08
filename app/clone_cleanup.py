#!/usr/bin/env python3
"""
Clone cleanup — undo (as far as CAT allows) what a clone run created.

Reads a run journal written by clone_apply.py and reverses it. Like clone_apply, this
defaults to dry_run=True and opens no sockets unless explicitly told otherwise.

WHAT CAT ACTUALLY ALLOWS
------------------------
Cleanup is *partial by design*. Derived from the CAT swagger, the objects a clone creates
fall into three tiers:

  DELETE            currency account, payment routing rule, payout routing rule,
                    payout setting, payout route
  DEACTIVATE ONLY   client, entity, processing profile, sessions channel
                    (PUT .../status -> Inactive; no DELETE exists)
  NEITHER           processing channel, processor

Nothing here is truly deleted except the five DELETE kinds. A client and an entity can be
set Inactive but never removed, so every live run leaves a permanent record. Processing
channels and processors cannot even be deactivated — their PUT schemas carry no status
field and they have no /status endpoint. Plan a live run accordingly.

Order matters: reverse creation order, so referencing objects go before referenced ones.
"""
import base64, json, os, time, pathlib, urllib.request, urllib.error

# Valid values, confirmed live from GET /configuration/statuses-and-reasons:
#   statuses: Active, Rejected, Pending, Inactive, RequirementsDue
#   reasons:  ... Duplicate, ErrorInSetUp, SwitchingEntities, Suspended, ...
INACTIVE = "Inactive"
REASON = "Duplicate"

# kind -> how to remove it.
#   mode "delete": DELETE path (formatted from the journal entry), optional body
#   mode "status": PUT <path>/status with {"status": ...}
#   mode "none":   not removable via the API at all
REMOVAL = {
    # NOTE: DeleteCurrencyAccountRequest in the swagger declares only `reason`, but CAT
    # rejects that with `version_required` (422) — the spec is incomplete here. `version`
    # is resolved at run time by resolve_version() below.
    "currency_account":     {"mode": "delete", "path": "/currency-accounts/{id}",
                             "body": {"reason": "clone cleanup"},
                             "needs_version": True},
    "payment_routing_rule": {"mode": "delete", "path": "/payment-routing-rules/{id}",
                             "body": {"reason": "clone cleanup", "version": 0}},
    "payout_routing_rule":  {"mode": "delete", "path": "/payout-routing-rules/{id}",
                             "body": {"reason": "clone cleanup"}},
    "payout_setting":       {"mode": "delete",
                             "path": "/entities/{entity}/payout-settings/{id}"},
    "payout_route":         {"mode": "delete",
                             "path": "/entities/{entity}/payout-routes/{country}/{currency}"},
    # status_reason values come from GET /configuration/statuses-and-reasons.
    # "Duplicate" is the apt one for a clone that is being unwound.
    "entity":               {"mode": "status", "path": "/entities/{id}/status",
                             "body": {"status": INACTIVE, "status_reason": REASON}},
    "client":               {"mode": "status", "path": "/clients/{id}/status",
                             "body": {"status": INACTIVE, "status_reason": REASON}},
    "processing_profile":   {"mode": "status", "path": "/processing-profiles/{id}/status",
                             "body": {"status": INACTIVE}},
    "sessions_channel":     {"mode": "status",
                             "path": "/sessions-processing-channels/{id}/status",
                             "body": {"status": INACTIVE}},
    # Genuinely no route: the PUT for a channel uses
    # UpdateGatewayAndSessionsProcessingChannelRequest, which has no status field, and
    # there is no /processing-channels/{id}/status. Same for a processor. The only lever
    # is disabling their individual features, which is a different thing from
    # deactivation and is not attempted here.
    "processing_channel":   {"mode": "none",
                             "why": "no DELETE and no /status endpoint; the channel PUT "
                                    "schema has no status field"},
    "processor":            {"mode": "none",
                             "why": "no DELETE and no /status endpoint; the processor PUT "
                                    "schema has no status field"},
    # Enabling an entity service is a PUT with {is_enabled: true}. Setting it back to
    # false is NOT the same as undoing it — the source may have had it enabled all
    # along, and cleanup has no way to know what the prior state was. Recorded as
    # permanent rather than guessing.
    "entity_service":       {"mode": "none",
                             "why": "a service was enabled on the entity; cleanup cannot "
                                    "know whether it was already enabled beforehand"},
    "client_risk_settings": {"mode": "none",
                             "why": "the Fraud Detection tier was set on the client; "
                                    "cleanup cannot know the prior tier, and the client "
                                    "is deactivated anyway"},
    "client_compass_settings": {"mode": "none",
                                "why": "Compass settings have no delete route; the client "
                                       "is deactivated anyway"},
    "client_flow_account":   {"mode": "none",
                              "why": "the Flow account flag was set on the client; the "
                                     "client is deactivated anyway"},
    "client_network_tokens": {"mode": "none",
                              "why": "network tokens were enabled and scheme onboarding "
                                     "started; neither can be undone by API, and the "
                                     "client is deactivated anyway"},
    # Webhooks live in the Checkout sandbox API, not CAT: DELETE /workflows/{id} exists
    # but needs the DESTINATION secret key, which cleanup (CAT-token only) does not carry.
    # Recorded as not removable here rather than half-supported.
    # The destination's API keys and the crypto key they were encrypted with. DELETE routes
    # exist for the crypto key (/clients/{client}/public-keys/{id}) but this map has no
    # client-scoped template and none of it has a known-good live call; the client is
    # deactivated anyway, which disables its keys.
    "client_public_crypto_key": {"mode": "none",
                                 "why": "the run's RSA public key on the clone; the client "
                                        "is deactivated anyway — delete in CAT if wanted"},
    "client_api_secret_key":    {"mode": "none",
                                 "why": "the clone's API secret key; deactivating the client "
                                        "disables it — delete in CAT if wanted"},
    "client_api_public_key":    {"mode": "none",
                                 "why": "the clone's API public key; deactivating the client "
                                        "disables it — delete in CAT if wanted"},
    "webhook_workflow":     {"mode": "none",
                             "why": "a Workflow in the Checkout sandbox API, not CAT — "
                                    "remove it in Dashboard > Developers > Workflows, or "
                                    "DELETE /workflows/{id} with the destination secret "
                                    "key; not attempted by this tool"},
}


# kind -> how to READ an object back, to prove what actually exists in CAT rather than
# trusting the POST response. All confirmed present in the swagger.
READBACK = {
    "client":               "/clients/{id}",
    "entity":               "/entities/{id}",
    "currency_account":     "/currency-accounts/{id}",
    "processing_profile":   "/processing-profiles/v2/{id}",
    "processing_channel":   "/processing-channels/{id}",
    "processor":            "/processing-channels/{parent}/processors/{id}",
    "payment_routing_rule": "/payment-routing-rules/{id}",
    "payout_routing_rule":  "/payout-routing-rules/{id}",
    "sessions_channel":     "/sessions-processing-channels/{id}",
}
# fields worth surfacing per kind, in preference order
NAME_FIELDS = ("name", "processing_profile_name", "holding_currency")


def _get(base, token, path, timeout=30):
    req = urllib.request.Request(base.rstrip("/") + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}"), r.status, None
    except urllib.error.HTTPError as e:
        return {}, e.code, e.read().decode()[:200]
    except Exception as e:
        return {}, 0, str(e)


def verify(created_objects, base, token, pace_seconds=0.25):
    """GET each created object back and report what CAT actually holds.

    Read-only. This is the authoritative view: a POST response can differ from stored
    state, and it also catches objects that were created but later removed.

    Returns rows with exists / status / name, plus the removal capability for each so the
    UI can offer per-item actions.
    """
    rows = []
    for obj in sorted(created_objects, key=lambda o: o.get("seq", 0)):
        kind, new_id = obj.get("kind"), obj.get("new_id")
        tmpl = READBACK.get(kind)
        row = {"seq": obj.get("seq"), "kind": kind, "label": obj.get("label"),
               "id": new_id}

        spec = REMOVAL.get(kind) or {}
        row["can"] = spec.get("mode", "none")
        if row["can"] == "none":
            row["why_not"] = spec.get("why", "")
        else:
            # The exact call the per-row button will make. Derived from build_cleanup so
            # the UI cannot advertise something different from what cleanup does.
            steps, _ = build_cleanup([obj])
            if steps:
                row["undo"] = {"method": steps[0]["method"], "path": steps[0]["path"],
                               "body": steps[0].get("body")}
            else:
                row["can"] = "none"
                row["why_not"] = "no removal call could be built for this object"

        if not tmpl or not new_id:
            row.update(exists=None,
                       note="no read-back route" if not tmpl else "no id recorded")
            rows.append(row)
            continue

        # processors are nested under their channel
        parent = None
        p = obj.get("path") or ""
        if kind == "processor" and "/processing-channels/" in p:
            parent = p.split("/processing-channels/", 1)[1].split("/", 1)[0]
        try:
            path = tmpl.format(id=new_id, parent=parent)
        except Exception as e:
            row.update(exists=None, note=f"could not build path: {e}")
            rows.append(row)
            continue
        if "{" in path:
            row.update(exists=None, note="read-back path needs a value we do not have")
            rows.append(row)
            continue

        d, code, err = _get(base, token, path)
        row["http"] = code
        if 200 <= code < 300:
            row.update(exists=True, status=d.get("status"),
                       name=next((d[f] for f in NAME_FIELDS if d.get(f)), None),
                       path=path)
        elif code == 404:
            row.update(exists=False, note="not found — already removed, or never created")
        else:
            row.update(exists=None, note=f"HTTP {code} {(err or '')[:80]}")
        rows.append(row)
        time.sleep(pace_seconds)

    summary = {"total": len(rows),
               "present": sum(1 for r in rows if r.get("exists") is True),
               "absent": sum(1 for r in rows if r.get("exists") is False),
               "unknown": sum(1 for r in rows if r.get("exists") is None),
               "removable": sum(1 for r in rows
                                if r.get("exists") is True and r.get("can") != "none")}
    return {"rows": rows, "summary": summary}


def read_journal(path):
    """Load a .jsonl run journal -> (header, entries, footer)."""
    header, footer, entries = {}, {}, []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if "_header" in d:
            header = d["_header"]
        elif "_footer" in d:
            footer = d["_footer"]
        else:
            entries.append(d)
    return header, entries, footer


def build_cleanup(created_objects, protected_ids=()):
    """Reverse-order removal steps for a list of created objects.

    created_objects: [{seq, kind, label, new_id, path}] as produced by clone_apply.
    protected_ids: ids that must NEVER be touched — normally every id belonging to the
        SOURCE client. Cleanup deactivates clients and entities, so a wrong id here would
        deactivate the account we cloned from. Anything in this set is refused outright
        rather than merely warned about.

    Returns (steps, unremovable).
    """
    protected = set(protected_ids or ())
    steps, unremovable = [], []
    for obj in sorted(created_objects, key=lambda o: -o.get("seq", 0)):
        kind, new_id = obj.get("kind"), obj.get("new_id")
        if new_id and new_id in protected:
            unremovable.append({**obj, "why": "REFUSED — this is a SOURCE id, not a "
                                              "created one. Cleanup will not touch the "
                                              "client it cloned from."})
            continue
        spec = REMOVAL.get(kind)
        if not spec or spec["mode"] == "none":
            unremovable.append({**obj, "why": (spec or {}).get(
                "why", f"no removal route known for kind '{kind}'")})
            continue
        if not new_id and kind not in ("payout_route",):
            unremovable.append({**obj, "why": "no id was recorded for this object"})
            continue

        # the entity id is embedded in the creation path for entity-scoped objects
        entity = None
        p = obj.get("path") or ""
        if "/entities/" in p:
            entity = p.split("/entities/", 1)[1].split("/", 1)[0]

        try:
            path = spec["path"].format(id=new_id, entity=entity,
                                       country="", currency="")
        except Exception as e:
            unremovable.append({**obj, "why": f"could not build removal path: {e}"})
            continue
        if "{" in path or "//" in path.replace("://", ""):
            unremovable.append({**obj,
                                "why": f"removal path incomplete ({path}) — needs values "
                                       "the journal does not record"})
            continue

        # Some deletes demand the object's current version (CAT returns version_required).
        # Carry the read-back path so run_cleanup can resolve it just before sending.
        readback = None
        rb_tmpl = READBACK.get(kind)
        if rb_tmpl:
            try:
                rb = rb_tmpl.format(id=new_id, parent=entity or "")
                readback = rb if "{" not in rb else None
            except Exception:
                readback = None

        steps.append({"seq": len(steps) + 1, "from_step": obj.get("seq"),
                      "kind": kind, "label": obj.get("label"), "target_id": new_id,
                      "method": "DELETE" if spec["mode"] == "delete" else "PUT",
                      "path": path, "body": spec.get("body"), "mode": spec["mode"],
                      "needs_version": bool(spec.get("needs_version")),
                      "readback": readback})
    return steps, unremovable


def etag_version(e_tag):
    """CAT's e_tag is base64 of `cv=<n>&rv=<n>`; rv is the row version.

        "Y3Y9MCZydj0x" -> cv=0&rv=1 -> 1

    Returns None if it cannot be decoded.
    """
    if not e_tag:
        return None
    try:
        raw = base64.b64decode(str(e_tag).strip('"')).decode()
        for part in raw.split("&"):
            k, _, v = part.partition("=")
            if k == "rv":
                return int(v)
    except Exception:
        pass
    return None


def resolve_version(base, token, kind, obj_id, readback_path):
    """Best-effort current version for a delete that demands one.

    UNVERIFIED for currency accounts: the swagger omits `version` from the delete body
    entirely, and the currency-accounts list response carries no e_tag. Order of attempts:
    an explicit `version` field, then the decoded e_tag, then 0 for a freshly created
    object. A wrong value should surface as a conflict rather than deleting the wrong
    thing, but this needs a live run to confirm.
    """
    if not readback_path:
        return 0
    d, code, _ = _get(base, token, readback_path)
    if not (200 <= code < 300):
        return 0
    if isinstance(d.get("version"), int):
        return d["version"]
    v = etag_version(d.get("e_tag"))
    return v if v is not None else 0


def _send(base, token, method, path, body, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, method=method, data=data,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)


def run_cleanup(created_objects, base=None, token=None, dry_run=True,
                pace_seconds=0.35, journal_path=None, protected_ids=()):
    """Execute or simulate a cleanup. dry_run=True opens no sockets.

    protected_ids: source-client ids that must never be touched (see build_cleanup).
    """
    if not dry_run and (not base or not token):
        raise ValueError("base and token are required for a live cleanup")

    steps, unremovable = build_cleanup(created_objects, protected_ids)
    journal, problems = [], []
    removed = failed = 0
    fh = None
    if journal_path:
        p = pathlib.Path(journal_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("a", encoding="utf-8")

    def jot(o):
        if fh:
            fh.write(json.dumps(o) + "\n"); fh.flush(); os.fsync(fh.fileno())

    jot({"_header": {"kind": "cleanup", "dry_run": dry_run,
                     "steps": len(steps), "unremovable": len(unremovable),
                     "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}})

    for s in steps:
        e = {k: s[k] for k in ("seq", "kind", "label", "target_id", "method", "path")}
        if dry_run:
            e["status"] = "dry-run"
            if s.get("needs_version"):
                e["note"] = "a version will be resolved from the object before sending"
            journal.append(e); jot(e); removed += 1
            continue
        body = dict(s["body"]) if s.get("body") else None
        if s.get("needs_version") and body is not None:
            body["version"] = resolve_version(base, token, s["kind"], s["target_id"],
                                              s.get("readback"))
            e["version_sent"] = body["version"]
        code, err = _send(base, token, s["method"], s["path"], body)
        e["status"] = code
        if 200 <= code < 300:
            removed += 1
        else:
            e["error"] = err
            problems.append(f"{s['kind']} {s['target_id']}: HTTP {code} {(err or '')[:100]}")
            failed += 1
        journal.append(e); jot(e)
        time.sleep(pace_seconds)

    jot({"_footer": {"removed": removed, "failed": failed,
                     "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}})
    if fh:
        fh.close()

    return {"dry_run": dry_run, "journal_path": journal_path,
            "counts": {"attempted": len(steps), "removed": removed, "failed": failed,
                       "unremovable": len(unremovable)},
            "journal": journal, "unremovable": unremovable, "problems": problems}


def cleanup_outlook(plan):
    """What cleanup could undo IF this plan were applied — computed before committing.

    Single source of truth is REMOVAL, so this cannot drift from what cleanup actually
    does. Returns per-kind counts bucketed by reversibility, plus a plain-English summary.
    """
    buckets = {"delete": [], "status": [], "none": []}
    for s in plan.get("steps", []):
        kind = s.get("kind")
        spec = REMOVAL.get(kind)
        mode = (spec or {}).get("mode", "none")
        # a step that records no id cannot be targeted afterwards
        if mode != "none" and not s.get("provides"):
            buckets["none"].append((kind, "no id is recorded for this object"))
        else:
            buckets[mode].append((kind, (spec or {}).get("why", "")))

    def tally(rows):
        out = {}
        for kind, _ in rows:
            out[kind] = out.get(kind, 0) + 1
        return out

    reasons = {}
    for kind, why in buckets["none"]:
        if why:
            reasons.setdefault(kind, why)

    return {
        "deletable": tally(buckets["delete"]),
        "deactivatable": tally(buckets["status"]),
        "permanent": tally(buckets["none"]),
        "permanent_reasons": reasons,
        "counts": {"deletable": len(buckets["delete"]),
                   "deactivatable": len(buckets["status"]),
                   "permanent": len(buckets["none"]),
                   "total": len(plan.get("steps", []))},
    }


def source_ids_of(plan):
    """Every SOURCE id a plan references — the protected set for cleanup."""
    ids = {(plan.get("source") or {}).get("client_id")}
    for s in plan.get("steps", []):
        if s.get("provides"): ids.add(s["provides"])
        ids.update(s.get("requires") or [])
        if s.get("parent"):   ids.add(s["parent"])
    return {i for i in ids if i}


def cleanup_from_journal(path, **kw):
    """Convenience: derive created objects from a run journal and clean up."""
    _, entries, _ = read_journal(path)
    created = [{"seq": e.get("seq"), "kind": e.get("kind"), "label": e.get("label"),
                "new_id": e.get("created_id"), "path": e.get("path")}
               for e in entries
               if isinstance(e.get("status"), int) and 200 <= e["status"] < 300]
    return run_cleanup(created, **kw)
