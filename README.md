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
| **CAT API bearer token** | **Sign in with Okta** (button under the field) or paste — short-lived Okta token, expires in ~1h; the line under the field shows who is signed in, which environment, and the countdown |
| Source Sandbox Secret Key | the **source** client's `sk_sbox_…` — prefills from the gitignored `app/dev-creds.local.json` if present, else typed. Read-only: capture uses it to read the source's webhooks (Workflows) from the Checkout sandbox API |
| Destination Sandbox Secret Key | **optional.** The run mints the new client's API keys itself (see *Step order*) and uses the secret one for the webhooks. Paste a key here only if the new client already has one you want used instead |
| Sandbox Public Key | `pk_sbox_…` — reserved for endpoints that take a public key; nothing uses it yet |

Top right, a **Sandbox → Sandbox / Prod → Sandbox** toggle says where the *source* is read
from; the clone is always created in sandbox. It defaults to Sandbox → Sandbox. In
**Prod → Sandbox** the panel gains a *Source CAT token (prod)* with its own Okta button, a
*Source production secret key* (webhooks) and a *Webhook receiver URL for the clone*, and a
red banner lists what is not carried from production — see *Prod → Sandbox* below.

The three stage cards across the top are buttons, each its own page. The same numbered
warnings open every page so they are never out of sight — each warning carries a short label
(its flag code) and a coloured pill saying what happened: *object still created — only this
value is left out*, *not created*, *value substituted*, *needs a value from you*:

- **Capture** — while it runs, a progress bar of what has been **read** so far (the server
  reports every completed GET, stamped with its phase; the page polls it like the apply
  bar): indeterminate until the entity list is in, then "entity 2 of 3 · processing
  channels", then the tail. When done it shows what was fetched from the source (client,
  scope, CAT calls, saved capture), what will be created, and what will not.
- **Plan** — every endpoint the apply will hit (grouped, and in order), the dry run, and
  the delete/deactivate steps cleanup will be able to offer afterwards.
- **Apply** — the live-apply box, live progress as each step is journalled (the page polls
  `/api/clone/progress` once a second; the server is threaded so the poll is answered
  mid-run), a read-out of what was done and any run-time findings, then the clean-up and
  verify tools for that run underneath.

The pipeline is still gated in order: **Load entities → Capture → Plan / Dry run → Apply →
Verify → Clean up**.

After **Load entities**, tick the entities you want cloned (all are ticked by default; "all"
and "none" shortcuts sit above the list). Scope a first run to one entity. Same information,
much smaller blast radius — and that matters, because most of what a clone creates cannot be
deleted afterwards. A ticked entity that CAT does not return refuses the capture outright
rather than quietly planning a smaller clone.

Cloning one entity of the reference client produces **37 CAT steps** (plus one step per
source webhook and a read-back) and runs in under a minute; the three-entity reference
client is **95 steps**.

### Requirements

Python **3.9+** and nothing else — pure standard library. No dependencies to install.

### Tests

```bash
python3 tests/test_plan.py        # 284 tests, well under a second
python3 tests/mutation_check.py   # 136 mutants — proves those tests have teeth
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
`provides_from` (dotted path into the response) and `retry` (`{attempts, delay_seconds}`,
plus an optional `when_error_contains` that limits retries to a matching error text — so a
step waiting out a known race does not burn its budget on an unrelated rejection).
Two more govern how a step behaves: `verify` (a read compared with the source at apply
time — differences become run-time flags) and **`optional`** — an enhancement nothing
downstream depends on, whose live failure is recorded as an `optional_step_failed` flag
while the run **continues**. Never set on a step that provides an id.

The journal header carries `plan_flags`, `plan_skipped` and `plan_counts`, so a run that
completed every step is still auditable for what the plan chose not to attempt.

### Step order

24 step kinds, in dependency order:

```
client → client_risk_settings → client_flow_account → client_compass_settings →
client_compass_check → vault_lookup → entity → currency_account → processing_profile →
entity_service → prism_service_check → processing_channel → processor → sessions_channel →
sessions_profile_processor → payment_routing_rule → payout_routing_rule →
payout_setting → payout_route_check → client_public_crypto_key →
client_api_secret_key → client_api_public_key → webhook_workflow → webhook_check
```

**The new client's API keys are minted inside the run** (`client_public_crypto_key`,
`client_api_secret_key`, `client_api_public_key`; contract from Patrick's live calls,
2026-09-08). CAT only ever hands a key's secret back **RSA-encrypted** with a public crypto
key registered on the client, so the run generates a keypair at apply time
(`clone_keys.py`, pure stdlib), registers the public half (`POST /clients/{id}/public-keys`,
`{name: "PUB KEY CAT SETUP nnnnn", type: "RSA", key: <PKCS#1 PEM>}`), creates a secret key
and a public key (`POST /clients/{id}/standalone-reference-tokens`; descriptions
`CAT SETUP SECRET` / `CAT SET UP PUB`). The **secret key carries only the workflow scopes**
(`flow`, `flow:workflows`, `flow:events`, `notifier:workflows`) **and no entity
assignment** — an entity-assigned key cannot create a workflow whose entity condition names
another entity, and a key with every secret scope needs an entity (four live runs,
2026-09-08). The public key carries every "public"-side scope from
`GET /access-keys/configuration`. The run decrypts the two `temporary_secret`s with the
private half — trying PKCS#1 v1.5 and OAEP, accepting only a result shaped like an API key.
The private key lives in process memory for the run and nowhere else. The secret key is
fed straight to the webhook steps that follow (a pasted Destination key wins if present);
both plaintexts are returned to the page **once** in `run["destination_keys"]` and are
never journalled — response excerpts redact `temporary_secret`/`secret`, and the journal
entry records only the key's role and prefix. All three steps are `optional`. The plan
carries a literal marker `<<RUN_PUBLIC_KEY_PEM>>`, not a placeholder, so no key material
is ever in a plan document. If the scope catalogue cannot be read, no key steps are
planned and `api_keys_not_planned` says so.

**Webhooks (`webhook_workflow`, `webhook_check`) are the two steps that do not go to CAT.**
Webhooks are Workflows in the client-facing Checkout sandbox API. Capture reads the
source's with the *Source* Sandbox Secret Key (`GET /workflows`, then each
`GET /workflows/{id}` — the list carries only id/name/active). The plan emits one
`POST /workflows` per source workflow with `auth: "sandbox_secret"`: server ids (`wf_`,
`wfc_`, `wfa_`) and `_links` are stripped at every level (`clean()` would not reach them),
entity and processing-channel conditions are remapped through the id map, and any id
outside the capture's scope is dropped — a condition left empty means the workflow is
**skipped and flagged** (`webhook_scope_emptied`), never created with a widened match. The
event condition is validated against `GET /workflows/event-types` (read at capture with the
source key): a retired event — `gateway.payment_authorized` on the run of 2026-09-08T16:03Z,
which made that one create fail with `condition_event_types_invalid` — is left out and
flagged (`webhook_event_dropped`, "will still be created, but …"); with the gate in place the
next run created 4 of 4 (2026-09-09). If the catalogue cannot be read nothing is filtered and
`webhook_events_not_checked` says so. The receiver URL, its
headers and the signing key are carried as-is
(they *are* the configuration for a sandbox → sandbox clone); the URL is flagged
(`webhook_url_carried`) so the operator confirms the receiver. Actions are allowlisted **per
type** (`WORKFLOW_ACTION_FIELDS`): `webhook` (url, headers, signature) and `aws` — Amazon
EventBridge, `account_id` + `region`; sent as a bare `{"type": "aws"}` it failed with
`region_required` on the production client's "EventBridge Notifications" workflow. A type the
table does not know makes the whole workflow a manual step (`webhook_action_unsupported`)
rather than a create missing an action; in Prod → Sandbox an `aws` action targets the
merchant's production AWS, so that workflow is a manual step too (`webhook_aws_manual`, with
the regions). Apply sends these with the
*Destination* Sandbox Secret Key against `api.sandbox.checkout.com`. Both steps are
`optional`: without the destination key they are **blocked with an `optional_step_blocked`
flag and the run continues**, so a CAT clone never fails because the new client's keys
have not been created yet. `webhook_check` reads the clone's workflows back and flags any
created name it does not list. Without a source key nothing is read and the plan says so
(`webhooks_not_read`); a refused key gives `webhooks_unavailable`.

**Three client-level services are never cloned, and every plan says so in one line each**,
at the top of every page:

```
NETWORK TOKEN SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT
RTAU SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT
IA SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT
```

(`network_tokens_manual` — raised when the source has NT, and carrying the source's settings
as structured fields; `rtau_manual` and `intelligent_acceptance_manual` — raised always, since
RTAU has no CAT route and IA is out of scope.) The network-tokens write path was
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
With a valid body the PUT returned **404 immediately after client create**, so it retries
like `vault_lookup` (6 × 3s). **Confirmed across seven live runs:** CAT provisions default
risk settings asynchronously — the PUT succeeded on attempt 3 or 4 (7–12s after client
create) every time and never needed a `POST`. The budget leaves ~2 spare attempts; raise
`attempts` rather than rediscover this. It sits at step 2 on purpose: if its body is ever
wrong, the run halts having created only a client. **Reserve rules under risk settings are
not carried** — see TODO §00.1.

`entity_service` is the one PUT a plan emits: a channel's `services[]` *references*
something enabled on the entity rather than enabling it, so a source channel carrying a
`prism` (fraud detection) service needs `PUT /entities/{id}/services/prism` first or the
channel create fails with `invalid_prism_merchant_service`. It is followed by
`prism_service_check`, a GET of `/entities/{id}/services` that asserts the service is enabled
and captures the `prism_key` CAT minted for the clone — the value a channel uses when its
source prism key is not the remappable `client|entity` composite (see the traps table).

---

## Prod → Sandbox

The source is read from **production CAT** (`client-admin.cko-prod.ckotech.co` — the host the CAT configuration helper uses; the
swagger's `client-admin-prod.ckotech.co` does not resolve, which is what the first prod run's
"HTTP 0" meant) with a **prod CAT token**; everything is still created in
**sandbox** with the sandbox token — `server.CAT_BASES[source_env]` picks the read host,
`TARGET_CAT_BASE` never varies, and a test pins that apply, verify and cleanup ignore the
prod token entirely. The page sends `source_env` with every request (built 2026-09-09).

Two things about the reads differ from sandbox → sandbox. **Capability reads go to the
target:** which currencies CAT accepts and which API-key scopes exist are read from the
sandbox CAT (a second `Reader`), because that is where the creates land; a prod-only
currency or acquirer is therefore dropped or refused against sandbox's rules, not prod's.
The per-acquirer currency read doubles as an **acquirer existence check**: a definitive
400/404 from the target means its profiles — and their processors — are skipped with
`acquirer_not_in_sandbox` rather than emitted to fail mid-run. **The NT portal is not
read** (its production host is unknown); network tokens stay the manual step they already
are. The source's webhooks are read from `api.checkout.com` with the *Source production
secret key*, accepted only in this mode and only for that read — it is never placed in the
keys handed to apply.

**What never leaves production**, by `PROD_*` tables in `clone_capture.py`, each raising a
flag in the numbered list so the handover says what was left out:

| Data | Treatment |
|---|---|
| `payment_instrument.bank_details` / `account_holder_details` | not carried; the payout-setting step declares `needs_manual` and is **blocked** until a sandbox test account is supplied as manual values `<seq>.payment_instrument.bank_details` (nested paths now resolve) |
| profile `custom_settings` credentials (`credentials[]`, `sensitive_password`, `username`, `project_api_key`, `merchant_id`, contract ids…) and `SE_CCY[].service_establishment_number` | stripped (`prod_credentials_stripped`); sandbox acquirers use their own |
| profile `custom_settings.siret` | replaced by the placeholder `12345678912345` (`prod_siret_placeholder`, action *substituted*) — a Cartes Bancaires profile's create **requires** one (`siret_required`, learned on the first prod run); the placeholder passes validation and is not a real registration |
| profile `custom_settings.SE_CCY[].service_establishment_number` (Amex) | replaced by **sandbox's own SE number for that currency** from the "oversized" tier (`AMEX_SANDBOX_SEN`, the CAT form's `merchant_size` option list — not in the swagger, pinned in code), with `custom_settings.merchant_size` set to `oversized` as the known-good sandbox Amex create does (`prod_sen_sandbox_oversized`). A currency sandbox has no SE number for is left out of the rows **and** the profile's currencies (`prod_sen_currency_unavailable`). An SE_CCY row without a number is refused (`custom_settings_se_ccy_0_invalid`, second prod run) |
| card acceptor ids — profile CAID (even with auto-generate off), processor `billing_information.card_acceptor_id` and CAID | blanked, auto-generate forced on (`prod_caid_regenerated`, `prod_processor_fields_dropped`) |
| processor `authorization_key` | dropped (sandbox mode keeps carrying it — known to work there) |
| webhook `url`, `headers`, `signature` | url replaced by the *Webhook receiver URL for the clone*, headers and signing key dropped (`webhook_receiver_substituted`); no URL given → the workflow is a manual step (`webhook_prod_manual`) |
| acquiring BINs | carried, flagged once (`prod_bin_carried`) — validation is the next build |
| contact details (client email, card-acceptor email/phone) | carried, flagged once (`prod_contact_details_carried`) — decision 2026-09-09 |

**Redaction on write.** A prod capture is saved to `clone-runs/` through
`redact_for_disk` (bank fields, credentials, keys, CAIDs, contact details and receiver URLs
become `REDACTED(n)`; shapes are kept), and a plan whose `source.env` is `prod` gets a
journal written the same way — the in-memory capture and run document are untouched, since
the scrub needs the real shapes and the page needs the real result. **Pagination** (both
modes): every CAT list is now read to `total_count` (`Reader.hal_all`); a list that could
not be completed is flagged `list_truncated` instead of trusted — 25 items was a silent
ceiling before, and a production client is where it would have bitten.

Not yet done for prod (TODO §00.5): BIN validation, a prod NT-portal host, and the first
live prod capture itself — do that read-only (capture + dry run) before any apply.

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
- **Sandbox API keys are sandbox-only.** Every route refuses a key not prefixed `sk_sbox_` /
  `pk_sbox_`, and a step that declares sandbox auth is **blocked** without its key — never
  sent with the CAT token. The prefill drops non-sandbox values before they reach the page.
- **Progress is read-only.** `/api/clone/progress` returns slim journalled entries (no
  bodies, no credentials) for a run the page started; an unknown `run_id` is an error.

---

## What cleanup can undo

Cleanup is **partial by design**. Derived from the CAT swagger, the objects a clone creates
fall into three tiers:

| Tier | Kinds |
|---|---|
| **DELETE** | currency account, payment routing rule, payout routing rule, payout setting, payout route |
| **DEACTIVATE ONLY** (`PUT …/status` → Inactive; no DELETE exists) | client, entity, processing profile, sessions channel |
| **NEITHER** | processing channel, processor, entity service (prism), client risk settings, Compass settings, Flow account, network tokens, the clone's API keys and RSA crypto key (disabled with the client; delete in CAT if wanted), webhook workflow (sandbox API, not CAT — remove in Dashboard › Developers › Workflows) |

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
valid" and would drop every currency in the plan. When a lookup that gates something is
unavailable the plan carries a `currency_validation_unavailable` flag rather than silently
looking clean. **Exception:** `/payout-routes/configuration` returns 400 on every capture
and gates nothing — routes are read back after apply, not created — so it is recorded in
the capture's `valid_currencies.unavailable` but does not raise the flag.

When a currency is dropped the object is still created; only the code is left out, and the
warning says so in those words (`profile 'X' will still be created, but LBP cannot be added
and is left out of its currencies — …`). A currency *account* in a dead holding currency is
the one case that is not created.

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
| **A prism service key is not always the client\|entity composite.** One of the production client's fourteen channels carried a legacy opaque id (32 hex chars). Nothing in it is a source id, so the remapper passed it through and the "unmappable key" check — which looks for id-shaped tokens — stayed silent; the clone's prism service does not know that key. Now: after `entity_service` enables prism, `prism_service_check` reads `GET /entities/{id}/services` back, asserts `prism.is_enabled`, and captures `prism.prism_key` (the composite CAT minted for the clone) into the id map; any channel whose source key is not the composite references that captured key (`prism_key_normalised`, naming channel, original key and replacement). No key returned → the channel blocks on its placeholder, never sent with a guess. | `invalid_prism_merchant_service` — again, and again silent on the cause |
| **A manual (profile-less) processor cannot be created by API.** The single create route returns 503 for one. Flagged and skipped, with its sessions link. | `gateway_manual_processor_creation_not_supported` (503) |
| **The payout-routes list shares no field names with its create.** The list is a UI-option shape (`country_value`, `currency_label`, `schemes_label`); reading it with the create's names sent nulls. Routes turned out to be provisioned capability — now parity-checked, never created. | `currency_code_required` + `country_code_required` |
| **Risk settings PUT wants part of the UI echo back**, and a valid body returned **404** 0.35s after client create. Body is now `id` (clone placeholder), `name` (clone's), `tier`, `read_only_restriction_enabled`; the PUT retries. Confirmed a race: 7/7 live runs succeeded on attempt 3–4, no POST needed. | `client_name_required`, then `404` |
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

**A full three-entity clone of the reference client runs end to end — 94 steps, 0 failed**
(2026-09-09T09:32Z; 95 today, with the prism read-back added since), including the minted
API keys and all four webhooks, read back and confirmed; a one-entity scope produces 37 CAT
steps. Confirmed live along the way:
cross-entity step ordering, the catch-all-first routing constraint on a real multi-rule
entity, the payout routing rule body (byte-identical to a known-good POST), prism
enablement, Flow, the risk-settings and Compass writes, API-key minting (PKCS#1 v1.5) and
webhook creation behind the event-type gate. Risk settings on a fresh client are provisioned
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

Known gaps — configuration the source has that the clone does **not** get today (TODO §00,
in priority order): risk-settings **reserve rules**, **FX** configuration, **pricing
profiles** (excluded by policy so far), **arrears** configuration, the **Prod → Sandbox
payout-schedule account details** callout, and **reporting profiles**. Until each is built
or ruled out it should become a one-line manual-step flag like the three above. Webhooks
are cloned for sandbox → sandbox; only the Prod → Sandbox variant remains open (§00.5).

**The new client's API keys are minted in the run and webhooks are cloned — both verified
live.** The public-key create accepts `{name, type, key}`, CAT encrypts secrets with
**PKCS#1 v1.5**, and the minted secret authenticates against the sandbox API in the same
run. Webhooks took four live runs on 2026-09-08 to land, and the lessons are the durable
part: an allowlisted body (the read echoes `created_at`/`updated_at` the create rejects), a
secret key with **only the workflow scopes and no entity assignment** (an entity-assigned
key cannot name other entities in a condition; a key with every secret scope needs an
entity), and the event-type gate (a source workflow can name a retired event). The first
clean run followed on 2026-09-09: 4 of 4 created and read back.

---

## Layout

```
TODO.md                # open work and API quirks — read first
app/
  server.py            # stdlib threaded HTTP server, port 8788 (+ /api/clone/progress)
  clone.html           # front end: three stage pages (Capture / Plan / Apply), no build step
  clone_capture.py     # read source -> ordered plan  (GET only; Reader on_call hook feeds the capture progress bar)
  clone_apply.py       # execute a plan (dry-run by default; on_step progress hook)
  clone_cleanup.py     # reverse a run, as far as CAT allows
  clone_keys.py        # stdlib RSA: keygen, PKCS#1 PEM, decrypt (padding detected) — mints the clone's API keys
  dev-creds.json       # tracked: sandbox source client id only
  dev-creds.local.json # gitignored, mode 600: your sandbox_sk / sandbox_pk for prefill
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
else. It used to carry a sandbox `pk`/`sk` pair; GitHub push protection flagged it, so it was
removed and the keys moved to the gitignored local file described below. Note
that `server.py` injects the *whole file* into the page as `window.__DEV_CREDS__`, so
anything added to it reaches the browser — never put API keys or a CAT bearer token in it;
the token is short-lived and always pasted at run time. Your own sandbox keys can be cached
in `app/dev-creds.local.json` (gitignored, mode 600) as `sandbox_sk` / `sandbox_pk`;
`server.dev_creds` merges it over the tracked file to prefill the two key boxes, and drops
any value without the `sk_sbox_` / `pk_sbox_` prefix so a production key can never be
pre-filled. The CAT token is never cached.

**Sandbox API keys** on the side panel are optional; the source key prefills from the local
file above when it exists. The CAT token is always pasted; nothing in this app can obtain
one. Three fields, three roles (`server.SANDBOX_KEY_FIELDS`):

| Field | Request key | Role |
|---|---|---|
| Source Sandbox Secret Key | `sandbox_sk` | `source_sandbox_secret` — **read-only**, used by capture to read the source's webhooks |
| Destination Sandbox Secret Key | `dest_sandbox_sk` | `sandbox_secret` — the auth mode `clone_apply` uses for sandbox-API steps (webhook creates) |
| Sandbox Public Key | `sandbox_pk` | `sandbox_public` — reserved |

`server.sandbox_keys` refuses anything not prefixed `sk_sbox_` / `pk_sbox_` on every route,
so a production key never gets past the handler. Inside `clone_apply`, a step opts in with
`auth: "sandbox_secret"` and is sent with the **destination** key against
`api.sandbox.checkout.com`; the source key is never promoted to fill a missing destination
key — the step is **blocked**, and a dry run shows that block. The journal header records
*which* keys were supplied, never their values.

### Signing in to CAT with Okta

The **Sign in with Okta** button under the token field runs the same login the CAT UI and
the CAT configuration helper use: OAuth 2.0 **implicit flow** against Checkout's Okta,
entirely in the browser. `server.OKTA` holds the public values (sandbox issuer
`checkout.oktapreview.com/oauth2/ausskuj3xaCB7FT2g0h7`, client `0oasktz00noN5cA5x0h7`; prod
issuer `checkout.okta.com/oauth2/aus14y376tJ9vBv7B357`, client `0oa4sy8n5hKfYBJK4357`; scope
`openid profile clientadmin-tool`) and serves them to the page as `window.__OKTA__`; the
server never talks to Okta and never sees anything but the resulting bearer, which lands in
the same `cat_token` field a pasted token uses. An OIDC public client has no secret, so
there is nothing to protect on the server side.

The page generates `state` and `nonce`, keeps them in `sessionStorage` across the redirect,
and on return **requires the `state` to match this tab** and the token's `nonce` to match
before accepting it; it then strips the fragment with `history.replaceState` so the token is
never left in the address bar or history. The line under the field decodes the token's own
claims (display only — CAT verifies the signature) to show who is signed in, which
environment the token is for (`cid` → sandbox or prod Okta app), and a countdown to `exp`,
turning amber at five minutes and red when expired. A token whose environment does not match
the Sandbox → Sandbox / Prod → Sandbox toggle is called out. The toggle also picks which
Okta app the button uses — the prod token is for the Prod → Sandbox source read (TODO §00.5).

**The redirect URI is `http://localhost:8788/`** (derived from `PORT`) and Okta only redirects
to URIs registered on the app. **Confirmed 2026-09-09:** the first sign-in stopped at Okta
with *"The 'redirect_uri' parameter must be a Login redirect URI in the client app
settings"* — so `http://localhost:8788/` is not yet registered on app `0oasktz00noN5cA5x0h7`.
The only way through is to have the app's owners add `http://localhost:8788/` (scheme, port
and trailing slash exactly) under *General → Login redirect URIs* on that app in Okta admin
(`checkout-admin.oktapreview.com`, the page the error links to), and later the same on the
prod app `0oa4sy8n5hKfYBJK4357`. Pasting a token keeps working meanwhile. If the registered
URI ever uses a different port, `CLONE_PORT=<port> python3 app/server.py` serves the page
there and the redirect URI follows.

`clone-runs/` is gitignored: journals contain real created-object ids from write runs, and
saved captures contain a real client's full configuration — addresses, emails, bank details.

This repo also contains the internal CAT swagger. **Keep it private.**
