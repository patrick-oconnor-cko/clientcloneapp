#!/usr/bin/env python3
"""
Mutation check — does test_plan.py actually have teeth?

A test suite that passes the first time it is run has proved nothing. This harness
reintroduces each bug that was already found and fixed, one at a time, into a throwaway
copy of the repo, and asserts that at least one test fails. A mutant that survives is a
gap in the suite, not a pass.

Every mutation below corresponds to a real bug: the five in README's "CAT contract traps"
table, the safety gates in CLAUDE.md, and the derivations that must refuse rather than
guess. Add a mutation whenever a new live failure is diagnosed — that is cheaper than
rediscovering it, and it keeps the suite honest as the plan builder changes.

No token, no network, no writes. Nothing outside a temporary directory is touched.

    python3 tests/mutation_check.py

Exit status: 0 all mutants caught · 1 a mutant survived · 2 a mutation no longer applies
(the source moved — update the mutation, do not delete it).
"""
import pathlib, shutil, subprocess, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent

# (name, file, exact source to replace, replacement) — replaced once, so the anchor text
# must be unique. A mutation that stops applying fails loudly rather than silently
# passing.
MUTATIONS = [
    # -- trap 1: profiles must be created via the v2 endpoint -------------------
    ("profile create posted to the v1 endpoint", "app/clone_capture.py",
     'f"/entities/{ph(eid)}/processing-profiles/v2"',
     'f"/entities/{ph(eid)}/processing-profiles"'),
    ("response-only checkout_legal_entity_code sent back", "app/clone_capture.py",
     '"processing_profile": {"checkout_legal_entity_code", "banking_partner_code"},',
     '"processing_profile": set(),'),

    # -- trap 2: the vault account is client-level ------------------------------
    ("source vault account named on the clone's channel", "app/clone_capture.py",
     '    ids = {src_cli, src_vault, eid}',
     '    ids = {src_cli, eid}'),
    # Anchored with the label line: the same retry literal now also appears on the
    # risk-settings step, and a bare anchor would mutate whichever comes first.
    ("vault lookup does not retry async provisioning", "app/clone_capture.py",
     '            label=f"resolve the new client\'s vault account (source {src_vault})",\n'
     '            retry={"attempts": 6, "delay_seconds": 3},',
     '            label=f"resolve the new client\'s vault account (source {src_vault})",\n'
     '            retry=None,'),
    # Anchored on the comment that immediately precedes the risk-settings retry line, so it
    # replaces THAT literal and not the vault lookup's identical one.
    ("risk settings PUT does not wait out default provisioning", "app/clone_capture.py",
     '            # idempotent, so retrying is safe.\n'
     '            retry={"attempts": 6, "delay_seconds": 3},',
     '            # idempotent, so retrying is safe.\n'
     '            retry=None,'),

    # -- composite service keys (prism = FraudDetection) ------------------------
    ("only the vault service key is remapped", "app/clone_capture.py",
     '                svc["key"], used = remap_service_key(key, known)',
     '                used = []\n'
     '                if svc.get("type") == "vault":\n'
     '                    svc["key"], used = remap_service_key(key, known)'),
    ("service key remap misses the entity half of a composite",
     "app/clone_capture.py",
     '    ids = {src_cli, src_vault, eid}',
     '    ids = {src_cli, src_vault}'),
    ("an unmappable service key is not flagged", "app/clone_capture.py",
     '                leftover = unmapped_source_ids(svc["key"])\n'
     '                if leftover:',
     '                leftover = []\n'
     '                if leftover:'),

    # -- trap 3: a sessions channel's services live only on the detail ----------
    ("sessions channel registered in the id map", "app/clone_capture.py",
     'provides=None, requires=sreq, entity=eid,\n'
     '                label=f"sessions layer for {gw}"',
     'provides=s.get("id"), requires=sreq, entity=eid,\n'
     '                label=f"sessions layer for {gw}"'),
    # NOTE the anchor context: `for svc in (s.get("services") or [])` also appears in the
    # entity-service detection block, and a bare anchor mutated that instead.
    ("sessions channel created without services", "app/clone_capture.py",
     '            svcs = []\n'
     '            sknown = service_key_ids(src_cli, src_vault, eid, ent)\n'
     '            for svc in (s.get("services") or []):',
     '            svcs = []\n'
     '            sknown = service_key_ids(src_cli, src_vault, eid, ent)\n'
     '            for svc in []:'),
    ("prism on a sessions channel does not enable the entity service",
     "app/clone_capture.py",
     '        for s in ent.get("sessions_channels", []):\n'
     '            for svc in (s.get("services") or []):\n'
     '                if (svc or {}).get("value"):\n'
     '                    svc_types.add(svc["value"])',
     '        pass'),

    # -- trap 4: the catch-all routing rule must be created first ---------------
    ("routing rules left in CAT's list order", "app/clone_capture.py",
     'key=lambda r: 0 if is_default_routing_rule(r) else 1)',
     'key=lambda r: 1)'),

    # -- trap 5: clean() strips only the top level -----------------------------
    ("nested payment_instrument keeps its ids and timestamps",
     "app/clone_capture.py",
     '            for k in ("id", "date_created", "date_modified", "e_tag"):\n'
     '                pi.pop(k, None)',
     '            pass'),
    ("nested vault_account_id not remapped", "app/clone_capture.py",
     '            if pi.get("vault_account_id") and pi["vault_account_id"] == src_vault:',
     '            if False:'),
    ("nested currency_account_ids not remapped", "app/clone_capture.py",
     '                if isinstance(ca, str) and ca.startswith("ca_"):\n'
     '                    cas.append(ph(ca)); preq.append(ca)',
     '                if False:\n                    pass'),
    ("empty scheduler_ids sent on a Legacy schedule", "app/clone_capture.py",
     '            if not sch.get("scheduler_ids"):\n'
     '                sch.pop("scheduler_ids", None)',
     '            pass'),

    # -- the v2 GET is not a create body (three known-good live creates) --------
    ("acquiring_bin left as the number the GET returned", "app/clone_capture.py",
     'PROFILE_STRING_FIELDS = ("acquiring_bin",)',
     'PROFILE_STRING_FIELDS = ()'),
    ("custom_settings numerics left as numbers", "app/clone_capture.py",
     'PROFILE_STRING_FIELDS_CUSTOM = ("authorization_validity_period", "processing_threshold")',
     'PROFILE_STRING_FIELDS_CUSTOM = ()'),
    ("nested SE_CCY processing_threshold not coerced", "app/clone_capture.py",
     '        se = cs.get("SE_CCY")',
     '        se = None'),
    ("source card acceptor identification code carried to the clone",
     "app/clone_capture.py",
     '                    row["card_acceptor_identification_code"] = ""',
     '                    pass'),
    ("CAID blanked even when auto_generate is false", "app/clone_capture.py",
     '                elif auto:\n'
     '                    row["card_acceptor_identification_code"] = ""',
     '                elif True:\n'
     '                    row["card_acceptor_identification_code"] = ""'),

    # -- checkout_legal_entity_codes (required, unreadable on older profiles) --
    ("legacy profile sent without its legal entity codes", "app/clone_capture.py",
     '    if not body.get("checkout_legal_entity_codes"):',
     '    if False:'),
    ("legal entity codes guessed when siblings disagree", "app/clone_capture.py",
     '    if len(seen) > 1:\n'
     '        # Sibling profiles disagree, so there is no single entity-level answer to copy.\n'
     '        return None, f"ambiguous-{len(seen)}-distinct-values"',
     '    if len(seen) > 1:\n'
     '        return list(seen[0]), "guessed"'),
    ("undocumented default_cko_legal_entity used as the value",
     "app/clone_capture.py",
     '    hint = (entity_detail or {}).get("default_cko_legal_entity")\n'
     '    return None, (f"no-sibling-profile-reports-one"',
     '    hint = (entity_detail or {}).get("default_cko_legal_entity")\n'
     '    if hint:\n        return [hint], "entity-default"\n'
     '    return None, (f"no-sibling-profile-reports-one"'),
    ("missing legal entity codes not flagged", "app/clone_capture.py",
     '            warns.append(make_flag(\n'
     '                "legal_entity_codes_unresolved",',
     '            _ = (make_flag(\n'
     '                "legal_entity_codes_unresolved",'),

    # -- manual processors: no API route exists to create one ------------------
    ("manual processor emitted as a doomed step", "app/clone_capture.py",
     '                if not pid:\n'
     '                    reason = (f"binds no processing profile ({conf}); CAT cannot create a "',
     '                if False:\n'
     '                    reason = (f"binds no processing profile ({conf}); CAT cannot create a "'),
    ("a skipped processor does not take its sessions link with it",
     "app/clone_capture.py",
     '                if pr_id and pr_id in skipped_processors:',
     '                if False:'),
    ("orphaned sessions link reported with the vaguer reason",
     "app/clone_capture.py",
     '                        "sessions_link_orphaned",',
     '                        "sessions_link_unresolved",'),
    ("routing rule currency scope not remediated", "app/clone_capture.py",
     '                for f in ("processing_currencies", "currencies"):\n'
     '                    if not body.get(f):\n'
     '                        continue',
     '                for f in ("processing_currencies", "currencies"):\n'
     '                    if True:\n'
     '                        continue'),
    ("a rule scoped only to unavailable currencies is created empty",
     "app/clone_capture.py",
     '                    if not fixed:\n'
     '                        scope_emptied = (f, fdropped)\n'
     '                        break',
     '                    if False:\n'
     '                        scope_emptied = (f, fdropped)\n'
     '                        break'),
    ("processor currency fields not remediated", "app/clone_capture.py",
     '                for f in ("currencies", "processing_currencies"):\n'
     '                    if f in body:',
     '                for f in ("currencies", "processing_currencies"):\n'
     '                    if False:'),

    # -- the flag mechanism (feeds the post-run report) ------------------------
    ("a flag is not mirrored into warnings", "app/clone_capture.py",
     '        flags.append(f)\n'
     '        warnings.append(f["message"])',
     '        flags.append(f)'),
    ("flags are not recorded on the plan at all", "app/clone_capture.py",
     '        "flags": flags,',
     '        "flags": [],'),

    # -- client risk settings (Fraud Detection tier) ----------------------------
    ("risk settings tier hardcoded instead of copied", "app/clone_capture.py",
     '        rs_body = {"id": ph(src_cli), "name": clone_name, "tier": rs["tier"]}',
     '        rs_body = {"id": ph(src_cli), "name": clone_name, "tier": "premium"}'),
    ("risk settings sent as POST instead of the proven PUT", "app/clone_capture.py",
     '        add("client_risk_settings", "PUT",',
     '        add("client_risk_settings", "POST",'),
    ("source client id echoed in the risk settings body", "app/clone_capture.py",
     '        if "read_only_restriction_enabled" in rs:',
     '        rs_body["id"] = rs.get("id")\n        if "read_only_restriction_enabled" in rs:'),
    ("risk settings name taken from the source client", "app/clone_capture.py",
     '        rs_body = {"id": ph(src_cli), "name": clone_name, "tier": rs["tier"]}',
     '        rs_body = {"id": ph(src_cli), "name": rs.get("name"), "tier": rs["tier"]}'),
    ("unreadable risk settings silently skipped", "app/clone_capture.py",
     '        flag(make_flag(\n'
     '            "risk_settings_not_captured",',
     '        _ = (make_flag(\n'
     '            "risk_settings_not_captured",'),

    # -- client Network Tokens ---------------------------------------------------
    # (was "default_entity_id sent as the SOURCE entity" — nothing is sent any more; the
    # step's structural output is the skipped[] record, so that is what is guarded now)
    ("network tokens skipped[] record dropped, leaving only the flag", "app/clone_capture.py",
     '            skipped.append({"kind": "client_network_tokens",',
     '            _ = ({"kind": "client_network_tokens",'),
    # Status lines carry a `label` and no `value`, so the real guard is `"value" in f` —
    # the plainText skip is belt-and-braces. The mutant that matters is a label being
    # read as a value, which is how a TRID would leak into a request.
    # Two redundant guards protect against this (the plainText skip and `"value" in f`),
    # so mutating either alone is an equivalent mutant. Both come down here.
    ("TRID status labels captured as field values", "app/clone_capture.py",
     '            name = f.get("name")\n'
     '            if not name or f.get("display_style") == "plainText":\n'
     '                continue\n'
     '            if name == "default_entity_id":\n'
     '                sel = f.get("value")\n'
     '                for c in ((f.get("conditional_preselected_value") or {}).get("conditions") or []):\n'
     '                    if (isinstance(c, dict) and c.get("operator") == "=" and sel\n'
     '                            and c.get("value") == sel and c.get("name")):\n'
     '                        vals.setdefault(c["name"], c.get("preselected_value"))\n'
     '            if "value" in f:\n'
     '                vals[name] = f["value"]',
     '            name = f.get("name")\n'
     '            if not name:\n'
     '                continue\n'
     '            if name == "default_entity_id":\n'
     '                sel = f.get("value")\n'
     '                for c in ((f.get("conditional_preselected_value") or {}).get("conditions") or []):\n'
     '                    if (isinstance(c, dict) and c.get("operator") == "=" and sel\n'
     '                            and c.get("value") == sel and c.get("name")):\n'
     '                        vals.setdefault(c["name"], c.get("preselected_value"))\n'
     '            vals[name] = f.get("value", f.get("label"))'),
    ("network tokens emitted even when the default entity was not captured",
     "app/clone_capture.py",
     '        if nt_vals is not None and default_eid in captured:',
     '        if nt_vals is not None:'),
    ("network tokens verifier ignores a changed switch", "app/clone_apply.py",
     '        if got != want:\n'
     '            differ[k] = {"source": want, "clone": got}',
     '        if False:\n'
     '            differ[k] = {"source": want, "clone": got}'),
    ("unreadable network tokens form silently skipped", "app/clone_capture.py",
     '            flag(make_flag(\n'
     '                "network_tokens_not_captured",',
     '            _ = (make_flag(\n'
     '                "network_tokens_not_captured",'),


    ("per-step base override ignored (portal read sent to CAT)", "app/clone_apply.py",
     '            resp, code, err = _send(step.get("base") or auth_base or base, bearer, method,',
     '            resp, code, err = _send(auth_base or base, bearer, method,'),
    ("a 2xx that persisted nothing reported as ordinary differences", "app/clone_apply.py",
     '    elif differ and all(v["clone"] is None for v in differ.values()) \\\n'
     '            and len(differ) == sum(1 for k in pairs if expected.get(k) is not None):',
     '    elif False:'),
    ("response excerpt not journalled for id-less writes", "app/clone_apply.py",
     '        if not step.get("provides") and resp not in (None, {}):\n'
     '            entry["response_excerpt"] = json.dumps(resp)[:1500]',
     '        pass'),

    ("network tokens manual flag silently dropped", "app/clone_capture.py",
     '            flag(make_flag(\n'
     '                "network_tokens_manual",',
     '            _ = (make_flag(\n'
     '                "network_tokens_manual",'),

    # -- network tokens: CAT returns the blank template; the portal has the config --
    ("portal form never consulted for network tokens", "app/clone_capture.py",
     '    for form, source in ((cat_form, "cat"), (portal_form, "nt-portal")):',
     '    for form, source in ((cat_form, "cat"),):'),
    ("a blank template treated as a populated configuration", "app/clone_capture.py",
     '        if vals and vals.get("default_entity_id"):\n'
     '            return form, source',
     '        if vals is not None:\n'
     '            return form, source'),

    # -- capture persistence + unreadable default entity -------------------------
    ("raw capture not persisted to clone-runs", "app/server.py",
     '        cap_path.write_text(json.dumps(cap, indent=1, default=str), encoding="utf-8")',
     '        pass'),
    ("missing default_entity_id value misreported as out-of-scope", "app/clone_capture.py",
     '        elif default_eid is None:',
     '        elif False:'),
    ("network tokens moved back before the entities", "app/clone_capture.py",
     '    _network_tokens_steps()\n\n'
     '    # not attempted in this pass',
     '    # not attempted in this pass'),

    # -- journal auditability + optional steps ----------------------------------
    ("journal header drops the plan's flags", "app/clone_apply.py",
     '        "plan_flags": plan.get("flags") or [],',
     '        "plan_flags": [],'),
    ("an optional step's failure halts the run anyway", "app/clone_apply.py",
     '            if step.get("optional"):\n'
     '                # An OPTIONAL step is an enhancement nothing else depends on (network',
     '            if False:\n'
     '                # An OPTIONAL step is an enhancement nothing else depends on (network'),

    # -- client Flow account -----------------------------------------------------
    ("flow account flag hardcoded instead of copied", "app/clone_capture.py",
     '            {"is_enabled": fa["is_enabled"]}, provides=None, requires=[src_cli],',
     '            {"is_enabled": True}, provides=None, requires=[src_cli],'),
    ("source flow account id echoed in the body", "app/clone_capture.py",
     '            {"is_enabled": fa["is_enabled"]}, provides=None, requires=[src_cli],',
     '            {"is_enabled": fa["is_enabled"], "id": fa.get("id")}, provides=None, requires=[src_cli],'),
    ("flow account sent as POST instead of the proven PUT", "app/clone_capture.py",
     '        add("client_flow_account", "PUT",',
     '        add("client_flow_account", "POST",'),
    ("unreadable flow account silently skipped", "app/clone_capture.py",
     '        flag(make_flag(\n'
     '            "flow_account_not_captured",',
     '        _ = (make_flag(\n'
     '            "flow_account_not_captured",'),

    # -- client Compass settings -------------------------------------------------
    ("compass settings sent as PUT, losing conversion_currencies", "app/clone_capture.py",
     '        add("client_compass_settings", "POST",',
     '        add("client_compass_settings", "PUT",'),
    ("compass conversion currencies not remediated", "app/clone_capture.py",
     '        conv, cnotes, cdropped = remediate_currencies(\n'
     '            comp.get("conversion_currencies") or [], valid=vc, scope=vscope)',
     '        conv, cnotes, cdropped = list(comp.get("conversion_currencies") or []), [], []'),
    ("compass POST emitted without its verify read", "app/clone_capture.py",
     '            verify={"compare": "compass_settings",',
     '            verify=None and {"compare": "compass_settings",'),
    ("a display currency that did not take is not flagged", "app/clone_apply.py",
     '    if want_disp and have_disp != want_disp:',
     '    if False:'),
    ("missing conversion currencies on the clone not flagged", "app/clone_apply.py",
     '    missing = sorted(want_conv - have_conv)\n    if missing:',
     '    missing = sorted(want_conv - have_conv)\n    if False:'),
    ("unreadable compass settings silently skipped", "app/clone_capture.py",
     '        flag(make_flag(\n'
     '            "compass_settings_not_captured",',
     '        _ = (make_flag(\n'
     '            "compass_settings_not_captured",'),

    # -- payout routes: parity-checked capability, never created -----------------
    ("payout routes POSTed instead of read", "app/clone_capture.py",
     '            add("payout_route_check", "GET",',
     '            add("payout_route_check", "POST",'),
    ("payout route check reads disabled corridors too", "app/clone_capture.py",
     '                f"/entities/{ph(eid)}/payout-routes?enabled=true", None,',
     '                f"/entities/{ph(eid)}/payout-routes", None,'),
    ("a missing corridor on the clone is not flagged", "app/clone_apply.py",
     '        if key not in have:',
     '        if False:'),
    ("a scheme difference on a corridor is not flagged", "app/clone_apply.py",
     '        elif (exp.get("schemes") or "") != (have[key].get("schemes") or ""):',
     '        elif False:'),
    ("run-time flags dropped from the run document", "app/clone_apply.py",
     '        "flags": run_flags,',
     '        "flags": [],'),
    ("dry run silent about what it would verify", "app/clone_apply.py",
     '            if step.get("verify"):\n'
     '                # Nothing to compare against without a response; say what would happen.',
     '            if False:\n'
     '                # Nothing to compare against without a response; say what would happen.'),
    ("an unreadable payout route item silently dropped", "app/clone_capture.py",
     '                flag(make_flag(\n'
     '                    "payout_route_unrecognised",',
     '                _ = (make_flag(\n'
     '                    "payout_route_unrecognised",'),

    # -- the currency validity gate (CAT is the source of truth; it is dynamic) -
    ("validity gate never runs", "app/clone_capture.py",
     '        if valid and code not in valid:',
     '        if False:'),
    ("an unparseable config response means nothing is valid", "app/clone_capture.py",
     '        found = codes_from(payload)\n'
     '        return found or None',
     '        found = codes_from(payload)\n'
     '        return found'),
    ("validity is not scoped per acquirer", "app/clone_capture.py",
     '    if acquirer and acquirer in (cur.get("by_acquirer") or {}):',
     '    if False:'),
    ("unavailable validation is silent", "app/clone_capture.py",
     '    elif cur_cfg.get("unavailable"):',
     '    elif False:'),
    ("a capture with no validity data is not flagged", "app/clone_capture.py",
     '    if cur_cfg is None:',
     '    if False:'),
    ("a replacement bypasses the validity gate", "app/clone_capture.py",
     '        if code in CURRENCY_REPLACE:\n'
     '            new, why = CURRENCY_REPLACE[code]',
     '        if code in CURRENCY_REPLACE:\n'
     '            new, why = CURRENCY_REPLACE[code]\n'
     '            seen.add(new); out.append(new); continue  # skip validation'),
    ("payout route currencies not keyed by country", "app/clone_capture.py",
     '        if code and found:\n'
     '            out[str(code).upper()] = sorted(found)',
     '        if code and found:\n'
     '            pass'),

    # -- currency remediation (Checkout's rules) -------------------------------
    ("retired currency codes sent as captured", "app/clone_capture.py",
     '    if "currencies" in body:\n'
     '        vc, vscope = valid_currencies or (None, None)',
     '    if False:\n'
     '        vc, vscope = valid_currencies or (None, None)'),
    ("SLL not replaced with SLE", "app/clone_capture.py",
     '    "SLL": ("SLE", "retired — Sierra Leone redenominated"),',
     ''),
    ("inactivated LBP still enabled on the clone", "app/clone_capture.py",
     '    "LBP": "valid ISO code, but CKO inactivates it and it must not be enabled for '
     'new "\n           "merchants — the clone proceeds without it",',
     ''),
    ("remediation guesses at codes it has no rule for", "app/clone_capture.py",
     '        if code in CURRENCY_DROP:',
     '        if code in CURRENCY_DROP or len(str(code)) == 3 and code.startswith("A"):'),
    ("SE_CCY rows keep a dropped currency", "app/clone_capture.py",
     '                    if "currency" in row:',
     '                    if False:'),
    ("currency account holding_currency not remediated", "app/clone_capture.py",
     '            cur = cabody.get("holding_currency")',
     '            cur = None'),
    # (was "payout route currency_code not remediated" — routes are no longer created,
    # so there is nothing to remediate; the check step's verify block is the new contract)
    ("payout route check emitted without its verify block", "app/clone_capture.py",
     '                verify={"compare": "payout_routes", "expected": expected},',
     '                verify=None,'),
    ("routing rule kept despite an orphaned currency account",
     "app/clone_capture.py",
     '                if orphan:',
     '                if False:'),
    ("payout schedule keeps an account that was never created",
     "app/clone_capture.py",
     '                if ca in dropped_cas:',
     '                if False:'),
    ("losing the catch-all rule is not flagged", "app/clone_capture.py",
     '            if kind == "payment_routing_rule" and emitted and not emitted_default:',
     '            if False:'),
    ("a dropped currency is silently swallowed, not flagged",
     "app/clone_capture.py",
     '            warns.append(make_flag(\n'
     '                "currency_not_available",',
     '            _ = (make_flag(\n'
     '                "currency_not_available",'),
    ("iDEAL CAID no longer forced to 0", "app/clone_capture.py",
     'CAID_BY_SCHEME = {"ideal": "0"}',
     'CAID_BY_SCHEME = {}'),

    # -- source-system bookkeeping ---------------------------------------------
    ("source salesforce_case_id carried into the clone", "app/clone_capture.py",
     'DROP_ALWAYS = {"salesforce_case_id", "salesforce_id", "custom_id"}',
     'DROP_ALWAYS = {"salesforce_id", "custom_id"}'),
    ("clean() strips nothing", "app/clone_capture.py",
     'DROP_COMMON = {"id", "_links", "date_created", "date_modified", "creation_date",\n'
     '               "updated_date", "e_tag", "client_id", "entity_id"}',
     'DROP_COMMON = set()'),

    # -- derivations must refuse, not guess ------------------------------------
    ("ambiguous sessions join picks the first candidate", "app/clone_capture.py",
     '    if len(cands) != 1:\n'
     '        return None, None, (f"ambiguous-{len(cands)}-candidates" if cands\n'
     '                            else "no-gateway-processor-for-scheme+mcc")',
     '    if not cands:\n'
     '        return None, None, "no-gateway-processor-for-scheme+mcc"'),

    # -- the safety gates ------------------------------------------------------
    ("DELETE permitted in a plan", "app/clone_apply.py",
     'ALLOWED_METHODS = {"GET", "POST", "PUT"}',
     'ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE"}'),
    ("live apply no longer requires the confirm string", "app/server.py",
     '    if payload.get("confirm") != "CLONE":\n'
     '        return {"error": \'live apply requires confirm == "CLONE"\'}',
     '    pass'),
    ("live apply gated on truthiness instead of `is False`", "app/server.py",
     '    live = payload.get("dry_run") is False',
     '    live = not payload.get("dry_run")'),
    ("live cleanup no longer requires the confirm string", "app/server.py",
     '    if payload.get("confirm") != "CLEANUP":\n'
     '        return {"error": \'live cleanup requires confirm == "CLEANUP"\'}',
     '    pass'),

    # -- entity scope: a ticked id CAT did not return must refuse, not shrink ----
    ("missing ticked entity silently dropped from the scope", "app/clone_capture.py",
     '    return kept, [w for w in wanted if w not in found]',
     '    return kept, []'),
    ("scope list ignored — every entity captured regardless of ticks", "app/clone_capture.py",
     '    kept = [e for e in ents if e.get("id") in wanted_set]',
     '    kept = list(ents)'),

    # -- sandbox API keys: sandbox-only, and never a silent fallback to CAT ------
    ("a production API key accepted as a sandbox key", "app/server.py",
     '        if not val.startswith(prefix):\n'
     '            return None, f"{label} must be a sandbox key starting with {prefix} — refused"',
     '        if False:\n'
     '            return None, "unreachable"'),
    ("a production key from the local creds file pre-filled into the page", "app/server.py",
     '            creds.pop(field, None)     # not a sandbox key: never reaches the page',
     '            pass'),
    ("sandbox step sent with the CAT token instead of the sandbox key", "app/clone_apply.py",
     '    return key, SANDBOX_API_BASE, None',
     '    return token, SANDBOX_API_BASE, None'),
    ("sandbox step with no key falls back to the CAT token", "app/clone_apply.py",
     '    if not key:\n'
     '        return None, None, (f"this step calls the sandbox API and needs the "',
     '    if not key:\n'
     '        return token, SANDBOX_API_BASE, None\n'
     '    if False:\n'
     '        return None, None, (f"this step calls the sandbox API and needs the "'),
]


def run_one(relpath, old, new):
    """Apply one mutation to a private copy of the repo and report what failed.

    Each mutant gets its OWN temporary directory. Reusing a single working copy let
    state leak between iterations and misattributed which tests caught which mutant —
    the one failure mode a harness like this must not have.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "repo"
        shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(
            ".git", ".claude", "cat-api", "clone-runs", "__pycache__"))
        f = work / relpath
        text = f.read_text()
        if old not in text:
            return None, "anchor not found — the source moved"
        f.write_text(text.replace(old, new, 1))
        p = subprocess.run([sys.executable, str(work / "tests" / "test_plan.py")],
                           capture_output=True, text=True)
        # A mutant that stops the suite from even importing (a SyntaxError, say) prints
        # no FAIL/ERROR headers and no "Ran N tests" line. That is NOT a survivor — it
        # is a malformed mutation — and it must not be reported as one. It happened: a
        # mutation that inserted a second `retry=` keyword looked like a coverage gap.
        if not any(line.startswith("Ran ") for line in p.stderr.splitlines()):
            tail = (p.stderr.strip().splitlines() or ["<no output>"])[-1]
            return None, f"mutant broke the build — fix the mutation, it proves nothing: {tail}"
        failed = [line.split(" (")[0].removeprefix("FAIL: ").removeprefix("ERROR: ")
                  for line in p.stderr.splitlines()
                  if line.startswith(("FAIL: ", "ERROR: "))]
        return failed, None


def main():
    survived, broken = [], []
    for name, relpath, old, new in MUTATIONS:
        failed, err = run_one(relpath, old, new)
        if err:
            broken.append(name)
            print(f"  ??  {name}\n      {err}")
        elif not failed:
            survived.append(name)
            print(f"  --  {name}\n      SURVIVED — no test caught this")
        else:
            shown = ", ".join(failed[:3]) + (" ..." if len(failed) > 3 else "")
            print(f"  ok  {name}\n      caught by {len(failed)}: {shown}")

    print(f"\n{len(MUTATIONS) - len(survived) - len(broken)}/{len(MUTATIONS)} caught")
    if broken:
        print(f"{len(broken)} mutation(s) no longer apply — update them, do not delete "
              f"them: {', '.join(broken)}")
        return 2
    if survived:
        print(f"{len(survived)} mutant(s) survived: {', '.join(survived)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
