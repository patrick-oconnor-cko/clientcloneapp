#!/usr/bin/env python3
"""
Synthetic captures for the plan tests.

These are NOT recordings of a real client. `clone-runs/` is gitignored and holds only
live-run journals, so there is no saved capture in the repo to build a plan from. Each
capture here is hand-built to the SHAPE the reference client has, as documented in
README.md and TODO.md:

    one entity · one channel · four processors, each a unique scheme+MCC pair ·
    six processing profiles · a sessions channel with four profile-backed processors

which is what produces the documented 26 steps. Every field present is here for a
reason, and the reasons are commented: either a create needs it, or `clean()` is
supposed to strip it, or a derivation joins on it.

Because these are synthetic, they prove the plan builder's *logic*, not CAT's contract.
A test that passes here can still be wrong about what CAT accepts — only a known-good
live call settles that. What they do catch is a regression in the five fixes already
made, all of which are visible in the plan document.

Variants (`ambiguous_*`, `direct_mode_*`, …) deep-copy the reference and mutate one
thing, so the difference under test is the diff.
"""
import copy

# CAT ids are <prefix>_<26 lowercase alphanumerics>. Keeping that shape matters: dry-run
# synthetic ids mimic it too, so any length or pattern check downstream sees realistic
# values rather than "ent_1".
def _id(prefix, tag):
    return f"{prefix}_{(tag.replace('-', '') + 'x' * 26)[:26]}"


SOURCE_CLIENT = _id("cli", "srcclient")
# A vault account is CLIENT-level — one per client, shared by every entity's channels.
SOURCE_VAULT = _id("vact", "srcvault")
# The source's real Salesforce case id. Must never reach a clone body: DROP_ALWAYS
# strips it and SANDBOX_SALESFORCE_CASE_ID is stamped in its place.
SOURCE_SALESFORCE_CASE_ID = "00998877"

# Server-assigned noise, attached to every captured object so the tests can prove
# `clean()` removes it rather than assuming it was never there.
def _server_fields(tag, kind_id):
    return {
        "id": kind_id,
        "_links": {"self": {"href": f"/{tag}/{kind_id}"}},
        "date_created": "2024-01-02T03:04:05Z",
        "date_modified": "2024-06-07T08:09:10Z",
        "e_tag": "dmVyc2lvbg==",
        "client_id": SOURCE_CLIENT,
    }


def _client():
    return {
        "id": SOURCE_CLIENT,
        "name": "Reference Retail Ltd",
        "email": "ops@reference-retail.example",
        "status": "Active",
        "salesforce_case_id": SOURCE_SALESFORCE_CASE_ID,
    }


# ---------------------------------------------------------------- entity

def _entity_detail(tag, eid):
    d = _server_fields("entities", eid)
    address = {"address_line1": "1 Reference Way", "city": "London",
               "zip": "EC1A 1AA", "country": "GB"}
    d.update({
        "name": f"Reference Entity {tag.upper()}",
        # status is REQUIRED on create (entity_status_required) and is copied from the
        # source rather than defaulted.
        "status": "Active",
        # Equal addresses => is_principal_same_as_registered must be derived as True.
        "principal_business_address": dict(address),
        "registered_business_address": dict(address),
        # A read model on the GET; the create schema declares `funding` with no
        # properties, so build_plan pops it and records a skipped[] entry.
        "funding": {"can_hold_funds": True},
        # Dropped per-kind: absent from CreateEntityRequest.
        "region": "EU",
        "type": "Individual",
        "default_cko_legal_entity": "CKO_GB",
        "salesforce_case_id": SOURCE_SALESFORCE_CASE_ID,
        # Kept: part of the create body.
        "business_registration_number": "12345678",
        "trading_name": f"Reference {tag.upper()}",
    })
    return d


def _currency_accounts(tag, eid):
    out = []
    for cur, name in (("GBP", "GBP revenue"), ("EUR", "EUR revenue")):
        caid = _id("ca", f"{tag}{cur.lower()}")
        ca = _server_fields("currency-accounts", caid)
        ca.update({
            "entity_id": eid,          # dropped: the entity is already in the path
            "name": name,
            "holding_currency": cur,
            "status": "Active",        # dropped per-kind: not on the create schema
        })
        out.append(ca)
    return out


# ---------------------------------------------------------------- profiles
#
# Six profiles, four of which back a processor. pp_visa_gb and pp_visa_food share an
# acquirer AND a scheme and are separated only by MCC — that is what exercises
# resolve_profile_id's MCC disambiguation rather than letting acquirer+scheme decide.

_PROFILES = [
    ("visagb",   "cko_visa_gb",   ["visa"],       5411, "Reference Retail — Visa GB"),
    ("mcgb",     "cko_mc_gb",     ["mastercard"], 5411, "Reference Retail — MC GB"),
    ("visafood", "cko_visa_gb",   ["visa"],       5812, "Reference Retail — Visa food"),
    ("amexgb",   "cko_amex_gb",   ["amex"],       5411, "Reference Retail — Amex GB"),
    # Unused by any processor: a payout profile and an APM profile. Present because a
    # real capture carries profiles no processor references, and resolve_profile_id must
    # not be confused by them.
    ("payoutgb", "cko_payout_gb", ["visa"],       6012, "Reference Retail — payouts"),
    ("apmnl",    "cko_apm_nl",    ["ideal"],      5411, "Reference Retail — iDEAL"),
]

# A profile's SHAPE depends on its scheme. This is not a detail — three known-good live
# profiles on the same client had three different custom_settings field sets, and Visa
# additionally carries two whole top-level objects (sca_exemptions_settings,
# payfac_settings) that Amex does not. A fixture where every profile looks the same would
# let a scheme-specific bug through, so each shape below is modelled on a real response.
#
# Note the NUMERIC types: acquiring_bin, authorization_validity_period and
# SE_CCY[].processing_threshold all come back from the v2 GET as JSON numbers and must be
# sent as strings. Keeping them as ints here is what gives the coercion tests teeth.
_SCHEME_SHAPES = {
    "visa": {
        "acquiring_bin": 402121,
        "sca_exemptions_settings": {"enable_transaction_risk_analysis": True,
                                    "enable_low_value": True,
                                    "enable_3ds_outage": False,
                                    "enable_trusted_listing": True,
                                    "enable_sca_delegation": True},
        "payfac_settings": {"payfac_id": "", "payfac_name": "",
                            "marketplace_id": "12345678"},
        "custom_settings": {
            "aft": {"override_aft_processing": "none",
                    "business_application_identifier": "AA",
                    "is_skip_recipient_name_enabled": True},
            "me_2_me_settings": {"is_enabled": False, "merchant_name": "",
                                 "card_acceptor_identification_code": ""},
            "is_crypto": False, "is_highrisk": False, "is_quasi_cash": False,
            "authorization_validity_period": 10,
            # Server-added on the response, absent from the request. clean() strips only
            # the top level, so a captured profile echoes these straight back.
            "product_code": None, "bin_processing_type": None,
            "merchant_volume_indicator": None, "allow_surcharge": False,
        },
    },
    "mastercard": {
        "acquiring_bin": 511111,
        "custom_settings": {"mastercard_assigned_id": "MC-9931",
                            "is_highrisk": False,
                            "authorization_validity_period": 7},
    },
    # Amex is the outlier: SE_CCY, program_feature, card_acceptor_terminal_id and
    # merchant_size appear on no other scheme.
    "amex": {
        "acquiring_bin": 10000000232,
        "program_feature": "OptBlue",
        "card_acceptor_terminal_id": "PG0001",
        "aggregator_name": "",
        "custom_settings": {
            "SE_CCY": [{"currency": "GBP", "processing_threshold": 0,
                        "service_establishment_number": "9450125034"}],
            "is_highrisk": False, "allow_merchant_reference": True,
            "authorization_validity_period": 7,
            "cap": None, "merchant_location_id": None,
        },
    },
    # A gateway-only APM profile, shaped like the one profile this tool has ever created
    # successfully live: a credentials block, no acquiring_bin at all.
    "ideal": {
        "is_gateway_only": True,
        "custom_settings": {
            "is_crypto": False,
            "credentials": [{"mid": "0", "token": "0", "currency": "EUR",
                             "reporting_auth_key": ""}],
            "authorization_validity_period": 7,
        },
    },
}


def _profiles(tag):
    out = []
    for slug, acquirer, schemes, mcc, name in _PROFILES:
        pid = _id("pp", f"{tag}{slug}")
        d = _server_fields("processing-profiles", pid)
        d.update({
            "entity_id": None,
            "processing_profile_name": name,
            "acquirer_key": acquirer,
            "schemes": list(schemes),
            # MCCs live in business_settings[], one entry per MCC. The top-level
            # merchant_category_code is null on both the list and the v2 detail.
            "merchant_category_code": None,
            "business_settings": [{
                "merchant_category_code": mcc,
                "card_acceptor_identification_code": f"CAI{mcc}",
            }],
            "processing_type": "PayIn",
            # CAT assigns the CAID per entity when this is true, so the create must send
            # the code empty. Both known-good live creates did exactly that.
            "auto_generate_card_acceptor_identification_code": True,
            "card_acceptor_trade_name": "Reference Retail",
            "card_acceptor_legal_name": "Reference Retail Ltd",
            # The plural is the input; the singular is a response-only echo and must not
            # be sent. banking_partner_code is server-derived. Both are dropped per-kind.
            "checkout_legal_entity_codes": ["CKO_GB"],
            "checkout_legal_entity_code": "CKO_GB",
            "banking_partner_code": "BP_GB",
            # v2-only fields, wrongly dropped once by applying v1 rules to a v2 endpoint.
            "acceptance_mode": "Standard",
            "is_external_certification_enabled": False,
            "salesforce_case_id": SOURCE_SALESFORCE_CASE_ID,
        })
        # Layer the scheme's own shape on top. Keyed on the scheme rather than the
        # acquirer, because that is what the divergence tracks.
        d.update(copy.deepcopy(_SCHEME_SHAPES.get(schemes[0], {})))
        out.append({"id": pid, "detail": d, "v2_ok": True})
    return out


# ---------------------------------------------------------------- channel + processors
#
# Four processors, each a unique (scheme, MCC) pair — the easy case, and the only one
# proven live. That uniqueness is what makes every sessions join unambiguous.

_PROCESSORS = [
    ("visagb",   "cko_visa_gb", "visa",       5411),
    ("mcgb",     "cko_mc_gb",   "mastercard", 5411),
    ("visafood", "cko_visa_gb", "visa",       5812),
    ("amexgb",   "cko_amex_gb", "amex",       5411),
]


def _processor(tag, cid, slug, acquirer, scheme, mcc):
    prid = _id("prc", f"{tag}{slug}")
    p = _server_fields("processors", prid)
    p.update({
        "name": f"{scheme} {mcc}",
        "acquirer_id": acquirer,
        "scheme": scheme,
        "merchant_category_code": mcc,
        # Recoverable only from the per-processor detail — absent from the channel's
        # nested processors[] array. Part of the create body.
        "billing_information": {"descriptor": "REFRETAIL", "city": "London"},
        "authorization_key": "authkey-" + slug,
        # All dropped per-kind: read-only echoes or capability read models.
        "status": "Active",
        "acquirer_name": acquirer.replace("_", " ").title(),
        "scheme_name": scheme.title(),
        "merchant_category_code_name": "Grocery Stores",
        "authorizations": True, "captures": True, "refunds": True, "voids": True,
        "gws_only": False,
        "sessions_processor_id": _id("spr", f"{tag}{slug}"),
    })
    return p


def _channel(tag, eid):
    cid = _id("pc", f"{tag}channel")
    d = _server_fields("processing-channels", cid)
    procs = [_processor(tag, cid, *spec) for spec in _PROCESSORS]
    d.update({
        "name": f"Reference channel {tag.upper()}",
        "services": [
            # The prism key is a COMPOSITE — pipe-delimited client and entity ids, both
            # belonging to the source. Special-casing the vault service missed this
            # entirely and CAT rejected it as invalid_prism_merchant_service.
            {"type": "prism", "key": f"{SOURCE_CLIENT}|{eid}"},
            # The vault key names the SOURCE client's vault account and must be remapped
            # to the clone's, resolved by the vault_lookup step.
            {"type": "vault", "key": SOURCE_VAULT},
            {"type": "gateway"},
        ],
        "salesforce_case_id": SOURCE_SALESFORCE_CASE_ID,
        # All dropped per-kind.
        "status": "Active",
        "features": ["3ds"],
        "processors": [{"id": p["id"]} for p in procs],
        "parent_entity_id": eid,
        "sessions_processing_channel_id": cid,
        "payment_pricing_profile_id": _id("ppp", f"{tag}pricing"),
        "payment_pricing_profile_name": "Standard pricing",
    })
    return {"id": cid, "detail": d, "processors": procs}


# ---------------------------------------------------------------- sessions channel
#
# A sessions channel SHARES its gateway channel's id, which is why build_plan registers
# no id for it (provides=None) — registering it would overwrite the gateway channel's
# mapping and later references would bind to the wrong object.

def _sessions_channel(tag, cid):
    s = _server_fields("sessions-processing-channels", cid)
    s.update({
        "id": cid,
        "gateway_processing_channel_id": cid,
        "name": f"Sessions layer {tag.upper()}",
        # Note `value`, not `type` — this schema spells the service type differently
        # from a gateway channel's services, and it exists only on the DETAIL endpoint
        # (the list omits it, and CAT rejects the create with services_required).
        "services": [{"value": "vault", "key": SOURCE_VAULT}],
        "salesforce_case_id": SOURCE_SALESFORCE_CASE_ID,
        "processors": [
            {"id": _id("spp", f"{tag}{slug}"),
             "processor_type": "profile",
             # A processor_key, not an acquirer_key — which is why the sessions join is
             # on scheme + MCC and never on acquirer.
             "acquirer_id": "cko-" + scheme[:4],
             "scheme_id": scheme,
             "merchant_category_code": mcc,
             "protocol_versions": ["2.1.0", "2.2.0"],
             "mode": "Live"}
            for slug, _acq, scheme, mcc in _PROCESSORS
        ],
    })
    return s


# ---------------------------------------------------------------- routing rules
#
# Capture order is CAT's list order, and on the reference client that put a
# narrowly-scoped rule FIRST. The catch-all must be created first or CAT rejects the
# others, so the plan has to reorder — the fixture keeps the wrong order deliberately.

def _routing_rules(tag, cid, ca_ids, prefix):
    scoped = _server_fields("routing-rules", _id(prefix, f"{tag}scoped"))
    scoped.update({
        "processing_channel_id": cid,
        "revenue_currency_account_id": ca_ids[0],
        "fees_currency_account_id": ca_ids[0],
        "allow_any_processing_channel": True,
        "allow_any_merchant_category_code": True,
        "allow_any_processing_currency": True,
        "allow_any_event_type": False,      # <- narrowed, so NOT the catch-all
        "event_types": ["Chargeback"],
        "status": "Active",                 # dropped per-kind
        "sub_entity_id": None,              # dropped per-kind
    })
    catchall = _server_fields("routing-rules", _id(prefix, f"{tag}catchall"))
    catchall.update({
        "processing_channel_id": cid,
        "revenue_currency_account_id": ca_ids[0],
        "fees_currency_account_id": ca_ids[1],
        "allow_any_processing_channel": True,
        "allow_any_merchant_category_code": True,
        "allow_any_processing_currency": True,
        "allow_any_event_type": True,
        "status": "Active",
        "sub_entity_id": None,
    })
    return [scoped, catchall]


# ---------------------------------------------------------------- payout setting
#
# The nested-body case. `clean()` strips only the TOP level, so both nested objects here
# carry their own server-assigned ids, timestamps and version, and — the part that
# actually failed live — SOURCE-client references: the schedule's currency_account_ids
# name the source's ca_*, and the payment instrument names the source's vault account.

def _payout_setting(tag, eid, ca_ids):
    return {
        "entity_id": eid,
        "status": "Active",                 # dropped per-kind
        "payment_instrument": {
            "id": _id("src", f"{tag}instrument"),
            "date_created": "2024-01-02T03:04:05Z",
            "date_modified": "2024-06-07T08:09:10Z",
            "e_tag": "dmVyc2lvbg==",
            "vault_account_id": SOURCE_VAULT,
            # CAT returns these in FULL on read — they are not redacted, so they are not
            # a manual-entry case.
            "account_number": "12345678",
            "bank_code": "200000",
            "account_holder": {"first_name": "Ref", "last_name": "Retail"},
            "branch": "Main",
        },
        "payout_schedule": {
            "id": _id("csi", f"{tag}schedule"),
            "date_created": "2024-01-02T03:04:05Z",
            "date_modified": "2024-06-07T08:09:10Z",
            "version": 3,
            "name": "Daily payout",
            "settlement_type": "Legacy",
            # Only intraday schedules may carry this, and CAT rejects it even as an
            # empty array. Legacy + [] is exactly the source shape that failed.
            "scheduler_ids": [],
            "currency_account_ids": list(ca_ids),
            "frequency": "Daily",
        },
    }


# ---------------------------------------------------------------- assembly

def _entity(tag):
    eid = _id("ent", tag + "entity")
    ch = _channel(tag, eid)
    cas = _currency_accounts(tag, eid)
    ca_ids = [c["id"] for c in cas]
    return {
        "id": eid,
        "detail": _entity_detail(tag, eid),
        "currency_accounts": cas,
        "processing_profiles": _profiles(tag),
        "processing_channels": [ch],
        "sessions_channels": [_sessions_channel(tag, ch["id"])],
        "payment_routing": _routing_rules(tag, ch["id"], ca_ids, "prr"),
        "payout_routing": [_routing_rules(tag, ch["id"], ca_ids, "por")[1]],
        "payout_settings": [_payout_setting(tag, eid, ca_ids)],
        # Shaped like the LIST response (PayoutRoutesListItem), which is what the capture
        # reads — not like the create request. The two share no field names, and reading
        # the list with the create's names is the bug that sent nulls to CAT.
        "payout_routes": [{"country_label": "United Kingdom", "country_value": "GBR",
                           "currency_label": "GBP", "schemes_label": "Faster Payments"}],
    }


# What CAT reports as valid, read fresh on every capture. CKO's currency metadata is
# DYNAMIC, so this is a snapshot of one run, not a constant — the point of fetching it is
# precisely that a hardcoded list goes stale. Deliberately excludes SLL, HRK, ZWL and
# LBP... except that LBP IS included, because LBP is a *valid* code excluded by policy
# rather than by validity. If the fixture omitted it, the LBP tests would pass for the
# wrong reason — the validity gate would catch it and the policy rule would never run.
VALID_CURRENCIES = {
    "global": ["AED", "ANG", "BGN", "EUR", "GBP", "LBP", "SEK", "SLE", "USD"],
    "holding": ["AED", "EUR", "GBP", "USD"],
    "payout_routes": {"GBR": ["GBP", "EUR"], "HRV": ["EUR"], "ZWE": ["USD"]},
    "by_acquirer": {
        "cko_visa_gb":   ["AED", "EUR", "GBP", "SLE", "USD"],
        "cko_mc_gb":     ["EUR", "GBP", "USD"],
        # No AED here but AED on cko_visa_gb — that difference is what the per-acquirer
        # scoping test turns on, and it is real: Amex and Visa do not support the same
        # currency sets.
        "cko_amex_gb":   ["GBP", "SLE", "USD"],
        "cko_payout_gb": ["GBP"],
        "cko_apm_nl":    ["EUR"],
        "cko-amex":      ["GBP", "USD"],
    },
    "unavailable": [],
}


def _network_tokens_form(eid, entity_name, provision_mode="sync", nt_state=True,
                         visa=True, mastercard=True):
    """A compact CAT Network Tokens FormResponse, shaped like the real one.

    The configuration is NOT returned as a resource: it is a UI form with values embedded.
    `default_entity_id` carries per-entity preselected merchant details; the plainText
    status lines carry TRIDs generated by scheme onboarding and must never be copied.
    """
    pre = [{"operator": "=", "value": eid, "name": f"scheme_configuration.{k}",
            "preselected_value": v}
           for k, v in (("identification_type", "vat"), ("trade_name", entity_name),
                        ("legal_name", entity_name + " Ltd"),
                        ("address.address_line1", "1 Reference Way"),
                        ("address.address_line2", ""), ("address.city", "London"),
                        ("address.zip", "EC1A 1AA"), ("address.country", "GBR"),
                        ("primary_url", ""))]
    return {
        "schema": {"label": "Network Tokens Configuration", "form_fields": [
            {"name": "nt_state", "datatype": "boolean", "display_style": "switchButton",
             "value": nt_state, "label": "Network Tokens allowed", "required": True},
            {"name": "default_entity_id", "datatype": "enum", "display_style": "selectMenu",
             "value": eid, "label": "Default Billed Entity", "required": True,
             "options": [{"label": entity_name, "value": eid}],
             "conditional_preselected_value": {"conditions": pre}},
            {"name": "pricing_start_date", "datatype": "string",
             "display_style": "textField", "label": "Pricing start date"},
            {"name": "provisioning_state", "datatype": "enum",
             "display_style": "segmentedControl", "value": "active",
             "options": [{"label": "Yes", "value": "active"},
                         {"label": "No", "value": "inactive"}]},
            {"name": "default_provision_mode", "datatype": "enum",
             "display_style": "segmentedControl", "value": provision_mode,
             "options": [{"label": "Asynchronous", "value": "async"},
                         {"label": "Synchronous", "value": "sync"}]},
            {"form_section": {"label": "Merchant details", "form_fields": [
                {"name": "scheme_configuration.mastercard_status", "datatype": "string",
                 "display_style": "plainText",
                 "label": "Onboarded to Mastercard on 21 Jul 2026, TRID: 98765400200"},
                {"name": "scheme_configuration.enabled_mastercard", "datatype": "boolean",
                 "display_style": "switchButton", "value": mastercard},
                {"name": "scheme_configuration.visa_status", "datatype": "string",
                 "display_style": "plainText",
                 "label": "Onboarded to VISA on 21 Jul 2026, TRID: 133700200"},
                {"name": "scheme_configuration.enabled_visa", "datatype": "boolean",
                 "display_style": "switchButton", "value": visa},
                {"name": "scheme_configuration.trade_name", "datatype": "string",
                 "display_style": "textField", "value": entity_name},
                {"name": "scheme_configuration.legal_name", "datatype": "string",
                 "display_style": "textField", "value": entity_name + " Ltd"},
                {"name": "scheme_configuration.primary_url", "datatype": "string",
                 "display_style": "textField", "default_value": "", "required": True},
            ]}},
            {"form_section": {"label": "Merchant Address", "form_fields": [
                {"name": "scheme_configuration.address.address_line1",
                 "display_style": "textField", "value": "1 Reference Way",
                 "when_creating": {"read_only": True}},
                {"name": "scheme_configuration.address.address_line2",
                 "display_style": "textField", "value": "",
                 "when_creating": {"read_only": True}},
                {"name": "scheme_configuration.address.city",
                 "display_style": "textField", "value": "London", "required": True,
                 "when_creating": {"read_only": True}},
                {"name": "scheme_configuration.address.zip",
                 "display_style": "textField", "value": "EC1A 1AA",
                 "when_creating": {"read_only": True}},
                {"name": "scheme_configuration.address.country",
                 "display_style": "selectMenu", "value": "GBR", "required": True,
                 "when_creating": {"read_only": True}},
            ]}},
            {"form_section": {"label": "Merchant Primary Contact", "form_fields": [
                {"name": "scheme_configuration.contact.email",
                 "display_style": "textField", "value": "ops@reference-retail.example",
                 "required": True, "when_creating": {"read_only": True}},
            ]}},
        ]},
        "endpoints": {"create": {"url": "https://nt-portal.example/:clientId"},
                      "update": {"url": "https://nt-portal.example/:clientId"}},
    }


# What the clone's own form might report back after the POST: provisioning mode stuck
# on the async default, everything else as sent. TRID lines differ too, but those are
# plainText and must not count as a difference.
def blank_network_tokens_template(eid="ent_unused"):
    """What CAT's GET /clients/{id}/network-tokens ACTUALLY returns, confirmed live on a
    fully configured client: the create template. Only default_value on most fields, no
    value on default_entity_id, and contact.email the one populated field."""
    f = _network_tokens_form(eid, "Whoever")
    def strip(items):
        for x in items or []:
            if not isinstance(x, dict):
                continue
            if "form_section" in x:
                strip(x["form_section"].get("form_fields")); continue
            if x.get("name") != "scheme_configuration.contact.email":
                x.pop("value", None)
    strip(f["schema"]["form_fields"])
    return f


def clone_network_tokens_form(eid):
    f = _network_tokens_form(eid, "Reference Entity A", provision_mode="async")
    for fld in f["schema"]["form_fields"]:
        if fld.get("form_section", {}).get("label") == "Merchant details":
            for x in fld["form_section"]["form_fields"]:
                if x.get("display_style") == "plainText":
                    x["label"] = "Onboarding PENDING"
    return f


def reference_capture():
    """One entity, shaped like the reference client. Produces 33 steps."""
    eid_a = _id("ent", "aentity")
    return {
        "client_id": SOURCE_CLIENT,
        "client": _client(),
        # Shaped like the real GET /clients/{id}/risk-settings. `premium` is Fraud
        # Detection Pro; a new client defaults to the free tier.
        "risk_settings": {
            "id": SOURCE_CLIENT, "name": "Reference Retail Ltd", "tier": "premium",
            "created_at": "2023-12-11T16:12:25Z",
            "last_updated_at": "2024-03-26T16:29:00Z",
            "read_only_restriction_enabled": False,
            "_links": {"self": {"href": f"/clients/{SOURCE_CLIENT}/risk-settings"}},
        },
        # Shaped like the real GET /clients/{id}/compass-settings. All four conversion
        # currencies are in VALID_CURRENCIES["global"], so the reference plan stays
        # flag-free; variants add codes that are not.
        "compass_settings": {
            "id": SOURCE_CLIENT, "display_currency": "USD",
            "conversion_currencies": ["EUR", "GBP", "SEK", "USD"],
            "_links": {"self": {"href": f"/clients/{SOURCE_CLIENT}/compass-settings"}},
        },
        # Shaped like the real GET /clients/{id}/flow-account. The acc_* id is the
        # SOURCE's and must never reach a request body.
        "flow_account": {
            "id": _id("acc", "srcflowaccount"), "is_enabled": True,
            "_links": {"self": {"href": f"/clients/{SOURCE_CLIENT}/flow-account"}},
        },
        # A FormResponse, not a resource — see _network_tokens_form. The default billed
        # entity is entity "a", so the step is emitted right after that entity's create.
        "network_tokens": _network_tokens_form(eid_a, "Reference Entity A"),
        # CAT's own endpoint returns only the blank template; the populated form above
        # comes from the NT portal. See clone_capture.choose_network_tokens_form.
        "network_tokens_source": "nt-portal",
        "only_entity": None,
        "entities": [_entity("a")],
        "valid_currencies": copy.deepcopy(VALID_CURRENCIES),
        "_meta": {"calls": 47, "errors": []},
    }


def no_currency_validation_capture():
    """A capture where CAT's currency configuration could not be read.

    Validity is then unchecked and the plan must SAY so — falling back silently to the
    explicit tables looks identical to a clean validation, and the tables only know the
    retirements someone has already written down.
    """
    cap = reference_capture()
    cap["valid_currencies"]["unavailable"] = ["/configuration/currencies (HTTP 503)"]
    return cap


def legacy_capture_without_validity():
    """A capture predating the validity lookup — no valid_currencies key at all."""
    cap = reference_capture()
    del cap["valid_currencies"]
    return cap


def multi_entity_capture():
    """Two independent entities. Exercises cross-entity step ordering, which no live
    run has ever covered (TODO #1)."""
    cap = reference_capture()
    cap["entities"].append(_entity("b"))
    return cap


def ambiguous_sessions_capture():
    """Two gateway processors sharing a scheme AND an MCC across different acquirers.

    This is TODO #1's silent failure mode: resolve_sessions_processor_link cannot pick
    between them, so the authentication link is dropped into skipped[] and the clone
    still completes "successfully" with the wiring missing.
    """
    cap = reference_capture()
    ent = cap["entities"][0]
    # Move the food processor onto a second Visa acquirer at the SAME MCC as visagb.
    proc = ent["processing_channels"][0]["processors"][2]
    proc["acquirer_id"] = "cko_visa_fr"
    proc["merchant_category_code"] = 5411
    # Its profile follows, so the processor itself still resolves — only the sessions
    # join becomes ambiguous.
    prof = ent["processing_profiles"][2]["detail"]
    prof["acquirer_key"] = "cko_visa_fr"
    prof["business_settings"][0]["merchant_category_code"] = 5411
    # Drop the now-unmatchable 5812 sessions processor; the visa/5411 one is the
    # ambiguous case under test.
    ent["sessions_channels"][0]["processors"] = [
        sp for sp in ent["sessions_channels"][0]["processors"]
        if sp["merchant_category_code"] != 5812
    ]
    return cap


def manual_processor_with_sessions_link_capture():
    """A manual (profile-less) processor whose sessions authentication link is retained.

    CAT cannot create the gateway processor, so the sessions link that references it has
    to be skipped too — otherwise its `requires` names an id no step provides.
    """
    cap = reference_capture()
    proc = cap["entities"][0]["processing_channels"][0]["processors"][3]  # amex
    proc["acquirer_id"] = "cko-amex"
    proc["acquirer_settings"] = {"mid": "9876543", "terminal_id": "TERM01"}
    return cap


def routing_rule_currency_capture():
    """A scoped payment routing rule narrowing on currency codes.

    A rule with `allow_any_processing_currency: false` carries `processing_currencies`,
    which is another place a bare currency code reaches CAT. The first rule keeps one
    usable currency; the second is scoped ENTIRELY to unavailable ones, so it would be
    created matching nothing.
    """
    cap = reference_capture()
    rules = cap["entities"][0]["payment_routing"]
    rules[0]["allow_any_processing_currency"] = False
    rules[0]["processing_currencies"] = ["GBP", "SLL", "LBP"]
    scoped_dead = dict(rules[0])
    scoped_dead["id"] = _id("prr", "adead")
    scoped_dead["allow_any_processing_currency"] = False
    scoped_dead["processing_currencies"] = ["ZWL", "LBP"]
    rules.append(scoped_dead)
    return cap


def processor_stale_currency_capture():
    """A processor carrying retired codes in BOTH of its currency fields.

    A processor uses `currencies` and `processing_currencies`; the live failure had ZWL
    in `processing_currencies`, which the first pass of the currency rules never touched.
    """
    cap = reference_capture()
    procs = cap["entities"][0]["processing_channels"][0]["processors"]
    procs[0]["currencies"] = ["GBP", "SLL"]
    procs[0]["processing_currencies"] = ["GBP", "ZWL", "HRK"]
    return cap


def direct_mode_capture():
    """One processor carrying inline acquirer_settings — direct mode, which binds no
    profile at all and must not be reported as an unresolved profile."""
    cap = reference_capture()
    ent = cap["entities"][0]
    proc = ent["processing_channels"][0]["processors"][3]
    # A direct-mode processor's acquirer_id is a bare processor_key, matching no
    # profile's acquirer_key.
    proc["acquirer_id"] = "cko-amex"
    proc["acquirer_settings"] = {"mid": "9876543", "terminal_id": "TERM01"}
    # Its sessions counterpart has no profile to link to either.
    ent["sessions_channels"][0]["processors"] = [
        sp for sp in ent["sessions_channels"][0]["processors"]
        if sp["scheme_id"] != "amex"
    ]
    return cap


def unresolvable_profile_capture():
    """A processor with neither a matching profile nor inline acquirer_settings. The
    plan must emit the step but warn loudly — it will fail on apply."""
    cap = reference_capture()
    ent = cap["entities"][0]
    ent["processing_channels"][0]["processors"][0]["acquirer_id"] = "cko_unknown_xx"
    ent["sessions_channels"][0]["processors"] = [
        sp for sp in ent["sessions_channels"][0]["processors"]
        if sp["scheme_id"] != "visa"
    ]
    return cap


def non_profile_sessions_capture():
    """A sessions processor whose processor_type is not `profile`. createType=existing
    links an existing profile, so a manual processor cannot be linked at all."""
    cap = reference_capture()
    sps = cap["entities"][0]["sessions_channels"][0]["processors"]
    sps[0]["processor_type"] = "manual"
    return cap


def manual_caid_capture():
    """A profile with auto_generate_card_acceptor_identification_code false.

    No known-good live create covers this combination, so build_plan must warn rather
    than silently carry the SOURCE's card acceptor identification code onto the clone.
    """
    cap = reference_capture()
    d = cap["entities"][0]["processing_profiles"][0]["detail"]
    d["auto_generate_card_acceptor_identification_code"] = False
    return cap


def stale_currency_capture():
    """A profile carrying retired ISO 4217 codes, as a profile created in 2023 does.

    Covers all four of Checkout's rules at once — SLL and HRK are replaced, ZWL and LBP
    are dropped — plus ANG, which is in no rule and must therefore be left alone. A live
    create with retired codes in `currencies` returns 422 `currency_invalid`, the one CAT
    error so far that named its own cause accurately.
    """
    cap = reference_capture()
    d = cap["entities"][0]["processing_profiles"][0]["detail"]
    d["currencies"] = ["GBP", "EUR", "USD", "SLL", "HRK", "ZWL", "LBP", "ANG"]
    return cap


def legacy_profile_capture():
    """A profile whose GET reports no checkout_legal_entity_codes at all.

    This is the real reference client's 2023-era Amex profile: CAT requires the field on
    create but does not return it, so a faithful copy of the GET fails with
    `checkout_legal_entity_code_required`. Its siblings on the entity DO report one, so
    the value is derivable.
    """
    cap = reference_capture()
    del cap["entities"][0]["processing_profiles"][3]["detail"][
        "checkout_legal_entity_codes"]
    return cap


def disagreeing_legal_codes_capture():
    """Sibling profiles on one entity reporting different legal entity codes.

    There is then no single entity-level answer, so the derivation must refuse rather
    than pick one.
    """
    cap = no_legal_codes_anywhere_capture()
    profs = cap["entities"][0]["processing_profiles"]
    profs[0]["detail"]["checkout_legal_entity_codes"] = ["cko-ltd-uk"]
    profs[1]["detail"]["checkout_legal_entity_codes"] = ["cko-sas-fr"]
    return cap


def no_legal_codes_anywhere_capture():
    """No profile on the entity reports a legal entity code. Nothing to copy, so the
    plan must say so and name the field to supply by hand."""
    cap = reference_capture()
    for p in cap["entities"][0]["processing_profiles"]:
        del p["detail"]["checkout_legal_entity_codes"]
    return cap


def sessions_only_prism_capture():
    """Prism reaching the plan only via a SESSIONS channel.

    SessionsMerchantServiceType includes `prism`, so the entity service has to be
    enabled even when no gateway channel mentions it.
    """
    cap = reference_capture()
    ent = cap["entities"][0]
    for ch in ent["processing_channels"]:
        ch["detail"]["services"] = [{"type": "vault", "key": SOURCE_VAULT}]
    ent["sessions_channels"][0]["services"] = [
        {"value": "vault", "key": SOURCE_VAULT},
        {"value": "prism", "key": f"{SOURCE_CLIENT}|{ent['id']}"},
    ]
    return cap


def dropped_currency_account_capture():
    """A currency account in a currency CKO will not enable.

    The account cannot be created, which orphans everything referencing it — a routing
    rule naming it, and its entry in the payout schedule. The run must continue anyway.
    The EUR account is used because the catch-all routing rule names it as `fees`, so
    this also exercises losing the catch-all.
    """
    cap = reference_capture()
    cap["entities"][0]["currency_accounts"][1]["holding_currency"] = "ZWL"
    return cap


def substituted_currency_account_capture():
    """A currency account in a retired currency that has a replacement — the account is
    still created, in the replacement currency."""
    cap = reference_capture()
    cap["entities"][0]["currency_accounts"][0]["holding_currency"] = "HRK"
    return cap


def payout_route_parity_capture():
    """Three enabled corridors on the source, in the LIST shape, plus one item with no
    readable corridor. The check step must carry the three and flag the fourth."""
    cap = reference_capture()
    cap["entities"][0]["payout_routes"] = [
        {"country_label": "United Kingdom", "country_value": "GBR",
         "currency_label": "GBP", "schemes_label": "Faster Payments"},
        {"country_label": "France", "country_value": "FRA",
         "currency_label": "EUR", "schemes_label": "SEPA"},
        {"country_label": "United States", "country_value": "USA",
         "currency_label": "USD", "schemes_label": "ACH, Fedwire"},
        {"country_label": "Nowhere"},                       # no corridor at all
    ]
    return cap


# What a clone entity's own GET /payout-routes?enabled=true might return, HAL-shaped:
# GBR/GBP present with the same schemes, FRA/EUR present with DIFFERENT schemes, USA/USD
# missing, and an extra corridor the source never had.
CLONE_PAYOUT_ROUTES_RESPONSE = {
    "limit": 25, "skip": 0, "total_count": 3,
    "_embedded": {"data": [
        {"country_label": "United Kingdom", "country_value": "GBR",
         "currency_label": "GBP", "schemes_label": "Faster Payments"},
        {"country_label": "France", "country_value": "FRA",
         "currency_label": "EUR", "schemes_label": "SEPA Instant"},
        {"country_label": "Germany", "country_value": "DEU",
         "currency_label": "EUR", "schemes_label": "SEPA"},
    ]},
}


def no_vault_capture():
    """Channels exist but no vault service is present, so the vault reference cannot be
    remapped. build_plan must warn rather than emit a channel naming the source's."""
    cap = reference_capture()
    for ch in cap["entities"][0]["processing_channels"]:
        ch["detail"]["services"] = [{"type": "gateway"}]
    return cap


def all_source_ids(cap):
    """Every source id the capture contains, for leak checking.

    Any of these appearing in a request body OUTSIDE a {{placeholder}} means a
    source-client reference is about to be POSTed to the clone.
    """
    ids = {SOURCE_CLIENT, SOURCE_VAULT}
    for ent in cap["entities"]:
        ids.add(ent["id"])
        ids.update(c["id"] for c in ent["currency_accounts"])
        ids.update(p["id"] for p in ent["processing_profiles"])
        for ch in ent["processing_channels"]:
            ids.add(ch["id"])
            ids.update(pr["id"] for pr in ch["processors"])
            ids.add(ch["detail"]["payment_pricing_profile_id"])
            ids.update(pr["sessions_processor_id"] for pr in ch["processors"])
        for s in ent["sessions_channels"]:
            ids.update(sp["id"] for sp in s["processors"])
        for key in ("payment_routing", "payout_routing"):
            ids.update(r["id"] for r in ent[key])
        for ps in ent["payout_settings"]:
            ids.add(ps["payment_instrument"]["id"])
            ids.add(ps["payout_schedule"]["id"])
    return {i for i in ids if i}


def deepcopy(obj):
    return copy.deepcopy(obj)
