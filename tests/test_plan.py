#!/usr/bin/env python3
"""
Plan-level assertions. No token, no network, no writes.

Every bug found in this project so far surfaced as a live 422 — a permanent record in
sandbox, because channels and processors can be neither deleted nor deactivated. These
tests exist so the next one surfaces here instead. They cover what is checkable without
CAT:

  * the plan document — step count, ordering, requires/provides integrity
  * substitution — that a dry run resolves every placeholder and mints ids of CAT's shape
  * the five live bugs already fixed, each pinned by the property that would regress
  * the derivations that must REFUSE rather than guess
  * the safety gates — that a live run needs three independent things

What they cannot cover is whether CAT accepts a body. The fixtures are synthetic (see
fixtures.py), so a passing test proves the builder is self-consistent, not that the
contract is right. Only a known-good live call settles that.

    python3 tests/test_plan.py
    python3 -m unittest discover -s tests
"""
import json, pathlib, re, sys, unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import clone_capture as cc
import clone_apply as capp
import clone_cleanup as ccl
import server
import clone_keys
import fixtures as fx

# RSA keygen costs ~1s per 2048-bit key and every live-mock apply would mint one. One
# 1024-bit keypair per test process is plenty: the suite asserts on the FLOW (register,
# decrypt, feed, redact), never on key strength. clone_keys itself is tested directly.
_TEST_KEYPAIR = clone_keys.generate(1024)
capp.clone_keys.generate = lambda bits=2048: _TEST_KEYPAIR

# Documented dependency order (README.md "Step order"). A single-entity plan must be
# monotonic in this sequence; multi-entity plans repeat the entity-scoped tail.
KIND_ORDER = ["client", "client_risk_settings", "client_flow_account",
              "client_compass_settings", "client_compass_check", "vault_lookup", "entity",
              "currency_account",
              "processing_profile", "entity_service", "processing_channel", "processor",
              "sessions_channel", "sessions_profile_processor",
              "payment_routing_rule", "payout_routing_rule", "payout_setting",
              "payout_route_check", "client_public_crypto_key", "client_api_secret_key",
              "client_api_public_key", "webhook_workflow", "webhook_check"]

# The reference shape: 1 entity, 2 currency accounts, 6 profiles, 1 channel with 4
# processors, a sessions channel with 4 profile-backed processors, 2 payment rules,
# 1 payout rule, 1 payout setting, 1 payout route.
EXPECTED_COUNTS = {
    "client": 1, "client_risk_settings": 1, "client_flow_account": 1,
    "client_compass_settings": 1, "client_compass_check": 1, "vault_lookup": 1,
    "entity": 1, "currency_account": 2,
    "processing_profile": 6, "entity_service": 1, "processing_channel": 1,
    "processor": 4,
    "sessions_channel": 1, "sessions_profile_processor": 4,
    "payment_routing_rule": 2, "payout_routing_rule": 1, "payout_setting": 1,
    "payout_route_check": 1,
    # the destination's API keys, minted in the run (2026-09-08)
    "client_public_crypto_key": 1, "client_api_secret_key": 1, "client_api_public_key": 1,
}
EXPECTED_STEPS = 34
# Steps nothing downstream depends on: a live failure (or a missing key) is a flag and the
# run continues. Everything else must be non-optional.
OPTIONAL_KINDS = {"client_public_crypto_key", "client_api_secret_key", "client_api_public_key",
                  "webhook_workflow", "webhook_check"}
# Steps whose CREATE body legitimately carries entity_id (the known-good token create names
# an entity); everywhere else entity_id is a server-assigned echo that must be stripped.
ENTITY_ID_IS_A_REQUEST_FIELD = {"client_api_secret_key", "client_api_public_key"}

ANY_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
CAT_ID = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9]{26}$")


def plan_for(cap, name="Clone Target"):
    return cc.build_plan(cap, name)


# Codes a plan built from the reference capture ALWAYS carries: the three client-level
# services this tool cannot carry across (network tokens, RTAU, Intelligent Acceptance),
# each a one-line manual step. Tests that mean "nothing else was flagged" filter exactly
# these and nothing more.
EXPECTED_REFERENCE_FLAG_CODES = ("network_tokens_manual", "rtau_manual",
                                 "intelligent_acceptance_manual")
MANUAL_STEP_MESSAGE = re.compile(
    r"^(NETWORK TOKEN|RTAU|IA) SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT$")


def unexpected_warnings(plan):
    """Warnings other than the ones every reference plan carries by design."""
    expected_msgs = {f["message"] for f in plan["flags"]
                     if f["code"] in EXPECTED_REFERENCE_FLAG_CODES}
    return [w for w in plan["warnings"] if w not in expected_msgs]


def steps_of(plan, kind):
    return [s for s in plan["steps"] if s["kind"] == kind]


def one_step(plan, kind):
    s = steps_of(plan, kind)
    assert len(s) == 1, f"expected exactly one {kind} step, got {len(s)}"
    return s[0]


def wire_text(step):
    """A step's path and body with every placeholder removed.

    What is left is what would go on the wire as a literal value. A source id surviving
    here is a source-client reference about to be POSTed to the clone — the class of bug
    that produced `invalid_client_settlement_currency_accounts`.
    """
    return ANY_PLACEHOLDER.sub("", json.dumps({"path": step["path"],
                                               "body": step.get("body")}))


class NoSocket:
    """Any attempt to open a socket during a dry-run test is a test failure."""
    def __enter__(self):
        def boom(*a, **k):
            raise AssertionError("a socket was opened during a dry run")
        self._patches = [mock.patch("urllib.request.urlopen", boom),
                         mock.patch("time.sleep", lambda *a, **k: None)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------- plan document

class TestReferencePlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cap = fx.reference_capture()
        cls.plan = plan_for(cls.cap)

    def test_step_count(self):
        self.assertEqual(len(self.plan["steps"]), EXPECTED_STEPS)

    def test_kind_counts(self):
        self.assertEqual(self.plan["counts"], dict(sorted(EXPECTED_COUNTS.items())))

    def test_kinds_are_in_dependency_order(self):
        idx = [KIND_ORDER.index(s["kind"]) for s in self.plan["steps"]]
        self.assertEqual(idx, sorted(idx),
                         "steps are not in the documented dependency order")

    def test_seq_is_dense_and_one_based(self):
        self.assertEqual([s["seq"] for s in self.plan["steps"]],
                         list(range(1, EXPECTED_STEPS + 1)))

    # Network tokens has a prerequisite this tool cannot satisfy (VAS pricing), so every
    # plan that clones it carries exactly one expected flag. "Clean" means nothing else.
    EXPECTED_FLAG_CODES = list(EXPECTED_REFERENCE_FLAG_CODES)

    def test_validate_plan_reports_only_the_known_prerequisites(self):
        problems = cc.validate_plan(self.plan)
        self.assertEqual(len(problems), len(self.EXPECTED_FLAG_CODES))
        for p in problems:
            self.assertTrue(p.startswith("warning: ") and MANUAL_STEP_MESSAGE.match(p[9:]), p)

    def test_no_warnings_beyond_the_known_prerequisites(self):
        self.assertEqual(sorted(f["code"] for f in self.plan["flags"]),
                         sorted(self.EXPECTED_FLAG_CODES))
        self.assertEqual(len(self.plan["warnings"]), len(self.EXPECTED_FLAG_CODES))

    def test_every_method_is_allowed(self):
        for s in self.plan["steps"]:
            self.assertIn(s["method"], capp.ALLOWED_METHODS,
                          f"step {s['seq']} uses {s['method']}")

    def test_delete_can_never_appear_in_a_plan(self):
        # Removal is clone_cleanup's job, behind its own gate. A plan must not be able
        # to destroy anything.
        self.assertNotIn("DELETE", capp.ALLOWED_METHODS)

    def test_every_requires_is_provided_by_an_earlier_step(self):
        provided = {}
        for s in self.plan["steps"]:
            for r in s["requires"]:
                self.assertIn(r, provided,
                              f"step {s['seq']} ({s['kind']}) requires {r}")
                self.assertLess(provided[r], s["seq"])
            if s.get("provides"):
                self.assertNotIn(s["provides"], provided,
                                 f"step {s['seq']} re-provides {s['provides']}")
                provided[s["provides"]] = s["seq"]

    def test_every_placeholder_has_a_providing_step(self):
        provides = {s["provides"] for s in self.plan["steps"] if s.get("provides")}
        for s in self.plan["steps"]:
            for src in capp.unresolved(s["path"], s.get("body")):
                self.assertIn(src, provides,
                              f"step {s['seq']} references {src}, which nothing provides")

    def test_only_get_steps_are_read_only(self):
        for s in self.plan["steps"]:
            if s["method"] == "GET":
                self.assertIsNone(s["body"])


# ---------------------------------------------------------------- substitution

class TestDryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())
        with NoSocket():
            cls.dry = capp.apply_plan(cls.plan, dry_run=True)

    def test_dry_run_opens_no_socket(self):
        # setUpClass already ran the whole plan inside NoSocket; re-assert explicitly so
        # the guarantee is named by a test rather than buried in setup.
        with NoSocket():
            run = capp.apply_plan(self.plan, dry_run=True)
        self.assertTrue(run["dry_run"])

    def test_dry_run_completes_every_step(self):
        self.assertEqual(self.dry["counts"],
                         {"steps": EXPECTED_STEPS, "created": EXPECTED_STEPS,
                          "failed": 0, "not_attempted": 0})
        self.assertEqual(self.dry["problems"], [])

    def test_no_placeholder_survives_substitution(self):
        for e in self.dry["journal"]:
            blob = json.dumps({"path": e["path"], "body": e.get("body")})
            self.assertNotIn("{{", blob, f"step {e['seq']} ({e['kind']}) left a placeholder")

    def test_id_map_covers_every_provides(self):
        want = {s["provides"] for s in self.plan["steps"] if s.get("provides")}
        self.assertEqual(set(self.dry["id_map"]), want)

    def test_synthetic_ids_match_cat_id_shape(self):
        # <prefix>_<26 lowercase alphanumerics>, so any length or pattern validation
        # downstream is genuinely exercised in dry-run.
        for src, new in self.dry["id_map"].items():
            self.assertRegex(new, CAT_ID, f"{src} -> {new}")
            self.assertEqual(new.split("_", 1)[0], src.split("_", 1)[0])

    def test_synthetic_ids_are_unique(self):
        vals = list(self.dry["id_map"].values())
        self.assertEqual(len(vals), len(set(vals)))

    def test_dry_run_records_no_created_objects(self):
        # created_objects is what cleanup reverses; a dry run created nothing.
        self.assertEqual(self.dry["created_objects"], [])

    def test_dry_run_writes_no_journal_file(self):
        self.assertIsNone(self.dry["journal_path"])

    def test_journal_header_carries_the_plans_flags_and_skips(self):
        # A run that completes 86/86 can still hide a feature the plan silently skipped.
        # The journal is the only durable record, so the plan's findings travel with it.
        import tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory() as tmp:
            jp = _pl.Path(tmp) / "run.jsonl"
            with NoSocket():
                capp.apply_plan(self.plan, dry_run=True, journal_path=str(jp))
            header = json.loads(jp.read_text().splitlines()[0])["_header"]
        self.assertEqual([f["code"] for f in header["plan_flags"]],
                         [f["code"] for f in self.plan["flags"]])
        self.assertEqual(header["plan_skipped"], self.plan["skipped"])
        self.assertEqual(header["plan_counts"], self.plan["counts"])

    def test_a_missing_provider_blocks_rather_than_sends(self):
        # Remove the step that provides the vault account and the channel must block on
        # an unresolved placeholder instead of POSTing `{{vact_...}}` as a literal.
        broken = json.loads(json.dumps(self.plan))
        broken["steps"] = [s for s in broken["steps"] if s["kind"] != "vault_lookup"]
        with NoSocket():
            run = capp.apply_plan(broken, dry_run=True)
        self.assertTrue(run["problems"])
        self.assertIn("unresolved", run["problems"][0])
        blocked = [e for e in run["journal"] if e.get("status") == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["kind"], "processing_channel")


# ---------------------------------------------------------------- the five live bugs

class TestKnownTraps(unittest.TestCase):
    """One test per trap in README's "CAT contract traps" table."""

    @classmethod
    def setUpClass(cls):
        cls.cap = fx.reference_capture()
        cls.plan = plan_for(cls.cap)

    # -- trap 1: profiles must be created via the v2 endpoint -------------------

    def test_profiles_use_the_v2_endpoint(self):
        for s in steps_of(self.plan, "processing_profile"):
            self.assertTrue(s["path"].endswith("/processing-profiles/v2"), s["path"])

    def test_profile_body_omits_response_only_and_server_derived_fields(self):
        for s in steps_of(self.plan, "processing_profile"):
            # the singular is a response-only echo; banking_partner_code is derived
            self.assertNotIn("checkout_legal_entity_code", s["body"])
            self.assertNotIn("banking_partner_code", s["body"])

    def test_profile_body_sends_the_plural_legal_entity_codes(self):
        for s in steps_of(self.plan, "processing_profile"):
            self.assertEqual(s["body"]["checkout_legal_entity_codes"], ["CKO_GB"])

    def test_profile_body_keeps_v2_only_fields(self):
        # Both were dropped once, purely because they are absent from the v1 schema.
        for s in steps_of(self.plan, "processing_profile"):
            self.assertIn("acceptance_mode", s["body"])
            self.assertIn("is_external_certification_enabled", s["body"])

    # -- trap 2: the vault account is client-level ------------------------------

    def test_vault_lookup_is_a_read_that_precedes_every_channel(self):
        v = one_step(self.plan, "vault_lookup")
        self.assertEqual(v["method"], "GET")
        self.assertIsNone(v["body"])
        self.assertEqual(v["provides"], fx.SOURCE_VAULT)
        for s in steps_of(self.plan, "processing_channel"):
            self.assertLess(v["seq"], s["seq"])

    def test_vault_lookup_retries_because_provisioning_is_async(self):
        v = one_step(self.plan, "vault_lookup")
        self.assertIsNotNone(v["retry"])
        # Measured at ~10s after client create, resolving on the 4th attempt.
        self.assertGreaterEqual(v["retry"]["attempts"] * v["retry"]["delay_seconds"], 12)

    def test_channel_vault_service_points_at_the_clones_account(self):
        ch = one_step(self.plan, "processing_channel")
        vault = [s for s in ch["body"]["services"] if s.get("type") == "vault"]
        self.assertEqual(len(vault), 1)
        self.assertEqual(vault[0]["key"], cc.ph(fx.SOURCE_VAULT))
        self.assertIn(fx.SOURCE_VAULT, ch["requires"])

    # -- trap 3: a sessions channel's services live only on the detail ----------

    def test_composite_service_keys_are_fully_remapped(self):
        # The prism service key is "<client_id>|<entity_id>", both source ids. Remapping
        # only the vault service sent this verbatim and CAT returned
        # invalid_prism_merchant_service, which names neither.
        ch = one_step(self.plan, "processing_channel")
        prism = [s for s in ch["body"]["services"] if s.get("type") == "prism"][0]
        eid = self.cap["entities"][0]["id"]
        self.assertEqual(prism["key"],
                         f"{cc.ph(fx.SOURCE_CLIENT)}|{cc.ph(eid)}")
        self.assertIn(fx.SOURCE_CLIENT, ch["requires"])
        self.assertIn(eid, ch["requires"])

    def test_a_composite_key_resolves_at_apply_time(self):
        # Two placeholders in one string must both substitute, or the step blocks.
        with NoSocket():
            run = capp.apply_plan(self.plan, dry_run=True)
        ch = next(e for e in run["journal"] if e["kind"] == "processing_channel")
        prism = [s for s in ch["body"]["services"] if s.get("type") == "prism"][0]
        self.assertNotIn("{{", prism["key"])
        self.assertNotIn(fx.SOURCE_CLIENT, prism["key"])
        self.assertEqual(len(prism["key"].split("|")), 2)

    def test_an_unmappable_service_key_is_flagged(self):
        # A key naming something no step creates cannot be remapped. It must warn rather
        # than quietly ship a source reference.
        cap = fx.reference_capture()
        svcs = cap["entities"][0]["processing_channels"][0]["detail"]["services"]
        svcs.append({"type": "mystery", "key": fx._id("xyz", "unknownthing")})
        plan = plan_for(cap)
        self.assertTrue(any("cannot be remapped" in w for w in plan["warnings"]))

    def test_prism_service_is_enabled_on_the_entity_before_the_channel(self):
        # A channel service REFERENCES something enabled on the entity; it does not
        # enable it. Without this PUT the channel create fails with
        # invalid_prism_merchant_service even when the key is remapped correctly.
        svc = one_step(self.plan, "entity_service")
        self.assertEqual(svc["method"], "PUT")
        self.assertTrue(svc["path"].endswith("/services/prism"))
        self.assertEqual(svc["body"], {"is_enabled": True})
        self.assertIsNone(svc["provides"], "an entity service mints no id")
        for ch in steps_of(self.plan, "processing_channel"):
            self.assertLess(svc["seq"], ch["seq"])

    def test_prism_via_a_sessions_channel_also_enables_it(self):
        # SessionsMerchantServiceType includes prism, so a gateway channel need not
        # mention it for the entity service to be required.
        plan = plan_for(fx.sessions_only_prism_capture())
        self.assertEqual(len(steps_of(plan, "entity_service")), 1)

    def test_no_prism_means_no_entity_service_step(self):
        cap = fx.reference_capture()
        for ch in cap["entities"][0]["processing_channels"]:
            ch["detail"]["services"] = [{"type": "vault", "key": fx.SOURCE_VAULT}]
        self.assertEqual(steps_of(plan_for(cap), "entity_service"), [])

    def test_enabling_a_service_is_not_reversible_by_cleanup(self):
        # Honest accounting: there is no removal route for an entity service, so it must
        # show up in the permanent bucket rather than looking undoable.
        outlook = ccl.cleanup_outlook(self.plan)
        self.assertEqual(outlook["permanent"].get("entity_service"), 1)

    def test_sessions_channel_is_deliberately_not_id_mapped(self):
        # It SHARES the gateway channel's id. Registering it would overwrite the gateway
        # channel's mapping and later references would bind to the wrong object.
        s = one_step(self.plan, "sessions_channel")
        self.assertIsNone(s["provides"])

    def test_sessions_channel_sends_services_spelled_value(self):
        s = one_step(self.plan, "sessions_channel")
        self.assertTrue(s["body"]["services"], "services_required")
        for svc in s["body"]["services"]:
            self.assertIn("value", svc)
            self.assertNotIn("type", svc)
        vault = [x for x in s["body"]["services"] if x["value"] == "vault"]
        self.assertEqual(vault[0]["key"], cc.ph(fx.SOURCE_VAULT))

    def test_sessions_processor_links_are_joined_on_scheme_and_mcc(self):
        links = steps_of(self.plan, "sessions_profile_processor")
        self.assertEqual(len(links), 4)
        for s in links:
            self.assertEqual(s["body"]["createType"], "existing")
            for f in ("gateway_profile_processor_id", "processing_profile_id"):
                self.assertRegex(s["body"][f], ANY_PLACEHOLDER)

    # -- trap 4: the catch-all routing rule must be created first ---------------

    def test_catch_all_payment_rule_is_created_first(self):
        rules = steps_of(self.plan, "payment_routing_rule")
        self.assertTrue(rules[0]["body"]["allow_any_event_type"],
                        "the narrowed rule was ordered before the catch-all")
        self.assertFalse(rules[1]["body"]["allow_any_event_type"])
        self.assertTrue(any("catch-all" in n for n in rules[0]["notes"]))

    def test_capture_order_really_is_the_wrong_order(self):
        # Guards the test above from becoming vacuous if the fixture is edited.
        raw = self.cap["entities"][0]["payment_routing"]
        self.assertFalse(cc.is_default_routing_rule(raw[0]))
        self.assertTrue(cc.is_default_routing_rule(raw[1]))

    def test_routing_rule_references_are_remapped(self):
        for s in steps_of(self.plan, "payment_routing_rule"):
            for f in ("processing_channel_id", "revenue_currency_account_id",
                      "fees_currency_account_id"):
                self.assertRegex(s["body"][f], ANY_PLACEHOLDER, f)

    # -- trap 5: clean() strips only the top level -----------------------------

    def test_payout_setting_nested_payment_instrument_is_cleaned(self):
        pi = one_step(self.plan, "payout_setting")["body"]["payment_instrument"]
        for k in ("id", "date_created", "date_modified", "e_tag"):
            self.assertNotIn(k, pi, f"payment_instrument kept {k}")
        self.assertEqual(pi["vault_account_id"], cc.ph(fx.SOURCE_VAULT))
        # CAT returns bank details in full on read — they are carried through, not
        # dropped, and are NOT a manual-entry case.
        self.assertEqual(pi["account_number"], "12345678")

    def test_payout_setting_nested_schedule_is_cleaned(self):
        sch = one_step(self.plan, "payout_setting")["body"]["payout_schedule"]
        for k in ("id", "date_created", "date_modified", "version"):
            self.assertNotIn(k, sch, f"payout_schedule kept {k}")

    def test_payout_schedule_drops_an_empty_scheduler_ids(self):
        # Only intraday schedules may carry it, and CAT rejects it even as [].
        sch = one_step(self.plan, "payout_setting")["body"]["payout_schedule"]
        self.assertNotIn("scheduler_ids", sch)

    def test_payout_schedule_currency_accounts_are_remapped(self):
        sch = one_step(self.plan, "payout_setting")["body"]["payout_schedule"]
        self.assertEqual(len(sch["currency_account_ids"]), 2)
        for ca in sch["currency_account_ids"]:
            self.assertRegex(ca, ANY_PLACEHOLDER)

    # -- salesforce case id ----------------------------------------------------

    def test_salesforce_case_id_is_always_the_sandbox_value(self):
        stamped = 0
        for s in self.plan["steps"]:
            v = (s.get("body") or {}).get("salesforce_case_id")
            if v is not None:
                self.assertEqual(v, cc.SANDBOX_SALESFORCE_CASE_ID)
                stamped += 1
        self.assertGreater(stamped, 0)

    def test_the_sources_real_case_id_appears_nowhere_in_the_plan(self):
        self.assertNotIn(fx.SOURCE_SALESFORCE_CASE_ID, json.dumps(self.plan))

    # -- the general form of trap 5 -------------------------------------------

    def test_no_source_id_reaches_the_wire_outside_a_placeholder(self):
        source_ids = fx.all_source_ids(self.cap)
        for s in self.plan["steps"]:
            text = wire_text(s)
            for sid in source_ids:
                self.assertNotIn(sid, text,
                                 f"step {s['seq']} ({s['kind']}) would send the source "
                                 f"id {sid} as a literal")

    def test_server_assigned_fields_are_stripped_everywhere(self):
        for s in self.plan["steps"]:
            body = s.get("body") or {}
            for k in ("id", "_links", "date_created", "date_modified", "e_tag",
                      "client_id", "entity_id"):
                # An `id` that is a PLACEHOLDER is the clone's own id, resolved at apply
                # time — the risk-settings PUT sends it because the proven body did. What
                # this test forbids is a SOURCE id echoed back as a literal.
                if k == "id" and isinstance(body.get("id"), str) \
                        and ANY_PLACEHOLDER.fullmatch(body["id"]):
                    continue
                if k == "entity_id" and s["kind"] in ENTITY_ID_IS_A_REQUEST_FIELD:
                    # a request field here, and it must be the CLONE's entity (placeholder
                    # or empty), never a source id echoed back
                    v = body.get("entity_id")
                    self.assertTrue(v == "" or ANY_PLACEHOLDER.fullmatch(v), (s["kind"], v))
                    continue
                self.assertNotIn(k, body, f"step {s['seq']} ({s['kind']}) kept {k}")

    def test_entity_body_is_shaped_for_create_not_echoed_from_the_get(self):
        e = one_step(self.plan, "entity")
        self.assertEqual(e["body"]["status"], "Active")          # required on create
        self.assertTrue(e["body"]["is_principal_same_as_registered"])  # derived
        self.assertNotIn("funding", e["body"])                   # read model, no shape
        for k in ("region", "type", "default_cko_legal_entity"):
            self.assertNotIn(k, e["body"])
        self.assertTrue(any(x["kind"] == "entity.funding" for x in self.plan["skipped"]))
        self.assertTrue(any(x["kind"] == "entity.acquiring_providers"
                            for x in self.plan["skipped"]))

    def test_processor_body_drops_capability_read_models(self):
        for s in steps_of(self.plan, "processor"):
            for k in ("status", "acquirer_name", "scheme_name", "gws_only",
                      "authorizations", "captures", "refunds", "voids",
                      "sessions_processor_id", "merchant_category_code_name"):
                self.assertNotIn(k, s["body"])
            # required on create, recoverable only from the per-processor detail
            self.assertIn("billing_information", s["body"])


# ---------------------------------------------------------------- profile create shape

class TestProfileCreateBody(unittest.TestCase):
    """The v2 GET is not a create body.

    Derived from three known-good live creates (mada, Amex, Visa) on one client. The
    central lesson is that **a profile's shape depends on its scheme** — those three had
    three different custom_settings field sets, and Visa alone carries top-level
    sca_exemptions_settings and payfac_settings. So nothing here may be keyed on a
    per-scheme whitelist; every rule is keyed on a field name wherever it appears.
    """

    @classmethod
    def setUpClass(cls):
        cls.cap = fx.reference_capture()
        cls.plan = plan_for(cls.cap)
        cls.profiles = steps_of(cls.plan, "processing_profile")

    def test_acquiring_bin_is_sent_as_a_string(self):
        # Confirmed twice: Amex sent "10000000232", Visa sent "402121", and both
        # responses echoed integers back. The swagger declares it string too.
        seen = 0
        for s in self.profiles:
            if "acquiring_bin" in s["body"]:
                self.assertIsInstance(s["body"]["acquiring_bin"], str,
                                      f"step {s['seq']} sent a number")
                seen += 1
        self.assertGreater(seen, 0, "fixture has no acquiring_bin to coerce")

    def test_authorization_validity_period_is_sent_as_a_string(self):
        seen = 0
        for s in self.profiles:
            cs = s["body"].get("custom_settings") or {}
            if "authorization_validity_period" in cs:
                self.assertIsInstance(cs["authorization_validity_period"], str)
                seen += 1
        self.assertGreater(seen, 0)

    def test_se_ccy_processing_threshold_is_sent_as_a_string(self):
        # Amex-only, and nested — clean() would never have reached it.
        seen = 0
        for s in self.profiles:
            for row in (s["body"].get("custom_settings") or {}).get("SE_CCY") or []:
                self.assertIsInstance(row["processing_threshold"], str)
                seen += 1
        self.assertGreater(seen, 0, "fixture has no SE_CCY block")

    def test_no_numeric_survives_in_a_string_field(self):
        # The general form, so a new scheme shape carrying one of these as a number is
        # caught even if no specific test above covers it.
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in cc.PROFILE_STRING_FIELDS + cc.PROFILE_STRING_FIELDS_CUSTOM:
                        self.assertNotIsInstance(v, (int, float),
                                                 f"{k} is still numeric")
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        for s in self.profiles:
            walk(s["body"])

    def test_card_acceptor_identification_code_is_blanked(self):
        # CAT assigns it per entity. Both known-good creates sent "" and both responses
        # came back with the same code for that entity. Carrying the source's is the
        # same class of error as carrying its vault account.
        seen = 0
        for s in self.profiles:
            # iDEAL takes a fixed "0" instead — see CAID_BY_SCHEME.
            if set(s["body"]["schemes"]) & set(cc.CAID_BY_SCHEME):
                continue
            for row in s["body"].get("business_settings") or []:
                self.assertEqual(row.get("card_acceptor_identification_code"), "",
                                 f"step {s['seq']} ({s['body']['schemes']})")
                seen += 1
        self.assertGreater(seen, 0)
        self.assertEqual(unexpected_warnings(self.plan), [])

    def test_manual_caid_warns_instead_of_carrying_the_source_code(self):
        plan = plan_for(fx.manual_caid_capture())
        self.assertTrue(any("auto_generate_card_acceptor_identification_code is false"
                            in w for w in plan["warnings"]))
        # and it must surface in the review view, not just sit in the plan
        self.assertTrue(cc.validate_plan(plan))

    def test_scheme_specific_blocks_are_preserved(self):
        # A per-scheme drop list derived from one scheme's known-good call would strip
        # these. Visa's two top-level objects and Amex's SE_CCY must all survive.
        bodies = {tuple(s["body"]["schemes"]): s["body"] for s in self.profiles}
        visa = bodies[("visa",)]
        self.assertIn("sca_exemptions_settings", visa)
        self.assertIn("payfac_settings", visa)
        self.assertIn("aft", visa["custom_settings"])
        amex = bodies[("amex",)]
        self.assertIn("SE_CCY", amex["custom_settings"])
        self.assertIn("program_feature", amex)
        ideal = bodies[("ideal",)]
        self.assertIn("credentials", ideal["custom_settings"])

    def test_the_three_scheme_shapes_really_do_differ(self):
        # Guards the test above from going vacuous if the fixture is flattened.
        shapes = {}
        for s in self.profiles:
            shapes[tuple(s["body"]["schemes"])] = frozenset(
                (s["body"].get("custom_settings") or {}).keys())
        self.assertGreaterEqual(len({v for v in shapes.values()}), 3,
                                "the fixture's profiles no longer differ by scheme")

    def test_salesforce_case_id_is_still_stamped(self):
        for s in self.profiles:
            self.assertEqual(s["body"]["salesforce_case_id"],
                             cc.SANDBOX_SALESFORCE_CASE_ID)

    # -- currency remediation (Checkout's rules, not ISO's) --------------------

    def test_retired_currencies_are_replaced(self):
        plan = plan_for(fx.stale_currency_capture())
        body = steps_of(plan, "processing_profile")[0]["body"]
        self.assertIn("SLE", body["currencies"])      # SLL redenominated
        self.assertNotIn("SLL", body["currencies"])
        self.assertTrue(any("SLL replaced with SLE" in n for n in
                            steps_of(plan, "processing_profile")[0]["notes"]))

    def test_removed_and_inactivated_currencies_are_dropped(self):
        plan = plan_for(fx.stale_currency_capture())
        step = steps_of(plan, "processing_profile")[0]
        for gone in ("ZWL", "LBP"):
            self.assertNotIn(gone, step["body"]["currencies"])
            self.assertTrue(any(f"{gone} dropped" in n for n in step["notes"]))

    def test_the_whole_remediation_in_one_pass(self):
        step = steps_of(plan_for(fx.stale_currency_capture()),
                        "processing_profile")[0]
        # SLL->SLE, HRK->EUR (already present, so de-duplicated), ZWL and LBP gone by
        # policy, ANG gone by validity.
        # ANG survives the table (no rule covers it) and is then dropped by the
        # validity gate, because cko_visa_gb does not list it.
        self.assertEqual(step["body"]["currencies"], ["GBP", "EUR", "USD", "SLE"])

    def test_dropped_currencies_are_flagged_but_do_not_halt(self):
        # Checkout's guidance for both LBP and ZWL is: say it cannot be added, then
        # carry on. So they must be FLAGGED (a warning, which renders as a banner) while
        # the profile is still created and every other step survives.
        for code in ("LBP", "ZWL"):
            cap = fx.reference_capture()
            cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
                ["GBP", code]
            plan = plan_for(cap)
            step = steps_of(plan, "processing_profile")[0]
            self.assertEqual(step["body"]["currencies"], ["GBP"], code)
            self.assertTrue(any(f"{code} dropped" in n for n in step["notes"]), code)
            self.assertTrue(any(f"{code} cannot be added" in w
                                for w in plan["warnings"]), code)
            # the run continues: nothing is skipped and the step count is unchanged
            self.assertEqual(len(plan["steps"]), EXPECTED_STEPS, code)
            # ...and the warning SAYS so: a dropped currency must never read as a
            # dropped profile. The operator reads this banner before applying.
            f = next(x for x in plan["flags"] if x["code"] == "currency_not_available"
                     and x["currency"] == code)
            self.assertEqual(f["action"], "dropped")
            self.assertIn("will still be created", f["message"], code)
            self.assertIn(code, f["message"])

    def test_every_dropped_currency_warning_says_the_object_survives(self):
        # Across every kind that drops a currency (profile, processor, routing rule,
        # compass): action "dropped" == the object is still created, and the message must
        # make that explicit. Only a currency ACCOUNT in a dead currency is not created.
        caps = [fx.reference_capture(), fx.routing_rule_currency_capture(),
                fx.processor_stale_currency_capture(), fx.stale_currency_capture(),
                fx.dropped_currency_account_capture()]
        seen = set()
        for cap in caps:
            for f in plan_for(cap)["flags"]:
                if f["code"] != "currency_not_available":
                    continue
                seen.add(f["action"])
                if f["action"] == "dropped":
                    self.assertRegex(f["message"], r"will still be (created|applied)", f)
                else:
                    self.assertEqual(f["action"], "not_created", f)
                    self.assertEqual(f["kind"], "currency_account", f)
                    self.assertIn("not created", f["message"], f)
        self.assertIn("dropped", seen)

    def test_hrk_becomes_eur_without_duplicating_it(self):
        cap = fx.stale_currency_capture()
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["EUR", "HRK", "GBP"]
        step = steps_of(plan_for(cap), "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["EUR", "GBP"])
        self.assertTrue(any("de-duplicated" in n for n in step["notes"]))

    def test_the_table_is_never_extended_by_inference(self):
        # ANG->XCG and SLL->SLE look like the same pattern, but only one is Checkout's
        # rule. The table must not invent the other: with validity unchecked, an
        # unrecognised code passes through untouched rather than being "corrected".
        cap = fx.legacy_capture_without_validity()
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["GBP", "ANG", "BGN"]
        step = steps_of(plan_for(cap), "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["GBP", "ANG", "BGN"])

    def test_an_unlisted_code_is_dropped_by_validity_not_by_a_guess(self):
        # With validity available the same codes go, but for a stated reason — "CAT does
        # not list it" — rather than via an invented successor mapping.
        cap = fx.reference_capture()
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["GBP", "ANG"]
        plan = plan_for(cap)
        step = steps_of(plan, "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["GBP"])
        f = [x for x in plan["flags"] if x.get("currency") == "ANG"][0]
        self.assertIn("does not list it as valid", f["reason"])
        self.assertIn("cko_visa_gb", f["reason"])

    def test_se_ccy_rows_follow_the_currency_rules(self):
        # An SE_CCY row naming a dropped currency has to go with it.
        cap = fx.reference_capture()
        d = cap["entities"][0]["processing_profiles"][3]["detail"]   # the amex one
        d["custom_settings"]["SE_CCY"] = [
            {"currency": "GBP", "processing_threshold": 0,
             "service_establishment_number": "9450125034"},
            {"currency": "ZWL", "processing_threshold": 0,
             "service_establishment_number": "9450125034"},
            {"currency": "SLL", "processing_threshold": 0,
             "service_establishment_number": "9450125034"},
        ]
        step = [s for s in steps_of(plan_for(cap), "processing_profile")
                if s["body"]["schemes"] == ["amex"]][0]
        got = [r["currency"] for r in step["body"]["custom_settings"]["SE_CCY"]]
        self.assertEqual(got, ["GBP", "SLE"])

    # -- scheme-specific CAID --------------------------------------------------

    def test_ideal_caid_is_a_fixed_zero(self):
        step = [s for s in self.profiles if s["body"]["schemes"] == ["ideal"]][0]
        for row in step["body"]["business_settings"]:
            self.assertEqual(row["card_acceptor_identification_code"], "0")

    def test_ideal_caid_wins_over_the_auto_generate_path(self):
        # "You can always set it to 0" — so iDEAL must not fall through to the warning
        # branch when auto_generate is false.
        cap = fx.reference_capture()
        for p in cap["entities"][0]["processing_profiles"]:
            if p["detail"]["schemes"] == ["ideal"]:
                p["detail"]["auto_generate_card_acceptor_identification_code"] = False
        plan = plan_for(cap)
        step = [s for s in steps_of(plan, "processing_profile")
                if s["body"]["schemes"] == ["ideal"]][0]
        self.assertEqual(
            step["body"]["business_settings"][0]["card_acceptor_identification_code"],
            "0")
        self.assertEqual(unexpected_warnings(plan), [])


class TestClientRiskSettings(unittest.TestCase):
    """The Fraud Detection tier is client-level. A new client comes up on the free tier,
    so the source's tier is applied by PUT straight after client create."""

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())
        cls.step = one_step(cls.plan, "client_risk_settings")

    def test_it_is_a_put_to_the_clones_risk_settings(self):
        self.assertEqual(self.step["method"], "PUT")
        self.assertEqual(self.step["path"], f"/clients/{cc.ph(fx.SOURCE_CLIENT)}/risk-settings")
        self.assertIn(fx.SOURCE_CLIENT, self.step["requires"])
        self.assertIsNone(self.step["provides"])

    def test_it_waits_out_asynchronous_default_provisioning(self):
        # A valid body returned 404 ~0.35s after client create; the vault account takes
        # ~10s to appear, so risk-settings defaults plausibly do too. Same threshold as
        # the vault lookup. PUT is idempotent, so retrying is safe.
        self.assertIsNotNone(self.step["retry"])
        self.assertGreaterEqual(self.step["retry"]["attempts"] * self.step["retry"]["delay_seconds"], 12)

    def test_it_runs_immediately_after_the_client(self):
        # Smallest blast radius: if the body is wrong, only a client exists.
        client = one_step(self.plan, "client")
        self.assertEqual(self.step["seq"], client["seq"] + 1)

    def test_body_is_the_proven_fields_without_server_echoes(self):
        # The known-good UI PUT carried the whole resource. Reducing it to tier alone
        # returned client_name_required live, so name (and id, which was also in the proven
        # body) are sent — as the CLONE's, not the source's.
        self.assertEqual(self.step["body"],
                         {"id": cc.ph(fx.SOURCE_CLIENT), "name": "Clone Target",
                          "tier": "premium", "read_only_restriction_enabled": False})

    def test_body_carries_no_source_echoes(self):
        # Timestamps, _links and the feature text are server-generated: never sent.
        for k in ("created_at", "last_updated_at", "_links",
                  "basic_tier_features", "premium_tier_features"):
            self.assertNotIn(k, self.step["body"], k)
        # id is the clone's placeholder, never the literal source id
        self.assertEqual(self.step["body"]["id"], cc.ph(fx.SOURCE_CLIENT))
        self.assertNotIn(fx.SOURCE_CLIENT, wire_text(self.step))
        # name is the clone's, never the source client's
        self.assertNotEqual(self.step["body"]["name"], "Reference Retail Ltd")

    def test_name_is_the_clones_not_the_sources(self):
        # Same expression the client step uses, so the two can never diverge.
        cap = fx.reference_capture()
        plan = cc.build_plan(cap, None)
        rs = one_step(plan, "client_risk_settings")
        cl = one_step(plan, "client")
        self.assertEqual(rs["body"]["name"], "Reference Retail Ltd (clone)")
        self.assertEqual(rs["body"]["name"], cl["body"]["name"])
        plan = cc.build_plan(cap, "Explicit Target")
        self.assertEqual(one_step(plan, "client_risk_settings")["body"]["name"],
                         "Explicit Target")

    def test_tier_is_copied_not_hardcoded(self):
        cap = fx.reference_capture()
        cap["risk_settings"]["tier"] = "basic"
        cap["risk_settings"]["read_only_restriction_enabled"] = True
        step = one_step(plan_for(cap), "client_risk_settings")
        self.assertEqual(step["body"],
                         {"id": cc.ph(fx.SOURCE_CLIENT), "name": "Clone Target",
                          "tier": "basic", "read_only_restriction_enabled": True})

    def test_read_only_flag_omitted_when_the_source_does_not_report_it(self):
        cap = fx.reference_capture()
        del cap["risk_settings"]["read_only_restriction_enabled"]
        step = one_step(plan_for(cap), "client_risk_settings")
        self.assertEqual(step["body"], {"id": cc.ph(fx.SOURCE_CLIENT),
                                        "name": "Clone Target", "tier": "premium"})

    def test_unreadable_risk_settings_are_flagged_not_guessed(self):
        for rs in ({}, None, {"error": "x"}):
            cap = fx.reference_capture()
            cap["risk_settings"] = rs
            plan = plan_for(cap)
            self.assertEqual(steps_of(plan, "client_risk_settings"), [], repr(rs))
            self.assertTrue(any(f["code"] == "risk_settings_not_captured"
                                for f in plan["flags"]), repr(rs))

    def test_not_reversible_by_cleanup(self):
        self.assertEqual(ccl.cleanup_outlook(self.plan)["permanent"]
                         .get("client_risk_settings"), 1)


class TestClientNetworkTokens(unittest.TestCase):
    """Network tokens are NOT cloned: flagged as a manual step, with the source's
    configuration summarised so the operator can replicate it. The read path (CAT returns
    the blank template; the portal has the configuration) is kept and tested, as is the
    apply-side machinery built for it (base override, verifier, response excerpts)."""

    @classmethod
    def setUpClass(cls):
        cls.cap = fx.reference_capture()
        cls.plan = plan_for(cls.cap)
        cls.eid = cls.cap["entities"][0]["id"]
        cls.flag = next(f for f in cls.plan["flags"] if f["code"] == "network_tokens_manual")

    def test_no_network_token_step_is_ever_emitted(self):
        for s in self.plan["steps"]:
            self.assertNotIn("network", s["kind"])
            self.assertIsNone(s["base"], s["kind"])       # nothing leaves CAT
            # the destination-key and webhook steps are the only optional ones
            if s["kind"] not in OPTIONAL_KINDS:
                self.assertFalse(s["optional"], s["kind"])
        self.assertTrue(any(x["kind"] == "client_network_tokens" for x in self.plan["skipped"]))

    def test_manual_flag_summarises_what_to_replicate(self):
        self.assertEqual(self.flag["action"], "not_created")
        self.assertIn("MANUALLY", self.flag["message"])
        src = self.flag["source_configuration"]
        self.assertEqual(src["nt_state"], True)
        self.assertEqual(src["default_billed_entity"], "Reference Entity A")
        self.assertEqual(src["provisioning_state"], "active")
        self.assertEqual(src["default_provision_mode"], "sync")
        self.assertEqual((src["onboard_visa"], src["onboard_mastercard"]), (True, True))
        self.assertEqual(self.flag["source"], "nt-portal")
        # The message is the one-line instruction Patrick asked for (2026-09-04); the
        # detail of what to replicate lives in source_configuration, asserted above.
        self.assertEqual(self.flag["message"],
                         "NETWORK TOKEN SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT")

    def test_rtau_and_ia_manual_steps_are_raised_once_on_every_plan(self):
        # Neither service has a route this tool can use; both must be in every handover.
        for cap in (fx.reference_capture(), fx.multi_entity_capture(),
                    fx.no_currency_validation_capture()):
            plan = plan_for(cap)
            for code, msg in (("rtau_manual",
                               "RTAU SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT"),
                              ("intelligent_acceptance_manual",
                               "IA SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT")):
                fl = [f for f in plan["flags"] if f["code"] == code]
                self.assertEqual(len(fl), 1, code)
                self.assertEqual(fl[0]["message"], msg)
                self.assertEqual(fl[0]["action"], "not_created")
                self.assertEqual(plan["warnings"].count(msg), 1)

    def test_nothing_generated_by_onboarding_is_in_the_summary(self):
        blob = json.dumps(self.flag)
        self.assertNotIn("TRID", blob)
        self.assertNotIn("12345678", blob)          # no invented identifier
        self.assertNotIn("placeholder", blob)

    def test_form_values_are_flattened_with_status_lines_skipped(self):
        vals = cc.network_token_form_values(self.cap["network_tokens"])
        self.assertEqual(vals["nt_state"], True)
        self.assertEqual(vals["default_entity_id"], self.eid)
        self.assertEqual(vals["scheme_configuration.enabled_visa"], True)
        self.assertEqual(vals["scheme_configuration.identification_type"], "vat")
        self.assertNotIn("scheme_configuration.visa_status", vals)
        self.assertNotIn("TRID", json.dumps(vals))

    def test_cat_template_is_recognised_as_blank_and_the_portal_preferred(self):
        blank = fx.blank_network_tokens_template()
        populated = fx._network_tokens_form(self.eid, "Reference Entity A")
        self.assertIsNone((cc.network_token_form_values(blank) or {}).get("default_entity_id"))
        self.assertEqual(cc.choose_network_tokens_form(blank, populated), (populated, "nt-portal"))
        self.assertEqual(cc.choose_network_tokens_form(populated, blank), (populated, "cat"))
        self.assertEqual(cc.choose_network_tokens_form(blank, None), (blank, "none"))
        self.assertEqual(cc.choose_network_tokens_form({}, None)[1], "none")

    def test_blank_template_with_no_portal_names_both_in_the_flag(self):
        cap = fx.reference_capture()
        cap["network_tokens"] = fx.blank_network_tokens_template()
        cap["network_tokens_source"] = "none"
        cap["_meta"] = {"calls": 1, "errors": [{"path": "/configurations/" + fx.SOURCE_CLIENT,
                                                "code": 401}]}
        plan = plan_for(cap)
        f = next(x for x in plan["flags"]
                 if x["code"] == "network_tokens_default_entity_unreadable")
        self.assertIn("blank create template", f["message"])
        self.assertIn("HTTP 401", f["message"])
        self.assertFalse(any(x["code"] == "network_tokens_manual" for x in plan["flags"]))

    def test_unreadable_form_is_flagged(self):
        for nt in ({}, None, {"schema": {}}, {"error": "x"}):
            cap = fx.reference_capture()
            cap["network_tokens"] = nt
            plan = plan_for(cap)
            self.assertEqual(len([x for x in plan["flags"]
                                  if x["code"] == "network_tokens_not_captured"]), 1, repr(nt))

    def test_default_entity_outside_the_capture_is_flagged_not_guessed(self):
        cap = fx.reference_capture()
        cap["network_tokens"] = fx._network_tokens_form(fx._id("ent", "elsewhere"), "Other")
        plan = plan_for(cap)
        f = [x for x in plan["flags"]
             if x["code"] == "network_tokens_default_entity_not_in_scope"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["default_entity_id"], fx._id("ent", "elsewhere"))

    # -- apply-side machinery built for NT stays, exercised synthetically -----------

    def test_per_step_base_override_is_honoured_live(self):
        plan = plan_for(fx.reference_capture())
        target = one_step(plan, "client_flow_account")
        target["base"] = "https://elsewhere.example/api"
        seen = []
        def fake_send(base, token, method, path, body=None, timeout=30):
            seen.append((base, method))
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", "x")}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://cat", token="t", dry_run=False,
                                  pace_seconds=0)
        self.assertEqual([m for b, m in seen if b == "https://elsewhere.example/api"], ["PUT"])
        e = next(x for x in run["journal"] if x["kind"] == "client_flow_account")
        self.assertEqual(e["base"], "https://elsewhere.example/api")

    def test_response_excerpt_is_journalled_for_id_less_writes(self):
        plan = plan_for(fx.reference_capture())
        def fake_send(base, token, method, path, body=None, timeout=30):
            if "compass-settings" in path and method == "POST":
                return {"display_currency": "USD", "echoed": "form"}, 201, None
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", "x")}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://cat", token="t", dry_run=False,
                                  pace_seconds=0)
        e = next(x for x in run["journal"] if x["kind"] == "client_compass_settings")
        self.assertIn("echoed", e["response_excerpt"])
        c = next(x for x in run["journal"] if x["kind"] == "client")
        self.assertNotIn("response_excerpt", c)

    def test_verifier_ignores_pending_status_but_catches_a_changed_switch(self):
        expected = {"nt_state": True, "provisioning_state": "active",
                    "default_provision_mode": "sync",
                    "enabled_visa": True, "enabled_mastercard": True}
        flags, summary = capp.verify_network_tokens(expected, fx.clone_network_tokens_form(self.eid))
        self.assertEqual([f["code"] for f in flags], ["network_tokens_differ"])
        self.assertEqual(flags[0]["differences"],
                         {"default_provision_mode": {"source": "sync", "clone": "async"}})
        self.assertTrue(summary["form_read"])

    def test_a_2xx_that_persisted_nothing_is_its_own_flag(self):
        expected = {"nt_state": True, "provisioning_state": "active",
                    "default_provision_mode": "sync",
                    "enabled_visa": True, "enabled_mastercard": True}
        flags, _ = capp.verify_network_tokens(expected, fx.blank_network_tokens_template())
        self.assertEqual([f["code"] for f in flags], ["network_tokens_not_persisted"])

    def test_verifier_accepts_cat_shaped_onboard_switch_names(self):
        form = fx._network_tokens_form(self.eid, "Reference Entity A", provision_mode="sync")
        for fld in form["schema"]["form_fields"]:
            if fld.get("form_section", {}).get("label") == "Merchant details":
                for x in fld["form_section"]["form_fields"]:
                    if x.get("name") == "scheme_configuration.enabled_visa":
                        x["name"] = "scheme_configuration.onboard_visa"
                    if x.get("name") == "scheme_configuration.enabled_mastercard":
                        x["name"] = "scheme_configuration.onboard_mastercard"
        expected = {"nt_state": True, "provisioning_state": "active",
                    "default_provision_mode": "sync",
                    "enabled_visa": True, "enabled_mastercard": True}
        self.assertEqual(capp.verify_network_tokens(expected, form)[0], [])

    def test_the_two_form_flatteners_agree_on_values(self):
        a = cc.network_token_form_values(self.cap["network_tokens"])
        b = capp._network_token_form_values(self.cap["network_tokens"])
        for k, v in b.items():
            self.assertEqual(a[k], v, k)


class TestClientFlowAccount(unittest.TestCase):
    """Flow (hosted checkout) is a client-level on/off flag. Known-good live PUT on a NEW
    client: {"is_enabled": true} -> 200, response minting a fresh acc_* id."""

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())
        cls.step = one_step(cls.plan, "client_flow_account")

    def test_it_is_the_proven_put(self):
        self.assertEqual(self.step["method"], "PUT")
        self.assertEqual(self.step["path"], f"/clients/{cc.ph(fx.SOURCE_CLIENT)}/flow-account")
        self.assertIsNone(self.step["provides"])
        self.assertIn(fx.SOURCE_CLIENT, self.step["requires"])

    def test_body_is_the_flag_alone(self):
        self.assertEqual(self.step["body"], {"is_enabled": True})

    def test_the_sources_account_id_never_reaches_the_body(self):
        # acc_* is client-specific and response-only, exactly like the vault account.
        src_acc = fx.reference_capture()["flow_account"]["id"]
        self.assertNotIn(src_acc, json.dumps(self.step["body"]))
        self.assertNotIn("id", self.step["body"])
        # nowhere on the wire — bodies and paths of every step (notes may mention it)
        for s in self.plan["steps"]:
            self.assertNotIn(src_acc, json.dumps({"path": s["path"], "body": s["body"]}))

    def test_the_value_is_copied_not_hardcoded(self):
        cap = fx.reference_capture()
        cap["flow_account"]["is_enabled"] = False
        step = one_step(plan_for(cap), "client_flow_account")
        self.assertEqual(step["body"], {"is_enabled": False})

    def test_it_runs_in_the_client_level_block_before_any_entity(self):
        client = one_step(self.plan, "client")
        entity = one_step(self.plan, "entity")
        self.assertGreater(self.step["seq"], client["seq"])
        self.assertLess(self.step["seq"], entity["seq"])

    def test_unreadable_flow_account_is_flagged_not_guessed(self):
        for fa in ({}, None, {"id": "acc_x"}, {"is_enabled": "true"}):
            cap = fx.reference_capture()
            cap["flow_account"] = fa
            plan = plan_for(cap)
            self.assertEqual(steps_of(plan, "client_flow_account"), [], repr(fa))
            self.assertTrue(any(f["code"] == "flow_account_not_captured"
                                for f in plan["flags"]), repr(fa))

    def test_not_reversible_by_cleanup(self):
        self.assertEqual(ccl.cleanup_outlook(self.plan)["permanent"]
                         .get("client_flow_account"), 1)


class TestClientCompassSettings(unittest.TestCase):
    """Compass (Dashboard) settings are client-level: display currency + conversion
    currencies. POSTed at step 3, then read back and compared, because a 2xx does not
    prove display_currency took — the support site says it cannot be changed once set."""

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())
        cls.post = one_step(cls.plan, "client_compass_settings")
        cls.check = one_step(cls.plan, "client_compass_check")

    def test_post_because_put_cannot_carry_conversion_currencies(self):
        self.assertEqual(self.post["method"], "POST")
        self.assertEqual(self.post["path"],
                         f"/clients/{cc.ph(fx.SOURCE_CLIENT)}/compass-settings")
        self.assertIsNone(self.post["provides"])
        self.assertIn(fx.SOURCE_CLIENT, self.post["requires"])

    def test_body_is_exactly_the_create_schema(self):
        self.assertEqual(self.post["body"],
                         {"conversion_currencies": ["EUR", "GBP", "SEK", "USD"],
                          "display_currency": "USD"})
        self.assertNotIn(fx.SOURCE_CLIENT, json.dumps(self.post["body"]))

    def test_ordered_in_the_client_level_block_then_verified(self):
        # client -> risk -> flow -> compass POST -> compass check, all before vault_lookup
        flow = one_step(self.plan, "client_flow_account")
        self.assertEqual(self.post["seq"], flow["seq"] + 1)
        self.assertLess(self.check["seq"], one_step(self.plan, "vault_lookup")["seq"])
        self.assertEqual(self.check["seq"], self.post["seq"] + 1)
        self.assertEqual(self.check["method"], "GET")
        self.assertIsNone(self.check["body"])
        self.assertEqual(self.check["verify"]["compare"], "compass_settings")
        self.assertEqual(self.check["verify"]["expected"],
                         {"display_currency": "USD",
                          "conversion_currencies": ["EUR", "GBP", "SEK", "USD"]})

    def test_conversion_currencies_go_through_the_global_rules(self):
        cap = fx.reference_capture()
        cap["compass_settings"]["conversion_currencies"] = \
            ["GBP", "SLL", "ZWL", "LBP", "JPY"]          # JPY: not in the valid set
        plan = plan_for(cap)
        post = one_step(plan, "client_compass_settings")
        self.assertEqual(post["body"]["conversion_currencies"], ["GBP", "SLE"])
        dropped = sorted(f["currency"] for f in plan["flags"]
                         if f["code"] == "currency_not_available"
                         and f["kind"] == "client_compass_settings")
        self.assertEqual(dropped, ["JPY", "LBP", "ZWL"])
        # and the verify expectation is the REMEDIATED list, not the source's
        self.assertEqual(one_step(plan, "client_compass_check")["verify"]["expected"]
                         ["conversion_currencies"], ["GBP", "SLE"])

    def test_unreadable_compass_settings_are_flagged_not_guessed(self):
        for comp in ({}, None, {"id": fx.SOURCE_CLIENT}):
            cap = fx.reference_capture()
            cap["compass_settings"] = comp
            plan = plan_for(cap)
            self.assertEqual(steps_of(plan, "client_compass_settings"), [], repr(comp))
            self.assertEqual(steps_of(plan, "client_compass_check"), [], repr(comp))
            self.assertTrue(any(f["code"] == "compass_settings_not_captured"
                                for f in plan["flags"]), repr(comp))

    def test_verifier_flags_a_display_currency_that_did_not_take(self):
        flags, summary = capp.verify_compass_settings(
            self.check["verify"]["expected"],
            {"display_currency": "GBP", "conversion_currencies": ["EUR", "GBP", "SEK", "USD"]})
        self.assertEqual([f["code"] for f in flags], ["display_currency_differs"])
        self.assertEqual((flags[0]["source"], flags[0]["clone"]), ("USD", "GBP"))
        self.assertEqual(summary["conversion_currencies"]["missing"], 0)

    def test_verifier_flags_missing_conversion_currencies_only(self):
        flags, summary = capp.verify_compass_settings(
            self.check["verify"]["expected"],
            {"display_currency": "usd", "conversion_currencies": ["EUR", "GBP", "CHF"]})
        self.assertEqual([f["code"] for f in flags], ["conversion_currencies_differ"])
        self.assertEqual(flags[0]["missing"], ["SEK", "USD"])
        self.assertEqual(flags[0]["extra_on_clone"], ["CHF"])   # noted, not a flag

    def test_verifier_is_quiet_when_everything_took(self):
        flags, _ = capp.verify_compass_settings(
            self.check["verify"]["expected"],
            {"display_currency": "USD", "conversion_currencies": ["USD", "SEK", "GBP", "EUR"]})
        self.assertEqual(flags, [])

    def test_verifier_survives_a_non_dict_response(self):
        flags, _ = capp.verify_compass_settings(self.check["verify"]["expected"], None)
        self.assertEqual(sorted(f["code"] for f in flags),
                         ["conversion_currencies_differ", "display_currency_differs"])

    def test_not_reversible_by_cleanup(self):
        self.assertEqual(ccl.cleanup_outlook(self.plan)["permanent"]
                         .get("client_compass_settings"), 1)


class TestPayoutRouteParity(unittest.TestCase):
    """A payout route is provisioned capability (a supported corridor), not merchant
    configuration. It is parity-checked at apply time and never created."""

    def test_no_payout_route_is_ever_created(self):
        for cap in (fx.reference_capture(), fx.payout_route_parity_capture()):
            plan = plan_for(cap)
            self.assertEqual(steps_of(plan, "payout_route"), [])
            for s in plan["steps"]:
                if "payout-routes" in s["path"]:
                    self.assertEqual(s["method"], "GET", s["path"])

    def test_the_check_step_reads_enabled_corridors_only(self):
        step = one_step(plan_for(fx.payout_route_parity_capture()), "payout_route_check")
        self.assertEqual(step["method"], "GET")
        self.assertIn("?enabled=true", step["path"])
        self.assertIsNone(step["body"])
        self.assertIsNone(step["provides"], "a read mints no id")
        self.assertEqual(step["verify"]["compare"], "payout_routes")

    def test_expected_corridors_are_normalised_from_the_list_shape(self):
        step = one_step(plan_for(fx.payout_route_parity_capture()), "payout_route_check")
        got = [(e["country"], e["currency"], e["schemes"])
               for e in step["verify"]["expected"]]
        self.assertEqual(got, [("GBR", "GBP", "Faster Payments"),
                               ("FRA", "EUR", "SEPA"),
                               ("USA", "USD", "ACH, Fedwire")])

    def test_an_unreadable_item_is_flagged_and_excluded(self):
        plan = plan_for(fx.payout_route_parity_capture())
        f = [x for x in plan["flags"] if x["code"] == "payout_route_unrecognised"]
        self.assertEqual(len(f), 1)
        self.assertEqual(len(one_step(plan, "payout_route_check")["verify"]["expected"]), 3)

    def test_normaliser_accepts_both_the_list_and_create_spellings(self):
        a = cc.normalise_payout_route({"country_value": "gbr", "currency_label": "gbp",
                                       "schemes_label": "FPS"})
        b = cc.normalise_payout_route({"country_iso3_code": "GBR", "currency_code": "GBP",
                                       "schemes": ["FPS"]})
        self.assertEqual((a["country"], a["currency"], a["schemes"]), ("GBR", "GBP", "FPS"))
        self.assertEqual((b["country"], b["currency"], b["schemes"]), ("GBR", "GBP", "FPS"))
        self.assertIsNone(cc.normalise_payout_route({"country_label": "x"}))
        self.assertIsNone(cc.normalise_payout_route("not a dict"))

    def test_the_two_normalisers_agree(self):
        # clone_apply carries a copy because the modules are self-contained. If they
        # drift, source and clone are reduced differently and the comparison is wrong.
        for item in fx.payout_route_parity_capture()["entities"][0]["payout_routes"]:
            self.assertEqual(cc.normalise_payout_route(item),
                             capp._normalise_payout_route(item))

    def test_verifier_flags_a_missing_corridor(self):
        expected = one_step(plan_for(fx.payout_route_parity_capture()),
                            "payout_route_check")["verify"]["expected"]
        flags, summary = capp.verify_payout_routes(expected, fx.CLONE_PAYOUT_ROUTES_RESPONSE)
        missing = [f for f in flags if f["code"] == "payout_route_missing_on_clone"]
        self.assertEqual([(f["country"], f["currency"]) for f in missing], [("USA", "USD")])
        self.assertIn("Payouts team", missing[0]["message"])
        self.assertEqual(summary["missing"], 1)

    def test_verifier_flags_a_scheme_difference(self):
        expected = one_step(plan_for(fx.payout_route_parity_capture()),
                            "payout_route_check")["verify"]["expected"]
        flags, _ = capp.verify_payout_routes(expected, fx.CLONE_PAYOUT_ROUTES_RESPONSE)
        diff = [f for f in flags if f["code"] == "payout_route_schemes_differ"]
        self.assertEqual([(f["country"], f["currency"]) for f in diff], [("FRA", "EUR")])
        self.assertEqual(diff[0]["source_schemes"], "SEPA")
        self.assertEqual(diff[0]["clone_schemes"], "SEPA Instant")

    def test_verifier_notes_but_does_not_flag_extra_corridors(self):
        expected = one_step(plan_for(fx.payout_route_parity_capture()),
                            "payout_route_check")["verify"]["expected"]
        flags, summary = capp.verify_payout_routes(expected, fx.CLONE_PAYOUT_ROUTES_RESPONSE)
        self.assertEqual(summary["extra_on_clone"], ["DEU/EUR"])
        self.assertFalse(any("DEU" in f["message"] for f in flags))

    def test_verifier_accepts_hal_and_bare_response_shapes(self):
        exp = [{"country": "GBR", "currency": "GBP", "schemes": "", "label": "GB / GBP"}]
        item = {"country_value": "GBR", "currency_label": "GBP"}
        for resp in ({"_embedded": {"data": [item]}}, {"data": [item]}, [item]):
            flags, _ = capp.verify_payout_routes(exp, resp)
            self.assertEqual(flags, [], repr(resp)[:50])
        flags, _ = capp.verify_payout_routes(exp, {})
        self.assertEqual(len(flags), 1)

    def test_dry_run_records_what_it_would_verify(self):
        plan = plan_for(fx.payout_route_parity_capture())
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True)
        e = next(x for x in run["journal"] if x["kind"] == "payout_route_check")
        self.assertEqual(e["would_verify"], {"compare": "payout_routes", "expected": 3})
        self.assertEqual(run["flags"], [])

    def test_live_verify_records_run_time_flags_without_failing_the_step(self):
        # Sockets are replaced by a fake _send: every write returns an id, the vault
        # lookup returns a vault id, and the payout-routes read returns the clone's
        # corridors. No network is touched.
        plan = plan_for(fx.payout_route_parity_capture())
        def fake_send(base, token, method, path, body=None, timeout=30):
            if "payout-routes" in path:
                return fx.CLONE_PAYOUT_ROUTES_RESPONSE, 200, None
            if "compass-settings" in path and method == "GET":
                # what the clone reports back: display currency stuck on a default
                return {"display_currency": "GBP",
                        "conversion_currencies": ["EUR", "GBP", "SEK", "USD"]}, 200, None
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", path.rsplit("/", 1)[-1][:8])}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://x", token="t", dry_run=False,
                                  pace_seconds=0)
        self.assertEqual(run["counts"]["failed"], 0, run["problems"])
        # this fake returns a bare id for the API-key creates, so their secrets cannot be
        # recovered — that path has its own tests (TestDestinationApiKeys); ignore it here
        codes = sorted(f["code"] for f in run["flags"] if not f["code"].startswith("api_key"))
        self.assertEqual(codes, ["display_currency_differs",
                                 "payout_route_missing_on_clone",
                                 "payout_route_schemes_differ"])
        e = next(x for x in run["journal"] if x["kind"] == "payout_route_check")
        self.assertEqual(e["verify"]["missing"], 1)
        self.assertEqual(len(e["flags"]), 2)
        # each run-time flag is stamped with the seq of the verify step that raised it
        raised_by = {"payout_route_": "payout_route_check",
                     "display_currency_differs": "client_compass_check",
                     "conversion_currencies_differ": "client_compass_check"}
        seq_of = {x["kind"]: x["seq"] for x in run["journal"]}
        for f in run["flags"]:
            if f["code"].startswith("api_key"):
                continue
            kind = next(v for k, v in raised_by.items() if f["code"].startswith(k))
            self.assertEqual(f["seq"], seq_of[kind], f["code"])
        # a read creates nothing to clean up
        self.assertFalse(any(o["kind"] == "payout_route_check"
                             for o in run["created_objects"]))

    def test_an_optional_step_failing_flags_and_continues(self):
        # An optional step returns 422; everything after it must still run. No step is
        # optional in the current plan, so one is marked so here.
        plan = plan_for(fx.payout_route_parity_capture())
        nt = one_step(plan, "client_flow_account")
        nt["optional"] = True
        def fake_send(base, token, method, path, body=None, timeout=30):
            if "flow-account" in path and method == "PUT":
                return {}, 422, '{"error_codes":["identification_value_required"]}'
            if "payout-routes" in path:
                return fx.CLONE_PAYOUT_ROUTES_RESPONSE, 200, None
            if "compass-settings" in path and method == "GET":
                return {"display_currency": "USD",
                        "conversion_currencies": ["EUR", "GBP", "SEK", "USD"]}, 200, None
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", path.rsplit("/", 1)[-1][:8])}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://x", token="t", dry_run=False,
                                  pace_seconds=0)
        self.assertEqual(run["counts"]["not_attempted"], 0, "the run halted")
        self.assertEqual(run["counts"]["failed"], 1)
        self.assertEqual(run["problems"], [], "an optional failure is a flag, not a problem")
        f = [x for x in run["flags"] if x["code"] == "optional_step_failed"]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["kind"], f[0]["seq"], f[0]["http"]),
                         ("client_flow_account", nt["seq"], 422))
        self.assertIn("identification_value_required", f[0]["error"])
        e = next(x for x in run["journal"] if x["seq"] == nt["seq"])
        self.assertEqual(e["status"], 422)
        self.assertEqual(e["flags"], f)

    def test_a_non_optional_failure_still_halts(self):
        plan = plan_for(fx.reference_capture())
        def fake_send(base, token, method, path, body=None, timeout=30):
            if path.endswith("/entities") and method == "POST":
                return {}, 422, '{"error_codes":["entity_status_required"]}'
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", "x")}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://x", token="t", dry_run=False,
                                  pace_seconds=0)
        self.assertGreater(run["counts"]["not_attempted"], 0)
        self.assertTrue(any("entity" in p for p in run["problems"]))

    def test_an_unknown_verifier_is_a_problem_not_a_crash(self):
        plan = plan_for(fx.payout_route_parity_capture())
        one_step(plan, "payout_route_check")["verify"]["compare"] = "nonsense"
        def fake_send(base, token, method, path, body=None, timeout=30):
            return {"id": fx._id("new", "x")}, 200 if method == "GET" else 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan, base="https://x", token="t", dry_run=False,
                                  pace_seconds=0)
        self.assertTrue(any("unknown verifier" in p for p in run["problems"]))


class TestCurrencyValidityLookup(unittest.TestCase):
    """CKO's currency metadata is dynamic, so CAT is the source of truth and is read
    fresh on every capture. The hardcoded tables cover only what a validity list cannot
    express: successor mappings, and codes excluded by policy rather than validity."""

    def test_parses_every_shape_cat_uses(self):
        for payload in (
                ["GBP", "eur"],                                   # bare codes
                [{"value": "GBP", "label": "x"}, {"value": "EUR"}],  # Option[]
                {"currencies": ["GBP", "EUR"]},                   # processors config
                {"currencies": [{"value": "GBP"}, {"value": "EUR"}]},
                {"countries": [{"value": "GBR", "currencies": [{"value": "GBP"}]},
                               {"value": "FRA", "currencies": [{"value": "EUR"}]}]},
                {"holding_currencies": ["GBP", "EUR"]}):
            self.assertEqual(cc.parse_currency_codes(payload), {"GBP", "EUR"},
                             repr(payload)[:60])

    def test_an_unparseable_response_is_none_not_empty(self):
        # THE important one. An empty set means "no currency is valid" and would drop
        # every currency in the plan; None means "not checked".
        for payload in ({}, None, [], "nope", {"unexpected": 1}, {"currencies": []}):
            self.assertIsNone(cc.parse_currency_codes(payload), repr(payload))

    def test_payout_route_currencies_are_keyed_by_country(self):
        got = cc.payout_route_currencies({"countries": [
            {"value": "GBR", "currencies": [{"value": "GBP"}, {"value": "EUR"}]},
            {"value": "hrv", "currencies": [{"value": "EUR"}]},
            {"value": "XXX", "currencies": []},          # no currencies -> omitted
        ]})
        self.assertEqual(got, {"GBR": ["EUR", "GBP"], "HRV": ["EUR"]})

    def test_the_acquirer_set_is_preferred_over_the_global_one(self):
        cap = fx.reference_capture()
        codes, scope = cc.valid_currency_set(cap, acquirer="cko_amex_gb")
        self.assertNotIn("AED", codes)          # global has AED, cko_amex_gb does not
        self.assertIn("cko_amex_gb", scope)

    def test_an_unknown_acquirer_falls_back_to_the_global_list(self):
        cap = fx.reference_capture()
        codes, scope = cc.valid_currency_set(cap, acquirer="cko_never_seen")
        self.assertIn("AED", codes)
        self.assertIn("currency list", scope)

    def test_unavailable_validation_is_flagged_not_silent(self):
        plan = plan_for(fx.no_currency_validation_capture())
        f = [x for x in plan["flags"]
             if x["code"] == "currency_validation_unavailable"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["action"], "not_checked")
        self.assertIn("/configuration/currencies", f[0]["message"])

    def test_the_payout_routes_list_being_unavailable_is_not_a_warning(self):
        # It 400s on every capture and gates nothing (routes are read back, not created),
        # so it must not put a warning at the top of every page. Other lists still do.
        cap = fx.reference_capture()
        cap["valid_currencies"]["unavailable"] = ["/payout-routes/configuration (HTTP 400)"]
        plan = plan_for(cap)
        self.assertFalse(any(x["code"] == "currency_validation_unavailable" for x in plan["flags"]))
        cap["valid_currencies"]["unavailable"].append("/currency-accounts/configuration (HTTP 503)")
        f = [x for x in plan_for(cap)["flags"] if x["code"] == "currency_validation_unavailable"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["endpoints"], ["/currency-accounts/configuration (HTTP 503)"])
        self.assertNotIn("payout-routes", f[0]["message"])

    def test_a_capture_with_no_validity_data_is_flagged(self):
        plan = plan_for(fx.legacy_capture_without_validity())
        self.assertTrue(any(x["code"] == "currency_validation_unavailable"
                            for x in plan["flags"]))

    def test_the_policy_rules_still_apply_when_validation_is_unavailable(self):
        cap = fx.legacy_capture_without_validity()
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["GBP", "SLL", "ZWL", "LBP"]
        step = steps_of(plan_for(cap), "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["GBP", "SLE"])

    def test_lbp_is_excluded_by_policy_even_though_cat_lists_it(self):
        # The fixture's valid list INCLUDES LBP on purpose. If it did not, this would
        # pass for the wrong reason — the validity gate would catch it and the policy
        # rule would never run.
        self.assertIn("LBP", fx.VALID_CURRENCIES["global"])
        cap = fx.reference_capture()
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["GBP", "LBP"]
        plan = plan_for(cap)
        step = steps_of(plan, "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["GBP"])
        f = [x for x in plan["flags"] if x.get("currency") == "LBP"][0]
        self.assertIn("must not be enabled for new merchants", f["reason"])

    def test_a_replacement_is_itself_validated(self):
        # SLL -> SLE is no use if SLE is unsupported for that acquirer.
        cap = fx.reference_capture()
        cap["valid_currencies"]["by_acquirer"]["cko_visa_gb"] = ["GBP"]
        cap["entities"][0]["processing_profiles"][0]["detail"]["currencies"] = \
            ["GBP", "SLL"]
        step = steps_of(plan_for(cap), "processing_profile")[0]
        self.assertEqual(step["body"]["currencies"], ["GBP"])


class TestFlags(unittest.TestCase):
    """Flags are the structured, non-halting findings a post-run report will be built
    from. They must be data, not prose — so the report can group and count rather than
    re-parse warning strings."""

    def test_a_clean_plan_carries_only_the_network_token_prerequisites(self):
        plan = plan_for(fx.reference_capture())
        self.assertEqual(sorted(f["code"] for f in plan["flags"]),
                         sorted(TestReferencePlan.EXPECTED_FLAG_CODES))
        self.assertEqual(len(plan["warnings"]), len(TestReferencePlan.EXPECTED_FLAG_CODES))

    def test_every_flag_is_mirrored_into_warnings(self):
        # warnings is what the review view already renders, so a flag must not be
        # invisible there.
        plan = plan_for(fx.manual_processor_with_sessions_link_capture())
        self.assertTrue(plan["flags"])
        for f in plan["flags"]:
            self.assertIn(f["message"], plan["warnings"])
        self.assertEqual(len(plan["flags"]), len(plan["warnings"]))

    def test_every_flag_carries_the_fields_a_report_needs(self):
        for cap in (fx.manual_processor_with_sessions_link_capture(),
                    fx.dropped_currency_account_capture(),
                    fx.no_legal_codes_anywhere_capture(),
                    fx.payout_route_parity_capture(),
                    fx.processor_stale_currency_capture()):
            for f in plan_for(cap)["flags"]:
                self.assertIsInstance(f["code"], str)
                self.assertRegex(f["code"], r"^[a-z][a-z0-9_]+$",
                                 "codes must be stable identifiers, groupable")
                self.assertTrue(f["message"])
                self.assertIn(f.get("action"),
                              ("dropped", "substituted", "not_created", "carried",
                               "not_supplied", "not_checked"), f["code"])

    def test_flags_never_halt_a_run(self):
        # The whole point: every one of these still produces an applicable plan.
        for cap in (fx.manual_processor_with_sessions_link_capture(),
                    fx.dropped_currency_account_capture(),
                    fx.payout_route_parity_capture(),
                    fx.processor_stale_currency_capture()):
            plan = plan_for(cap)
            self.assertTrue(plan["flags"])
            with NoSocket():
                run = capp.apply_plan(plan, dry_run=True)
            self.assertEqual(run["problems"], [])
            self.assertEqual(run["counts"]["failed"], 0)

    def test_a_flag_is_serialisable(self):
        # The plan is downloaded as JSON and the report will be built from it.
        plan = plan_for(fx.manual_processor_with_sessions_link_capture())
        self.assertEqual(json.loads(json.dumps(plan))["flags"], plan["flags"])


class TestLegalEntityCodes(unittest.TestCase):
    """`checkout_legal_entity_codes` is required on create and absent from older
    profiles' GETs — the failure that produced `checkout_legal_entity_code_required`
    on a live run."""

    def test_every_profile_body_carries_the_codes(self):
        plan = plan_for(fx.reference_capture())
        for s in steps_of(plan, "processing_profile"):
            self.assertTrue(s["body"].get("checkout_legal_entity_codes"),
                            f"step {s['seq']} would fail "
                            f"checkout_legal_entity_code_required")

    def test_a_legacy_profile_inherits_the_entitys_consensus(self):
        # Two known-good creates on one entity used the same code for different schemes,
        # so the value tracks the entity rather than the scheme.
        plan = plan_for(fx.legacy_profile_capture())
        amex = [s for s in steps_of(plan, "processing_profile")
                if s["body"]["schemes"] == ["amex"]][0]
        self.assertEqual(amex["body"]["checkout_legal_entity_codes"], ["CKO_GB"])
        self.assertTrue(any("entity-consensus" in n for n in amex["notes"]))
        self.assertEqual(unexpected_warnings(plan), [])

    def test_it_is_still_derived_at_apply_time(self):
        # The whole point is that the create succeeds, so prove the value survives to
        # the wire rather than just sitting in the plan.
        plan = plan_for(fx.legacy_profile_capture())
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True)
        for e in run["journal"]:
            if e["kind"] == "processing_profile":
                self.assertTrue(e["body"].get("checkout_legal_entity_codes"))

    def test_disagreeing_siblings_refuse_rather_than_pick_one(self):
        plan = plan_for(fx.disagreeing_legal_codes_capture())
        amex = [s for s in steps_of(plan, "processing_profile")
                if s["body"]["schemes"] == ["amex"]][0]
        self.assertNotIn("checkout_legal_entity_codes", amex["body"])
        self.assertTrue(any("ambiguous-2-distinct-values" in w
                            for w in plan["warnings"]))

    def test_nothing_to_copy_names_the_field_and_the_error(self):
        plan = plan_for(fx.no_legal_codes_anywhere_capture())
        warns = " ".join(plan["warnings"])
        self.assertIn("checkout_legal_entity_code_required", warns)
        self.assertIn("supply it by hand", warns)

    def test_the_hint_is_offered_but_never_used_as_the_value(self):
        # default_cko_legal_entity is undocumented and its vocabulary has never been
        # confirmed, so it must appear in the message and NOT in the body.
        cap = fx.no_legal_codes_anywhere_capture()
        cap["entities"][0]["detail"]["default_cko_legal_entity"] = "SOME_OTHER_VOCAB"
        plan = plan_for(cap)
        self.assertTrue(any("SOME_OTHER_VOCAB" in w for w in plan["warnings"]))
        for s in steps_of(plan, "processing_profile"):
            self.assertNotIn("SOME_OTHER_VOCAB", json.dumps(s["body"]))

    def test_a_profile_that_reports_its_own_codes_keeps_them(self):
        plan = plan_for(fx.disagreeing_legal_codes_capture())
        visa = [s for s in steps_of(plan, "processing_profile")
                if s["body"].get("acquirer_key") == "cko_visa_gb"][0]
        self.assertEqual(visa["body"]["checkout_legal_entity_codes"], ["cko-ltd-uk"])


class TestCurrencyRulesAreGlobal(unittest.TestCase):
    """The currency rules are not per-scheme and not profile-only.

    Every place a bare currency code reaches CAT goes through remediate_currencies: a
    profile's `currencies`, an Amex SE_CCY row, a currency account's `holding_currency`,
    a payout route's `currency_code`.
    """

    def test_the_policy_rules_are_scheme_blind(self):
        # The SLL/HRK/ZWL/LBP rules apply identically to every scheme. Tested with
        # validity unchecked so the per-acquirer gate cannot confound the result.
        cap = fx.legacy_capture_without_validity()
        for p in cap["entities"][0]["processing_profiles"]:
            p["detail"]["currencies"] = ["GBP", "SLL", "ZWL"]
        plan = plan_for(cap)
        profiles = steps_of(plan, "processing_profile")
        self.assertEqual(len(profiles), 6)
        for s in profiles:
            self.assertEqual(s["body"]["currencies"], ["GBP", "SLE"],
                             f"scheme {s['body']['schemes']} was treated differently")

    def test_validity_is_scoped_per_acquirer(self):
        # And this is where schemes SHOULD differ: AED is valid for cko_visa_gb and not
        # for cko_amex_gb, so the same source list yields different bodies.
        cap = fx.reference_capture()
        for p in cap["entities"][0]["processing_profiles"]:
            p["detail"]["currencies"] = ["GBP", "AED"]
        plan = plan_for(cap)
        by_acq = {s["body"]["acquirer_key"]: s["body"]["currencies"]
                  for s in steps_of(plan, "processing_profile")}
        self.assertEqual(by_acq["cko_visa_gb"], ["GBP", "AED"])
        self.assertEqual(by_acq["cko_amex_gb"], ["GBP"])

    def test_currency_account_in_a_retired_currency_is_substituted(self):
        plan = plan_for(fx.substituted_currency_account_capture())
        accounts = steps_of(plan, "currency_account")
        self.assertEqual(len(accounts), 2, "the account must still be created")
        self.assertEqual(accounts[0]["body"]["holding_currency"], "EUR")
        self.assertTrue(any("HRK replaced with EUR" in n for n in accounts[0]["notes"]))
        self.assertEqual(len(plan["steps"]), EXPECTED_STEPS)

    def test_currency_account_in_a_dropped_currency_is_not_created(self):
        plan = plan_for(fx.dropped_currency_account_capture())
        self.assertEqual(len(steps_of(plan, "currency_account")), 1)
        self.assertTrue(any(x["kind"] == "currency_account" for x in plan["skipped"]))
        self.assertTrue(any("ZWL cannot be added" in w for w in plan["warnings"]))

    def test_a_dropped_account_orphans_its_routing_rule_rather_than_failing_mid_run(self):
        plan = plan_for(fx.dropped_currency_account_capture())
        # The catch-all named the EUR account as `fees`, so it cannot be created.
        self.assertEqual(len(steps_of(plan, "payment_routing_rule")), 1)
        self.assertTrue(any(x["kind"] == "payment_routing_rule"
                            for x in plan["skipped"]))
        # And the consequence is named, because CAT's error would be three steps removed
        # from the cause.
        self.assertTrue(any("no catch-all rule will be created" in w
                            for w in plan["warnings"]))

    def test_the_plan_is_still_structurally_valid_after_the_cascade(self):
        # "Do not halt" is only true if the plan still applies: every remaining step's
        # requires must still be satisfied by an earlier step.
        plan = plan_for(fx.dropped_currency_account_capture())
        provided = set()
        for s in plan["steps"]:
            for r in s["requires"]:
                self.assertIn(r, provided,
                              f"step {s['seq']} ({s['kind']}) requires {r}, orphaned "
                              f"by the dropped currency account")
            if s.get("provides"):
                provided.add(s["provides"])
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True)
        self.assertEqual(run["problems"], [])
        self.assertEqual(run["counts"]["failed"], 0)

    def test_payout_schedule_omits_the_dropped_account(self):
        plan = plan_for(fx.dropped_currency_account_capture())
        sch = one_step(plan, "payout_setting")["body"]["payout_schedule"]
        self.assertEqual(len(sch["currency_account_ids"]), 1)
        self.assertTrue(any("omitted from the payout schedule" in n
                            for n in one_step(plan, "payout_setting")["notes"]))

    def test_routing_rule_currency_scope_is_remediated(self):
        # A scoped rule narrows on currency codes — another place a bare code reaches
        # CAT, and one the profile-only rules never touched.
        plan = plan_for(fx.routing_rule_currency_capture())
        scoped = [s for s in steps_of(plan, "payment_routing_rule")
                  if s["body"].get("allow_any_processing_currency") is False]
        self.assertTrue(scoped)
        self.assertEqual(scoped[0]["body"]["processing_currencies"], ["GBP", "SLE"])
        dropped = [x for x in plan["flags"]
                   if x["code"] == "currency_not_available"
                   and x.get("field") == "processing_currencies"]
        self.assertEqual([x["currency"] for x in dropped], ["LBP"])

    def test_a_rule_scoped_only_to_unavailable_currencies_is_not_created(self):
        # Creating it with an empty scope would match no traffic — worse than not
        # creating it, because it looks present.
        plan = plan_for(fx.routing_rule_currency_capture())
        for s in steps_of(plan, "payment_routing_rule"):
            self.assertTrue(s["body"].get("allow_any_processing_currency")
                            or s["body"].get("processing_currencies"),
                            "a rule was created with an empty currency scope")
        f = [x for x in plan["flags"] if x["code"] == "routing_rule_scope_emptied"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["currencies"], ["ZWL", "LBP"])
        self.assertEqual(f[0]["action"], "not_created")

    def test_dropping_a_rule_still_leaves_an_applicable_plan(self):
        plan = plan_for(fx.routing_rule_currency_capture())
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True)
        self.assertEqual(run["problems"], [])
        self.assertEqual(run["counts"]["failed"], 0)



# ---------------------------------------------------------------- derivations

class TestDerivations(unittest.TestCase):
    """The four unreadable ids. An ambiguous join must REFUSE, not guess."""

    def test_profile_id_is_disambiguated_by_mcc_not_acquirer(self):
        cap = fx.reference_capture()
        ent = cap["entities"][0]
        profiles = ent["processing_profiles"]
        procs = ent["processing_channels"][0]["processors"]
        # visagb and visafood share acquirer AND scheme; only the MCC separates them.
        gb, food = procs[0], procs[2]
        self.assertEqual(gb["acquirer_id"], food["acquirer_id"])
        self.assertEqual(gb["scheme"], food["scheme"])
        gb_id, gb_conf = cc.resolve_profile_id(gb, profiles)
        food_id, food_conf = cc.resolve_profile_id(food, profiles)
        self.assertEqual(gb_conf, "exact")
        self.assertEqual(food_conf, "exact")
        self.assertNotEqual(gb_id, food_id)
        self.assertEqual(gb_id, profiles[0]["id"])
        self.assertEqual(food_id, profiles[2]["id"])

    def test_every_processor_binds_a_profile_in_the_reference_shape(self):
        plan = plan_for(fx.reference_capture())
        for s in steps_of(plan, "processor"):
            self.assertRegex(s["body"]["profile_id"], ANY_PLACEHOLDER)

    def test_a_direct_mode_processor_is_flagged_not_emitted(self):
        # CAT has exactly one gateway processor create route and it returns 503
        # gateway_manual_processor_creation_not_supported for a processor that binds no
        # profile. Emitting it halted a live run with 51 steps unattempted.
        plan = plan_for(fx.direct_mode_capture())
        self.assertEqual(len(steps_of(plan, "processor")), 3)
        for s in steps_of(plan, "processor"):
            self.assertIn("profile_id", s["body"])
        f = [x for x in plan["flags"]
             if x["code"] == "manual_processor_not_creatable"]
        self.assertEqual(len(f), 1)
        self.assertIn("direct-mode", f[0]["confidence"])
        self.assertEqual(f[0]["action"], "not_created")

    def test_an_unresolvable_profile_is_flagged_not_emitted(self):
        plan = plan_for(fx.unresolvable_profile_capture())
        self.assertEqual(len(steps_of(plan, "processor")), 3)
        f = [x for x in plan["flags"]
             if x["code"] == "manual_processor_not_creatable"]
        self.assertEqual(len(f), 1)
        # The two ways of having no profile are told apart for the report.
        self.assertIn("no-candidate", f[0]["confidence"])

    def test_a_skipped_processor_takes_its_sessions_link_with_it(self):
        plan = plan_for(fx.manual_processor_with_sessions_link_capture())
        self.assertEqual(len(steps_of(plan, "processor")), 3)
        self.assertEqual(len(steps_of(plan, "sessions_profile_processor")), 3)
        f = [x for x in plan["flags"] if x["code"] == "sessions_link_orphaned"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["scheme"], "amex")

    def test_the_plan_still_applies_after_a_processor_is_skipped(self):
        # "Flag but do not halt" is only true if what is left is runnable.
        plan = plan_for(fx.manual_processor_with_sessions_link_capture())
        provided = set()
        for s in plan["steps"]:
            for r in s["requires"]:
                self.assertIn(r, provided,
                              f"step {s['seq']} ({s['kind']}) requires {r}")
            if s.get("provides"):
                provided.add(s["provides"])
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True)
        self.assertEqual(run["problems"], [])
        self.assertEqual(run["counts"]["failed"], 0)

    def test_processor_currency_fields_are_both_remediated(self):
        # A processor carries currency codes under TWO field names; the live failure had
        # ZWL in processing_currencies, which the profile-only rules never touched.
        plan = plan_for(fx.processor_stale_currency_capture())
        proc = [s for s in steps_of(plan, "processor")
                if s["body"].get("scheme") == "visa"][0]
        self.assertEqual(proc["body"]["currencies"], ["GBP", "SLE"])
        self.assertEqual(proc["body"]["processing_currencies"], ["GBP", "EUR"])
        dropped = [x for x in plan["flags"]
                   if x["code"] == "currency_not_available"
                   and x["kind"] == "processor"]
        self.assertEqual([x["currency"] for x in dropped], ["ZWL"])
        self.assertEqual(dropped[0]["field"], "processing_currencies")

    def test_ambiguous_sessions_link_is_skipped_and_warned(self):
        # TODO #1's silent failure: two gateway processors share scheme+MCC, so the
        # authentication link cannot be derived. The clone still "succeeds" with the
        # wiring missing, and a warning is the only signal.
        plan = plan_for(fx.ambiguous_sessions_capture())
        self.assertEqual(len(steps_of(plan, "sessions_profile_processor")), 2)
        dropped = [x for x in plan["skipped"]
                   if x["kind"] == "sessions_profile_processor"]
        self.assertEqual(len(dropped), 1)
        self.assertIn("ambiguous-2-candidates", dropped[0]["reason"])
        self.assertTrue(any("link unresolved" in w for w in plan["warnings"]))
        # And it must not be silent: the warning surfaces as a validate_plan problem.
        self.assertTrue(any("link unresolved" in p for p in cc.validate_plan(plan)))

    def test_non_profile_sessions_processor_is_skipped_with_a_reason(self):
        # createType=existing links an existing PROFILE, so a manual processor cannot
        # be linked at all.
        plan = plan_for(fx.non_profile_sessions_capture())
        self.assertEqual(len(steps_of(plan, "sessions_profile_processor")), 3)
        dropped = [x for x in plan["skipped"]
                   if x["kind"] == "sessions_profile_processor"]
        self.assertEqual(len(dropped), 1)
        self.assertIn("processor_type=manual", dropped[0]["reason"])

    def test_missing_vault_service_warns_instead_of_emitting_the_sources(self):
        plan = plan_for(fx.no_vault_capture())
        self.assertEqual(steps_of(plan, "vault_lookup"), [])
        self.assertTrue(any("vault reference cannot be remapped" in w
                            for w in plan["warnings"]))
        ch = one_step(plan, "processing_channel")
        self.assertNotIn(fx.SOURCE_VAULT, json.dumps(ch["body"]))

    def test_sessions_link_join_ignores_acquirer(self):
        # The sessions record carries a processor_key (`cko-visa`) while the gateway
        # processor carries an acquirer_key (`cko_visa_gb`) — they never compare equal,
        # which is why the join is on scheme + MCC.
        cap = fx.reference_capture()
        ent = cap["entities"][0]
        gwch = ent["processing_channels"][0]
        sp = ent["sessions_channels"][0]["processors"][0]
        self.assertNotEqual(sp["acquirer_id"], gwch["processors"][0]["acquirer_id"])
        pr_id, pp_id, conf = cc.resolve_sessions_processor_link(
            sp, gwch, ent["processing_profiles"])
        self.assertEqual(pr_id, gwch["processors"][0]["id"])
        self.assertEqual(pp_id, ent["processing_profiles"][0]["id"])
        self.assertIn("scheme+mcc", conf)


# ---------------------------------------------------------------- multi-entity

class TestMultiEntity(unittest.TestCase):
    """Never exercised live (TODO #1). validate_plan should catch any cross-entity
    dependency violation; this is what asks it to."""

    @classmethod
    def setUpClass(cls):
        cls.cap = fx.multi_entity_capture()
        cls.plan = plan_for(cls.cap)

    def test_entity_scoped_steps_are_doubled(self):
        # client, risk, flow, compass settings + check, vault_lookup, and the three
        # destination-key steps (crypto key, secret key, public key)
        client_level = 9
        per_entity = EXPECTED_STEPS - client_level
        self.assertEqual(len(self.plan["steps"]), client_level + 2 * per_entity)

    def test_client_level_steps_are_not_duplicated(self):
        self.assertEqual(len(steps_of(self.plan, "client")), 1)
        self.assertEqual(len(steps_of(self.plan, "client_risk_settings")), 1)
        self.assertEqual(len(steps_of(self.plan, "client_flow_account")), 1)
        self.assertEqual(len(steps_of(self.plan, "client_compass_settings")), 1)
        self.assertEqual(len(steps_of(self.plan, "client_compass_check")), 1)
        self.assertEqual(len(steps_of(self.plan, "vault_lookup")), 1)

    def test_validate_plan_reports_only_the_known_prerequisites(self):
        for p in cc.validate_plan(self.plan):
            self.assertTrue(p.startswith("warning: ") and MANUAL_STEP_MESSAGE.match(p[9:]), p)

    def test_network_tokens_manual_flag_is_raised_once(self):
        self.assertEqual(steps_of(self.plan, "client_network_tokens"), [])
        f = [x for x in self.plan["flags"] if x["code"] == "network_tokens_manual"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["default_entity_id"], self.cap["entities"][0]["id"])

    def test_each_entitys_steps_are_contiguous(self):
        blocks = {}
        for s in self.plan["steps"]:
            if s.get("entity"):
                blocks.setdefault(s["entity"], []).append(s["seq"])
        self.assertEqual(len(blocks), 2)
        for eid, seqs in blocks.items():
            self.assertEqual(seqs, list(range(seqs[0], seqs[-1] + 1)),
                             f"entity {eid}'s steps are interleaved with another's")

    def test_each_entitys_steps_are_internally_ordered(self):
        blocks = {}
        for s in self.plan["steps"]:
            if s.get("entity"):
                blocks.setdefault(s["entity"], []).append(KIND_ORDER.index(s["kind"]))
        for eid, idx in blocks.items():
            self.assertEqual(idx, sorted(idx), f"entity {eid} is out of order")

    def test_no_step_requires_another_entitys_object(self):
        owner = {}
        for s in self.plan["steps"]:
            if s.get("provides"):
                owner[s["provides"]] = s.get("entity")
        for s in self.plan["steps"]:
            if not s.get("entity"):
                continue
            for r in s["requires"]:
                o = owner.get(r)
                self.assertIn(o, (None, s["entity"]),
                              f"step {s['seq']} ({s['kind']}) in {s['entity']} "
                              f"requires {r}, which belongs to {o}")

    def test_dry_run_completes(self):
        with NoSocket():
            run = capp.apply_plan(self.plan, dry_run=True)
        self.assertEqual(run["problems"], [])
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertEqual(run["counts"]["created"], len(self.plan["steps"]))


# ---------------------------------------------------------------- safety gates

class TestSafetyGates(unittest.TestCase):
    """A live run needs three independent things: dry_run explicitly false, the right
    confirm string, and a token. These must never collapse into fewer checks."""

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())

    def test_apply_plan_defaults_to_dry_run(self):
        with NoSocket():
            run = capp.apply_plan(self.plan)
        self.assertTrue(run["dry_run"])

    def test_apply_plan_refuses_live_without_base_or_token(self):
        for kw in ({}, {"base": "https://x"}, {"token": "t"}):
            with self.assertRaises(ValueError):
                capp.apply_plan(self.plan, dry_run=False, **kw)

    def test_run_cleanup_defaults_to_dry_run(self):
        with NoSocket():
            run = ccl.run_cleanup([{"seq": 1, "kind": "currency_account",
                                    "new_id": fx._id("ca", "new"), "path": "/x"}])
        self.assertTrue(run["dry_run"])

    def test_run_cleanup_refuses_live_without_base_or_token(self):
        with self.assertRaises(ValueError):
            ccl.run_cleanup([], dry_run=False)

    def test_handler_treats_a_missing_dry_run_flag_as_a_dry_run(self):
        with NoSocket():
            out = server.clone_apply_handler({"plan": self.plan})
        self.assertTrue(out["run"]["dry_run"])

    def test_handler_treats_the_string_false_as_a_dry_run(self):
        # `is False` is an identity check on purpose — "false" from a form field must
        # not be enough to write.
        with NoSocket():
            out = server.clone_apply_handler({"plan": self.plan, "dry_run": "false",
                                              "confirm": "CLONE", "cat_token": "t"})
        self.assertTrue(out["run"]["dry_run"])

    def test_live_apply_requires_the_confirm_string(self):
        with NoSocket():
            out = server.clone_apply_handler({"plan": self.plan, "dry_run": False,
                                              "cat_token": "t", "confirm": "clone"})
        self.assertIn("error", out)
        self.assertIn("CLONE", out["error"])

    def test_live_apply_requires_a_token(self):
        with NoSocket():
            out = server.clone_apply_handler({"plan": self.plan, "dry_run": False,
                                              "confirm": "CLONE", "cat_token": "  "})
        self.assertIn("error", out)
        self.assertIn("token", out["error"])

    def test_live_cleanup_requires_the_confirm_string(self):
        objs = [{"seq": 1, "kind": "currency_account", "new_id": fx._id("ca", "new"),
                 "path": "/x"}]
        with NoSocket():
            out = server.clone_cleanup_handler({"created_objects": objs,
                                                "dry_run": False, "cat_token": "t",
                                                "confirm": "CLONE"})
        self.assertIn("error", out)
        self.assertIn("CLEANUP", out["error"])

    def test_live_cleanup_requires_a_token(self):
        objs = [{"seq": 1, "kind": "currency_account", "new_id": fx._id("ca", "new"),
                 "path": "/x"}]
        with NoSocket():
            out = server.clone_cleanup_handler({"created_objects": objs,
                                                "dry_run": False, "confirm": "CLEANUP",
                                                "cat_token": ""})
        self.assertIn("error", out)
        self.assertIn("token", out["error"])

    def test_capture_handler_persists_the_raw_capture(self):
        # Every capture is written to clone-runs/ before anything is derived from it, so
        # "what did CAT actually return?" never again depends on someone pasting it.
        import tempfile, pathlib as _pl
        cap = fx.reference_capture()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server.cc, "capture", return_value=cap), \
             mock.patch.object(server, "RUNS_DIR", _pl.Path(tmp)), NoSocket():
            out = server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT,
                                                "cat_token": "t"})
            files = sorted(_pl.Path(tmp).glob("capture-*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn(fx.SOURCE_CLIENT, files[0].name)
            self.assertEqual(out["capture_path"], str(files[0]))
            saved = json.loads(files[0].read_text())
        self.assertEqual(saved["client_id"], cap["client_id"])
        self.assertEqual(saved["network_tokens"], cap["network_tokens"])
        self.assertEqual(len(saved["entities"]), len(cap["entities"]))
        self.assertIn("plan", out)

    def test_a_plan_without_steps_is_rejected(self):
        for bad in (None, {}, {"steps": []}, "nope"):
            self.assertIn("error", server.clone_apply_handler({"plan": bad}))


class TestEntityScope(unittest.TestCase):
    """The entity picker is tick boxes: any subset of a client's entities can be cloned.
    A ticked id that CAT does not return must refuse the capture, never shrink it."""

    ENTS = [{"id": fx._id("ent", f"scope{i}"), "name": f"E{i}"} for i in range(3)]

    def test_no_scope_keeps_every_entity(self):
        kept, missing = cc.scope_entities(self.ENTS)
        self.assertEqual(kept, self.ENTS); self.assertEqual(missing, [])
        kept, missing = cc.scope_entities(self.ENTS, only_entities=[])
        self.assertEqual(kept, self.ENTS); self.assertEqual(missing, [])

    def test_a_list_keeps_exactly_the_ticked_entities_in_cat_order(self):
        ids = [self.ENTS[2]["id"], self.ENTS[0]["id"]]      # ticked out of order
        kept, missing = cc.scope_entities(self.ENTS, only_entities=ids)
        self.assertEqual([e["id"] for e in kept], [self.ENTS[0]["id"], self.ENTS[2]["id"]])
        self.assertEqual(missing, [])

    def test_the_single_id_form_still_works(self):
        kept, missing = cc.scope_entities(self.ENTS, only_entity=self.ENTS[1]["id"])
        self.assertEqual([e["id"] for e in kept], [self.ENTS[1]["id"]])
        self.assertEqual(missing, [])

    def test_a_ticked_id_cat_did_not_return_is_reported_not_dropped(self):
        ghost = fx._id("ent", "ghost")
        kept, missing = cc.scope_entities(self.ENTS, only_entities=[self.ENTS[0]["id"], ghost])
        self.assertEqual([e["id"] for e in kept], [self.ENTS[0]["id"]])
        self.assertEqual(missing, [ghost])

    def test_handler_passes_the_ticked_ids_normalised(self):
        cap = fx.reference_capture()
        ids = [" " + self.ENTS[1]["id"], self.ENTS[0]["id"], self.ENTS[0]["id"], ""]
        with mock.patch.object(server.cc, "capture", return_value=cap) as c, \
             mock.patch.object(server, "RUNS_DIR", _tmp_runs_dir()), NoSocket():
            out = server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                                "only_entities": ids})
        self.assertIn("plan", out)
        self.assertEqual(c.call_args.kwargs["only_entities"],
                         sorted([self.ENTS[0]["id"], self.ENTS[1]["id"]]))
        self.assertIsNone(c.call_args.kwargs["only_entity"])

    def test_handler_refuses_when_a_ticked_entity_is_missing(self):
        cap = fx.reference_capture()
        cap["scope_missing"] = [fx._id("ent", "ghost")]
        with mock.patch.object(server.cc, "capture", return_value=cap), \
             mock.patch.object(server.cc, "build_plan") as bp, \
             mock.patch.object(server, "RUNS_DIR", _tmp_runs_dir()), NoSocket():
            out = server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                                "only_entities": [fx._id("ent", "ghost")]})
        self.assertIn("error", out)
        self.assertIn("ghost", out["error"])
        self.assertNotIn("plan", out)
        bp.assert_not_called()

    def test_handler_rejects_a_non_list_scope(self):
        with mock.patch.object(server.cc, "capture") as c, NoSocket():
            out = server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                                "only_entities": "ent_x"})
        self.assertIn("error", out); c.assert_not_called()


def _tmp_runs_dir():
    import tempfile, pathlib as _pl
    return _pl.Path(tempfile.mkdtemp())


class TestWebhooks(unittest.TestCase):
    """Webhooks are Workflows in the Checkout sandbox API: read with the SOURCE client's
    secret key at capture, created with the DESTINATION's at apply, optional throughout."""

    SRC_SK, DEST_SK = "sk_sbox_" + "s" * 32, "sk_sbox_" + "d" * 32

    class StubReader:
        """Answers GET /workflows and GET /workflows/{id} like the sandbox API."""
        def __init__(self, workflows, list_code=200):
            self.wfs = {w["id"]: w for w in workflows}
            self.list_code = list_code
            self.calls, self.errors, self.paths = 0, [], []
        def get(self, path):
            self.calls += 1; self.paths.append(path)
            if path == "/workflows":
                if self.list_code != 200:
                    self.errors.append({"path": path, "code": self.list_code})
                    return {}, self.list_code
                return {"data": [{"id": w["id"], "name": w["name"], "active": w["active"],
                                  "_links": w["_links"]} for w in self.wfs.values()]}, 200
            wid = path.rsplit("/", 1)[-1]
            return (self.wfs[wid], 200) if wid in self.wfs else ({}, 404)

    @classmethod
    def setUpClass(cls):
        cls.cap = fx.webhooks_capture()
        cls.plan = plan_for(cls.cap)
        cls.eid = cls.cap["entities"][0]["id"]
        cls.pcid = cls.cap["entities"][0]["processing_channels"][0]["id"]

    # -- capture side ------------------------------------------------------------
    def test_read_workflows_fetches_each_detail_and_labels_the_source(self):
        wfs = self.cap["workflows"]
        r = self.StubReader(wfs)
        out, src = cc.read_workflows(r)
        self.assertEqual(src, "sandbox-api")
        self.assertEqual([w["id"] for w in out], [w["id"] for w in wfs])
        self.assertTrue(all("conditions" in w and "actions" in w for w in out))
        self.assertEqual(r.paths, ["/workflows"] + [f"/workflows/{w['id']}" for w in wfs])

    def test_a_refused_list_is_unavailable_not_empty(self):
        out, src = cc.read_workflows(self.StubReader([], list_code=401))
        self.assertIsNone(out)
        self.assertEqual(src, "unavailable")

    def test_capture_reads_workflows_only_when_a_source_key_is_given(self):
        # No key: nothing is read and the capture says so. With a key: a Reader is built
        # against the sandbox API base with THAT key and the result lands in the capture.
        made = []
        class FakeReader:
            def __init__(self, base, token):
                made.append((base, token)); self.calls = 0; self.errors = []
            def get(self, path):
                self.calls += 1
                if path == "/workflows":
                    return {"data": []}, 200
                return ({}, 200) if "clients" in path or "entities" in path else ({}, 404)
            def hal(self, path, key):
                return [], 200
        with mock.patch.object(cc, "Reader", FakeReader):
            cap = cc.capture("https://cat", "cat-tok", fx.SOURCE_CLIENT)
            self.assertEqual((cap["workflows"], cap["workflows_source"]), (None, "no_key"))
            self.assertFalse(any(t == self.SRC_SK for _, t in made))
            made.clear()
            cap = cc.capture("https://cat", "cat-tok", fx.SOURCE_CLIENT,
                             sandbox_secret_key=self.SRC_SK)
        self.assertEqual((cap["workflows"], cap["workflows_source"]), ([], "sandbox-api"))
        self.assertIn((cc.SANDBOX_API_BASE, self.SRC_SK), made)

    def test_capture_handler_passes_the_source_key_not_the_destination_key(self):
        with mock.patch.object(server.cc, "capture", return_value=fx.reference_capture()) as c, \
             mock.patch.object(server, "RUNS_DIR", _tmp_runs_dir()), NoSocket():
            out = server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                                "sandbox_sk": self.SRC_SK,
                                                "dest_sandbox_sk": self.DEST_SK})
        self.assertIn("plan", out)
        self.assertEqual(c.call_args.kwargs["sandbox_secret_key"], self.SRC_SK)

    # -- plan side ---------------------------------------------------------------
    def test_create_body_strips_every_server_id_and_remaps_scope_ids(self):
        wf = self.cap["workflows"][0]
        body, req, notes, emptied = cc.workflow_create_body(wf, {self.eid}, {self.pcid})
        blob = json.dumps(body)
        for prefix in ("wf_", "wfc_", "wfa_", "_links", "created_at", "updated_at"):
            self.assertNotIn(prefix, blob, prefix)
        # allowlisted shape at every level — exactly what add-workflow-request declares
        self.assertEqual(set(body), {"name", "active", "conditions", "actions"})
        for c in body["conditions"]:
            self.assertTrue(set(c) <= cc.WORKFLOW_CONDITION_FIELDS, c)
        for a in body["actions"]:
            self.assertTrue(set(a) <= cc.WORKFLOW_ACTION_FIELDS, a)
        self.assertEqual(body["name"], "Payments webhook")
        ents = next(c for c in body["conditions"] if c["type"] == "entity")
        pcs = next(c for c in body["conditions"] if c["type"] == "processing_channel")
        self.assertEqual(ents["entities"], [cc.ph(self.eid)])      # out-of-scope id gone
        self.assertEqual(pcs["processing_channels"], [cc.ph(self.pcid)])
        self.assertEqual(sorted(req), sorted([self.eid, self.pcid]))
        self.assertTrue(any("not in this capture's scope" in n for n in notes))
        self.assertIsNone(emptied)
        # event condition, url, headers and signature are the configuration — carried
        ev = next(c for c in body["conditions"] if c["type"] == "event")
        self.assertEqual(ev["events"]["gateway"], ["payment_approved", "payment_declined"])
        self.assertEqual(body["actions"][0]["url"], "https://merchant.example/hooks")
        self.assertIn("signature", body["actions"][0]); self.assertIn("headers", body["actions"][0])

    def test_a_workflow_scoped_only_outside_the_capture_is_skipped_and_flagged(self):
        wf = self.cap["workflows"][1]
        body, req, notes, emptied = cc.workflow_create_body(wf, {self.eid}, {self.pcid})
        self.assertEqual(emptied, "entity")
        hooks = steps_of(self.plan, "webhook_workflow")
        self.assertEqual([s["label"] for s in hooks], ["Payments webhook"])
        self.assertTrue(any(x["kind"] == "webhook_workflow" and "Other entity webhook" in x["reason"]
                            for x in self.plan["skipped"]))
        f = [x for x in self.plan["flags"] if x["code"] == "webhook_scope_emptied"]
        self.assertEqual(len(f), 1); self.assertEqual(f[0]["action"], "not_created")

    def test_webhook_steps_are_optional_sandbox_secret_posts_placed_last(self):
        hooks = steps_of(self.plan, "webhook_workflow")
        checks = steps_of(self.plan, "webhook_check")
        self.assertEqual(len(hooks), 1); self.assertEqual(len(checks), 1)
        h, c = hooks[0], checks[0]
        self.assertEqual((h["method"], h["path"], h["auth"], h["optional"]),
                         ("POST", "/workflows", "sandbox_secret", True))
        self.assertEqual((c["method"], c["path"], c["auth"], c["optional"]),
                         ("GET", "/workflows", "sandbox_secret", True))
        self.assertEqual(c["verify"], {"compare": "workflows", "expected": ["Payments webhook"]})
        self.assertIsNone(h["provides"])
        self.assertEqual(sorted(h["requires"]), sorted([self.eid, self.pcid]))
        # after every CAT step: the ids it references are minted earlier in the run
        self.assertEqual([s["kind"] for s in self.plan["steps"][-2:]],
                         ["webhook_workflow", "webhook_check"])
        # every step it requires is provided earlier — validate_plan raises nothing new
        problems = [p for p in cc.validate_plan(self.plan) if not p.startswith("warning:")]
        self.assertEqual(problems, [])
        # every CAT step is untouched by auth
        self.assertTrue(all(s["auth"] is None for s in self.plan["steps"]
                            if not s["kind"].startswith("webhook")))

    def test_the_receiver_url_is_flagged_for_confirmation(self):
        f = [x for x in self.plan["flags"] if x["code"] == "webhook_url_carried"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["url"], "https://merchant.example/hooks")
        self.assertEqual(f[0]["action"], "carried")

    def test_no_workflows_means_no_webhook_steps_or_flags(self):
        plan = plan_for(fx.reference_capture())
        self.assertEqual(steps_of(plan, "webhook_workflow") + steps_of(plan, "webhook_check"), [])
        self.assertFalse(any(x["code"].startswith("webhook") for x in plan["flags"]))

    def test_unread_and_unavailable_workflows_are_flagged_not_silent(self):
        plan = plan_for(fx.webhooks_not_read_capture())
        f = [x for x in plan["flags"] if x["code"] == "webhooks_not_read"]
        self.assertEqual(len(f), 1); self.assertIn("Source Sandbox Secret Key", f[0]["message"])
        self.assertEqual(steps_of(plan, "webhook_workflow"), [])
        plan = plan_for(fx.webhooks_unavailable_capture())
        f = [x for x in plan["flags"] if x["code"] == "webhooks_unavailable"]
        self.assertEqual(len(f), 1); self.assertEqual(f[0]["errors"][0]["code"], 401)

    # -- apply side --------------------------------------------------------------
    def test_without_the_destination_key_webhooks_block_and_the_clone_still_completes(self):
        with NoSocket():
            run = capp.apply_plan(self.plan)          # dry run, no keys at all
        hooks = [e for e in run["journal"] if e["kind"].startswith("webhook")]
        self.assertEqual(len(hooks), 2)
        for e in hooks:
            self.assertEqual(e["status"], "blocked")
            self.assertIn("Destination Sandbox Secret Key", e["error"])
            self.assertEqual(e["flags"][0]["code"], "optional_step_blocked")
        cat_steps = len(self.plan["steps"]) - 2
        self.assertEqual(run["counts"], {"steps": cat_steps + 2, "created": cat_steps,
                                         "failed": 2, "not_attempted": 0})
        self.assertEqual([f["code"] for f in run["flags"]], ["optional_step_blocked"] * 2)

    def test_with_the_destination_key_the_dry_run_resolves_every_placeholder(self):
        with NoSocket():
            run = capp.apply_plan(self.plan, sandbox_keys={"sandbox_secret": self.DEST_SK})
        self.assertEqual(run["counts"]["failed"], 0)
        hook = next(e for e in run["journal"] if e["kind"] == "webhook_workflow")
        blob = json.dumps(hook["body"])
        self.assertNotIn("{{", blob)
        self.assertNotIn(self.eid, blob); self.assertNotIn(self.pcid, blob)
        self.assertEqual(hook["auth"], "sandbox_secret")

    def test_live_webhook_steps_go_to_the_sandbox_api_with_the_destination_key(self):
        sent = []
        def fake_send(base, bearer, method, path, body=None, **kw):
            sent.append((base, bearer, method, path))
            if path == "/workflows" and method == "GET":
                return {"data": [{"id": "wf_x", "name": "Payments webhook", "active": True}]}, 200, None
            return {"id": "new_" + str(len(sent))}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(self.plan, base="https://cat", token="cat-token",
                                  dry_run=False, pace_seconds=0,
                                  sandbox_keys={"sandbox_secret": self.DEST_SK,
                                                "source_sandbox_secret": self.SRC_SK})
        hooks = [s for s in sent if s[3] == "/workflows"]
        self.assertEqual(len(hooks), 2)
        for base, bearer, _, _ in hooks:
            self.assertEqual(base, capp.SANDBOX_API_BASE)
            self.assertEqual(bearer, self.DEST_SK)
        self.assertTrue(all(s[1] == "cat-token" and s[0] == "https://cat"
                            for s in sent if s[3] != "/workflows"))
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertFalse(any(f["code"] == "webhook_missing_on_clone" for f in run["flags"]))
        self.assertNotIn(self.SRC_SK, json.dumps(run)); self.assertNotIn(self.DEST_SK, json.dumps(run))

    def test_read_back_does_not_report_a_webhook_whose_own_create_failed(self):
        # First live run: four creates 422'd, then the read-back flagged all four as
        # "missing" too. The create failure is already a flag; the check must only expect
        # what this run created.
        def fake_send(base, bearer, method, path, body=None, **kw):
            if path == "/workflows" and method == "POST":
                return {}, 422, '{"error_codes":["condition_entity_entity_id_invalid"]}'
            if path == "/workflows":
                return {"data": []}, 200, None
            return {"id": "x_" + str(len(path))}, 201, None
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(self.plan, base="https://cat", token="cat-token",
                                  dry_run=False, pace_seconds=0,
                                  sandbox_keys={"sandbox_secret": self.DEST_SK})
        codes = [f["code"] for f in run["flags"]]
        self.assertIn("optional_step_failed", codes)
        self.assertNotIn("webhook_missing_on_clone", codes)
        chk = next(e for e in run["journal"] if e["kind"] == "webhook_check")
        self.assertEqual(chk["verify_expected"], [])
        self.assertEqual(chk["verify"]["expected"], 0)

    def test_read_back_flags_a_workflow_the_clone_does_not_list(self):
        flags, summary = capp.verify_workflows(["A", "B"], {"data": [{"name": "A"}, {"name": "Z"}]})
        self.assertEqual([f["code"] for f in flags], ["webhook_missing_on_clone"])
        self.assertEqual(flags[0]["name"], "B")
        self.assertEqual(summary, {"expected": 2, "found": 1, "missing": ["B"], "on_clone": 2})
        self.assertEqual(capp.verify_workflows(["A"], {"data": [{"name": "A"}]})[0], [])

    def test_cleanup_knows_a_workflow_cannot_be_removed_via_cat(self):
        self.assertEqual(ccl.REMOVAL["webhook_workflow"]["mode"], "none")
        self.assertIn("destination secret key", ccl.REMOVAL["webhook_workflow"]["why"])


class TestDestinationApiKeys(unittest.TestCase):
    """The new client's API keys are minted inside the run: register the run's RSA public
    key, create a secret and a public key encrypted with it, decrypt them here, feed the
    secret to the webhook steps, show both once, journal neither."""

    @classmethod
    def setUpClass(cls):
        import clone_keys
        cls.ck = clone_keys
        cls.plan = plan_for(fx.webhooks_capture())     # keys + a webhook that needs them
        cls.SECRET, cls.PUBLIC = "sk_sbox_" + "m" * 26, "pk_sbox_" + "n" * 26

    # -- the RSA module ------------------------------------------------------------
    def test_keypair_pem_is_pkcs1_and_roundtrips(self):
        kp = _TEST_KEYPAIR
        pem = kp.public_pem
        self.assertTrue(pem.startswith("-----BEGIN RSA PUBLIC KEY-----\n"))
        self.assertTrue(pem.rstrip().endswith("-----END RSA PUBLIC KEY-----"))
        self.assertEqual(self.ck.parse_public_pem(pem), (kp.n, kp.e))
        for ct, pad in ((self.ck.encrypt_pkcs1_v15(pem, self.SECRET.encode()), "PKCS1-v1.5"),
                        (self.ck.encrypt_oaep(pem, self.PUBLIC.encode()), "OAEP-SHA256")):
            value, padding = kp.decrypt(ct)
            self.assertEqual(padding, pad)
            self.assertIn(value, (self.SECRET, self.PUBLIC))
        with self.assertRaises(ValueError):
            kp.decrypt(self.ck.encrypt_pkcs1_v15(pem, b"definitely not an api key"))

    def test_scope_catalogue_splits_by_side(self):
        cfg = {"scopes": [{"value": "gateway", "label": "gateway", "side_label": "secret"},
                          {"value": "vault:tokenization", "label": "vault:tokenization",
                           "side_label": "public"},
                          {"value": "notifier:workflows", "label": "notifier:workflows",
                           "side_label": "secret"}]}
        self.assertEqual(cc.api_key_scopes_from(cfg),
                         {"secret": ["gateway", "notifier:workflows"],
                          "public": ["vault:tokenization"]})
        self.assertIsNone(cc.api_key_scopes_from({}))
        self.assertIsNone(cc.api_key_scopes_from({"scopes": [{"value": "x", "side_label": "secret"}]}))

    # -- the plan ------------------------------------------------------------------
    def test_three_optional_key_steps_follow_the_entities_and_precede_the_webhooks(self):
        kinds = [s["kind"] for s in self.plan["steps"]]
        i_pk, i_sk, i_pub = (kinds.index(k) for k in
                             ("client_public_crypto_key", "client_api_secret_key", "client_api_public_key"))
        self.assertTrue(i_pk < i_sk < i_pub < kinds.index("webhook_workflow"))
        self.assertTrue(max(i for i, k in enumerate(kinds) if k == "entity") < i_pk)
        pk, sk, pub = (steps_of(self.plan, k)[0] for k in
                       ("client_public_crypto_key", "client_api_secret_key", "client_api_public_key"))
        for s in (pk, sk, pub):
            self.assertTrue(s["optional"]); self.assertIsNone(s["auth"]); self.assertEqual(s["method"], "POST")
        self.assertEqual(pk["body"], {"name": pk["body"]["name"], "type": "RSA",
                                      "key": cc.PUBLIC_KEY_PEM_MARKER})
        self.assertRegex(pk["body"]["name"], r"^PUB KEY CAT SETUP \d{5}$")
        self.assertEqual(pk["provides"], cc.PUBLIC_CRYPTO_KEY_PROVIDES)
        self.assertTrue(pk["path"].endswith("/public-keys"))
        for s, side, desc in ((sk, "secret", "CAT SETUP SECRET"), (pub, "public", "CAT SET UP PUB")):
            self.assertTrue(s["path"].endswith("/standalone-reference-tokens"))
            self.assertEqual(s["body"]["description"], desc)
            self.assertEqual(s["body"]["scopes"], fx.reference_capture()["api_key_scopes"][side])
            self.assertEqual(s["body"]["public_key_id"], cc.ph(cc.PUBLIC_CRYPTO_KEY_PROVIDES))
            self.assertEqual((s["body"]["allow_any_processing_channel"], s["body"]["processing_channel_ids"]), (True, []))
        self.assertEqual(sk["body"]["entity_id"], cc.ph(fx.reference_capture()["entities"][0]["id"]))
        self.assertEqual(pub["body"]["entity_id"], "")
        # the marker is not a placeholder: validate_plan and unresolved() must ignore it
        self.assertEqual([p for p in cc.validate_plan(self.plan) if not p.startswith("warning:")], [])

    def test_no_scope_catalogue_means_no_key_steps_and_a_flag(self):
        plan = plan_for(fx.no_api_key_scopes_capture())
        self.assertEqual([s for s in plan["steps"] if s["kind"].startswith("client_api") or s["kind"] == "client_public_crypto_key"], [])
        f = [x for x in plan["flags"] if x["code"] == "api_keys_not_planned"]
        self.assertEqual(len(f), 1); self.assertIn("DESTINATION API KEYS NOT CREATED", f[0]["message"])

    # -- apply ----------------------------------------------------------------------
    def test_dry_run_substitutes_a_stand_in_pem_and_resolves_everything(self):
        with NoSocket():
            run = capp.apply_plan(self.plan, sandbox_keys={"sandbox_secret": "sk_sbox_" + "z" * 26})
        pk = next(e for e in run["journal"] if e["kind"] == "client_public_crypto_key")
        self.assertEqual(pk["body"]["key"], capp.DRY_RUN_PEM)
        self.assertNotIn(cc.PUBLIC_KEY_PEM_MARKER, json.dumps(run))
        self.assertNotIn(cc.PUBLIC_KEY_PEM_MARKER, json.dumps(run["id_map"]))
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertEqual(run["destination_keys"], {})

    def _live(self, plan=None, sandbox_keys=None, secret_ok=True):
        """Play CAT: capture the PEM the run registers, hand back secrets encrypted with it."""
        import tempfile, pathlib as _pl
        state = {"pem": None, "sent": []}
        def fake_send(base, bearer, method, path, body=None, **kw):
            state["sent"].append((base, bearer, method, path, body))
            if path.endswith("/public-keys"):
                state["pem"] = body["key"]
                return {"id": "3i3jgthodwk6ywjvlhxywynimm", "name": body["name"], "type": "RSA"}, 201, None
            if path.endswith("/standalone-reference-tokens"):
                if not secret_ok:
                    return {"id": "FD47"}, 201, None
                value = self.SECRET if body["description"] == "CAT SETUP SECRET" else self.PUBLIC
                return {"id": "FD4746005185C0518EE4E7A4FD1CE383",
                        "temporary_secret": self.ck.encrypt_pkcs1_v15(state["pem"], value.encode())}, 201, None
            if path == "/workflows" and method == "GET":
                return {"data": [{"id": "wf_x", "name": "Payments webhook"}]}, 200, None
            if method == "GET":
                return {"id": fx._id("vact", "clonevault")}, 200, None
            return {"id": fx._id("new", path.rsplit("/", 1)[-1][:8])}, 201, None
        tmp = _pl.Path(tempfile.mkdtemp()) / "j.jsonl"
        with mock.patch.object(capp, "_send", side_effect=fake_send), \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(plan or self.plan, base="https://cat", token="cat-token",
                                  dry_run=False, pace_seconds=0, journal_path=str(tmp),
                                  sandbox_keys=sandbox_keys)
        return run, state, tmp.read_text()

    def test_live_run_mints_decrypts_and_uses_the_secret_for_the_webhooks(self):
        run, state, journal_text = self._live()
        self.assertEqual(run["counts"]["failed"], 0, run["problems"])
        # a real PEM was registered, not the marker or the stand-in
        self.assertTrue(state["pem"].startswith("-----BEGIN RSA PUBLIC KEY-----"))
        self.assertNotIn(state["pem"], (cc.PUBLIC_KEY_PEM_MARKER, capp.DRY_RUN_PEM))
        # both keys decrypted and returned once
        dk = run["destination_keys"]
        self.assertEqual((dk["sandbox_secret"]["value"], dk["sandbox_public"]["value"]), (self.SECRET, self.PUBLIC))
        self.assertEqual(dk["sandbox_secret"]["description"], "CAT SETUP SECRET")
        self.assertEqual(dk["sandbox_secret"]["padding"], "PKCS1-v1.5")
        # the webhook steps were sent with the MINTED secret, with no key pasted
        hooks = [s for s in state["sent"] if s[3] == "/workflows"]
        self.assertEqual(len(hooks), 2)
        self.assertTrue(all(b == self.SECRET and base == capp.SANDBOX_API_BASE for base, b, _, _, _ in hooks))
        # the journal entry says which role and prefix, and that is all
        e = next(x for x in run["journal"] if x["kind"] == "client_api_secret_key")
        self.assertEqual(e["decrypted"]["role"], "sandbox_secret")
        self.assertEqual(e["decrypted"]["prefix"], self.SECRET[:8])
        self.assertIn("used_for", e["decrypted"])
        self.assertIn('"temporary_secret": "REDACTED"', e["response_excerpt"])

    def test_plaintext_and_ciphertext_never_reach_the_journal(self):
        run, state, journal_text = self._live()
        journal_blob = json.dumps(run["journal"])
        for secret in (self.SECRET, self.PUBLIC):
            self.assertNotIn(secret, journal_blob)
            self.assertNotIn(secret, journal_text)
        for ct in (s[4] for s in state["sent"] if s[4] and "temporary_secret" in json.dumps(s[4])):
            self.fail("a ciphertext was sent in a request body")   # sanity: never happens
        self.assertNotIn("temporary_secret\": \"" + "ey", journal_text)   # no ciphertext leak
        # the only exit is the run document's destination_keys
        self.assertIn(self.SECRET, json.dumps(run["destination_keys"]))

    def test_a_pasted_destination_key_wins_over_the_minted_one(self):
        pasted = "sk_sbox_" + "p" * 26
        run, state, _ = self._live(sandbox_keys={"sandbox_secret": pasted})
        hooks = [s for s in state["sent"] if s[3] == "/workflows"]
        self.assertTrue(all(b == pasted for _, b, _, _, _ in hooks))
        self.assertEqual(run["destination_keys"]["sandbox_secret"]["value"], self.SECRET)
        e = next(x for x in run["journal"] if x["kind"] == "client_api_secret_key")
        self.assertNotIn("used_for", e["decrypted"])

    def test_an_unrecoverable_secret_is_flagged_and_the_webhooks_block(self):
        run, state, _ = self._live(secret_ok=False)
        codes = [f["code"] for f in run["flags"]]
        self.assertEqual(codes.count("api_key_undecryptable"), 2)
        self.assertEqual(codes.count("optional_step_blocked"), 2)   # webhook_workflow + check
        self.assertEqual([s for s in state["sent"] if s[3] == "/workflows"], [])
        self.assertEqual(run["destination_keys"], {})
        self.assertEqual(run["counts"]["failed"], 2)                 # only the two webhook steps


class TestLiveProgress(unittest.TestCase):
    """The Apply page shows where a run is up to. apply_plan tells a listener about each
    step as it is journalled; the server keeps a slim copy per run_id for the page to poll."""

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())

    def test_on_step_fires_once_per_step_in_journal_order(self):
        seen = []
        with NoSocket():
            run = capp.apply_plan(self.plan, on_step=lambda e, n: seen.append((e["seq"], n)))
        self.assertEqual([s for s, _ in seen], [e["seq"] for e in run["journal"]])
        self.assertEqual({n for _, n in seen}, {len(self.plan["steps"])})

    def test_a_failing_listener_cannot_affect_the_run(self):
        def boom(e, n):
            raise RuntimeError("listener died")
        with NoSocket():
            run = capp.apply_plan(self.plan, on_step=boom)
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertEqual(run["counts"]["created"], len(self.plan["steps"]))

    def test_progress_store_keeps_slim_entries_and_marks_done(self):
        rid = "run-test-1"
        server.progress_start(rid, 3)
        server.progress_step(rid, {"seq": 1, "kind": "client", "method": "POST", "path": "/clients",
                                   "status": 201, "created_id": "cli_x", "body": {"secret": "no"}})
        out = server.clone_progress_handler({"run_id": rid})
        self.assertEqual((out["total"], out["done"], len(out["entries"])), (3, False, 1))
        self.assertNotIn("body", out["entries"][0])
        self.assertEqual(out["entries"][0]["created_id"], "cli_x")
        server.progress_finish(rid)
        self.assertTrue(server.clone_progress_handler({"run_id": rid})["done"])

    def test_unknown_run_id_is_an_error_not_an_empty_run(self):
        self.assertIn("error", server.clone_progress_handler({"run_id": "nope"}))
        self.assertIn("error", server.clone_progress_handler({}))

    def test_live_apply_handler_wires_progress_under_the_pages_run_id(self):
        # The page picks run_id and polls it while the request is in flight; the handler
        # must register it BEFORE apply_plan starts and mark it done after — even on error.
        def fake_apply(plan, **kw):
            kw["on_step"]({"seq": 1, "kind": "client", "method": "POST", "path": "/clients",
                           "status": 201, "body": {"x": 1}}, len(plan["steps"]))
            # mid-run: the page's poll must already see the step
            mid = server.clone_progress_handler({"run_id": "run-page-7"})
            self.assertEqual((mid["done"], len(mid["entries"])), (False, 1))
            return {"counts": {}, "journal": [], "dry_run": False, "problems": [], "flags": [],
                    "created_objects": [], "elapsed_seconds": 0}
        with mock.patch.object(server.capp, "apply_plan", side_effect=fake_apply), \
             mock.patch.object(server.capp, "render_run", return_value=""), \
             mock.patch.object(server, "RUNS_DIR", _tmp_runs_dir()):
            out = server.clone_apply_handler({"plan": self.plan, "dry_run": False,
                                              "confirm": "CLONE", "cat_token": "t",
                                              "run_id": "run-page-7"})
        self.assertEqual(out["run_id"], "run-page-7")
        after = server.clone_progress_handler({"run_id": "run-page-7"})
        self.assertTrue(after["done"])
        self.assertEqual(len(after["entries"]), 1)
        self.assertNotIn("body", after["entries"][0])

    def test_progress_is_marked_done_even_when_apply_raises(self):
        with mock.patch.object(server.capp, "apply_plan", side_effect=RuntimeError("cat down")), \
             mock.patch.object(server, "RUNS_DIR", _tmp_runs_dir()):
            with self.assertRaises(RuntimeError):
                server.clone_apply_handler({"plan": self.plan, "dry_run": False,
                                            "confirm": "CLONE", "cat_token": "t",
                                            "run_id": "run-page-8"})
        self.assertTrue(server.clone_progress_handler({"run_id": "run-page-8"})["done"])

    def test_the_server_class_is_threaded_so_polls_are_answered_mid_run(self):
        import socketserver
        self.assertTrue(issubclass(server.Server, socketserver.ThreadingMixIn))


class TestSandboxKeys(unittest.TestCase):
    """The optional Sandbox Secret / Public Keys: typed into the page, sent with every
    request, refused unless sandbox-prefixed, used ONLY by steps that declare a sandbox
    auth mode, and never written to a journal."""

    SK, PK = "sk_sbox_" + "a" * 32, "pk_sbox_" + "b" * 32
    PROD_SK = "sk_" + "c" * 36            # a production-shaped key

    @staticmethod
    def sandbox_plan(auth="sandbox_secret"):
        return {"plan_version": 1, "source": {"client_id": fx.SOURCE_CLIENT},
                "target": {"client_name": "t"},
                "steps": [{"seq": 1, "kind": "sandbox_probe", "op": "Sandbox_Probe",
                           "method": "GET", "path": "/workflows", "auth": auth,
                           "label": "probe"}]}

    def test_a_production_key_is_refused_on_every_route(self):
        cat_plan = plan_for(fx.reference_capture())
        objs = [{"seq": 1, "kind": "currency_account", "new_id": fx._id("ca", "new"),
                 "path": "/x"}]
        with NoSocket(), mock.patch.object(server.cc, "capture") as cap, \
             mock.patch.object(server.capp, "apply_plan") as ap:
            outs = [
                server.clone_capture_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                              "sandbox_sk": self.PROD_SK}),
                server.clone_entities_handler({"client_id": fx.SOURCE_CLIENT, "cat_token": "t",
                                               "sandbox_pk": "pk_" + "d" * 36}),
                server.clone_apply_handler({"plan": cat_plan, "sandbox_sk": self.PROD_SK}),
                server.clone_verify_handler({"created_objects": objs, "cat_token": "t",
                                             "sandbox_sk": self.PROD_SK}),
                server.clone_cleanup_handler({"created_objects": objs,
                                              "sandbox_sk": self.PROD_SK}),
            ]
            cap.assert_not_called(); ap.assert_not_called()
        for out in outs:
            self.assertIn("error", out)
            self.assertIn("refused", out["error"])
            self.assertIn("sk_sbox_" if "Secret" in out["error"] else "pk_sbox_", out["error"])

    def test_sandbox_prefixed_keys_are_accepted_and_reach_apply(self):
        # dest_sandbox_sk is the DESTINATION client's key — the one apply sends sandbox-API
        # steps with. sandbox_sk is the SOURCE's and only ever reads.
        src = "sk_sbox_" + "s" * 32
        with NoSocket(), mock.patch.object(server.capp, "apply_plan",
                                          wraps=server.capp.apply_plan) as ap:
            out = server.clone_apply_handler({"plan": self.sandbox_plan(),
                                              "dest_sandbox_sk": f"  {self.SK} ",
                                              "sandbox_sk": src, "sandbox_pk": self.PK})
        self.assertNotIn("error", out)
        self.assertEqual(ap.call_args.kwargs["sandbox_keys"],
                         {"sandbox_secret": self.SK, "source_sandbox_secret": src,
                          "sandbox_public": self.PK})
        self.assertEqual(out["run"]["counts"]["failed"], 0)

    def test_the_source_key_alone_never_authenticates_an_apply_step(self):
        # Only the destination key may create on the sandbox API. A source key on its own
        # leaves a sandbox_secret step blocked — it is never promoted to fill the gap.
        with NoSocket():
            run = capp.apply_plan(self.sandbox_plan(),
                                  sandbox_keys={"source_sandbox_secret": self.SK})
        self.assertEqual(run["journal"][0]["status"], "blocked")
        self.assertIn("Sandbox Secret Key", run["journal"][0]["error"])

    def test_absent_keys_do_not_affect_a_cat_only_plan(self):
        plan = plan_for(fx.reference_capture())
        with NoSocket():
            run = capp.apply_plan(plan)          # no sandbox_keys at all
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertFalse(any("auth" in e for e in run["journal"]))

    def test_a_sandbox_step_without_its_key_is_blocked_in_a_dry_run(self):
        with NoSocket():
            run = capp.apply_plan(self.sandbox_plan())
        e = run["journal"][0]
        self.assertEqual(e["status"], "blocked")
        self.assertEqual(e["auth"], "sandbox_secret")
        self.assertIn("Sandbox Secret Key", e["error"])
        self.assertEqual(run["counts"]["failed"], 1)
        self.assertTrue(any("Sandbox Secret Key" in p for p in run["problems"]))

    def test_a_sandbox_step_without_its_key_is_never_sent_live(self):
        # The one thing that must not happen: falling back to the CAT token.
        with mock.patch.object(capp, "_send") as send, \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(self.sandbox_plan("sandbox_public"), base="https://cat",
                                  token="cat-token", dry_run=False, pace_seconds=0,
                                  sandbox_keys={"sandbox_secret": self.SK})
        send.assert_not_called()
        self.assertEqual(run["journal"][0]["status"], "blocked")
        self.assertIn("Sandbox Public Key", run["journal"][0]["error"])

    def test_a_sandbox_step_is_sent_with_the_key_against_the_sandbox_host(self):
        with mock.patch.object(capp, "_send", return_value=({"id": "wf_1"}, 200, None)) as send, \
             mock.patch("time.sleep", lambda *a, **k: None):
            run = capp.apply_plan(self.sandbox_plan(), base="https://cat", token="cat-token",
                                  dry_run=False, pace_seconds=0,
                                  sandbox_keys={"sandbox_secret": self.SK})
        send.assert_called_once()
        base, bearer, method, path, body = send.call_args.args[:5]
        self.assertEqual(base, capp.SANDBOX_API_BASE)
        self.assertEqual(bearer, self.SK)
        self.assertNotEqual(bearer, "cat-token")
        self.assertEqual((method, path), ("GET", "/workflows"))
        self.assertEqual(run["journal"][0]["status"], 200)
        self.assertEqual(run["journal"][0]["auth"], "sandbox_secret")

    def test_a_cat_step_still_uses_the_cat_token_when_keys_are_present(self):
        plan = self.sandbox_plan("cat")
        with mock.patch.object(capp, "_send", return_value=({"id": "x"}, 200, None)) as send, \
             mock.patch("time.sleep", lambda *a, **k: None):
            capp.apply_plan(plan, base="https://cat", token="cat-token", dry_run=False,
                            pace_seconds=0, sandbox_keys={"sandbox_secret": self.SK})
        base, bearer = send.call_args.args[:2]
        self.assertEqual((base, bearer), ("https://cat", "cat-token"))

    def test_an_unknown_auth_mode_is_blocked(self):
        with NoSocket():
            run = capp.apply_plan(self.sandbox_plan("production"))
        self.assertEqual(run["journal"][0]["status"], "blocked")
        self.assertIn("unknown auth mode", run["journal"][0]["error"])

    def test_dev_creds_merge_the_local_file_over_the_tracked_one(self):
        import tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory() as tmp:
            tracked, local = _pl.Path(tmp) / "a.json", _pl.Path(tmp) / "b.json"
            tracked.write_text(json.dumps({"client_id": fx.SOURCE_CLIENT}))
            local.write_text(json.dumps({"sandbox_sk": self.SK, "sandbox_pk": self.PK}))
            out = server.dev_creds((tracked, local))
            self.assertEqual(out, {"client_id": fx.SOURCE_CLIENT,
                                   "sandbox_sk": self.SK, "sandbox_pk": self.PK})
            # a missing local file contributes nothing and breaks nothing
            self.assertEqual(server.dev_creds((tracked, _pl.Path(tmp) / "absent.json")),
                             {"client_id": fx.SOURCE_CLIENT})
            local.write_text("not json")
            self.assertEqual(server.dev_creds((tracked, local)), {"client_id": fx.SOURCE_CLIENT})

    def test_dev_creds_never_prefill_a_non_sandbox_key(self):
        import tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory() as tmp:
            local = _pl.Path(tmp) / "b.json"
            local.write_text(json.dumps({"sandbox_sk": self.PROD_SK, "sandbox_pk": self.PK,
                                         "client_id": fx.SOURCE_CLIENT}))
            out = server.dev_creds((local,))
        self.assertNotIn("sandbox_sk", out)
        self.assertEqual(out["sandbox_pk"], self.PK)
        self.assertNotIn(self.PROD_SK, json.dumps(out))

    def test_the_local_creds_file_is_gitignored(self):
        # The cache exists precisely so the keys are never committed.
        import pathlib as _pl, subprocess
        root = _pl.Path(server.HERE).parent
        r = subprocess.run(["git", "check-ignore", "-q", "app/dev-creds.local.json"],
                           cwd=root, capture_output=True)
        if r.returncode == 128:
            self.skipTest("not a git checkout")
        self.assertEqual(r.returncode, 0, "app/dev-creds.local.json is not gitignored")

    def test_keys_never_reach_the_run_document_or_the_journal_file(self):
        import tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(capp, "_send", return_value=({"id": "wf_1"}, 200, None)), \
             mock.patch("time.sleep", lambda *a, **k: None):
            jp = _pl.Path(tmp) / "j.jsonl"
            run = capp.apply_plan(self.sandbox_plan(), base="https://cat", token="cat-token",
                                  dry_run=False, pace_seconds=0, journal_path=str(jp),
                                  sandbox_keys={"sandbox_secret": self.SK,
                                                "sandbox_public": self.PK})
            text = jp.read_text()
        for secret in (self.SK, self.PK, "cat-token"):
            self.assertNotIn(secret, json.dumps(run))
            self.assertNotIn(secret, text)
        header = json.loads(text.splitlines()[0])["_header"]
        self.assertEqual(header["sandbox_keys_supplied"], ["sandbox_public", "sandbox_secret"])


# ---------------------------------------------------------------- cleanup

class TestCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = plan_for(fx.reference_capture())

    def test_removal_tiers_match_the_documented_table(self):
        expected = {
            "currency_account": "delete", "payment_routing_rule": "delete",
            "payout_routing_rule": "delete", "payout_setting": "delete",
            "payout_route": "delete",
            "client": "status", "entity": "status", "processing_profile": "status",
            "sessions_channel": "status",
            "processing_channel": "none", "processor": "none",
            "entity_service": "none", "client_risk_settings": "none",
            "client_compass_settings": "none", "client_flow_account": "none",
            "client_network_tokens": "none",   # entry kept for runs that did create one
            "webhook_workflow": "none",        # sandbox API, not CAT; needs the dest key
            "client_public_crypto_key": "none", "client_api_secret_key": "none",
            "client_api_public_key": "none",   # disabled with the client; delete in CAT
        }
        self.assertEqual({k: v["mode"] for k, v in ccl.REMOVAL.items()}, expected)

    def test_channels_and_processors_are_permanent(self):
        outlook = ccl.cleanup_outlook(self.plan)
        self.assertEqual(outlook["permanent"]["processing_channel"], 1)
        self.assertEqual(outlook["permanent"]["processor"], 4)
        self.assertEqual(outlook["counts"]["total"], EXPECTED_STEPS)

    def test_a_sessions_channel_is_permanent_because_no_id_is_recorded(self):
        # It shares the gateway channel's id, so it is not id-mapped — and an object
        # with no recorded id cannot be targeted afterwards.
        outlook = ccl.cleanup_outlook(self.plan)
        self.assertEqual(outlook["permanent"].get("sessions_channel"), 1)
        self.assertIn("sessions_channel", outlook["permanent_reasons"])

    def test_outlook_buckets_every_step(self):
        o = ccl.cleanup_outlook(self.plan)
        self.assertEqual(o["counts"]["deletable"] + o["counts"]["deactivatable"]
                         + o["counts"]["permanent"], EXPECTED_STEPS)

    def test_cleanup_refuses_to_touch_a_source_id(self):
        protected = ccl.source_ids_of(self.plan)
        self.assertIn(fx.SOURCE_CLIENT, protected)
        self.assertIn(fx.reference_capture()["entities"][0]["id"], protected)
        steps, unremovable = ccl.build_cleanup(
            [{"seq": 1, "kind": "client", "new_id": fx.SOURCE_CLIENT, "path": "/clients"}],
            protected)
        self.assertEqual(steps, [])
        self.assertIn("REFUSED", unremovable[0]["why"])

    def test_cleanup_is_reverse_creation_order(self):
        objs = [{"seq": i, "kind": "currency_account",
                 "new_id": fx._id("ca", f"new{i}"), "path": "/entities/ent_x/y"}
                for i in (1, 2, 3)]
        steps, _ = ccl.build_cleanup(objs)
        self.assertEqual([s["from_step"] for s in steps], [3, 2, 1])

    def test_etag_version_decodes_the_row_version(self):
        # base64("cv=0&rv=1") -> 1. UNVERIFIED against a live currency-account delete
        # (TODO #3) — this pins the decoding only.
        self.assertEqual(ccl.etag_version("Y3Y9MCZydj0x"), 1)
        self.assertEqual(ccl.etag_version('"Y3Y9MCZydj0x"'), 1)
        self.assertIsNone(ccl.etag_version(None))
        self.assertIsNone(ccl.etag_version("not base64 at all"))


# ---------------------------------------------------------------- known gaps

class TestKnownGaps(unittest.TestCase):
    """Tests that pin CURRENT behaviour where it is known to be insufficient. Each one
    should FAIL when the corresponding TODO item is implemented — that is the point."""

    def test_manual_values_supports_only_top_level_fields(self):
        # TODO #2: apply splits the key on the FIRST dot and assigns body[field], so a
        # nested path becomes a literal flat key. Nested support is a prerequisite for
        # supplying custom_settings.credentials[].
        plan = plan_for(fx.reference_capture())
        prof = steps_of(plan, "processing_profile")[0]
        with NoSocket():
            run = capp.apply_plan(plan, dry_run=True, manual_values={
                f"{prof['seq']}.custom_settings.credentials": "SENTINEL"})
        body = next(e["body"] for e in run["journal"] if e["seq"] == prof["seq"])
        self.assertEqual(body["custom_settings.credentials"], "SENTINEL",
                         "nested manual_values paths are now resolved — update this "
                         "test and TODO.md item 2")

    def test_client_level_services_are_recorded_as_skipped_not_cloned(self):
        # A cloned client does not inherit IA, network tokens, RTAU, risk settings or
        # processing region. Nothing tells the operator beyond this plan note.
        plan = plan_for(fx.reference_capture())
        kinds = {x["kind"] for x in plan["skipped"]}
        # risk settings used to be in this list; the client-level tier is now applied
        # (client_risk_settings), so only the entity-level ones remain a recorded gap.
        self.assertIn("processing_region", kinds)
        self.assertNotIn("network_tokens / processing_region", kinds)   # now cloned
        self.assertIn("entity risk_settings", kinds)
        # RTAU has no CAT endpoint at all, so it can only ever be a recorded gap.
        self.assertIn("real_time_account_updater", kinds)
        self.assertNotIn("risk_settings / network_tokens / processing_region", kinds)
        self.assertIn("pricing_profiles", kinds)
        self.assertIn("access_keys / public_keys", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
