#!/usr/bin/env python3
"""
Sandbox CAT client clone — local front end for the clone tool.

Run:  python3 app/server.py            (serves http://localhost:8788)
Then open the URL, paste Client ID + CAT API key (and, optionally, the Sandbox Secret /
Public Keys for steps that call the Checkout sandbox API), and work through the stages:
Load entities -> Capture -> review plan -> Dry run -> Apply -> Verify -> Clean up.

Nothing is persisted except run journals under clone-runs/: credentials live only
for the duration of the request. Sandbox keys are refused unless they carry the
sk_sbox_ / pk_sbox_ prefix — a production key never gets past the handler.

THIS APPLICATION WRITES. Every path defaults to not writing, and a live run needs
dry_run=false AND confirm=="CLONE" AND a token. CAT offers no rollback, so read the
safety notes in README.md before running anything live.
"""
import json, os, pathlib, datetime, http.server, socketserver, threading
import clone_capture as cc
import clone_apply as capp
import clone_cleanup as ccl

HERE = pathlib.Path(__file__).parent
# Overridable so the page can be served on whatever port the registered Okta login redirect
# URI names (CLONE_PORT=<port>). The Okta redirect_uri below follows this value.
PORT = int(os.environ.get("CLONE_PORT", "8788"))
# Where the SOURCE is read from, by mode (the page's Sandbox → Sandbox / Prod → Sandbox
# toggle, sent as source_env). Hosts from the CAT swagger's servers list. Writes — apply,
# verify, cleanup — ALWAYS go to the sandbox: TARGET_CAT_BASE never varies.
# NOTE the prod host: the swagger's servers list says client-admin-prod.ckotech.co, which
# does not resolve (verified 2026-09-09: DNS failure -> "HTTP 0" on the first prod run).
# The CAT configuration helper uses client-admin.cko-prod.ckotech.co, which resolves and
# answers 401 without a token — that is the real one.
CAT_BASES = {"sandbox": "https://client-admin.cko-sbox.ckotech.co/api",
             "prod": "https://client-admin.cko-prod.ckotech.co/api"}
TARGET_CAT_BASE = CAT_BASES["sandbox"]
CAT_BASE = TARGET_CAT_BASE          # the write base; kept under its old name
# The client-facing API the source's webhooks are read from, by mode (reads only).
API_BASES = {"sandbox": cc.SANDBOX_API_BASE, "prod": cc.PROD_API_BASE}


def source_env(payload):
    """("sandbox"|"prod", None) from the page's toggle, or (None, error)."""
    v = str(payload.get("source_env") or "sandbox").strip().lower()
    if v not in CAT_BASES:
        return None, f"source_env must be 'sandbox' or 'prod', not {v!r}"
    return v, None


def source_cat_token(payload, env):
    """The token the SOURCE reads use: the prod token in prod mode, else the CAT token."""
    if env == "prod":
        return (payload.get("src_cat_token") or "").strip()
    return (payload.get("cat_token") or "").strip()


def prod_source_key(payload, env):
    """The source client's PRODUCTION secret key — read-only, prod mode only.

    Returns (key|None, error). Refused outright outside Prod → Sandbox, and refused if it
    looks like a sandbox key (that would be the wrong field). It is used for exactly one
    thing: reading the source's webhooks from api.checkout.com at capture. It is never
    placed in the keys handed to apply_plan, so it cannot reach a write.
    """
    val = (payload.get("src_prod_sk") or "").strip()
    if not val:
        return None, None
    if env != "prod":
        return None, "Source production secret key is only accepted in Prod → Sandbox mode"
    if not val.startswith("sk_") or val.startswith("sk_sbox_"):
        return None, "Source production secret key must be a production sk_ key (not sk_sbox_)"
    return val, None

# Okta SSO for the CAT token — the same Okta apps the CAT UI and the CAT configuration
# helper use, OAuth 2.0 implicit flow, entirely in the browser. These are PUBLIC values (an
# OIDC public client has no secret): the server only serves them to the page and takes no
# part in the login; the bearer arrives in the same cat_token field a pasted token uses.
# redirect_uri must be registered on the Okta app — if it is not, Okta shows a
# redirect_uri error on the way in and http://localhost:PORT/ has to be added by the app's
# owners. Prod is served too, for the Prod → Sandbox mode (read-only source, TODO §00.5).
OKTA = {
    "sandbox": {"issuer": "https://checkout.oktapreview.com/oauth2/ausskuj3xaCB7FT2g0h7",
                "client_id": "0oasktz00noN5cA5x0h7"},
    "prod":    {"issuer": "https://checkout.okta.com/oauth2/aus14y376tJ9vBv7B357",
                "client_id": "0oa4sy8n5hKfYBJK4357"},
}
OKTA_SCOPE = "openid profile clientadmin-tool"


def okta_config(port=None):
    """What the page needs to start a login. Public values only; nothing here is a secret."""
    return {"envs": OKTA, "scope": OKTA_SCOPE,
            "redirect_uri": f"http://localhost:{port or PORT}/"}
# Live clone runs journal here, one .jsonl per run. Gitignored: contains
# real created-object ids from a write run.
RUNS_DIR = HERE.parent / "clone-runs"

# The optional Checkout sandbox API keys, alongside the CAT token:
# (request field, key role, required prefix, label on the page). Two secret keys because a
# webhook clone reads the SOURCE client's workflows and creates them on the DESTINATION:
#   sandbox_sk       -> "source_sandbox_secret": read-only, used by capture (GET /workflows)
#   dest_sandbox_sk  -> "sandbox_secret":        clone_apply's auth mode for sandbox-API
#                       steps — the new client's key, which exists only once its access
#                       keys have been created by hand
#   sandbox_pk       -> "sandbox_public":        reserved for endpoints that take a public key
SANDBOX_KEY_FIELDS = (
    ("sandbox_sk", "source_sandbox_secret", "sk_sbox_", "Source Sandbox Secret Key"),
    ("dest_sandbox_sk", "sandbox_secret", "sk_sbox_", "Destination Sandbox Secret Key"),
    ("sandbox_pk", "sandbox_public", "pk_sbox_", "Sandbox Public Key"))


def sandbox_keys(payload):
    """Return ({auth_mode: key}, error) from the optional sandbox key fields.

    This is a SANDBOX tool. A supplied key that does not carry the sandbox prefix — a
    production sk_/pk_ above all — is refused outright on every route, before anything
    else happens, rather than passed anywhere. Absent keys are simply absent: they are
    only needed by steps that call the sandbox API. Keys live for the request only.
    """
    keys = {}
    for field, mode, prefix, label in SANDBOX_KEY_FIELDS:
        val = (payload.get(field) or "").strip()
        if not val:
            continue
        if not val.startswith(prefix):
            return None, f"{label} must be a sandbox key starting with {prefix} — refused"
        keys[mode] = val
    return keys, None


def reachability_note(reader, code):
    """Explain an empty read honestly. HTTP 0 is not a CAT answer: the request never got
    one (DNS, connection, TLS, timeout) — the token was never even evaluated. Say so, with
    the host and the recorded error, instead of the misleading "check the token"."""
    if code == 0:
        err = next((e.get("error") for e in reader.errors if e.get("error")), "no HTTP response")
        return (f"could not reach {reader.base} ({err}). The token was not evaluated — "
                f"check the hostname and that you are on the VPN.")
    if code in (401, 403):
        return f"HTTP {code} from {reader.base} — the token was refused (expired, or for the other environment?)"
    return f"HTTP {code} from {reader.base} (check the client id and that the token is valid/unexpired)"


def clone_capture_handler(payload):
    """Read-only: reads the source client and returns an ordered clone plan."""
    keys, kerr = sandbox_keys(payload)
    if kerr:
        return {"error": kerr}
    env, eerr = source_env(payload)
    if eerr:
        return {"error": eerr}
    prod_key, perr = prod_source_key(payload, env)
    if perr:
        return {"error": perr}
    client_id = (payload.get("client_id") or "").strip()
    token     = (payload.get("cat_token") or "").strip()      # the TARGET (sandbox) token
    src_token = source_cat_token(payload, env)                # what reads the source
    if not client_id or not token:
        return {"error": "client_id and cat_token are required"}
    if env == "prod" and not src_token:
        return {"error": "Prod → Sandbox needs the Source CAT token (prod) as well as the "
                         "sandbox CAT token — the source is read with the former, the "
                         "target's currency and scope lists with the latter"}
    # Scope: the entities ticked on the page (a list), or the older single-id field.
    raw_scope = payload.get("only_entities")
    if raw_scope is not None and not isinstance(raw_scope, list):
        return {"error": "only_entities must be a list of entity ids"}
    only_entities = sorted({x.strip() for x in (raw_scope or [])
                            if isinstance(x, str) and x.strip()})
    # Live progress for the page: it picks a run_id ("cap-…") and polls
    # /api/clone/progress while this request is in flight; every completed read is
    # recorded as it happens. total is unknown up front — the entries carry entity_total
    # once the entity list has been read. Marked done whatever happens.
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_id = str(payload.get("run_id") or stamp)
    progress_start(run_id, None, kind="capture")
    try:
        cap = cc.capture(CAT_BASES[env], src_token, client_id,
                         only_entity=(payload.get("only_entity") or "").strip() or None,
                         only_entities=only_entities or None,
                         # the SOURCE's secret key reads its webhooks; never the
                         # destination's. In prod mode that is the production key, against
                         # the production API — reads only.
                         sandbox_secret_key=(prod_key if env == "prod"
                                             else keys.get("source_sandbox_secret")),
                         sandbox_api_base=API_BASES[env],
                         source_env=env,
                         # prod mode: capability reads (currencies, scopes) go to the target
                         target_base=TARGET_CAT_BASE if env == "prod" else None,
                         target_token=token if env == "prod" else None,
                         on_progress=lambda e: progress_step(run_id, e))
    finally:
        progress_finish(run_id)
    # the sandbox receiver every prod webhook is pointed at (prod mode; see build_plan)
    cap["webhook_receiver_url"] = (payload.get("webhook_receiver_url") or "").strip()
    # Persist the RAW capture, every time, before anything is derived from it. When a
    # plan skips something, the question is always "what did CAT actually return?" — and
    # until now the only answer was to ask someone to paste it. Gitignored with the
    # journals; also the first real saved capture the tests have ever had available.
    cap_path = None
    try:
        cap_path = RUNS_DIR / f"capture-{stamp}-{client_id}.json"
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        # A PRODUCTION capture is redacted before it touches disk: bank details, acquirer
        # credentials, keys, contact details become "REDACTED(n)". The in-memory capture
        # the plan is built from is not — the scrub needs the real shapes.
        on_disk = cc.redact_for_disk(cap) if env == "prod" else cap
        cap_path.write_text(json.dumps(on_disk, indent=1, default=str), encoding="utf-8")
    except Exception as ex:  # never let bookkeeping break a capture
        cap_path = f"not saved: {type(ex).__name__}: {ex}"
    if not cap.get("entities"):
        scope = cap.get("only_entities") or ([cap["only_entity"]] if cap.get("only_entity") else [])
        errs = (cap.get("_meta") or {}).get("errors") or []
        transport = next((e.get("error") for e in errs if e.get("error")), None)
        codes = sorted({e.get("code") for e in errs if e.get("code")})
        if transport:
            why = (f"could not reach {CAT_BASES[env]} ({transport}). The token was not "
                   f"evaluated — check the hostname and that you are on the VPN.")
        elif codes and set(codes) <= {401, 403}:
            why = f"CAT refused the token (HTTP {', '.join(map(str, codes))}) — expired, or for the other environment?"
        else:
            why = "check the ids and that the token is valid/unexpired."
        return {"error": f"No entities found for {client_id} "
                         + (f"matching the selected entities {', '.join(scope)}. " if scope else "— ")
                         + why,
                "run_id": run_id, "calls": cap.get("_meta", {}).get("calls")}
    if cap.get("scope_missing"):
        # Refuse, don't guess: a plan for fewer entities than were ticked would apply
        # cleanly and leave the operator believing the clone is complete.
        return {"error": f"{len(cap['scope_missing'])} selected entity(ies) were not returned "
                         f"by CAT for {client_id}: {', '.join(cap['scope_missing'])}. "
                         f"Reload the entity list and tick again. Nothing was planned.",
                "capture_path": str(cap_path), "run_id": run_id}
    plan = cc.build_plan(cap, (payload.get("new_client_name") or "").strip() or None)
    return {"plan": plan, "problems": cc.validate_plan(plan),
            "calls": cap.get("_meta", {}).get("calls"),
            "run_id": run_id, "source_env": env,
            "capture_path": str(cap_path),
            # what cleanup could undo if this plan were applied — shown before committing
            "cleanup_outlook": ccl.cleanup_outlook(plan)}


def clone_apply_handler(payload):
    """Dry-run or live-apply a clone plan.

    A live run requires ALL of: dry_run explicitly false, confirm == "CLONE", and a CAT
    token. Anything missing falls back to an error rather than a write — the default of
    every path here is "do not write".
    """
    plan = payload.get("plan")
    if not isinstance(plan, dict) or not plan.get("steps"):
        return {"error": "a plan with steps is required"}
    keys, kerr = sandbox_keys(payload)
    if kerr:
        return {"error": kerr}

    live = payload.get("dry_run") is False
    if not live:
        run = capp.apply_plan(plan, dry_run=True,
                             manual_values=payload.get("manual_values") or {},
                             sandbox_keys=keys)
        return {"run": run, "summary": capp.render_run(run)}

    # ---- live write path ----
    token = (payload.get("cat_token") or "").strip()
    if payload.get("confirm") != "CLONE":
        return {"error": 'live apply requires confirm == "CLONE"'}
    if not token:
        return {"error": "cat_token is required for a live apply"}

    # Journal first, always. A partial run has no API rollback, so this file is the only
    # record of what was created.
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    src = (plan.get("source") or {}).get("client_id") or "unknown"
    jpath = RUNS_DIR / f"clone-{stamp}-{src}.jsonl"

    # Live progress: the page picks a run_id, polls /api/clone/progress with it while this
    # request is in flight, and sees each step the moment it is journalled.
    run_id = str(payload.get("run_id") or stamp)
    progress_start(run_id, len(plan["steps"]))
    try:
        # Writes go to the sandbox whatever the source was (TARGET_CAT_BASE); a plan read
        # from production gets a redacted journal — the bodies would carry prod values.
        prod_source = (plan.get("source") or {}).get("env") == "prod"
        run = capp.apply_plan(plan, base=TARGET_CAT_BASE, token=token, dry_run=False,
                              stop_on_error=True, pace_seconds=0.35,
                              manual_values=payload.get("manual_values") or {},
                              journal_path=str(jpath), sandbox_keys=keys,
                              on_step=lambda e, n: progress_step(run_id, e),
                              redact=cc.redact_for_disk if prod_source else None)
    finally:
        progress_finish(run_id)
    return {"run": run, "summary": capp.render_run(run), "journal_path": str(jpath),
            "run_id": run_id}


# ---- live progress for the page -------------------------------------------------
# One slim entry per journalled step, kept in memory for the life of the process. The
# journal file stays the durable record; this is only what the page polls while an apply
# is in flight. Never carries a request body or a credential.
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()
PROGRESS_FIELDS = ("seq", "kind", "op", "method", "path", "status", "error", "created_id",
                   "resolved_id", "attempts", "auth", "elapsed_ms",
                   # capture stamps: which phase a read belonged to
                   "sub", "entity_index", "entity_total")


def progress_start(run_id, total, kind="apply"):
    """total may be None: a capture's read count is not known until it has happened."""
    with PROGRESS_LOCK:
        PROGRESS[run_id] = {"total": total, "kind": kind, "entries": [], "done": False,
                            "started_at": datetime.datetime.utcnow().isoformat() + "Z"}


def progress_step(run_id, entry):
    slim = {k: entry[k] for k in PROGRESS_FIELDS if k in entry}
    with PROGRESS_LOCK:
        p = PROGRESS.get(run_id)
        if p is not None:
            p["entries"].append(slim)


def progress_finish(run_id):
    with PROGRESS_LOCK:
        p = PROGRESS.get(run_id)
        if p is not None:
            p["done"] = True


def clone_progress_handler(payload):
    """Where a live apply is up to. Read-only, no CAT call; unknown run_id is an error."""
    run_id = str(payload.get("run_id") or "")
    with PROGRESS_LOCK:
        p = PROGRESS.get(run_id)
        if p is None:
            return {"error": f"unknown run_id {run_id!r}"}
        return {"run_id": run_id, "total": p["total"], "done": p["done"],
                "kind": p.get("kind", "apply"),
                "started_at": p["started_at"], "entries": [dict(e) for e in p["entries"]]}


def clone_entities_handler(payload):
    """List a client's entities so the UI can offer a scope picker. Read-only, 1 GET."""
    _, kerr = sandbox_keys(payload)
    if kerr:
        return {"error": kerr}
    env, eerr = source_env(payload)
    if eerr:
        return {"error": eerr}
    client_id = (payload.get("client_id") or "").strip()
    token     = source_cat_token(payload, env)      # reads the SOURCE: prod token in prod mode
    if not client_id or not token:
        return {"error": "client_id and cat_token are required"
                         + (" (the Source CAT token in Prod → Sandbox mode)" if env == "prod" else "")}
    r = cc.Reader(CAT_BASES[env], token)
    ents, code = r.hal_all(f"/clients/{client_id}/entities", "entities")
    if not ents:
        return {"error": f"No entities found for {client_id} — " + reachability_note(r, code)}
    return {"entities": [{"id": e.get("id"), "name": e.get("name"),
                          "status": e.get("status"), "region": e.get("region")}
                         for e in ents]}


def clone_verify_handler(payload):
    """Read back created objects from CAT. GETs only — proves what actually exists."""
    _, kerr = sandbox_keys(payload)
    if kerr:
        return {"error": kerr}
    objs = payload.get("created_objects")
    token = (payload.get("cat_token") or "").strip()
    if not isinstance(objs, list) or not objs:
        return {"error": "created_objects is required"}
    if not token:
        return {"error": "cat_token is required"}
    return ccl.verify(objs, CAT_BASE, token)


def clone_cleanup_handler(payload):
    """Remove what a clone run created, as far as CAT allows.

    Same gating as apply: live requires dry_run false + confirm CLEANUP + a token.
    """
    _, kerr = sandbox_keys(payload)
    if kerr:
        return {"error": kerr}
    objs = payload.get("created_objects")
    if not isinstance(objs, list) or not objs:
        return {"error": "created_objects is required (from a run journal)"}
    # Never let cleanup touch the source account: it deactivates clients and entities,
    # so a bad id would take down what we cloned FROM.
    prot = ccl.source_ids_of(payload.get("plan") or {})
    if payload.get("dry_run") is not False:
        return {"run": ccl.run_cleanup(objs, dry_run=True, protected_ids=prot)}
    token = (payload.get("cat_token") or "").strip()
    if payload.get("confirm") != "CLEANUP":
        return {"error": 'live cleanup requires confirm == "CLEANUP"'}
    if not token:
        return {"error": "cat_token is required for a live cleanup"}
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    jpath = RUNS_DIR / f"cleanup-{stamp}.jsonl"
    return {"run": ccl.run_cleanup(objs, base=CAT_BASE, token=token, dry_run=False,
                                   journal_path=str(jpath), protected_ids=prot),
            "journal_path": str(jpath)}


# Prefill for the page, so the operator does not retype the same values every session:
#   dev-creds.json        tracked — holds ONLY the sandbox source client id
#   dev-creds.local.json  gitignored — the operator's own sandbox keys (sandbox_sk /
#                         sandbox_pk), cached at their request. Never committed.
# The CAT token is deliberately NOT cached: it is a short-lived Okta bearer and is pasted
# every session. Whatever is injected reaches the browser, so a key without the sandbox
# prefix is dropped here — the page must never be pre-filled with a production key.
DEV_CREDS_FILES = (HERE / "dev-creds.json", HERE / "dev-creds.local.json")


def dev_creds(files=DEV_CREDS_FILES):
    """Merge the prefill files (later wins). Missing or unreadable files contribute nothing."""
    creds = {}
    for f in files:
        try:
            if pathlib.Path(f).exists():
                data = json.loads(pathlib.Path(f).read_text())
                if isinstance(data, dict):
                    creds.update(data)
        except Exception:
            pass
    for field, _, prefix, _ in SANDBOX_KEY_FIELDS:
        val = creds.get(field)
        if val is not None and not (isinstance(val, str) and val.startswith(prefix)):
            creds.pop(field, None)     # not a sandbox key: never reaches the page
    return creds


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self._send(204, "")
    def _page(self, name):
        """Serve a tracked HTML page, injecting local dev creds for prefill."""
        html = (HERE/name).read_text()
        # Okta values for the sign-in button (public; see OKTA above)
        html = html.replace("</head>", "<script>window.__OKTA__=" + json.dumps(okta_config())
                            + ";</script></head>", 1)
        creds = dev_creds()
        if creds:
            tag = "<script>window.__DEV_CREDS__=" + json.dumps(creds) + ";</script>"
            html = html.replace("</head>", tag + "</head>", 1)
        self._send(200, html, "text/html; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/clone", "/clone.html"):
            self._page("clone.html")
        else:
            self._send(404, json.dumps({"error":"not found"}))
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) or b"{}"
        try: payload = json.loads(raw)
        except Exception: return self._send(400, json.dumps({"error":"bad json"}))
        if self.path == "/api/clone/capture":
            try: result = clone_capture_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            return self._send(200 if "error" not in result else 400, json.dumps(result))
        if self.path == "/api/clone/entities":
            try: result = clone_entities_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            return self._send(200 if "error" not in result else 400, json.dumps(result))
        if self.path == "/api/clone/verify":
            try: result = clone_verify_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            return self._send(200 if "error" not in result else 400, json.dumps(result))
        if self.path == "/api/clone/cleanup":
            try: result = clone_cleanup_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            return self._send(200 if "error" not in result else 400, json.dumps(result))
        if self.path == "/api/clone/apply":
            try: result = clone_apply_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            code = 200 if "error" not in result else (501 if result.get("stage") else 400)
            return self._send(code, json.dumps(result))
        if self.path == "/api/clone/progress":
            try: result = clone_progress_handler(payload)
            except Exception as ex: result = {"error": f"{type(ex).__name__}: {ex}"}
            return self._send(200 if "error" not in result else 404, json.dumps(result))
        self._send(404, json.dumps({"error":"not found"}))
    def log_message(self, *a): pass  # quiet


class Server(socketserver.ThreadingTCPServer):
    """Threaded so the page can poll /api/clone/progress while an apply is in flight —
    a single-threaded server would hold the poll until the whole run finished."""
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Sandbox CAT client clone -> http://localhost:{PORT}")
        httpd.serve_forever()
