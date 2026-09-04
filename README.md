# CKO Client Clone

Copies one client's entire Checkout.com CAT configuration into a **brand-new client**. It
reads the source, emits an ordered plan of HTTP calls that would rebuild that configuration,
lets you review and dry-run it, then executes it.

Sandbox only.

> ## This application writes, and CAT has no rollback
>
> Every path defaults to not writing. A live run needs `dry_run` explicitly false **and**
> `confirm == "CLONE"` **and** a token. Read *What cleanup can undo* below before running
> anything live: **processing channels and processors can be neither deleted nor
> deactivated**, so every live run leaves a permanent record in the sandbox.

---

## Quick start

```bash
python3 app/server.py     # http://localhost:8788
```

| Field | Notes |
|---|---|
| Client ID | prefills from `app/dev-creds.json` — this is the **source** client |
| **CAT API bearer token** | **paste this** — short-lived Okta token, expires in ~1h |
| Sandbox Secret Key | optional, typed each session — `sk_sbox_…`; used only by steps that call the Checkout sandbox API |
| Sandbox Public Key | optional, typed each session — `pk_sbox_…`; same, for endpoints that take a public key |

The page is the pipeline as gates:

**Load entities → Capture source config → review plan (downloadable JSON) → Dry run →
Apply → Verify → Clean up**

After **Load entities**, tick the entities you want cloned (all are ticked by default; "all"
and "none" shortcuts sit above the list). Scope a first run to one entity. Same information,
much smaller blast radius — and that matters, because most of what a clone creates cannot be
deleted afterwards. A ticked entity that CAT does not return refuses the capture outright
rather than quietly planning a smaller clone.

Cloning one entity of the reference client produces **33 steps** and runs in under a minute.

### Requirements

Python **3.9+** and nothing else — pure standard library. No dependencies to install.

### Tests

```bash
python3 tests/test_plan.py        # ~200 assertions, well under a second
python3 tests/mutation_check.py   # proves those assertions have teeth
```

No token, no network, no writes. The suite asserts on the plan document and on a dry run:
step and kind counts, dependency ordering, `requires`/`provides` integrity, that every
placeholder resolves and none survives, that synthetic ids match CAT's id shape, one test
per trap below, that each derivation refuses rather than guesses, and that a live run
still needs three independent things. `mutation_check.py` reintroduces each fixed bug into
a throwaway copy of the repo and fails if no test notices. Every fixed bug has a mutant;
the count grows with each live failure diagnosed.

The fixtures are **synthetic** (`tests/fixtures.py`), hand-built to the reference client's
shape rather than recorded from it, so a pass proves the builder is self-consistent, not
that CAT accepts the body. **A known-good live call is still the only authority for a
create body.**

---

## The plan model

The design turns on **the plan being a reviewable data document**, not a function that does
things. A plan is an ordered list of steps, each one HTTP call, each declaring:

- `provides` — the **source** id this call creates a counterpart for
- `requires` — the source ids its body depends on

Bodies carry source ids wrapped as mustache placeholders, `{{ent_abc…}}`, resolved at apply
time through an id map built from real response ids. Keeping the *source* id visible makes the
plan self-documenting: you can read any step and see which source object each reference points
at. `provides`/`requires` make it **checkable before anything is created** — `validate_plan`
proves every `requires` is satisfied by an *earlier* step and that no source id is provided
twice.

Two optional step fields exist for ids CAT mints on the target that no create call returns:
`provides_from` (dotted path into the response) and `retry` (`{attempts, delay_seconds}`).
Two more govern how a step behaves: `verify` (a read compared with the source at apply
time — differences become run-time flags) and **`optional`** — an enhancement nothing
downstream depends on, whose live failure is recorded as an `optional_step_failed` flag
while the run **continues**. Never set on a step that provides an id.

The journal header carries `plan_flags`, `plan_skipped` and `plan_counts`, so a run that
completed every step is still auditable for what the plan chose not to attempt.

### Step order

18 step kinds, in dependency order:

```
client → client_risk_settings → client_flow_account → client_compass_settings →
client_compass_check → vault_lookup → entity → currency_account → processing_profile →
entity_service → processing_channel → processor → sessions_channel →
sessions_profile_processor → payment_routing_rule → payout_routing_rule →
payout_setting → payout_route_check
```

**Network tokens are not cloned — every plan flags them as a manual step**
(`network_tokens_manual`, carrying the source's settings to replicate). The write path was
exercised end to end before deciding this, and the facts are worth keeping: CAT's
`/clients/{id}/network-tokens` and the NT portal
(`nt-portal.sbox.checkout.internal/vault-nt-portal/cat/configurations/{id}`) are **two
separate stores** — CAT's POST returns 201 into a store the portal never shows, and CAT's GET
returns only the blank template even for a configured client. The portal accepts the CAT
bearer token and a known-good portal POST exists (nested `NetworkTokenConfigurationRequest`
shape), but it **requires `identification_value`** (the business identifier) when onboarding
Visa and not VAT-exempt, and that field is exposed on no readable form. Inventing it was
rejected. The capture still reads the portal — the one read outside CAT — so the flag can say
exactly what the source has; `clone_apply` keeps the `base` override and the `network_tokens`
verifier for the day this is revisited.

`client_flow_account` copies the source's Flow (hosted checkout) `is_enabled` flag by PUT —
the one client-level step with a known-good live call *on a new client*. The `acc_*` id in
the response is client-specific and never sent, exactly like the vault account.

`client_compass_settings` POSTs the source's Compass (Dashboard) display currency and
conversion currencies — POST because the update schema carries `display_currency` only.
It is immediately read back (`client_compass_check`) because a 2xx does not prove the
values took: the support site says a client's display currency cannot be changed once
set, so a clone that came up with a default may keep it. A difference is flagged, not
failed.

`client_risk_settings` applies the source's Fraud Detection tier by PUT straight after the
client is created — a new client defaults to the free tier, so a `premium` source would
otherwise silently lose its custom rules and ML threshold control. The body is the four
proven fields — `id` (the clone's, as a placeholder), `name` (the clone's), `tier`,
`read_only_restriction_enabled`; timestamps, `_links` and the feature text are still dropped.
`name` was learned live: reducing the body to `tier` alone returned `client_name_required`.
With a valid body the PUT then returned **404 immediately after client create**, so it now
retries like `vault_lookup` (6 × 3s) on the reading that CAT provisions default risk
settings asynchronously. **Unconfirmed:** the contract has separate create (`POST`) and
update (`PUT`) operations, so the resource may instead never exist until created — a 404
that persists through every retry means the step must become a `POST`. It sits at step 2
on purpose: if its body is ever wrong, the run halts having created only a client.

`entity_service` is the one PUT a plan emits: a channel's `services[]` *references*
something enabled on the entity rather than enabling it, so a source channel carrying a
`prism` (fraud detection) service needs `PUT /entities/{id}/services/prism` first or the
channel create fails with `invalid_prism_merchant_service`.

---

## Safety model

Everything defaults to not writing, and the defaults are layered rather than trusted once.

- **`dry_run=True` is the default** in both `apply_plan` and `run_cleanup`. In dry-run **no
  socket is opened at all** — placeholders resolve against synthetic ids that mimic real CAT
  id shape (`prefix_` + 26 chars), so any length or pattern validation downstream is genuinely
  exercised.
- **A live apply requires three independent things**: `dry_run` explicitly `false`,
  `confirm == "CLONE"`, and a token. Missing any one returns an error instead of writing.
  Cleanup has the same gate with `confirm == "CLEANUP"`.
- **Verbs are allowlisted to `{GET, POST, PUT}`.** `DELETE` is deliberately excluded, so a
  plan can never destroy anything — removal is `clone_cleanup.py`'s job, behind its own gate.
- **Unresolved placeholders are a hard stop.** If a resolved path or body still contains
  `{{`, the step is not sent and the run halts.
- **A lookup that resolves nothing fails the run** rather than warning and continuing, so the
  error names the real cause instead of surfacing a confusing unresolved placeholder later.
- **Journal first, always.** One JSON line per step, `flush()` + `os.fsync()` immediately,
  into `clone-runs/`. CAT offers no rollback, so that file is the only record of what a
  partially-failed run created.
- **Cleanup cannot touch the source.** `source_ids_of(plan)` builds a protected-id set before
  any removal, because cleanup deactivates clients and entities and a bad id would take down
  what you cloned *from*.
- **`GET` steps never enter `created_objects`** — that list is what cleanup reverses, and a
  read created nothing to reverse.

---

## What cleanup can undo

Cleanup is **partial by design**. Derived from the CAT swagger, the objects a clone creates
fall into three tiers:

| Tier | Kinds |
|---|---|
| **DELETE** | currency account, payment routing rule, payout routing rule, payout setting, payout route |
| **DEACTIVATE ONLY** (`PUT …/status` → Inactive; no DELETE exists) | client, entity, processing profile, sessions channel |
| **NEITHER** | processing channel, processor, entity service (prism), client risk settings, Compass settings, Flow account, network tokens |

Channels and processors can be neither deleted nor deactivated — their PUT schema carries no
status field and there is no `/status` endpoint. The client-level settings steps are
recorded as permanent for a different reason: cleanup cannot know the prior state (was
Flow already on? which tier?), and the client is deactivated anyway. Payout routes remain
deletable, but the tool no longer creates them (see below). **Every live run therefore
leaves a permanent record in sandbox.** That asymmetry is why the dry-run apparatus is as
heavy as it is.

---

## Four ids that are not readable, and how they are derived

CAT requires ids on create that no GET returns. Each is derived, with a confidence label, and
**an ambiguous join refuses rather than guesses** — the step is dropped into `skipped[]` with a
reason and a warning, because emitting a step that cannot resolve fails mid-run and leaves a
partial clone.

| Id | Derivation |
|---|---|
| `processor.profile_id` | acquirer_key + scheme, disambiguated by MCC. Deliberately **not** `processor_key`, which is shared across profiles (`cko-apm` covers four) and yields false positives. A processor with inline `acquirer_settings` is direct-mode and needs no profile. |
| `session_profile_processor.gateway_profile_processor_id` and `.processing_profile_id` | scheme + MCC, then `resolve_profile_id` on the match. **Not** acquirer: the sessions record reports a *processor_key* (`cko-visa`) while the gateway processor reports an *acquirer_key* (`cko_visa_gb`), so those never compare equal. |
| the target's vault account | read back from the new client — see the traps table below |

A profile's MCCs live in `business_settings[]`, one entry per MCC; the top-level
`merchant_category_code` is null on both the list and the v2 detail.

---

## Three things called "payout" — and one of them is not cloned

| kind | what it is | product | cloned how |
|---|---|---|---|
| `payout_setting` | destination instrument (bank details) + payout schedule — **how Checkout pays the merchant** | merchant settlement | created |
| `payout_routing_rule` | **which currency account funds a payout, and which receives fees** (`source_identifier`, revenue and fees accounts) | pay-to-bank fund-flow routing | created |
| `payout_route` | **a supported payout corridor available to the entity** — country + currency + scheme/network labels | pay-to-bank capability | **parity-checked, never created** |

A payout route is provisioned capability, not merchant configuration: the entity endpoint
returns enabled and disabled corridors, and the data model maps it to `dim_payout_routes`.
It is not a hard prerequisite for a `payout_setting` — schedule and instrument are separate
resources — but CAT consults it when validating a payout destination and returns
`route_not_found` if the corridor does not exist.

So the clone reads its own enabled corridors once the entity exists
(`payout_route_check`, a GET with a `verify` block), compares them with the source's, and
flags any the source has and the clone lacks (`payout_route_missing_on_clone`) for the
operator to raise with the Payouts team. POSTing them would risk enabling corridors the
source has disabled or that are centrally controlled. Pay-to-card is a separate family of
resources and is not touched by this tool at all.

---

## Currencies: CAT is the source of truth, and it moves

A captured object carries the currency list it had when it was created, and **CKO's
currency metadata is dynamic**. A profile created in 2023 can therefore name codes CAT
no longer accepts — which is what `422 currency_invalid` means, the one CAT error so far
that named its own cause accurately.

Two mechanisms, because neither is sufficient alone:

- **A validity gate, read fresh on every capture.** `/configuration/currencies`,
  `/currency-accounts/configuration`, `/payout-routes/configuration`, and
  `/processors/configuration/currencies?acquirerId=` — the last one **per acquirer**,
  because Amex and Visa do not support the same set. Anything CAT does not list is
  dropped and flagged. Never cached across runs.
- **Two small explicit tables**, for what a validity list cannot express. `SLL → SLE`
  and `HRK → EUR` are *successor mappings*: a list can say `SLL` is gone, not what
  replaced it. `LBP` is a *valid* code excluded by policy — CKO lists it but it must not
  be enabled for new merchants — so no lookup will ever catch it.

Three of those four endpoints declare no response schema, so the parser accepts every
shape CAT uses elsewhere and returns **`None` (not an empty set)** when it recognises
nothing. That distinction is load-bearing: an empty set would mean "no currency is
valid" and would drop every currency in the plan. When a lookup is unavailable the plan
carries a `currency_validation_unavailable` flag rather than silently looking clean.

Every place a bare currency code reaches CAT goes through the same rules: a profile's
`currencies`, an Amex `SE_CCY` row, a processor's `currencies` **and**
`processing_currencies`, a scoped routing rule's `processing_currencies`, a currency
account's `holding_currency`, and a payout route's `currency_code`.

---

## CAT contract traps

Every one of these was found by running live. Most came with an error message that pointed
at a symptom rather than a cause — a field that was present, a service that existed, a
resource that "wasn't found" because validation had run first. Every diagnosis came from
diffing the sent body against a known-good live call, or from reading the saved capture.
The first five are the original set; the rest were found in the multi-entity runs.

| Trap | Error it produced |
|---|---|
| **Profiles must be created via `/processing-profiles/v2`.** The capture reads v2, so the body is v2-shaped and the v1 path rejects it. Send `checkout_legal_entity_codes` (plural); the singular is a response-only echo, and `banking_partner_code` is server-derived. | `checkout_legal_entity_code_required`, earlier `custom_settings_credentials_required` |
| **A vault account is client-level and no two clients share one.** The source's `vact_*` is never valid on the clone. CAT provisions the target's ~10s after client create, so `vault_lookup` retries — in practice it resolves on the 4th attempt. | `required_service_vault_account_has_not_been_enabled` |
| **A sessions channel's `services` is only on the detail**, not the list — and it spells the type `value`, not `type`. | `services_required` |
| **The catch-all payment routing rule must be created first.** Capture order is CAT's list order, which put a chargeback-scoped rule first. Payout routing has **no** such constraint (verified). | `default_payment_routing_rule_must_exist_before_other_routing_rule_changes` |
| **`clean()` strips only the top level.** `payout_setting`'s nested objects kept their own `src_*`/`csi_*` ids, timestamps and `version`, and still named the *source's* `ca_*` and `vact_*`. `scheduler_ids` is rejected even as an empty array unless the schedule is intraday. | `invalid_client_settlement_currency_accounts`, then `partial_success_payment_instrument_created_without_client_settlement` + `schedule_configuration_only_intraday_schedules_can_have_schedule_ids` |
| **The v2 profile GET is not a create body.** `acquiring_bin`, `authorization_validity_period` and `SE_CCY[].processing_threshold` come back as numbers and must be sent as strings (confirmed by two live creates *and* the swagger's declared types). `card_acceptor_identification_code` is assigned by CAT per entity — send it empty (`"0"` for iDEAL). A profile's shape depends on its **scheme**: mada, Amex and Visa had three different `custom_settings` field sets. | (would have been type/validation 422s; caught by diff) |
| **Currencies are dynamic.** A 2023 profile carried `SLL`, `ZWL`, `LBP`; CAT no longer accepts them. See *Currencies* above — validity gate plus a small successor/policy table. | `currency_invalid` — accurate, for once |
| **`checkout_legal_entity_codes` is required on create and unreadable on older profiles.** The 2023 Amex profile's GET has no such key; newer profiles return it. Derived from sibling profiles on the same entity (it tracks the entity, not the scheme — amex and visa on one entity both `cko-ltd-uk`); refuses if siblings disagree. | `checkout_legal_entity_code_required` |
| **Service keys can be composites.** The `prism` (FraudDetection) service key is `<client_id>\|<entity_id>` — both source ids. Remapping only the vault key sent it verbatim. Every service key is now remapped token by token, and prism is **enabled on the entity first** (`PUT /entities/{id}/services/prism`) because a channel service *references* an enabled service, it doesn't enable it. | `invalid_prism_merchant_service` |
| **A manual (profile-less) processor cannot be created by API.** The single create route returns 503 for one. Flagged and skipped, with its sessions link. | `gateway_manual_processor_creation_not_supported` (503) |
| **The payout-routes list shares no field names with its create.** The list is a UI-option shape (`country_value`, `currency_label`, `schemes_label`); reading it with the create's names sent nulls. Routes turned out to be provisioned capability — now parity-checked, never created. | `currency_code_required` + `country_code_required` |
| **Risk settings PUT wants part of the UI echo back**, and a valid body returned **404** 0.35s after client create. Body is now `id` (clone placeholder), `name` (clone's), `tier`, `read_only_restriction_enabled`; the PUT retries. Whether it is a race or needs a POST first is unconfirmed. | `client_name_required`, then `404` |
| **CAT's `/network-tokens` GET returns the blank create template**, even for a configured client — every field a `default_value`, no `default_entity_id`. The configuration lives on the NT portal. Two runs "completed" with NT silently skipped before the journal carried plan flags. | (no error — a silent skip, which is worse) |

> **The swagger is not authoritative for writes.** The v2 profile POST declares its request
> body as `{}` — no contract at all — and the only profile schema with properties is the **v1**
> `ProcessingProfileRequest`. Deriving drop-lists from the swagger therefore silently applies
> v1 rules. `checkout_legal_entity_code` and `credential` appear **zero times** in all 905KB,
> yet CAT requires both. **A known-good live call is the only authority for a create body.**

**Sandbox `salesforce_case_id` is always `12345678`.** Never carry the source object's real
case id into a clone, and never invent one. `clone_capture.SANDBOX_SALESFORCE_CASE_ID` holds
the constant; the field stays in `DROP_ALWAYS` so the source value cannot survive, and is
re-stamped on the bodies that need it.

---

## Status

**A full three-entity clone of the reference client runs end to end** — 86 steps, 0 failed —
and a one-entity scope produces 33 steps. Confirmed live along the way: cross-entity step
ordering, the catch-all-first routing constraint on a real multi-rule entity, the payout
routing rule body (byte-identical to a known-good POST), prism enablement, Flow, and the
risk-settings and Compass writes. Risk settings on a fresh client are provisioned
**asynchronously**: across seven live runs the PUT succeeded on attempt 3 or 4 (7–12s after
client create) and never needed a POST. The network-tokens portal POST was also run live and
rejected the body with `identification_value_required`, which is what settled the decision
to flag network tokens as a manual step rather than clone them.

Not yet verified:

- **The ambiguous sessions join** (two processors sharing scheme+MCC across acquirers) has
  never been hit live; tests pin that it drops exactly one link and flags. The
  `GET /session-profile-processors/configuration` endpoint could replace the derivation
  entirely — see TODO.
- **Manual sessions processors.** A second create path exists
  (`POST /sessions-processing-channels/{id}/sessions-processors`) that the README used to say
  did not; untested.
- **Currency-account delete.** Returns `422 version_required`; cleanup resolves the version
  from the `e_tag` (base64 `cv=0&rv=N`), still unexercised.
- **Write-only fields.** `entity.acquiring_providers`, `custom_settings.credentials[]`,
  NT `identification_value`, `SE_CCY[].service_establishment_number` — manual-entry inputs
  are the main open work (TODO #2).

---

## Layout

```
TODO.md                # open work and API quirks — read first
app/
  server.py            # stdlib HTTP server, port 8788
  clone.html           # front end (the pipeline as gates)
  clone_capture.py     # read source -> ordered plan  (GET only)
  clone_apply.py       # execute a plan (dry-run by default)
  clone_cleanup.py     # reverse a run, as far as CAT allows
  dev-creds.json       # SANDBOX-ONLY prefill
tests/
  fixtures.py          # synthetic captures — the reference client's SHAPE, not a recording
  test_plan.py         # plan + dry-run assertions (no token, no network)
  mutation_check.py    # reintroduces each fixed bug; fails if no test catches it
cat-api/
  swagger.json         # internal CAT API contract — incomplete for writes, see above
clone-runs/            # gitignored. clone-*.jsonl: run journals (header carries the plan's
                       # flags/skips). capture-*.json: every raw capture — diagnose from these
```

Where to start reading: `clone_capture.py` reads top to bottom as a story — the `DROP_*` tables
and their comments, then `Reader`, then `capture()`, then the derivation helpers, then
`build_plan()` with its numbered step comments, then `validate_plan()`. `clone_apply.py` is
worth reading in full — the retry loop, the verifiers, and the `optional` handling are all
there.

The real documentation is the **inline comments**, not the docstrings. `DROP_BY_KIND`,
`resolve_profile_id` and each `build_plan` step carry notes on why something is done a
particular way and what happened when it wasn't.

---

## Credentials

`app/dev-creds.json` holds a **sandbox-only** Client ID for prefill convenience and nothing
else (the front end uses only `client_id` and `cat_token`). It used to carry a sandbox
`pk`/`sk` pair that nothing read; GitHub push protection flagged it, so it was removed. Note
that `server.py` injects the *whole file* into the page as `window.__DEV_CREDS__`, so
anything added to it reaches the browser — never put API keys or a CAT bearer token in it;
the token is short-lived and always pasted at run time. Your own sandbox keys can be cached
in `app/dev-creds.local.json` (gitignored, mode 600) as `sandbox_sk` / `sandbox_pk`;
`server.dev_creds` merges it over the tracked file to prefill the two key boxes, and drops
any value without the `sk_sbox_` / `pk_sbox_` prefix so a production key can never be
pre-filled. The CAT token is never cached.

**Sandbox API keys** (`Sandbox Secret Key` / `Sandbox Public Key` on the side panel) are
manual, optional and per-session. The page sends them with every request alongside the
CAT token; `server.sandbox_keys` refuses anything not prefixed `sk_sbox_` / `pk_sbox_` on
every route, so a production key never gets past the handler. Inside `clone_apply`, a step
opts in with `auth: "sandbox_secret"` (or `"sandbox_public"`) and is then sent with that key
against `api.sandbox.checkout.com` instead of the CAT token; a sandbox step with no key is
**blocked**, never sent with the CAT token, and a dry run shows that block. No plan step
uses this yet — it is the seam for the sandbox-side actions to come. The journal header
records *which* keys were supplied, never their values.

`clone-runs/` is gitignored: journals contain real created-object ids from write runs, and
saved captures contain a real client's full configuration — addresses, emails, bank details.

This repo also contains the internal CAT swagger. **Keep it private.**
