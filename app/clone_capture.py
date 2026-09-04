#!/usr/bin/env python3
"""
Clone capture — read a client's CAT configuration and emit an ordered clone plan.

READ-ONLY. This module performs GETs and produces a plan document; it never writes.
Executing the plan is clone_apply.py's job.

Deliberately self-contained: standard library only, no shared modules.

Plan model
----------
A plan is an ordered list of steps. Each step is one HTTP call, and declares:

  provides : the SOURCE id this call will create a counterpart for (registered in the
             id map at apply time from the response's `id`)
  requires : SOURCE ids this call's body depends on

Bodies carry source ids wrapped as mustache placeholders — `{{ent_abc...}}`. At apply
time each placeholder resolves through the id map to the newly created id. Keeping the
*source* id visible in the plan makes it self-documenting: you can read a step and see
exactly which source object each reference points at.

`requires`/`provides` also make the plan checkable before anything is created: every
`requires` must be provided by an earlier step (see validate_plan).
"""
import json, re, urllib.request, urllib.error, urllib.parse, datetime

PLAN_VERSION = "0.1.0"

# Fallback only. An entity's status is copied from the SOURCE entity — a clone should
# mirror what it cloned, not a hardcoded value. This is used only if the source somehow
# has no status at all. (CAT requires the field: omitting it -> entity_status_required.)
ENTITY_STATUS_FALLBACK = "Pending"

# Server-assigned or read-only fields, stripped from every captured body. Determined by
# diffing live GET responses against their POST request schemas.
#
# NOTE on `status`: it is NOT blanket server-assigned. CreateEntityRequest and
# ProcessingProfileRequest both accept it, and the entity endpoint *requires* it —
# dropping it returned `entity_status_required` (422). So status is dropped per-kind
# below, only where the create schema has no such field.
DROP_COMMON = {"id", "_links", "date_created", "date_modified", "creation_date",
               "updated_date", "e_tag", "client_id", "entity_id"}
DROP_BY_KIND = {
    "entity":            {"region", "type", "default_cko_legal_entity"},
    "currency_account":  {"status"},
    # Profiles are created via the **v2** endpoint, whose swagger request body is `{}` —
    # no contract at all. The only schema carrying properties is ProcessingProfileRequest,
    # which is v1, so deriving this list from the swagger silently applied v1 rules and
    # stripped fields v2 accepts. Corrected against a known-good live v2 call:
    #   * acceptance_mode / is_external_certification_enabled ARE valid on v2 — previously
    #     dropped here purely because they are absent from the v1 schema
    #   * checkout_legal_entity_codes (plural) is the input; the singular
    #     checkout_legal_entity_code is a response-only echo and is not sent
    #   * banking_partner_code is server-derived — absent from the known-good request,
    #     present in its response
    "processing_profile": {"checkout_legal_entity_code", "banking_partner_code"},
    # parent_entity_id: not declared by CreateProcessingChannelRequest and absent from a
    # known-good live call — the entity is already in the path. The schema's field is
    # parent_processing_channel_id, a different thing. Previously set explicitly here.
    "processing_channel": {"status", "features", "processors", "parent_entity_id",
                           "sessions_processing_channel_id",
                           "payment_pricing_profile_id", "payment_pricing_profile_name"},
    "processor":         {"status", "acquirer_name", "scheme_name",
                          "merchant_category_code_name",
                          "authorizations", "captures", "refunds", "voids", "gws_only",
                          "sessions_processor_id"},
    "routing_rule":      {"status", "sub_entity_id"},
    "payout_setting":    {"status"},
}
# Never carried across: source-system bookkeeping. salesforce_case_id is dropped so the
# SOURCE's real case id cannot follow the clone, then re-stamped with the sandbox
# placeholder below on the bodies that require it.
DROP_ALWAYS = {"salesforce_case_id", "salesforce_id", "custom_id"}

# Sandbox placeholder. A clone must not inherit a real Salesforce case id, and inventing
# one is worse; this is the agreed sandbox value.
SANDBOX_SALESFORCE_CASE_ID = "12345678"

# Fields the v2 profile GET returns as JSON NUMBERS but the create requires as STRINGS.
# Round-tripping a captured profile therefore sends the wrong type unless it is coerced.
#
# Confirmed twice over, which is why this is a table and not a guess:
#   * two known-good live creates (Amex `acquiring_bin: "10000000232"`, Visa
#     `"402121"`; both `authorization_validity_period` sent as a string) whose responses
#     echo the values back as integers
#   * the swagger's own request schemas declare them as string —
#     ProcessingProfileRequest.acquiring_bin and SenCurrencyThreshold.processing_threshold
#     are both `["null", "string"]`
#
# `authorization_validity_period` has only the live calls behind it: the swagger's
# CustomSettings does not declare the field at all (it is `additionalProperties: false`
# with 16 properties and is missing most of what CAT actually accepts).
PROFILE_STRING_FIELDS = ("acquiring_bin",)
PROFILE_STRING_FIELDS_CUSTOM = ("authorization_validity_period", "processing_threshold")

# Currency remediation. A profile captured from an older client carries the currency list
# as it was when the profile was created, and ISO 4217 has moved since — a create with a
# retired code returns 422 `currency_invalid`, which is (unusually for CAT) an accurate
# description of the problem.
#
# These rules are Checkout's, supplied directly; they are NOT derived from ISO tables,
# because "what ISO retired" and "what CKO will accept" are different questions. Do not
# extend this table by inference — an unrecognised code is left alone deliberately.
#
# **CKO's currency metadata is DYNAMIC — it changes from time to time.** So this table is
# not the source of truth and will drift. The source of truth is CAT itself, read fresh
# on every capture (see the configuration endpoints in capture()); a code CAT no longer
# lists is dropped by the validity gate whether or not it appears below. Never cache
# that lookup across runs, and never treat this table as complete.
#
# What each part is actually for, given that:
#   * CURRENCY_REPLACE is load-bearing and cannot be derived. A validity lookup can say
#     SLL is gone; only a mapping says its successor is SLE.
#   * CURRENCY_DROP["ZWL"] is belt-and-braces — if it has been removed from CKO's
#     metadata the validity gate catches it anyway. Harmless to keep, and it still
#     works when the lookup is unavailable.
#   * CURRENCY_DROP["LBP"] IS load-bearing and must not be removed: LBP is a *valid*
#     code that CKO will still list, excluded by policy rather than by validity. No
#     lookup will ever catch it.
CURRENCY_REPLACE = {
    "SLL": ("SLE", "retired — Sierra Leone redenominated"),
    "HRK": ("EUR", "retired — Croatia adopted the euro"),
}
# Dropped rather than replaced. LBP is a valid ISO code and is deliberately NOT a
# warning: Checkout's guidance is that it must not be enabled for new merchants, so the
# clone simply proceeds without it and says so.
CURRENCY_DROP = {
    "ZWL": "removed from CKO currency metadata",
    "LBP": "valid ISO code, but CKO inactivates it and it must not be enabled for new "
           "merchants — the clone proceeds without it",
}

# Scheme-specific card acceptor identification code. A profile's shape depends on its
# scheme, so keying on scheme here is correct rather than a shortcut — but only where a
# rule is actually known. iDEAL takes a literal "0"; everything else falls through to the
# auto-generate path below.
CAID_BY_SCHEME = {"ideal": "0"}


def parse_currency_codes(payload):
    """Pull currency codes out of a CAT configuration response.

    Written defensively because three of the four currency configuration endpoints
    declare NO response schema in the swagger, so their shape is unverified. Handles the
    shapes CAT uses elsewhere:

        ["GBP", "EUR"]                            bare codes
        [{"value": "GBP", "label": "..."}]        Option[]
        {"currencies": <either of the above>}     GetProcessorCurrenciesResponse
        {"countries": [{"currencies": [...]}]}    PayoutRouteConfiguration

    Returns None — NOT an empty set — when nothing recognisable is found. That
    distinction matters: an empty set would mean "no currency is valid" and drop
    everything, so callers must treat None as "validation unavailable" and fall back to
    the explicit rules alone.
    """
    def codes_from(seq):
        out = set()
        for x in seq or []:
            if isinstance(x, str) and x.strip():
                out.add(x.strip().upper())
            elif isinstance(x, dict):
                v = x.get("value") or x.get("code") or x.get("currency")
                if isinstance(v, str) and v.strip():
                    out.add(v.strip().upper())
        return out

    if isinstance(payload, list):
        found = codes_from(payload)
        return found or None
    if isinstance(payload, dict):
        found = codes_from(payload.get("currencies"))
        for country in (payload.get("countries") or []):
            if isinstance(country, dict):
                found |= codes_from(country.get("currencies"))
        for key in ("holding_currencies", "data", "items"):
            found |= codes_from(payload.get(key))
        return found or None
    return None


def payout_route_currencies(payload):
    """country ISO3 -> valid currency codes, from PayoutRouteConfiguration."""
    out = {}
    for country in ((payload or {}).get("countries") or []):
        if not isinstance(country, dict):
            continue
        code = country.get("value")
        found = parse_currency_codes({"currencies": country.get("currencies")})
        if code and found:
            out[str(code).upper()] = sorted(found)
    return out


# The NT portal behind CAT's Network Tokens page. CAT proxies only the blank form
# template at /clients/{id}/network-tokens; the client's actual configuration is read
# from here. Sandbox host — this tool is sandbox-only.
NT_PORTAL_BASE = "https://nt-portal.sbox.checkout.internal/vault-nt-portal/cat/configurations"

# Some sandbox clients have no webpage URL, and the network-tokens create rejects the body
# without one (learned live: 422 primary_url_required). Sent in its place and flagged as a
# callout so the operator replaces it.
NT_PRIMARY_URL_PLACEHOLDER = "https://www.placeholder.com"


def choose_network_tokens_form(cat_form, portal_form):
    """Pick the network-tokens form that actually carries the client's configuration.

    A populated form has a `value` on default_entity_id; the blank template does not.
    CAT is tried first (same host as everything else), then the portal. Returns
    (form, source) where source is "cat", "nt-portal", or "none" when neither carried a
    configuration — in which case the CAT template is returned so the caller can still
    see the field shape.
    """
    for form, source in ((cat_form, "cat"), (portal_form, "nt-portal")):
        vals = network_token_form_values(form)
        if vals and vals.get("default_entity_id"):
            return form, source
    return (cat_form if cat_form else portal_form), "none"


def network_token_form_values(form):
    """Flatten CAT's Network Tokens FormResponse into {field_name: value}.

    GET /clients/{id}/network-tokens does not return the configuration as a resource; it
    returns the UI form that renders it — `schema.form_fields[]`, some nested under
    `form_section.form_fields[]`, each carrying its current `value`. Field names are
    dotted (`scheme_configuration.address.city`). plainText fields are status lines
    ("Onboarded to VISA on ..., TRID: ...") and are skipped: those are generated by scheme
    onboarding and must never be copied.

    `default_entity_id` also carries `conditional_preselected_value`: the per-entity
    merchant details (identification_type, trade/legal name, address, primary_url) that
    the UI pre-fills for whichever entity is selected. Those are taken for the selected
    entity, but an explicit field `value` always wins over a preselected one.

    Returns None if the response is not a recognisable form.
    """
    if not isinstance(form, dict):
        return None
    fields = ((form.get("schema") or {}).get("form_fields")) if isinstance(form.get("schema"), dict) else None
    if not isinstance(fields, list):
        return None
    vals = {}

    def walk(items):
        for f in items or []:
            if not isinstance(f, dict):
                continue
            sec = f.get("form_section")
            if isinstance(sec, dict):
                walk(sec.get("form_fields"))
                continue
            name = f.get("name")
            if not name or f.get("display_style") == "plainText":
                continue
            if name == "default_entity_id":
                sel = f.get("value")
                for c in ((f.get("conditional_preselected_value") or {}).get("conditions") or []):
                    if (isinstance(c, dict) and c.get("operator") == "=" and sel
                            and c.get("value") == sel and c.get("name")):
                        vals.setdefault(c["name"], c.get("preselected_value"))
            if "value" in f:
                vals[name] = f["value"]

    walk(fields)
    return vals or None


def network_tokens_body(vals, entity_placeholder):
    """Shape flattened form values into NetworkTokenConfigurationRequest.

    The request is NESTED (scheme_configuration.address.city), not the dotted form names.
    `default_entity_id` is a SOURCE entity id in the form and is replaced by the clone's
    placeholder. Address and contact are read-only in the form ("derived from the entity")
    but `city`, `country` and `contact.email` are non-nullable in the request schema, so
    they are sent — from the source, which the clone entity was copied from anyway.

    onboard_visa / onboard_mastercard are taken from the form's enabled_* switches. They
    START scheme onboarding on the clone (a TRID gets assigned); per guidance that is the
    expected sandbox behaviour. Status text and TRIDs are never copied.
    """
    def g(k):
        return vals.get("scheme_configuration." + k)
    body = {
        "nt_state": bool(vals.get("nt_state")),
        "default_entity_id": entity_placeholder,
        "provisioning_state": vals.get("provisioning_state"),
        "default_provision_mode": vals.get("default_provision_mode"),
    }
    if vals.get("pricing_start_date"):
        body["pricing_start_date"] = vals["pricing_start_date"]
    sc = {}
    for k in ("trade_name", "legal_name"):
        if g(k) is not None:
            sc[k] = g(k)
    for k in ("identification_type", "identification_value", "primary_url"):
        if g(k) not in (None, ""):
            sc[k] = g(k)
    if g("enabled_visa") is not None:
        sc["onboard_visa"] = bool(g("enabled_visa"))
    if g("enabled_mastercard") is not None:
        sc["onboard_mastercard"] = bool(g("enabled_mastercard"))
    addr = {k: g("address." + k) for k in ("address_line1", "address_line2", "zip",
                                            "city", "country")
            if g("address." + k) is not None}
    if addr:
        sc["address"] = addr
    if g("contact.email"):
        sc["contact"] = {"email": g("contact.email")}
    body["scheme_configuration"] = sc
    return body


def normalise_payout_route(item):
    """Reduce a payout-route list item to the corridor it describes.

    The list item's field names are the one thing about this endpoint the swagger is
    least trustworthy on — it declares `country_label`/`country_value`/`currency_label`/
    `schemes_label`, while the create schema says `country_iso3_code`/`currency_code`.
    So every plausible spelling is accepted. The comparison in clone_apply puts the
    SOURCE's items and the CLONE's items through this same function, so it is sound as
    long as both sides come from the same endpoint — which they do — even if the guess
    about which field carries the ISO code is wrong.

    Returns {"country", "currency", "schemes", "label"} with upper-cased codes, or None
    if the item carries no recognisable corridor.
    """
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


def remediate_currencies(codes, valid=None, scope=None):
    """Apply the currency rules above. Returns (codes, notes, dropped).

    `dropped` is a list of {"currency", "reason"} — the reason matters because a code
    can be dropped either by the explicit policy rules or by the validity gate, and the
    report should say which.

    `valid` is the set of codes CAT says are acceptable in this context, from one of the
    configuration endpoints (see parse_currency_codes). When supplied, a code that
    survives the explicit rules but is absent from it is dropped too — that is the
    general validity gate, and it catches retirements nobody has added to the tables
    yet. When it is None the explicit rules still apply on their own; validity is simply
    not checked, and build_plan flags that the check was unavailable.

    Order matters: policy drops, then successor replacement, then the validity gate — so
    a replacement is itself validated (SLL -> SLE is no use if SLE is unsupported here).
    `scope` only names the source of `valid` in the note text.

    GLOBAL, not per-scheme and not per-step kind: every place a bare currency code
    reaches CAT goes through here — a profile's `currencies`, an Amex SE_CCY row, a
    currency account's `holding_currency`, a payout route's `currency_code`.

    `dropped` is returned separately so callers can FLAG a code that cannot be added
    without halting the run: the guidance for ZWL and LBP is to say so and continue.

    Order is preserved, and a replacement that collides with a code already present is
    de-duplicated rather than sent twice.
    """
    out, notes, dropped, seen = [], [], [], set()
    for c in codes or []:
        code = c.upper() if isinstance(c, str) else c
        if code in CURRENCY_DROP:
            notes.append(f"currency {code} dropped — {CURRENCY_DROP[code]}")
            dropped.append({"currency": code, "reason": CURRENCY_DROP[code]})
            continue
        if code in CURRENCY_REPLACE:
            new, why = CURRENCY_REPLACE[code]
            notes.append(f"currency {code} replaced with {new} — {why}")
            code = new
        if valid and code not in valid:
            why = ("CAT does not list it as valid"
                   + (f" for {scope}" if scope else ""))
            notes.append(f"currency {code} dropped — {why}")
            dropped.append({"currency": code, "reason": why})
            continue
        if code in seen:
            notes.append(f"currency {code} de-duplicated after replacement")
            continue
        seen.add(code)
        out.append(code)
    return out, notes, dropped


def ph(src_id):
    """Wrap a source id as a placeholder for apply-time substitution."""
    return "{{%s}}" % src_id


def make_flag(code, message, **fields):
    """A non-halting finding: something the clone could not carry across faithfully.

    Structured on purpose. These are the raw material for the post-run report, so the
    report can be generated from data rather than by re-parsing warning strings. Each
    flag's `message` is also mirrored into plan["warnings"], which is what the review
    view already renders.

    A flag NEVER halts a run. Something that should stop the plan being applied belongs
    in `skipped[]` as well, or should not be emitted at all.

      code    stable identifier, safe to group or count by
      action  what the plan did about it: "dropped", "substituted", "not_created"
    """
    return {"code": code, "message": message, **fields}


def clean(obj, kind):
    drop = DROP_COMMON | DROP_BY_KIND.get(kind, set()) | DROP_ALWAYS
    return {k: v for k, v in (obj or {}).items() if k not in drop and v is not None}


# ---------------------------------------------------------------- http (read-only)

class Reader:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token
        self.calls = 0
        self.errors = []

    def get(self, path):
        self.calls += 1
        req = urllib.request.Request(self.base + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}"), r.status
        except urllib.error.HTTPError as e:
            self.errors.append({"path": path, "code": e.code})
            return {}, e.code
        except Exception as e:
            self.errors.append({"path": path, "error": str(e)})
            return {}, 0

    def hal(self, path, key):
        d, code = self.get(path)
        return ((d.get("_embedded") or {}).get(key) or []), code


# ---------------------------------------------------------------- capture

def scope_entities(ents, only_entity=None, only_entities=None):
    """Restrict a client's entity list to the requested scope.

    Returns (kept, missing): the entities to capture, in CAT's order, and any requested
    id CAT did not return. `missing` is never silently dropped by the caller — a clone of
    two entities when three were ticked is exactly the kind of quiet partial result this
    tool refuses to produce. An empty scope means every entity.
    """
    wanted = [x.strip() for x in (only_entities or []) if isinstance(x, str) and x.strip()]
    if only_entity and only_entity not in wanted:
        wanted.append(only_entity)
    if not wanted:
        return list(ents), []
    wanted_set = set(wanted)
    kept = [e for e in ents if e.get("id") in wanted_set]
    found = {e.get("id") for e in kept}
    return kept, [w for w in wanted if w not in found]


def capture(base, token, client_id, only_entity=None, only_entities=None):
    """Read the source configuration. Returns a raw capture dict.

    only_entities: restrict the capture (and therefore the plan) to these ent_* ids — the
    entities ticked on the page. only_entity is the older single-id form and still works.
    Useful for a first live run — same information, much smaller blast radius, and it
    matters because most of what a clone creates cannot be deleted afterwards. Requested
    ids CAT did not return are recorded in cap["scope_missing"] for the caller to refuse.
    """
    r = Reader(base, token)
    cap = {"client_id": client_id, "entities": [], "only_entity": only_entity,
           "only_entities": sorted({*(only_entities or []), *([only_entity] if only_entity else [])}) or None,
           "scope_missing": []}

    cap["client"], _ = r.get(f"/clients/{client_id}")
    # Client-level risk settings (the Fraud Detection tier). A new client comes up on
    # the free tier; the source's tier is applied by a PUT straight after client create.
    cap["risk_settings"], _ = r.get(f"/clients/{client_id}/risk-settings")
    # Compass (Dashboard) settings: display currency + conversion currencies. Also
    # client-level; a new client does not inherit them.
    cap["compass_settings"], _ = r.get(f"/clients/{client_id}/compass-settings")
    # Flow (hosted checkout) account: client-level, a single is_enabled flag. The `acc_*`
    # id CAT returns is client-specific and never copied — like the vault account.
    cap["flow_account"], _ = r.get(f"/clients/{client_id}/flow-account")
    # Network Tokens. CAT's own GET /clients/{id}/network-tokens returns the BLANK CREATE
    # TEMPLATE — every field carries only a default_value, default_entity_id has no value
    # at all — regardless of what the client has configured (confirmed live: the source
    # client is fully configured and CAT returned an empty template). The populated
    # configuration lives on the NT portal, a different host, which is the one read this
    # tool makes outside CAT. Same bearer token; if that is refused, the portal response
    # lands in _meta.errors and the plan flags exactly that.
    cat_nt, _ = r.get(f"/clients/{client_id}/network-tokens")
    portal_nt = None
    if not (network_token_form_values(cat_nt) or {}).get("default_entity_id"):
        rp = Reader(NT_PORTAL_BASE, token)
        portal_nt, _ = rp.get(f"/{client_id}")
        r.calls += rp.calls
        r.errors.extend(rp.errors)
    cap["network_tokens"], cap["network_tokens_source"] = \
        choose_network_tokens_form(cat_nt, portal_nt)
    cap["network_tokens_cat_raw"] = cat_nt      # kept for diagnosis
    ents, _ = r.hal(f"/clients/{client_id}/entities?limit=25&skip=0", "entities")
    ents, cap["scope_missing"] = scope_entities(ents, only_entity, only_entities)

    for e in ents:
        eid = e.get("id")
        detail, _ = r.get(f"/entities/{eid}")
        ent = {"id": eid, "detail": detail or e}

        ent["currency_accounts"], _ = r.hal(
            f"/entities/{eid}/currency-accounts?limit=25&skip=0", "currency_accounts")

        # profiles: list gives ids, v2 detail gives the clonable custom_settings
        plist, _ = r.hal(f"/entities/{eid}/processing-profiles?limit=25&skip=0",
                         "processing_profiles")
        ent["processing_profiles"] = []
        for p in plist:
            v2, code = r.get(f"/processing-profiles/v2/{p['id']}")
            ent["processing_profiles"].append({"id": p["id"], "detail": v2 or p,
                                               "v2_ok": code == 200})

        # channels: detail gives the channel body; per-processor detail is REQUIRED to
        # recover billing_information / authorization_key / acquirer_settings, none of
        # which appear in the channel's nested processors[] array.
        clist, _ = r.hal(f"/entities/{eid}/processing-channels?limit=25&skip=0",
                         "processing_channels")
        ent["processing_channels"] = []
        for c in clist:
            cd, _ = r.get(f"/processing-channels/{c['id']}")
            procs = []
            for pr in (cd.get("processors") or []):
                pd, code = r.get(f"/processing-channels/{c['id']}/processors/{pr['id']}")
                procs.append(pd if code == 200 else pr)
            ent["processing_channels"].append({"id": c["id"], "detail": cd or c,
                                              "processors": procs})

        # sessions channels: the LIST returns only id/name/status/business_model — it omits
        # `services`, which is REQUIRED on create (`services_required`). Only the detail
        # carries it, so fetch per channel. Note its shape differs from a gateway channel's
        # services: {"value": "vault", ...} rather than {"type": "vault", ...}.
        slist, _ = r.hal(
            f"/entities/{eid}/sessions-processing-channels?limit=25&skip=0",
            "sessions_processing_channels")
        ent["sessions_channels"] = []
        for s in slist:
            sd, code = r.get(f"/sessions-processing-channels/{s['id']}")
            ent["sessions_channels"].append(sd if code == 200 else s)

        # routing rules: list ids, then detail for the actual conditions
        for kind, seg, key in (("payment_routing", "payment-routing-rules", "routing_rules"),
                               ("payout_routing", "payout-routing-rules", "routing_rules")):
            lst, _ = r.hal(f"/entities/{eid}/{seg}?limit=25&skip=0", key)
            out = []
            for x in lst:
                d, code = r.get(f"/{seg}/{x['id']}")
                out.append(d if code == 200 else x)
            ent[kind] = out

        pset, _ = r.hal(f"/entities/{eid}/payout-settings?limit=25&skip=0", "payout_settings")
        ent["payout_settings"] = []
        for s in pset:
            d, code = r.get(f"/entities/{eid}/payout-settings/{s['id']}")
            ent["payout_settings"].append(d if code == 200 else s)

        # Payout routes are provisioned CAPABILITY, not merchant configuration — the
        # supported payout corridors (country + currency + scheme) available to the
        # entity, backed by a dimension table. The unfiltered GET returns disabled
        # corridors too, which a clone must never turn on; only the enabled ones are the
        # source's real footprint. These are never POSTed — see the parity check in
        # build_plan.
        ent["payout_routes"], _ = r.hal(
            f"/entities/{eid}/payout-routes?enabled=true", "data")
        cap["entities"].append(ent)

    # ---- valid currency sets, straight from CAT --------------------------------
    #
    # Asking is better than maintaining a list: a validity gate catches retirements
    # nobody has added to CURRENCY_REPLACE/CURRENCY_DROP yet. It cannot replace those
    # tables — a valid-currency list can't tell you SLL's successor is SLE, and LBP is a
    # *valid* code CKO simply won't enable for new merchants — so both are used together.
    #
    # Three of these four endpoints declare no response schema in the swagger, so the
    # parsing is defensive and an unrecognised response yields None ("unavailable")
    # rather than an empty set ("nothing is valid"), which would drop every currency.
    cur = {"global": None, "by_acquirer": {}, "payout_routes": {},
           "holding": None, "unavailable": []}

    d, code = r.get("/configuration/currencies")
    cur["global"] = parse_currency_codes(d)
    if cur["global"] is None:
        cur["unavailable"].append(f"/configuration/currencies (HTTP {code})")

    d, code = r.get("/currency-accounts/configuration")
    cur["holding"] = parse_currency_codes(d)
    if cur["holding"] is None:
        cur["unavailable"].append(f"/currency-accounts/configuration (HTTP {code})")

    d, code = r.get("/payout-routes/configuration")
    cur["payout_routes"] = payout_route_currencies(d)
    if not cur["payout_routes"]:
        cur["unavailable"].append(f"/payout-routes/configuration (HTTP {code})")

    # Per acquirer, because Amex and Visa do not support the same set. Only the
    # acquirers this capture actually references are fetched.
    acquirers = set()
    for ent in cap["entities"]:
        for p in ent.get("processing_profiles", []):
            a = (p.get("detail") or {}).get("acquirer_key")
            if a:
                acquirers.add(a)
        for c in ent.get("processing_channels", []):
            for pr in c.get("processors", []):
                if pr.get("acquirer_id"):
                    acquirers.add(pr["acquirer_id"])
    for a in sorted(acquirers):
        d, code = r.get("/processors/configuration/currencies?acquirerId="
                        + urllib.parse.quote(a))
        parsed = parse_currency_codes(d)
        if parsed is None:
            cur["unavailable"].append(
                f"/processors/configuration/currencies?acquirerId={a} (HTTP {code})")
        else:
            cur["by_acquirer"][a] = sorted(parsed)

    # Sets are not JSON-serialisable and the capture is downloadable.
    for k in ("global", "holding"):
        if cur[k] is not None:
            cur[k] = sorted(cur[k])
    cap["valid_currencies"] = cur

    cap["_meta"] = {"calls": r.calls, "errors": r.errors}
    return cap


def valid_currency_set(cap, acquirer=None, purpose=None):
    """The currency codes CAT accepts in a given context, or None if unavailable.

    Returns (codes | None, scope_label). None means "not checked" — callers must not
    treat it as "nothing is valid".
    """
    cur = (cap or {}).get("valid_currencies") or {}
    if purpose == "holding":
        return (set(cur["holding"]), "currency accounts") \
            if cur.get("holding") else (None, None)
    if acquirer and acquirer in (cur.get("by_acquirer") or {}):
        return set(cur["by_acquirer"][acquirer]), f"acquirer {acquirer}"
    if cur.get("global"):
        return set(cur["global"]), "CAT's currency list"
    return None, None


# ---------------------------------------------------------------- profile join

def profile_mccs(detail):
    """MCCs a profile covers.

    NOT the top-level `merchant_category_code` — that is null on both the list and the v2
    detail. MCCs live in `business_settings[]`, one entry per MCC, each with its own
    card_acceptor_identification_code. A profile can therefore cover several MCCs.
    """
    out = set()
    for bs in (detail.get("business_settings") or []):
        m = (bs or {}).get("merchant_category_code")
        if m is not None:
            out.add(str(m))
    m = detail.get("merchant_category_code")
    if m is not None:
        out.add(str(m))
    return out


def profile_label(detail):
    """A readable name for a profile.

    `processing_profile_name` is sometimes literally the profile's own id (CAT appears to
    default it that way when created unnamed), which is useless in a review view — fall
    back to the card acceptor trade/legal name, then to acquirer + schemes.
    """
    n = (detail.get("processing_profile_name") or "").strip()
    if n and not n.startswith("pp_"):
        return n
    for f in ("card_acceptor_trade_name", "card_acceptor_legal_name"):
        v = (detail.get(f) or "").strip()
        if v:
            return f"{v} ({detail.get('processing_type') or '?'})"
    schemes = ", ".join(detail.get("schemes") or []) or "?"
    return f"{detail.get('acquirer_key') or '?'} · {schemes}"


def _as_str(v):
    """Render a captured number as the string the create expects.

    Guards two things str() gets wrong: bools are ints in Python, and a float that CAT
    round-tripped (10.0) must not become "10.0".
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return v


# A CAT id: <prefix>_<20+ lowercase alphanumerics>. Used to spot a SOURCE id still
# sitting in a value after remapping, which is otherwise invisible until CAT rejects it.
SOURCE_ID_RE = re.compile(r"[a-z][a-z0-9]*_[a-z0-9]{20,}")


def service_key_ids(src_cli, src_vault, eid, ent):
    """Source ids a service key could plausibly name, all of which an earlier step
    provides — so adding one to `requires` keeps the plan valid."""
    ids = {src_cli, src_vault, eid}
    ids |= {ca.get("id") for ca in (ent.get("currency_accounts") or [])}
    ids |= {p.get("id") for p in (ent.get("processing_profiles") or [])}
    return {i for i in ids if i}


def remap_service_key(key, known_ids):
    """Rewrite every SOURCE id embedded in a channel service key as a placeholder.

    A service key is NOT always a bare id. The `prism` service's key is a pipe-delimited
    composite of the client and entity ids:

        "cli_scna7ew7mxdenl3h36zlmkyh6m|ent_juznxdro7lhwrf3c5e4wprftgu"

    The original code special-cased `type == "vault"` and so sent that composite to the
    clone untouched, naming the SOURCE's client and entity. CAT rejected it with
    `invalid_prism_merchant_service` — a message that says nothing about either.

    Substring collisions are not a concern: CAT ids carry a 26-character random tail, so
    one id is never a substring of another.

    Returns (key, used_ids).
    """
    out, used = key, []
    for sid in sorted(known_ids or (), key=len, reverse=True):
        if sid and sid in out:
            out = out.replace(sid, ph(sid))
            used.append(sid)
    return out, used


def unmapped_source_ids(value):
    """Source-looking ids left in a value after remapping — i.e. ids we could not map.

    These are the dangerous ones: they are not placeholders, so nothing downstream
    notices, and they reach CAT as literal references to the source client.
    """
    stripped = re.sub(r"\{\{[^{}]*\}\}", "", value or "")
    return sorted(set(SOURCE_ID_RE.findall(stripped)))


def resolve_legal_entity_codes(entity_detail, profiles):
    """Derive `checkout_legal_entity_codes` for a profile that cannot report its own.

    CAT requires the field on create but does not return it for older profiles — the
    reference client's 2023-era Amex profile has no such key at all, which is why a
    faithful copy of its GET fails with `checkout_legal_entity_code_required`.

    Derived from the SIBLING profiles on the same entity. Two known-good creates on one
    entity used the same code for different schemes (amex and visa both `cko-ltd-uk`),
    while a profile on a different, Saudi entity used `cko-ksa-sau` — so the value tracks
    the entity, not the scheme.

    Deliberately does NOT fall back to the entity's `default_cko_legal_entity`: that field
    is undocumented (it appears in no swagger schema) and its vocabulary has never been
    seen, so it might not even be the same identifier space. Its value is surfaced in the
    refusal message as a hint for manual entry instead of being guessed at.

    Returns (codes | None, confidence).
    """
    seen = []
    for p in profiles or []:
        codes = ((p.get("detail") or {}).get("checkout_legal_entity_codes") or None)
        if codes:
            entry = tuple(codes)
            if entry not in seen:
                seen.append(entry)
    if len(seen) == 1:
        return list(seen[0]), "entity-consensus"
    if len(seen) > 1:
        # Sibling profiles disagree, so there is no single entity-level answer to copy.
        return None, f"ambiguous-{len(seen)}-distinct-values"
    hint = (entity_detail or {}).get("default_cko_legal_entity")
    return None, (f"no-sibling-profile-reports-one"
                  + (f" (entity default_cko_legal_entity={hint})" if hint else ""))


def profile_create_body(detail, fallback_legal_codes=None, valid_currencies=None):
    """Shape a captured v2 profile detail into a create body.

    The GET is not a create body, and for profiles the gap is more than the usual
    server-assigned fields — see PROFILE_STRING_FIELDS above and the CAID note below.

    Returns (body, notes, warnings).
    """
    body = clean(detail, "processing_profile")
    body["salesforce_case_id"] = SANDBOX_SALESFORCE_CASE_ID
    notes, warns = [], []

    # (0) checkout_legal_entity_codes is REQUIRED on create and absent from older
    # profiles' GETs, so it is supplied from the sibling profiles on the same entity.
    # Without it the create fails with checkout_legal_entity_code_required.
    if not body.get("checkout_legal_entity_codes"):
        codes, conf = fallback_legal_codes or (None, "not-resolved")
        if codes:
            body["checkout_legal_entity_codes"] = list(codes)
            notes.append(f"checkout_legal_entity_codes supplied as {list(codes)} "
                         f"({conf}) — this profile's own GET does not report it, and "
                         f"CAT requires it on create")
        else:
            warns.append(make_flag(
                "legal_entity_codes_unresolved",
                f"profile {detail.get('id')}: checkout_legal_entity_codes is required on "
                f"create but neither this profile nor any sibling on the entity reports "
                f"one ({conf}). The create will fail with "
                f"checkout_legal_entity_code_required — supply it by hand.",
                kind="processing_profile", object_id=detail.get("id"),
                confidence=conf, action="not_supplied"))

    # (a) numbers the GET returns but the create wants as strings
    coerced = []
    for f in PROFILE_STRING_FIELDS:
        if f in body and _as_str(body[f]) != body[f]:
            body[f] = _as_str(body[f]); coerced.append(f)

    # (a2) retired / inactivated currency codes, before anything else looks at them
    if "currencies" in body:
        vc, vscope = valid_currencies or (None, None)
        body["currencies"], cnotes, cdropped = remediate_currencies(
            body["currencies"], valid=vc, scope=vscope)
        notes.extend(cnotes)
        for d in cdropped:
            # Flagged, not fatal: the profile is still created, without that currency.
            warns.append(make_flag(
                "currency_not_available",
                f"profile '{detail.get('name') or detail.get('id')}' will still be created, "
                f"but {d['currency']} cannot be added and is left out of its currencies — "
                f"{d['reason']}",
                kind="processing_profile", object_id=detail.get("id"),
                currency=d["currency"], reason=d["reason"], action="dropped"))

    cs = body.get("custom_settings")
    if isinstance(cs, dict):
        cs = dict(cs)
        for f in PROFILE_STRING_FIELDS_CUSTOM:
            if f in cs and _as_str(cs[f]) != cs[f]:
                cs[f] = _as_str(cs[f]); coerced.append(f"custom_settings.{f}")
        # SE_CCY is Amex-only and nests processing_threshold, which has the same problem.
        # clean() strips only the top level, so nothing else reaches these entries.
        se = cs.get("SE_CCY")
        if isinstance(se, list):
            rows, hit = [], False
            for row in se:
                if isinstance(row, dict):
                    row = dict(row)
                    for f in PROFILE_STRING_FIELDS_CUSTOM:
                        if f in row and _as_str(row[f]) != row[f]:
                            row[f] = _as_str(row[f]); hit = True
                    # An SE_CCY row names a currency too, so the same remediation has to
                    # reach it — a row for a dropped currency goes with it.
                    if "currency" in row:
                        vc, vscope = valid_currencies or (None, None)
                        fixed, rnotes, _ = remediate_currencies(
                            [row["currency"]], valid=vc, scope=vscope)
                        if not fixed:
                            notes.append(f"SE_CCY row for {row['currency']} removed "
                                         f"with the currency")
                            continue
                        if fixed[0] != row["currency"]:
                            notes.extend(rnotes)
                            row["currency"] = fixed[0]
                rows.append(row)
            cs["SE_CCY"] = rows
            if hit:
                coerced.append("custom_settings.SE_CCY[].processing_threshold")
        body["custom_settings"] = cs
    if coerced:
        notes.append("coerced to string for the create (the v2 GET returns these as "
                     "numbers): " + ", ".join(coerced))

    # (b) the card acceptor identification code is assigned BY CAT, per entity — two
    # known-good creates both sent it empty with auto_generate true, and both responses
    # came back with the same 538990 for that entity. Carrying the SOURCE's CAID is the
    # same class of error as carrying its vault account.
    auto = detail.get("auto_generate_card_acceptor_identification_code")
    schemes = {str(s).lower() for s in (detail.get("schemes") or [])}
    forced_caid = next((v for s, v in CAID_BY_SCHEME.items() if s in schemes), None)
    bs = body.get("business_settings")
    if isinstance(bs, list):
        rows = []
        for row in bs:
            if isinstance(row, dict) and row.get("card_acceptor_identification_code"):
                row = dict(row)
                src_caid = row["card_acceptor_identification_code"]
                if forced_caid is not None:
                    row["card_acceptor_identification_code"] = forced_caid
                    notes.append(f"card_acceptor_identification_code set to "
                                 f"{forced_caid!r} (source had {src_caid}) — a fixed "
                                 f"value for {'/'.join(sorted(schemes))}")
                elif auto:
                    row["card_acceptor_identification_code"] = ""
                    notes.append(f"card_acceptor_identification_code blanked (source had "
                                 f"{src_caid}) — CAT assigns it per entity when "
                                 f"auto_generate_card_acceptor_identification_code is true")
                else:
                    # No known-good call covers this combination, so do not guess which
                    # way it goes: say so and let the operator decide.
                    warns.append(make_flag(
                        "source_caid_carried",
                        f"profile {detail.get('id')}: auto_generate_card_acceptor_"
                        f"identification_code is false, so the SOURCE's CAID {src_caid} "
                        f"would be sent to the clone. No known-good create covers this "
                        f"case — verify before applying.",
                        kind="processing_profile", object_id=detail.get("id"),
                        source_caid=src_caid, action="carried"))
            rows.append(row)
        body["business_settings"] = rows

    return body, notes, warns


ALLOW_KEYS = ("allow_any_processing_channel", "allow_any_merchant_category_code",
              "allow_any_processing_currency", "allow_any_event_type",
              "allow_any_card_type", "allow_any_region", "allow_any_banking_partner",
              "allow_any_payment_method")


def is_default_routing_rule(rule):
    """True for the catch-all rule — unrestricted on every condition it declares.

    CAT requires this one to exist before any other rule on the entity, rejecting the
    others with `default_payment_routing_rule_must_exist_before_other_routing_rule_changes`.
    Capture order is CAT's list order, which put a chargeback-scoped rule first and failed.
    """
    present = [k for k in ALLOW_KEYS if k in rule]
    return bool(present) and all(rule.get(k) for k in present)


def rule_label(rule):
    """Routing rules carry no name in CAT, so describe them by what they match."""
    present = [k for k in ALLOW_KEYS if k in rule]
    if present and all(rule.get(k) for k in present):
        desc = "all traffic (unrestricted)"
    elif present:
        narrowed = [k.replace("allow_any_", "").replace("_", " ")
                    for k in present if not rule.get(k)]
        desc = "restricted by " + ", ".join(narrowed)
    elif rule.get("source_identifier"):
        desc = f"source {rule['source_identifier']}"
    else:
        desc = rule.get("id") or "rule"
    rev = rule.get("revenue_currency_account_id")
    return f"{desc} → revenue {rev}" if rev else desc


def resolve_profile_id(processor, profiles):
    """Derive a processor's profile_id — it is NOT readable from any GET.

    Join: processor.acquirer_id == profile.acquirer_key, narrowed by scheme, then
    disambiguated by MCC. Acquirer alone is ambiguous: one acquirer can carry a pay-in
    profile at one MCC and payout profiles at others.

    Deliberately does NOT fall back to profile.processor_key — that value is shared
    across profiles (`cko-apm` covers four; `cko-visa` covers both GB and FR), so
    matching on it yields false positives. A processor whose acquirer_id is a bare
    processor_key is a direct-mode processor and needs no profile at all.

    Returns (source_profile_id | None, confidence).
    """
    acq = processor.get("acquirer_id")
    sch = (processor.get("scheme") or "").lower()
    mcc = processor.get("merchant_category_code")
    mcc = str(mcc) if mcc is not None else None

    def det(p): return p.get("detail") or {}
    def schemes_of(p): return {str(s).lower() for s in (det(p).get("schemes") or [])}

    cands = [p for p in profiles
             if det(p).get("acquirer_key") == acq and sch in schemes_of(p)]
    exact = [p for p in cands if mcc and mcc in profile_mccs(det(p))]
    if len(exact) == 1:
        return exact[0]["id"], "exact"
    if exact:
        return exact[0]["id"], "ambiguous-mcc"
    if len(cands) == 1:
        return cands[0]["id"], "acquirer+scheme"
    if cands:
        return None, f"ambiguous-{len(cands)}-candidates"
    return None, "no-candidate"


def resolve_sessions_processor_link(sp, gwch, profiles):
    """Link a sessions-channel processor to its gateway processor and processing profile.

    Neither id is readable. A sessions processor's GET exposes only acquirer_id, scheme_id,
    merchant_category_code, protocol_versions, mode and processor_type — never the
    `gateway_profile_processor_id` and `processing_profile_id` the create requires, so both
    are derived.

    Joined on **scheme + MCC**, deliberately NOT acquirer_id: the sessions record reports a
    *processor_key* (`cko-visa`, `cko-mc`) while the gateway processor reports an
    *acquirer_key* (`cko_visa_gb`, `cko_mc_gb`), so those values never compare equal.

    Returns (gateway_processor_source_id | None, profile_source_id | None, confidence).
    """
    scheme = str(sp.get("scheme_id") or "").lower()
    mcc = sp.get("merchant_category_code")
    mcc = str(mcc) if mcc is not None else None
    cands = [pr for pr in ((gwch or {}).get("processors") or [])
             if str(pr.get("scheme") or "").lower() == scheme
             and str(pr.get("merchant_category_code")) == mcc]
    if len(cands) != 1:
        return None, None, (f"ambiguous-{len(cands)}-candidates" if cands
                            else "no-gateway-processor-for-scheme+mcc")
    pr = cands[0]
    pid, pconf = resolve_profile_id(pr, profiles)
    return pr.get("id"), pid, f"scheme+mcc, profile={pconf}"


# ---------------------------------------------------------------- plan

def source_vault_id(cap):
    """The source client's vault account id, as named by its channels' vault service.

    Read from the channels rather than a dedicated endpoint because that is the only place
    the capture already has it, and it is the exact value that needs remapping.
    """
    for ent in cap.get("entities") or []:
        for c in ent.get("processing_channels") or []:
            for svc in ((c.get("detail") or {}).get("services") or []):
                if (svc or {}).get("type") == "vault" and svc.get("key"):
                    return svc["key"]
    return None


def build_plan(cap, target_client_name=None):
    steps, skipped, warnings, flags = [], [], [], []
    src_cli = cap["client_id"]
    client = cap.get("client") or {}

    def flag(f):
        """Record a non-halting finding, and mirror its message into warnings."""
        flags.append(f)
        warnings.append(f["message"])

    # Currency codes are validated against CAT's own configuration endpoints wherever
    # they could be read. Where one could not be, say so — silently falling back to the
    # explicit tables alone would look identical to a clean validation, and the tables
    # only know about the retirements someone has already added to them.
    cur_cfg = cap.get("valid_currencies")
    if cur_cfg is None:
        flag(make_flag(
            "currency_validation_unavailable",
            "currency validity was not checked against CAT: this capture carries no "
            "valid_currencies data. The explicit SLL/HRK/ZWL/LBP rules still applied.",
            kind="capture", action="not_checked"))
    else:
        # /payout-routes/configuration returns 400 on every capture and gates nothing:
        # payout routes are not created by the plan, only read back and compared after
        # apply. Flagging it every run was noise (removed 2026-09-04 at Patrick's request);
        # it stays in the capture's `unavailable` list for diagnosis. Any OTHER list being
        # unavailable still matters — those gate profile/processor/account currencies.
        gating = [u for u in cur_cfg.get("unavailable") or []
                  if not u.startswith("/payout-routes/configuration")]
        if gating:
            flag(make_flag(
                "currency_validation_unavailable",
                "currency validity could not be checked against CAT for: "
                + ", ".join(gating)
                + ". The explicit SLL/HRK/ZWL/LBP rules still applied, but a currency CAT "
                  "has retired since would not be caught.",
                kind="capture", endpoints=gating, action="not_checked"))

    def add(kind, method, path, body, provides=None, requires=(), op=None, notes=None,
            entity=None, label=None, parent=None, provides_from=None, retry=None,
            verify=None, optional=False, base=None):
        steps.append({"seq": len(steps) + 1, "kind": kind, "op": op, "method": method,
                      "path": path, "body": body,
                      "provides": provides, "requires": sorted(set(requires)),
                      "notes": notes or [],
                      # provides_from: dotted path to pull the id out of the response when
                      # it is not the top-level `id`. retry: for ids CAT provisions
                      # asynchronously on the target.
                      "provides_from": provides_from, "retry": retry,
                      # verify: for read-only steps that check the clone against the
                      # source at apply time — {"compare": <verifier>, "expected": [...]}.
                      # clone_apply runs the named verifier on the response and records
                      # what differs as run-time flags. Creates nothing.
                      "verify": verify,
                      # optional: an enhancement nothing downstream depends on. If it
                      # fails live, clone_apply records a flag and CONTINUES instead of
                      # halting the run. Never set on anything that provides an id.
                      "optional": bool(optional),
                      # base: an absolute base URL overriding CAT's, for the one service
                      # whose configuration lives on another host (the NT portal). None
                      # means CAT.
                      "base": base,
                      # presentation metadata: which entity this belongs to, a
                      # human label, and the source id of its parent object
                      "entity": entity, "label": label, "parent": parent})

    # 1 — client
    # The clone's name is used again by the risk-settings PUT below, so it is computed
    # once — the two must never diverge.
    clone_name = target_client_name or ((client.get("name") or "clone") + " (clone)")
    add("client", "POST", "/clients", {
        "name": clone_name,
        "email": client.get("email"), "status": client.get("status") or "Active",
    }, provides=src_cli, op="Clients_CreateClient")

    # 1b — client risk settings: the Fraud Detection tier. A new client comes up on the
    # free tier, so a source on `premium` (Fraud Detection Pro) would otherwise silently
    # lose its custom rules, risk profiles and ML threshold control.
    #
    # PUT, not POST: a new client already has default settings, and the one known-good
    # live call is a PUT. That call carried the whole UI echo — id, name, timestamps,
    # _links, two blocks of feature text — but `tier` is the only field the swagger
    # declares (RiskSettingRequest) and the only one that differed, so the body is
    # reduced to the two real settings. If CAT turns out to want an echo back, this note
    # is where to widen it.
    #
    # Placed straight after the client on purpose: if the reduced body is wrong the run
    # halts having created only a client, which can be deactivated.
    rs = cap.get("risk_settings") or {}
    if rs.get("tier"):
        # id and name are the CLONE's: the id as a placeholder resolved at apply time, the
        # name the same value the client step just created. Neither is source data.
        # `name` was learned live — reducing the body to tier alone returned
        # client_name_required. `id` was in the proven body too and costs nothing.
        rs_body = {"id": ph(src_cli), "name": clone_name, "tier": rs["tier"]}
        if "read_only_restriction_enabled" in rs:
            rs_body["read_only_restriction_enabled"] = rs["read_only_restriction_enabled"]
        add("client_risk_settings", "PUT", f"/clients/{ph(src_cli)}/risk-settings",
            rs_body, provides=None, requires=[src_cli],
            op="RiskSettings_UpdateRiskSetting",
            label=f"set Fraud Detection tier to {rs['tier']!r} (as on the source)",
            # A valid body returned 404 ~0.35s after client create, where an invalid one
            # had returned 422 — i.e. validation ran but the resource was not there yet.
            # Either CAT provisions default risk settings asynchronously (the vault
            # account takes ~10s, hence vault_lookup's retry), or the resource must be
            # POSTed first. The retry covers the former and is diagnostic for the latter:
            # a 404 that persists through six attempts rules the race out. PUT is
            # idempotent, so retrying is safe.
            retry={"attempts": 6, "delay_seconds": 3},
            notes=[f"source client is on tier {rs['tier']!r}; a new client defaults to "
                   f"the free tier",
                   "body reduced from a known-good UI PUT to id, name, tier and "
                   "read_only_restriction_enabled — timestamps, _links and the "
                   "*_tier_features text are server-generated and still dropped",
                   "name is REQUIRED (learned live: tier alone returned "
                   "client_name_required) and is the CLONE's name, never the source's; "
                   "id is the clone's own, resolved from the client step",
                   "PUT is idempotent; retried up to 6x3s because a valid body returned "
                   "404 immediately after client create — default risk settings may be "
                   "provisioned asynchronously. If the 404 persists through every attempt "
                   "the resource is not auto-created and this step must become a POST",
                   "NOT reversible by cleanup — it cannot know the prior tier, and the "
                   "client is deactivated anyway"])
    else:
        flag(make_flag(
            "risk_settings_not_captured",
            f"client {src_cli}: risk settings could not be read (no tier in the "
            f"response), so the clone will be left on the default free tier",
            kind="client", object_id=src_cli, action="not_supplied"))

    # 1d — Flow account (hosted checkout), client-level. A new client comes up with it
    # disabled. Known-good live call: PUT /clients/{id}/flow-account {"is_enabled": true}
    # on a NEW client, 200, response minting a fresh `acc_*` id. That id is client-specific
    # and response-only — the body is the flag alone, and the source's `acc_*` must never
    # be sent. The value is copied from the source, not hardcoded, so a source with Flow
    # disabled produces a disabled clone. (An entity-level
    # PUT /entities/{id}/services/flow-account also exists; no channel service type
    # references flow, so it is not driven here.)
    fa = cap.get("flow_account") or {}
    if isinstance(fa.get("is_enabled"), bool):
        add("client_flow_account", "PUT", f"/clients/{ph(src_cli)}/flow-account",
            {"is_enabled": fa["is_enabled"]}, provides=None, requires=[src_cli],
            op="ClientValueAddedServices_SetFlowAccountService",
            label=f"set Flow account is_enabled={fa['is_enabled']} (as on the source)",
            notes=[f"source client's Flow account is_enabled={fa['is_enabled']}; a new "
                   f"client defaults to disabled",
                   "body is the flag alone — the acc_* id in the GET is client-specific "
                   "and response-only, exactly like the vault account",
                   "known-good live PUT on a new client: {\"is_enabled\": true} -> 200",
                   "NOT reversible by cleanup — the client is deactivated anyway"])
    else:
        flag(make_flag(
            "flow_account_not_captured",
            f"client {src_cli}: Flow account could not be read (no is_enabled in the "
            f"response), so the clone keeps Flow disabled",
            kind="client", object_id=src_cli, action="not_supplied"))

    # 1c — client Compass (Dashboard) settings: display currency + conversion currencies.
    #
    # POST, because CreateCompassSettingsRequest carries BOTH fields and matches the GET
    # field-for-field, whereas UpdateCompassSettingsRequest (PUT) carries display_currency
    # only — so POST is the sole route by which conversion_currencies can reach the clone.
    # There is no known-good live write for this; the create schema being fully declared
    # is the mitigating fact, and it sits here at step 3 so a wrong body costs only a
    # client. Two things a 2xx does NOT prove, hence the verify read that follows:
    #   * the clone may already carry default settings, in which case POST may 409 and
    #     conversion_currencies become unreachable by API
    #   * the support site says a client's display currency "cannot be changed" once set,
    #     so it may be immutable and silently keep its default
    # conversion_currencies is a bare currency list, so the global currency rules apply.
    comp = cap.get("compass_settings") or {}
    if comp.get("display_currency") or comp.get("conversion_currencies"):
        vc, vscope = valid_currency_set(cap)
        conv, cnotes, cdropped = remediate_currencies(
            comp.get("conversion_currencies") or [], valid=vc, scope=vscope)
        for d in cdropped:
            flag(make_flag(
                "currency_not_available",
                f"compass settings will still be applied, but {d['currency']} cannot be "
                f"added and is left out of conversion_currencies — {d['reason']}",
                kind="client_compass_settings", object_id=src_cli,
                field="conversion_currencies", currency=d["currency"],
                reason=d["reason"], action="dropped"))
        comp_body = {"conversion_currencies": conv}
        if comp.get("display_currency"):
            comp_body["display_currency"] = comp["display_currency"]
        add("client_compass_settings", "POST", f"/clients/{ph(src_cli)}/compass-settings",
            comp_body, provides=None, requires=[src_cli],
            op="CompassSettings_CreateCompassSettings",
            label=f"set Compass display currency {comp.get('display_currency')!r} and "
                  f"{len(conv)} conversion currencies (as on the source)",
            notes=[f"conversion_currencies: {n}" for n in cnotes] + [
                   "POST, not PUT: the update schema carries display_currency only, so "
                   "POST is the only route for conversion_currencies",
                   "no known-good live write exists for this endpoint — the create schema "
                   "is fully declared and matches the GET, which is the mitigating fact",
                   "a 2xx does not prove the values took: display_currency may be "
                   "immutable once set — see the verify read that follows",
                   "NOT reversible by cleanup — no delete route; the client is "
                   "deactivated anyway"])
        add("client_compass_check", "GET", f"/clients/{ph(src_cli)}/compass-settings",
            None, provides=None, requires=[src_cli],
            op="CompassSettings_GetCompassSettings",
            label="verify Compass settings took on the clone",
            verify={"compare": "compass_settings",
                    "expected": {"display_currency": comp.get("display_currency"),
                                 "conversion_currencies": conv}},
            notes=["read-only; creates nothing",
                   "compares the clone's Compass settings with what was just POSTed — a "
                   "display_currency that did not change is flagged, not failed"])
    else:
        flag(make_flag(
            "compass_settings_not_captured",
            f"client {src_cli}: Compass settings could not be read (no display_currency "
            f"or conversion_currencies in the response), so the clone keeps its defaults",
            kind="client", object_id=src_cli, action="not_supplied"))

    # 2 — resolve the NEW client's vault account.
    #
    # A vault account is CLIENT-level and no two clients share one, so the source's
    # `vact_*` cannot be reused — a channel naming it fails with
    # `required_service_vault_account_has_not_been_enabled`, which is about ownership, not
    # enablement. CAT mints the target's vault account itself, and no create step returns
    # it, so it has to be read back. Registering it under the SOURCE vault id means every
    # channel body can reference it as an ordinary placeholder.
    #
    # It is provisioned asynchronously — measured at ~10s after the client is created,
    # which is ~2s later than the first channel step reached in two live runs — hence the
    # retry.
    src_vault = source_vault_id(cap)
    if src_vault:
        add("vault_lookup", "GET", f"/clients/{ph(src_cli)}/vault-account", None,
            provides=src_vault, requires=[src_cli],
            op="VaultAccounts_GetVaultAccount",
            label=f"resolve the new client's vault account (source {src_vault})",
            retry={"attempts": 6, "delay_seconds": 3},
            notes=["read-only; creates nothing",
                   "vault accounts are client-level and cannot be shared between "
                   "clients, so the source's id is never valid on the clone",
                   "provisioned asynchronously after client create — retried"])
    elif any(e.get("processing_channels") for e in cap["entities"]):
        warnings.append("channels exist but no vault service was found on any captured "
                        "channel; the vault reference cannot be remapped")

    for ent in cap["entities"]:
        eid, det = ent["id"], (ent.get("detail") or {})

        # 2 — entity. Shaped against a known-good create body rather than a raw GET echo.
        ent_body = clean(det, "entity")
        # (a) status is REQUIRED on create — omitting it returns entity_status_required.
        #     Copy the SOURCE entity's status so the clone mirrors it. (A client create
        #     with status Active returns 201, so Active is accepted on create.)
        ent_body["status"] = det.get("status") or ENTITY_STATUS_FALLBACK
        # (b) the create schema has this flag; no GET returns it, so derive it by
        #     comparing the two addresses.
        pba = det.get("principal_business_address") or {}
        rba = det.get("registered_business_address") or {}
        ent_body["is_principal_same_as_registered"] = (pba == rba)
        # (c) `funding` on the GET is a read model ({"can_hold_funds": ...}). The create
        #     schema declares `funding` as an object with NO properties, so its accepted
        #     shape is unknown and a known-good create body omits it. Sending a read-model
        #     object risks a validation failure for a field we cannot verify.
        ent_body.pop("funding", None)
        add("entity", "POST", f"/clients/{ph(src_cli)}/entities",
            ent_body, provides=eid, requires=[src_cli],
            op="Entities_CreateEntity", entity=eid,
            label=det.get("name") or eid,
            notes=[f"status copied from source: {ent_body['status']}",
                   "funding omitted — GET returns a read model and the create schema "
                   "declares no shape for it",
                   "acquiring_providers is write-only in CAT (absent from every GET) — "
                   "cannot be captured; set manually or via PUT after creation."])
        if det.get("funding"):
            skipped.append({"kind": "entity.funding", "entity": eid,
                            "reason": "GET returns a read model; create schema declares "
                                      "no shape. Set via PUT after creation."})
        skipped.append({"kind": "entity.acquiring_providers", "entity": eid,
                        "reason": "write-only field; no GET exposes it"})

        # 3 — currency accounts
        #
        # holding_currency is a bare currency code, so the same global rules apply. A
        # retired code is substituted; a code CKO will not enable means the account
        # cannot be created at all, so the step is dropped and flagged rather than sent
        # to fail. dropped_cas is carried forward because later steps reference these ids.
        dropped_cas = set()
        for ca in ent.get("currency_accounts", []):
            cabody = clean(ca, "currency_account")
            canotes = []
            cur = cabody.get("holding_currency")
            if cur:
                vc, vscope = valid_currency_set(cap, purpose="holding")
                fixed, cnotes, cdropped = remediate_currencies(
                    [cur], valid=vc, scope=vscope)
                canotes += cnotes
                if not fixed:
                    skipped.append({
                        "kind": "currency_account", "entity": eid,
                        "reason": f"holding_currency {cdropped[0]['currency']} cannot "
                                  f"be enabled — {cdropped[0]['reason']}"})
                    flag(make_flag(
                        "currency_not_available",
                        f"currency account '{ca.get('name') or ca.get('id')}': "
                        f"{cdropped[0]['currency']} cannot be added ("
                        f"{cdropped[0]['reason']}), so the account is not created. The "
                        f"rest of the clone continues.",
                        kind="currency_account", entity=eid, object_id=ca.get("id"),
                        currency=cdropped[0]["currency"],
                        reason=cdropped[0]["reason"], action="not_created"))
                    dropped_cas.add(ca.get("id"))
                    continue
                if fixed[0] != cur:
                    cabody["holding_currency"] = fixed[0]
            add("currency_account", "POST", f"/entities/{ph(eid)}/currency-accounts",
                cabody, provides=ca.get("id"), requires=[eid],
                op="CurrencyAccounts_CreateCurrencyAccount", entity=eid, notes=canotes,
                label=f'{cabody.get("holding_currency") or "?"} — {ca.get("name") or "unnamed"}')

        # 4 — processing profiles
        #
        # Resolved once per entity: the legal entity code tracks the entity, so every
        # profile that cannot report its own takes the entity's consensus value.
        legal_codes = resolve_legal_entity_codes(det, ent.get("processing_profiles"))
        for p in ent.get("processing_profiles", []):
            body, pnotes, pwarns = profile_create_body(
                p.get("detail") or {}, fallback_legal_codes=legal_codes,
                valid_currencies=valid_currency_set(
                    cap, acquirer=(p.get("detail") or {}).get("acquirer_key")))
            for f in pwarns:
                flag(f)
            n = list(pnotes)
            if not p.get("v2_ok"):
                n.append("v2 detail unavailable — custom_settings may be incomplete")
                warnings.append(f"profile {p['id']}: v2 detail unavailable; clone fidelity degraded")
            n.append("created via the v2 endpoint — capture reads /processing-profiles/v2/{id}, "
                     "so the body is v2-shaped and the v1 create path rejects it")
            n.append(f"salesforce_case_id stamped as the sandbox placeholder "
                     f"{SANDBOX_SALESFORCE_CASE_ID}, not copied from the source")
            add("processing_profile", "POST", f"/entities/{ph(eid)}/processing-profiles/v2",
                body, provides=p["id"], requires=[eid],
                op="ProcessingProfiles_CreateProcessingProfileV2", notes=n, entity=eid,
                label=profile_label(p.get("detail") or {}))

        # 4b — enable the entity services the source's channels rely on.
        #
        # A channel service is a REFERENCE to something enabled on the entity, not a
        # request to enable it. Remapping the prism key to the clone's client|entity is
        # necessary but not sufficient: the clone's entity has no prism service, and CAT
        # rejects the channel with `invalid_prism_merchant_service` either way.
        #
        # Only prism is emitted. The vault account is provisioned automatically (hence
        # vault_lookup) and flow-account is not something this tool captures, so neither
        # is asserted here. PUT is idempotent, so enabling an already-enabled service is
        # harmless.
        svc_types = set()
        for c in ent.get("processing_channels", []):
            for svc in ((c.get("detail") or {}).get("services") or []):
                if (svc or {}).get("type"):
                    svc_types.add(svc["type"])
        for s in ent.get("sessions_channels", []):
            for svc in (s.get("services") or []):
                if (svc or {}).get("value"):
                    svc_types.add(svc["value"])
        if "prism" in svc_types:
            add("entity_service", "PUT", f"/entities/{ph(eid)}/services/prism",
                {"is_enabled": True}, provides=None, requires=[eid], entity=eid,
                op="Entities_SetEntityPrismServiceStatus",
                label="enable the prism (fraud detection) service",
                notes=["the source's channels reference a prism service, which is "
                       "enabled per ENTITY — the clone's entity has none, so the channel "
                       "create fails with invalid_prism_merchant_service without this",
                       "`prism` is the wire value for MerchantServiceType.FraudDetection",
                       "idempotent: enabling an already-enabled service is a no-op",
                       "NOT reversible by cleanup — there is no removal route for an "
                       "entity service"])

        # 5/6 — channels, then their processors (profile_id must be derived).
        # Processors that cannot be created are collected so their sessions
        # authentication links can be skipped too — the link references the gateway
        # processor, so emitting it would leave an unsatisfiable `requires`.
        skipped_processors = set()
        for c in ent.get("processing_channels", []):
            cbody = clean(c.get("detail") or {}, "processing_channel")
            cbody["salesforce_case_id"] = SANDBOX_SALESFORCE_CASE_ID
            creq = [eid]
            cnotes = []
            # EVERY service key is remapped, not just the vault one — see
            # remap_service_key. A key may be a composite naming several source objects.
            known = service_key_ids(src_cli, src_vault, eid, ent)
            for svc in (cbody.get("services") or []):
                key = (svc or {}).get("key")
                if not isinstance(key, str) or not key:
                    continue
                svc["key"], used = remap_service_key(key, known)
                if used:
                    creq.extend(used)
                    cnotes.append(f"{svc.get('type') or 'service'} service key remapped "
                                  f"to the clone's " + ", ".join(sorted(used)))
                leftover = unmapped_source_ids(svc["key"])
                if leftover:
                    cnotes.append(f"{svc.get('type') or 'service'} service key still "
                                  f"names {', '.join(leftover)} — no step on this plan "
                                  f"creates a counterpart, so it will be sent as-is")
                    flag(make_flag(
                        "service_key_unmappable",
                        f"channel {c['id']}: the {svc.get('type') or 'service'} service "
                        f"key names source object(s) {', '.join(leftover)} that cannot "
                        f"be remapped; CAT will almost certainly reject it",
                        kind="processing_channel", entity=eid, object_id=c["id"],
                        service=svc.get("type"), unmapped=leftover, action="carried"))
            add("processing_channel", "POST", f"/entities/{ph(eid)}/processing-channels",
                cbody, provides=c["id"], requires=creq,
                op="ProcessingChannels_CreateProcessingChannel", entity=eid,
                notes=cnotes,
                label=(c.get("detail") or {}).get("name") or c["id"])

            for pr in c.get("processors", []):
                # A processor that binds no processing profile is a MANUAL processor, and
                # CAT has no API route that creates one: the single create endpoint
                # returns 503 service_unavailable /
                # gateway_manual_processor_creation_not_supported. Emitting the step
                # anyway halted a live run at step 33 of 84, leaving 51 steps
                # unattempted and 32 objects — channels and processors among them —
                # permanently created. So it is flagged and skipped, and the rest of the
                # clone proceeds.
                #
                # Two distinct ways to end up without a profile, worth telling apart in
                # the report: the processor is deliberately direct-mode (inline
                # acquirer_settings, acquirer_id is a bare processor_key), or the profile
                # join failed.
                if pr.get("acquirer_settings"):
                    pid, conf = None, "direct-mode (inline acquirer_settings)"
                else:
                    pid, conf = resolve_profile_id(pr, ent.get("processing_profiles", []))
                if not pid:
                    reason = (f"binds no processing profile ({conf}); CAT cannot create a "
                              f"manual processor — the create endpoint returns "
                              f"gateway_manual_processor_creation_not_supported")
                    skipped.append({"kind": "processor", "entity": eid,
                                    "channel": c["id"], "processor": pr.get("id"),
                                    "reason": reason})
                    flag(make_flag(
                        "manual_processor_not_creatable",
                        f"processor '{pr.get('name') or pr.get('id')}' on channel "
                        f"{c['id']}: {reason}. Not created; the clone will be missing it.",
                        kind="processor", entity=eid, object_id=pr.get("id"),
                        channel=c["id"], scheme=pr.get("scheme"),
                        merchant_category_code=pr.get("merchant_category_code"),
                        acquirer_id=pr.get("acquirer_id"), confidence=conf,
                        action="not_created"))
                    skipped_processors.add(pr.get("id"))
                    continue

                body = clean(pr, "processor")
                req = [c["id"], pid]
                body["profile_id"] = ph(pid)
                notes = [f"profile_id derived ({conf}) — not readable from any GET"]
                # A processor carries currency codes too, under TWO different field
                # names. The global currency rules apply to both.
                for f in ("currencies", "processing_currencies"):
                    if f in body:
                        vc, vscope = valid_currency_set(
                            cap, acquirer=pr.get("acquirer_id"))
                        body[f], pcn, pcd = remediate_currencies(
                            body[f], valid=vc, scope=vscope)
                        notes += [f"{f}: {n}" for n in pcn]
                        for d in pcd:
                            flag(make_flag(
                                "currency_not_available",
                                f"processor '{pr.get('name') or pr.get('id')}' will still "
                                f"be created, but {d['currency']} cannot be added and is "
                                f"left out of {f} — {d['reason']}",
                                kind="processor", entity=eid, object_id=pr.get("id"),
                                field=f, currency=d["currency"], reason=d["reason"],
                                action="dropped"))
                add("processor", "POST",
                    f"/processing-channels/{ph(c['id'])}/processors", body,
                    provides=pr.get("id"), requires=req,
                    op="Processors_CreateProcessor", notes=notes, entity=eid,
                    label=pr.get("name") or pr.get("id"), parent=c["id"])

        # 7 — sessions channels (CAT has a native clone-from-gateway-channel operation).
        # A sessions channel SHARES the gateway channel's id, so it must NOT be registered
        # in the id map: doing so overwrites the gateway channel's mapping and any later
        # reference (e.g. a routing rule naming a channel) would silently bind to the
        # wrong object. provides is deliberately None.
        for s in ent.get("sessions_channels", []):
            gw = s.get("gateway_processing_channel_id") or s.get("id")
            sreq = [eid, gw]
            snotes = ["uses CAT's native clone operation rather than rebuilding",
                      "shares the gateway channel's id — intentionally not id-mapped",
                      "services read from the channel DETAIL — the list endpoint omits it "
                      "and CAT rejects the create with services_required"]
            # Same vault remap as a gateway channel, but this schema spells the service
            # type `value`, not `type`.
            svcs = []
            sknown = service_key_ids(src_cli, src_vault, eid, ent)
            for svc in (s.get("services") or []):
                svc = dict(svc)
                key = svc.get("key")
                if isinstance(key, str) and key:
                    svc["key"], used = remap_service_key(key, sknown)
                    if used:
                        sreq.extend(used)
                        snotes.append(f"{svc.get('value') or 'service'} service key "
                                      f"remapped to the clone's "
                                      + ", ".join(sorted(used)))
                    leftover = unmapped_source_ids(svc["key"])
                    if leftover:
                        snotes.append(f"{svc.get('value') or 'service'} service key "
                                      f"still names {', '.join(leftover)}")
                        flag(make_flag(
                            "service_key_unmappable",
                            f"sessions channel {s.get('id')}: the "
                            f"{svc.get('value') or 'service'} service key names source "
                            f"object(s) {', '.join(leftover)} that cannot be remapped",
                            kind="sessions_channel", entity=eid, object_id=s.get("id"),
                            service=svc.get("value"), unmapped=leftover,
                            action="carried"))
                svcs.append(svc)
            if not svcs:
                snotes.append("NO SERVICES on the source sessions channel — CAT requires "
                              "at least one; this step will fail")
                warnings.append(f"sessions channel {s.get('id')}: no services captured; "
                                f"create requires them")
            add("sessions_channel", "POST",
                f"/entities/{ph(eid)}/sessions-processing-channels",
                {"gateway_processing_channel_id": ph(gw),
                 "services": svcs,
                 "salesforce_case_id": SANDBOX_SALESFORCE_CASE_ID},
                provides=None, requires=sreq, entity=eid,
                label=f"sessions layer for {gw}", parent=gw,
                op="SessionsProcessingChannels_CloneGatewayProcessingChannelToSessions",
                notes=snotes)

            # Creating the sessions channel does NOT link its processors to the gateway's
            # processors and processing profiles — that is a separate call per processor.
            # Note the path is `session-processing-channels` (singular), unlike the
            # `sessions-processing-channels` path used to create the channel above.
            gwch = next((c for c in ent.get("processing_channels", [])
                         if c["id"] == gw), None)
            for sp in (s.get("processors") or []):
                sp_id = sp.get("id")
                if sp.get("processor_type") != "profile":
                    skipped.append({"kind": "sessions_profile_processor", "entity": eid,
                                    "reason": f"processor_type={sp.get('processor_type')} "
                                              f"is not profile-backed, so it cannot be "
                                              f"linked with createType=existing"})
                    flag(make_flag(
                        "sessions_processor_not_profile_backed",
                        f"sessions processor {sp_id}: processor_type="
                        f"{sp.get('processor_type')} is not profile-backed, so it cannot "
                        f"be linked with createType=existing. Authentication will not be "
                        f"wired for {sp.get('scheme_id')}/"
                        f"{sp.get('merchant_category_code')}.",
                        kind="sessions_profile_processor", entity=eid, object_id=sp_id,
                        processor_type=sp.get("processor_type"), action="not_created"))
                    continue
                pr_id, pp_id, conf = resolve_sessions_processor_link(
                    sp, gwch, ent.get("processing_profiles", []))
                # Checked FIRST. A manual gateway processor also fails the profile join,
                # so the generic "link unresolved" branch below would otherwise mask the
                # real and far more actionable reason: its gateway processor does not
                # exist on the clone.
                if pr_id and pr_id in skipped_processors:
                    skipped.append({"kind": "sessions_profile_processor", "entity": eid,
                                    "reason": f"its gateway processor {pr_id} was not "
                                              f"created (manual processor), so the "
                                              f"authentication link cannot be made"})
                    flag(make_flag(
                        "sessions_link_orphaned",
                        f"sessions processor {sp_id}: not created — its gateway "
                        f"processor {pr_id} could not be created, so authentication is "
                        f"not wired for "
                        f"{sp.get('scheme_id')}/{sp.get('merchant_category_code')}",
                        kind="sessions_profile_processor", entity=eid, object_id=sp_id,
                        gateway_processor=pr_id, scheme=sp.get("scheme_id"),
                        merchant_category_code=sp.get("merchant_category_code"),
                        action="not_created"))
                    continue
                if not pr_id or not pp_id:
                    # Emitting a step whose ids cannot resolve would fail mid-run and
                    # leave a partial clone; record it instead.
                    skipped.append({"kind": "sessions_profile_processor", "entity": eid,
                                    "reason": f"could not derive the link for {sp_id} "
                                              f"({conf}); scheme={sp.get('scheme_id')} "
                                              f"mcc={sp.get('merchant_category_code')}"})
                    flag(make_flag(
                        "sessions_link_unresolved",
                        f"sessions processor {sp_id}: link unresolved ({conf}) — "
                        f"authentication will not be wired for "
                        f"{sp.get('scheme_id')}/{sp.get('merchant_category_code')}",
                        kind="sessions_profile_processor", entity=eid, object_id=sp_id,
                        scheme=sp.get("scheme_id"),
                        merchant_category_code=sp.get("merchant_category_code"),
                        confidence=conf, action="not_created"))
                    continue
                add("sessions_profile_processor", "POST",
                    f"/session-processing-channels/{ph(gw)}/session-profile-processors",
                    {"createType": "existing",
                     "gateway_profile_processor_id": ph(pr_id),
                     "processing_profile_id": ph(pp_id),
                     "salesforce_case_id": SANDBOX_SALESFORCE_CASE_ID,
                     "scheme": sp.get("scheme_id"),
                     "merchant_category_code": str(sp.get("merchant_category_code")),
                     "versions": sp.get("protocol_versions") or []},
                    provides=sp_id, requires=[eid, gw, pr_id, pp_id], entity=eid,
                    parent=gw,
                    label=f"auth link {sp.get('scheme_id')}/"
                          f"{sp.get('merchant_category_code')}",
                    op="SessionsProfileProcessors_CreateSessionProfileProcessor",
                    notes=[f"gateway_profile_processor_id and processing_profile_id both "
                           f"derived ({conf}) — neither is readable from any GET",
                           "joined on scheme + MCC, not acquirer: the sessions record "
                           "carries a processor_key while the gateway processor carries "
                           "an acquirer_key"])

        # 8/9 — routing rules last: they reference channels AND currency accounts
        for kind, src_key, seg, op in (
                ("payment_routing_rule", "payment_routing", "payment-routing-rules",
                 "PaymentRoutingRules_CreatePaymentRoutingRule"),
                ("payout_routing_rule", "payout_routing", "payout-routing-rules",
                 "PayoutRoutingRules_CreatePayoutRoutingRule")):
            # The catch-all rule must be created first — see is_default_routing_rule.
            # sorted() is stable, so the remaining rules keep CAT's own order.
            rules = sorted(ent.get(src_key, []),
                           key=lambda r: 0 if is_default_routing_rule(r) else 1)
            # Only asserted for PAYMENT routing — that is where the constraint was observed
            # live. Whether payout routing enforces the same thing is untested, so it does
            # not raise a warning it cannot justify.
            if kind == "payment_routing_rule" and rules \
                    and not is_default_routing_rule(rules[0]):
                warnings.append("payment_routing_rule: no catch-all rule on the source "
                                "entity, so the default cannot be created first and CAT "
                                "will reject the others")
            emitted, emitted_default = 0, 0
            for rule in rules:
                # A rule naming a currency account that could not be created cannot be
                # created either, and no substitute can be derived without guessing which
                # account the operator would have wanted. Drop it and flag it: emitting
                # the step would fail mid-run and leave a partial clone.
                orphan = [rule.get(f) for f in ("revenue_currency_account_id",
                                                "fees_currency_account_id")
                          if rule.get(f) in dropped_cas]
                if orphan:
                    skipped.append({
                        "kind": kind, "entity": eid,
                        "reason": f"references currency account(s) "
                                  f"{', '.join(sorted(set(orphan)))}, which were not "
                                  f"created because their currency cannot be enabled"})
                    flag(make_flag(
                        "routing_rule_orphaned",
                        f"{kind} '{rule_label(rule)}': not created — it references a "
                        f"currency account that could not be created. Routing will "
                        f"differ from the source.",
                        kind=kind, entity=eid, object_id=rule.get("id"),
                        currency_accounts=sorted(set(orphan)), action="not_created"))
                    continue
                body = clean(rule, "routing_rule")
                req = [eid]
                for f in ("processing_channel_id", "revenue_currency_account_id",
                          "fees_currency_account_id", "source_identifier"):
                    v = body.get(f)
                    if isinstance(v, str) and v.startswith(("pc_", "ca_", "pp_")):
                        body[f] = ph(v); req.append(v)
                rnotes = ["catch-all rule — ordered first because CAT requires the default "
                          "to exist before any other rule on the entity"] \
                    if is_default_routing_rule(rule) else []

                # A scoped rule narrows on currency codes, so the global currency rules
                # apply here too. If remediation empties the list, the rule's scope has
                # become nothing — sending `allow_any_processing_currency: false` with an
                # empty list would match no traffic, so the rule is dropped and flagged
                # instead of created in a state that silently does nothing.
                scope_emptied = None
                for f in ("processing_currencies", "currencies"):
                    if not body.get(f):
                        continue
                    vc, vscope = valid_currency_set(cap)
                    fixed, fnotes, fdropped = remediate_currencies(
                        body[f], valid=vc, scope=vscope)
                    rnotes += [f"{f}: {n}" for n in fnotes]
                    if not fixed:
                        scope_emptied = (f, fdropped)
                        break
                    body[f] = fixed
                    for d in fdropped:
                        flag(make_flag(
                            "currency_not_available",
                            f"{kind} '{rule_label(rule)}' will still be created, but "
                            f"{d['currency']} cannot be added and is left out of {f} — "
                            f"{d['reason']}",
                            kind=kind, entity=eid, object_id=rule.get("id"),
                            field=f, currency=d["currency"], reason=d["reason"],
                            action="dropped"))
                if scope_emptied:
                    f, fdropped = scope_emptied
                    lost = [d["currency"] for d in fdropped]
                    skipped.append({
                        "kind": kind, "entity": eid, "rule": rule.get("id"),
                        "reason": f"every currency in {f} ({', '.join(lost)}) is "
                                  f"unavailable, so the rule would be created with an "
                                  f"empty scope and match nothing"})
                    flag(make_flag(
                        "routing_rule_scope_emptied",
                        f"{kind} '{rule_label(rule)}': not created — every currency in "
                        f"{f} ({', '.join(lost)}) is unavailable, so the rule would "
                        f"match no traffic",
                        kind=kind, entity=eid, object_id=rule.get("id"),
                        field=f, currencies=lost, action="not_created"))
                    continue

                add(kind, "POST", f"/entities/{ph(eid)}/{seg}", body,
                    provides=rule.get("id"), requires=req, op=op, entity=eid,
                    notes=rnotes, label=rule_label(rule))
                emitted += 1
                emitted_default += 1 if is_default_routing_rule(rule) else 0

            # If the catch-all was itself dropped above, every remaining payment rule
            # will be rejected — CAT requires the default to exist first. Worth saying
            # explicitly, because the cause (a currency account that could not be
            # created) is three steps removed from the error CAT would return.
            if kind == "payment_routing_rule" and emitted and not emitted_default:
                flag(make_flag(
                    "catch_all_routing_rule_missing",
                    "payment_routing_rule: no catch-all rule will be created, so CAT "
                    "will reject the remaining rules with default_payment_routing_rule_"
                    "must_exist_before_other_routing_rule_changes",
                    kind="payment_routing_rule", entity=eid, action="not_created"))

        # 10 — payout settings / routes
        for s in ent.get("payout_settings", []):
            # The detail response carries no `id`, and nothing downstream references a
            # payout setting, so it provides no id-map entry.
            body = clean(s, "payout_setting")
            preq = [eid]
            pnotes = []
            # clean() strips only the TOP level, so both nested objects were being POSTed
            # with their own server-assigned ids, timestamps and version — and, worse, with
            # SOURCE-client references: payout_schedule.currency_account_ids named the
            # source's ca_*, and payment_instrument.vault_account_id the source's vault.
            # That is what CAT rejected as invalid_client_settlement_currency_accounts.
            pi = dict(body.get("payment_instrument") or {})
            for k in ("id", "date_created", "date_modified", "e_tag"):
                pi.pop(k, None)
            if pi.get("vault_account_id") and pi["vault_account_id"] == src_vault:
                pi["vault_account_id"] = ph(src_vault)
                preq.append(src_vault)
                pnotes.append("payment_instrument.vault_account_id remapped to the "
                              "clone's vault account")
            if pi:
                body["payment_instrument"] = pi

            sch = dict(body.get("payout_schedule") or {})
            for k in ("id", "date_created", "date_modified", "version"):
                sch.pop(k, None)
            # Only intraday schedules may carry scheduler_ids, and CAT rejects the field
            # even when it is an empty array
            # (`schedule_configuration_only_intraday_schedules_can_have_schedule_ids`).
            # This source schedule is settlement_type=Legacy with scheduler_ids=[].
            if not sch.get("scheduler_ids"):
                sch.pop("scheduler_ids", None)
            cas = []
            for ca in (sch.get("currency_account_ids") or []):
                if ca in dropped_cas:
                    # The account does not exist on the clone, so its placeholder could
                    # never resolve. Unlike a routing rule this is a list, so the setting
                    # survives without it.
                    pnotes.append(f"currency account {ca} omitted from the payout "
                                  f"schedule — it was not created")
                    continue
                if isinstance(ca, str) and ca.startswith("ca_"):
                    cas.append(ph(ca)); preq.append(ca)
                else:
                    cas.append(ca)
            if cas:
                sch["currency_account_ids"] = cas
                pnotes.append(f"payout_schedule.currency_account_ids remapped "
                              f"({len(cas)} account(s))")
            if sch:
                body["payout_schedule"] = sch

            add("payout_setting", "POST", f"/entities/{ph(eid)}/payout-settings",
                body, provides=None, requires=preq, entity=eid,
                label=(s.get("payout_schedule") or {}).get("name") or "payout instruction",
                op="PayoutSettings_CreatePayoutSetting",
                notes=pnotes + ["bank details are carried through from the CAT read — "
                                "check them against the target before applying"])
        # 11 — payout routes: PARITY CHECK, never a create.
        #
        # A payout route is a supported payout corridor available to the entity — country
        # + currency + scheme — provisioned centrally and backed by a dimension table. It
        # is capability, not merchant configuration, and it is not a hard prerequisite for
        # a payout_setting (schedule and instrument are separate resources); CAT consults
        # it when validating a payout destination and returns route_not_found otherwise.
        # POSTing routes would risk enabling corridors the source has disabled or that
        # are centrally controlled. So the clone READS its own enabled corridors once the
        # entity exists, compares them with the source's, and flags what is missing for
        # the operator to raise — it does not try to fix it.
        expected = []
        for rt in ent.get("payout_routes", []):
            n = normalise_payout_route(rt)
            if n:
                expected.append(n)
            else:
                flag(make_flag(
                    "payout_route_unrecognised",
                    f"entity {eid}: a payout route item could not be read "
                    f"({sorted((rt or {}).keys()) if isinstance(rt, dict) else type(rt).__name__}); "
                    f"it is excluded from the parity check",
                    kind="payout_route", entity=eid, action="not_checked"))
        if expected:
            add("payout_route_check", "GET",
                f"/entities/{ph(eid)}/payout-routes?enabled=true", None,
                provides=None, requires=[eid], entity=eid,
                op="PayoutRoutes_GetPayoutRoutes",
                label=f"verify {len(expected)} payout corridor(s) exist on the clone",
                verify={"compare": "payout_routes", "expected": expected},
                notes=["read-only; creates nothing",
                       "payout routes are provisioned capability (backed by "
                       "dim_payout_routes), not merchant configuration — they are never "
                       "POSTed by this tool",
                       "the clone's enabled corridors are compared with the source's at "
                       "apply time; any the source has and the clone lacks are flagged "
                       "for the operator to raise with the Payouts team",
                       "not a hard prerequisite for payout_setting — CAT consults the "
                       "route when validating a payout destination (route_not_found)"])

    def _network_tokens_steps():
        """Network tokens are NOT cloned — flagged as a manual step. See inside."""
        # 11 — Network Tokens: FLAGGED, NOT CREATED. Decision taken 2026-09-04 after the
        # live write path was exercised end to end:
        #   * CAT's /clients/{id}/network-tokens and the NT portal are separate stores;
        #     only the portal's counts, and CAT's POST (201) never reached it.
        #   * The portal accepts the CAT token and a known-good portal POST exists — but it
        #     requires scheme_configuration.identification_value (the business identifier,
        #     e.g. VAT number) when onboarding Visa and not VAT-exempt, and that field is
        #     exposed on NO form this tool can read. Inventing it was rejected.
        #   * VAS pricing (NT Provisioning + Update fees) must exist on every entity first;
        #     pricing is excluded from this tool by policy.
        # So the operator configures it by hand. The source's configuration is still READ
        # (from the portal, since CAT returns only the blank template) so the flag can say
        # exactly what to replicate. network_tokens_body is still used to build that
        # summary, so the shape knowledge stays exercised for the day this is revisited.
        nt_vals = network_token_form_values(cap.get("network_tokens"))
        default_eid = (nt_vals or {}).get("default_entity_id")
        captured = {e["id"]: e for e in cap["entities"]}
        if nt_vals is not None and default_eid in captured:
            det = captured[default_eid].get("detail") or {}
            summary = network_tokens_body(nt_vals, default_eid)
            sc = summary.get("scheme_configuration") or {}
            to_replicate = {
                "nt_state": summary.get("nt_state"),
                "default_billed_entity": det.get("name") or default_eid,
                "provisioning_state": summary.get("provisioning_state"),
                "default_provision_mode": summary.get("default_provision_mode"),
                "onboard_visa": sc.get("onboard_visa"),
                "onboard_mastercard": sc.get("onboard_mastercard"),
                "trade_name": sc.get("trade_name"),
                "legal_name": sc.get("legal_name"),
                "identification_type": sc.get("identification_type"),
            }
            skipped.append({"kind": "client_network_tokens",
                            "reason": "network tokens are configured by hand — "
                                      "identification_value is required by the portal "
                                      "and unreadable on the source"})
            flag(make_flag(
                "network_tokens_manual",
                # The source's settings travel on the flag as source_configuration; the
                # message itself is the one-line instruction Patrick asked for.
                "NETWORK TOKEN SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT",
                kind="client_network_tokens", object_id=src_cli,
                default_entity_id=default_eid, source_configuration=to_replicate,
                source=cap.get("network_tokens_source"), action="not_created"))
        elif nt_vals is None:
            flag(make_flag(
                "network_tokens_not_captured",
                f"client {src_cli}: network tokens form could not be read — check the "
                f"source's NT portal page and configure the clone by hand if it is enabled",
                kind="client", object_id=src_cli, action="not_supplied"))
        elif default_eid is None:
            portal_err = next((e for e in (cap.get("_meta") or {}).get("errors", [])
                               if "configurations" in str(e.get("path", ""))), None)
            why = (f"the NT portal ({NT_PORTAL_BASE}) was also tried and "
                   + (f"returned HTTP {portal_err.get('code') or portal_err.get('error')}"
                      if portal_err else "returned no populated form either"))
            flag(make_flag(
                "network_tokens_default_entity_unreadable",
                f"network tokens: CAT's /network-tokens returns only the blank create "
                f"template (no value for default_entity_id); {why}. Whether the source has "
                f"network tokens enabled could not be determined — check by hand. Fields "
                f"that did carry a value: {', '.join(sorted(nt_vals)) or 'none'}",
                kind="client", object_id=src_cli, fields_with_values=sorted(nt_vals),
                source=cap.get("network_tokens_source"),
                portal_error=portal_err, action="not_created"))
        else:
            flag(make_flag(
                "network_tokens_default_entity_not_in_scope",
                f"network tokens: the source's default billed entity "
                f"{nt_vals.get('default_entity_id')} is not part of this capture (scoped "
                f"run?) — configure by hand if needed",
                kind="client", object_id=src_cli,
                default_entity_id=nt_vals.get("default_entity_id"), action="not_created"))

    _network_tokens_steps()

    # not attempted in this pass — recorded explicitly rather than silently omitted
    for k, why in (("pay_to_card_entity", "GET /pay-to-card-entity returns 503"),
                   ("pay_to_card_schemes", "depends on pay-to-card entity profile"),
                   ("pricing_profiles", "commercially sensitive; excluded by policy"),
                   ("access_keys / public_keys", "credentials — must be created by hand"),
                   ("processing_region",
                    "client-level; not captured in this pass"),
                   ("real_time_account_updater",
                    "client-level value-added service; not captured or enabled by this "
                    "tool — the clone will not have RTAU until it is configured by hand"),
                   ("entity risk_settings",
                    "GET/PUT /entities/{id}/risk-settings exists but its shape is "
                    "unknown and there is no known-good write; client-level tier IS "
                    "applied")):
        skipped.append({"kind": k, "reason": why})

    # Two client-level services this tool can neither read nor write (no CAT endpoint for
    # RTAU; Intelligent Acceptance is out of scope). Raised on EVERY plan as manual steps,
    # in the same one-line form as the network-tokens flag, so the handover always lists
    # them — a skipped[] entry alone was too easy to miss.
    flag(make_flag("rtau_manual",
                   "RTAU SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT",
                   kind="client", object_id=src_cli, action="not_created"))
    flag(make_flag("intelligent_acceptance_manual",
                   "IA SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT",
                   kind="client", object_id=src_cli, action="not_created"))

    return {
        "plan_version": PLAN_VERSION,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"client_id": src_cli, "client_name": client.get("name"),
                   "entities": [e["id"] for e in cap["entities"]]},
        "target": {"client_name": target_client_name, "mode": "new_client"},
        "counts": _counts(steps),
        "steps": steps,
        "skipped": skipped,
        "warnings": warnings,
        # Structured, non-halting findings — the raw material for the post-run report.
        # Every entry's message also appears in `warnings`.
        "flags": flags,
        "capture_meta": cap.get("_meta", {}),
    }


def _counts(steps):
    out = {}
    for s in steps:
        out[s["kind"]] = out.get(s["kind"], 0) + 1
    return dict(sorted(out.items()))


def validate_plan(plan):
    """Structural checks on a plan. Returns a list of problems.

    1. every `requires` must be satisfied by an EARLIER step
    2. no source id may be provided twice — two steps claiming the same id means the
       second overwrites the first in the id map, and later references silently bind to
       the wrong object (this is how the sessions-channel/gateway-channel shared-id bug
       manifested)
    """
    seen, problems = {}, []
    for s in plan["steps"]:
        for r in s["requires"]:
            if r not in seen:
                problems.append(f"step {s['seq']} ({s['kind']}) requires {r}, "
                                f"which no earlier step provides")
        p = s.get("provides")
        if p:
            if p in seen:
                problems.append(f"step {s['seq']} ({s['kind']}) provides {p}, already "
                                f"provided by step {seen[p]} — id-map collision")
            seen[p] = s["seq"]
    for w in plan.get("warnings", []):
        problems.append("warning: " + w)
    return problems
