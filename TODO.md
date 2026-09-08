# TODO

## Where things stand (2026-09-04)

- **Full multi-entity clone runs end to end**: last live run (journal `clone-20260904T115003Z`)
  88 steps, 87 created, 1 failed — the failure was the `optional` network-tokens POST
  (422 `scheme_configuration.identification_value_required`), so the run **continued** and
  finished. That step has since been removed (see below); a plan built from the same
  capture now yields **86 steps, none optional**. 3-entity reference client
  `cli_scna7ew7mxdenl3h36zlmkyh6m`.
- **Tests:** `python3 tests/test_plan.py` → 252 pass. `python3 tests/mutation_check.py` →
  115/115 mutants caught. Both need no token or network. (The suite patches
  `clone_keys.generate` to one shared 1024-bit key so live-mock applies stay fast.)
- **Destination API keys minted in the run (2026-09-08), not yet run live** — see §00.7a.
  The Destination Sandbox Secret Key box is now optional.
- **Webhooks (sandbox → sandbox) built 2026-09-08, not yet run live** — see §00.7. Needs the
  Source Sandbox Secret Key at capture and the Destination Sandbox Secret Key at apply;
  without the latter the two webhook steps block with `optional_step_blocked` and the CAT
  clone still completes.
- **Three always-on manual-step warnings** on every plan, one line each: NETWORK TOKEN /
  RTAU / IA `SETTINGS MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT`
  (`network_tokens_manual`, `rtau_manual`, `intelligent_acceptance_manual`).
- **Front end is tabbed (2026-09-04):** stage cards are buttons → Capture / Plan / Apply
  pages, warnings at the top of each. Live apply shows real per-step progress:
  `apply_plan(on_step=…)` → `server.PROGRESS[run_id]` → `POST /api/clone/progress`, polled
  by the page; `server.Server` is a `ThreadingTCPServer` so the poll is answered mid-run.
  Cleanup and verify live under the results on the Apply page. `MODE` (Sandbox→Sandbox /
  Prod→Sandbox toggle) exists but nothing branches on it yet.
- **Warnings render as a numbered list** at the top of the plan view, each labelled by
  flag code with an action pill; a dropped currency reads "X will still be created, but
  CUR cannot be added and is left out of…" so it is never mistaken for a dropped object.
- **Sandbox keys are cached locally** in `app/dev-creds.local.json` (gitignored, mode 600)
  and prefilled by `server.dev_creds`; non-sandbox values are dropped before injection. The
  CAT token is never cached.
- **Entity picker is tick boxes (2026-09-04):** any subset of entities can be cloned; the
  page sends `only_entities[]` (the single `only_entity` still works). A ticked id CAT does
  not return makes the capture handler **refuse** (`scope_missing`), never plan a smaller
  clone.
- **Sandbox API keys (2026-09-04):** the page has optional `Sandbox Secret Key` /
  `Sandbox Public Key` fields, sent with every request; the server refuses non-`_sbox_`
  keys; `clone_apply` steps opt in with `auth: "sandbox_secret"|"sandbox_public"` and are
  blocked without a key. **No step uses the seam yet** — the front-end rework Patrick has
  started will add the sandbox-side actions that do.
- **Every capture is saved** to `clone-runs/capture-<stamp>-<client>.json` (gitignored);
  every journal header carries `plan_flags` / `plan_skipped`. Diagnose from those files
  first — never from memory.
- **Network tokens — NOT cloned; flagged as a manual step (decision 2026-09-04).** The
  write path was proven end to end: the portal (not CAT — two separate stores) accepts the
  CAT token and a known-good POST exists, but it requires `identification_value`, which is
  readable nowhere; inventing it was rejected. Every plan raises `network_tokens_manual`
  with the source's settings to replicate. Revisit only if a readable source for the
  business identifier appears (or via TODO #2 manual entry). Earlier notes: CAT's
  `/network-tokens` GET returns only the blank template; the NT portal **does accept the
  CAT token** (`network_tokens_source: nt-portal` in the saved capture) and the POST ran.
  First attempt: 422 `primary_url_required` → fixed, `https://www.placeholder.com` sent and
  flagged as a callout when the source has no URL. **Next expected failure:**
  `identification_value` (required when onboarding Visa, unreadable on the source) — if it
  422s, that field needs manual entry (TODO #2). Either way the step is `optional` and the
  clone completes.
- **Confirmed (from the journals, 7 of 7 live runs on 2026-09-04):** risk settings on a new
  client are provisioned **asynchronously** and the PUT is right — it returned 200 on
  attempt 3 or 4 every time (6.8s–11.6s after client create), never a 404 that outlasted the
  retries. No POST is needed. The 6×3s budget leaves only ~2 spare attempts, so raise
  `attempts` rather than rediscover this if it ever fails with a persistent 404.
- **`/payout-routes/configuration` returns 400 on every capture.** It gates nothing (routes
  are read back after apply, not created), so since 2026-09-04 it no longer raises
  `currency_validation_unavailable`; it is still recorded in the capture's
  `valid_currencies.unavailable` for diagnosis. The other three lists still warn if missing.
- Standing operator actions the tool flags but cannot do: VAS pricing (NT prerequisite),
  RTAU, processing region, entity-level risk settings, Compass display currency if
  immutable, payout corridors missing on the clone.

Open work, in priority order. A one-entity scope produces **33 steps**; the three-entity
reference client, 86.

---

## 00. Gaps Patrick has called out (2026-09-04) — not yet cloned

Each of these is configuration the source has that the clone does not get today. Until
each is either built or ruled out, it should at minimum become a plan flag (like the three
`… MUST BE CREATED MANUALLY ON THE DESTINATION CLIENT` lines) so it reaches the handover.
None of them is flagged yet except pricing profiles (in `skipped[]`).

1. **Risk settings — reserve rules are missing.** The client-level Fraud Detection tier is
   applied (`client_risk_settings` PUT, verified live), but reserve rules under risk
   settings are not read or written. Find where they live in CAT (client- or
   entity-level; the entity `risk-settings` endpoint has no known-good write), capture a
   live GET, and add a step or a flag.
2. **FX configuration missing.** Not captured at all. Locate the endpoint(s), confirm they
   are readable, and decide clone vs flag.
3. **Pricing profiles missing.** Currently in `skipped[]` as "commercially sensitive;
   excluded by policy". Revisit the policy: if pricing must be carried, it needs a
   known-good create body; if not, promote the skip to a top-of-page manual-step flag.
   Note VAS pricing is also the network-tokens prerequisite.
4. **Arrears configuration missing.** Not captured. Same treatment as FX: find, read, decide.
5. **Prod → Sandbox: payout-schedule account details will not transfer — needs a callout,
   then a fix.** `payout_setting` carries `payment_instrument` (bank details) and
   `payout_schedule` from the source; when the source is production and the target is
   sandbox those details cannot be carried across. When `MODE === "prod"` the plan must
   (a) flag it at the top of the page and (b) decide what to send in their place. Nothing
   branches on `MODE` yet — this is the first thing that should.
6. **Reporting profiles not working.** Out of scope per CLAUDE.md ("this application does
   not read the reporting-profile … services"), so a cloned client has none. Establish
   what "not working" means on the clone (missing entirely vs created but broken), then
   either clone it or raise a manual-step flag.
7a. **Destination API keys — BUILT (2026-09-08), not yet run live.** The run registers an
   RSA public key (`POST /clients/{id}/public-keys` `{name, type: "RSA", key}`), creates
   `CAT SETUP SECRET` (all secret-side scopes) and `CAT SET UP PUB` (all public-side scopes)
   via `POST /clients/{id}/standalone-reference-tokens`, decrypts the `temporary_secret`s
   with `clone_keys`, uses the secret for the webhooks, shows both once. **Open:** first live
   run confirms the public-key create body (swagger says `{name,type,key}`; the other
   Access Key API wants `{name, public_key_ascii}` + `version=2` — CAT is the former) and
   CAT's RSA padding (decryptor tries PKCS#1 v1.5 and OAEP-SHA1/256 and reports which).
   If `create_public_key`/`create_access_key` permission is missing on the token the two
   steps 403 — optional, so the run continues and the webhooks block.
7. **Webhooks — BUILT for sandbox → sandbox (2026-09-08), not yet run live.** Workflows in
   the Checkout sandbox API (contract via `checkout-mcp-sandbox`: `addWorkflow` accepts
   nested `conditions[]` + `actions[]` in one POST; `get-webhook-action` returns `url`,
   `headers` and `signature.key` in clear). What exists: `read_workflows` (capture, source
   key), `workflow_create_body` (strips ids/_links at every level, remaps entity/channel
   conditions, skips + flags a workflow whose scoped condition empties),
   `webhook_workflow` / `webhook_check` steps (`auth: "sandbox_secret"`, `optional`),
   `verify_workflows`, `optional_step_blocked` handling in `clone_apply`, the Source /
   Destination Sandbox Secret Key fields. **Open:**
   - **First live run.** Confirm `POST /workflows` accepts the carried body (headers +
     signature copied). If CAT-style surprises appear, diff against the MCP schema.
   - **Event-type validation** against `GET /workflows/event-types` on the destination
     before creating (an event the destination does not offer would 422 the whole create).
   - **Cleanup**: `DELETE /workflows/{id}` exists but needs the destination key; cleanup is
     CAT-token only, so workflows are `mode: none` for now.
   - **Prod → Sandbox**: needs a read-only prod key field and URL substitution (§00.5).

---

## 0. Post-run report, built from `plan["flags"]`

Some source configuration **cannot** be carried across, and the answer in every case is
the same: **flag it and keep going**, never halt. A clone that stops at step 33 of 84 is
worse than one that completes with a list of gaps, because the 32 objects it already
created are permanent.

`build_plan` therefore emits `flags[]` — structured, non-halting findings, one per thing
the clone could not reproduce faithfully. Each carries a stable `code` (groupable),
a `message` (mirrored into `warnings[]`, which the review view already renders), an
`action` (`dropped` / `substituted` / `not_created` / `carried` / `not_supplied`), and
whatever identifying fields that kind of finding needs.

Codes so far:

| code | meaning |
|---|---|
| `manual_processor_not_creatable` | processor binds no profile; CAT has no API route for it |
| `sessions_link_orphaned` | its gateway processor was not created |
| `sessions_link_unresolved` | scheme+MCC join was ambiguous |
| `sessions_processor_not_profile_backed` | `processor_type != profile` |
| `currency_not_available` | a currency dropped from a list, account, route or rule — the flag's `reason` says whether by policy or by validity |
| `currency_validation_unavailable` | CAT's currency configuration could not be read, so only the explicit tables applied |
| `routing_rule_scope_emptied` | every currency in a scoped rule was unavailable, so it would have matched nothing |
| `legal_entity_codes_unresolved` | required on create, unreadable, no sibling to copy |
| `source_caid_carried` | `auto_generate` false, so the source's CAID would be sent |
| `routing_rule_orphaned` | references a currency account that was not created |
| `catch_all_routing_rule_missing` | the default rule was dropped, so CAT rejects the rest |
| `service_key_unmappable` | a service key names a source object with no counterpart |
| `payout_route_missing_on_clone` | **run-time**: a corridor enabled on the source is absent on the clone — raise with Payouts |
| `payout_route_schemes_differ` | **run-time**: corridor exists on both but with different schemes |
| `payout_route_unrecognised` | a source payout-route item had no readable corridor; excluded from the check |
| `risk_settings_not_captured` | the source's Fraud Detection tier could not be read; clone stays on the free tier |
| `compass_settings_not_captured` | the source's Compass settings could not be read; clone keeps defaults |
| `flow_account_not_captured` | the source's Flow account flag could not be read; clone keeps Flow disabled |
| `network_tokens_manual` | **always, when the source has NT**: not cloned — the flag carries the source's settings for the operator to replicate in the NT portal |
| `rtau_manual` | **always**: RTAU has no CAT route; one-line manual step for the destination client |
| `intelligent_acceptance_manual` | **always**: IA is out of scope; one-line manual step for the destination client |
| `network_tokens_not_captured` | the source's network tokens form could not be read — check by hand |
| `network_tokens_default_entity_not_in_scope` | the default billed entity is not in this (scoped) capture |
| `network_tokens_default_entity_unreadable` | CAT returns only the blank template AND the NT portal returned nothing usable (the flag names the portal's HTTP status) — configure by hand |
| `optional_step_failed` | **run-time**: an `optional` step failed live and the run continued (no step is optional today; the mechanism stays) |
| `display_currency_differs` | **run-time**: Compass display currency did not take on the clone (likely immutable once set) |
| `conversion_currencies_differ` | **run-time**: the clone lacks conversion currencies the source has |

Two kinds of flag now exist and the report must merge both: **plan-time** flags in
`plan["flags"]` (what could not be carried across, known before applying) and **run-time**
flags in `run["flags"]` (what could only be learned by reading the clone after it existed —
today, the payout-route parity check). A step declares a run-time check with
`verify = {"compare": <verifier>, "expected": [...]}`; `clone_apply.VERIFIERS` runs it.

**Still to build:** the report itself. It should combine `plan["flags"]` with the run
journal — what was flagged before applying, plus what actually failed during the run —
into one document the operator hands over with the clone. The flags are deliberately
data, not prose, so the report can group and count rather than re-parse strings.

---

## 0b. Real Time Account Updater (RTAU) is not cloned — and cannot be, via CAT

RTAU is a client-level value-added service. The clone tool does not carry it across, and
**the CAT swagger exposes no endpoint for it at all** — no `account-updater` path, no
schema, nothing under `ClientValueAddedServices`/`EntityValueAddedServices` (those cover
Flow, vault, prism and risk settings only). So a source with RTAU produces a clone
without it, and there is no API call this tool could make to change that.

Recorded in every plan's `skipped[]` as `real_time_account_updater`, so it appears in the
review view and will land in the post-run report.

**To do:**
- Confirm where RTAU is actually configured (a different admin surface? a Salesforce
  flow?) and whether the source's state is readable from anywhere this tool can reach.
  Until then it is a manual step for the operator and should be called out as such in the
  handover.
- If a CAT endpoint appears in a later swagger, treat it like Flow: read `is_enabled` on
  the source, PUT it on the clone at the client-level block, never copy any account id.

**Network tokens are NOT cloned** — flagged as a manual step (`network_tokens_manual`, see
"Where things stand"), with the VAS-pricing prerequisite and the unreadable
`identification_value` named in the flag. A separate portal for RTAU exists at
`rtau.sbox.checkout.internal/vault-rtau-portal/...` — but the response pasted from it was
the *Network Tokens* form, so whether RTAU has its own form and what it contains is still
unconfirmed. **Processing region** remains client-level, recorded in `skipped[]`, no capture.
Intelligent Acceptance and the reporting profile are likewise out of scope (see CLAUDE.md).
Entity-level risk settings have an endpoint but no known-good write yet.

---

## 1. Test against a multi-channel, multi-entity client

The only client proven so far has **one channel with four processors, each a unique
scheme+MCC pair**, so every derived join was unambiguous. That is the easy case.

The failure mode to look for is **silent**: if two processors on a channel share a scheme and
MCC across different acquirers, `resolve_sessions_processor_link` hits its ambiguous branch,
drops the step into `skipped[]` with a warning, and the clone completes "successfully" with an
authentication link missing. A warning in the plan review is the only signal.

Test against the 5-channel entity on the reference client. Specifically confirm:

- every gateway processor gets a `sessions_profile_processor` step, or a named warning says why
- `processor_type != "profile"` sessions processors are skipped with a reason (cached responses
  show `manual` processors exist elsewhere, and `createType: "existing"` cannot link them)
- a multi-entity capture orders steps correctly across entities — `validate_plan` should catch
  any cross-entity dependency violation, but this has never been exercised

Consider whether an ambiguous join should **fail the plan** rather than warn. A clone that
silently omits authentication wiring is arguably worse than one that refuses to build.

---

## 2. Manual-entry fields for write-only CAT config

Some CAT config can be WRITTEN but not READ, so a clone cannot capture it. Surface these as
manual text-box inputs on the clone page.

**Derive the list properly, don't guess.** Compare each create-request schema's properties
against the union of properties across all NON-request schemas in the CAT swagger. A field in a
`Create*Request` that appears in no response schema anywhere is truly write-only. Distinguish
that from "readable but unset for this merchant" by also diffing against a live capture.

> **Caveat learned the hard way:** this method found 23 candidates across the 11 create
> schemas, and **most were false positives** that `clone_capture.py` demonstrably reads from
> live GETs — `acquirer_settings`, `holding_currency`, `source_identifier`, the `allow_any_*`
> family. The one true positive it found, `is_principal_same_as_registered`, is already derived
> in code. Worse, it **missed the field that actually blocked a run**, because
> `custom_settings.credentials` appears nowhere in the swagger at all. **The live-capture diff
> is the method that works; the swagger diff is close to worthless here.**

Schemas to check: `CreateClientRequest`, `CreateEntityRequest`, `CreateCurrencyAccountRequest`,
`ProcessingProfileRequest`, `CreateProcessingChannelRequest`, `CreateGatewayProcessorRequest`,
`CloneGatewayProcessingChannelRequest`, `CreatePaymentRoutingRuleRequest`,
`CreatePayoutRoutingRuleRequest`, `PayoutSettingRequest`, `CreatePayoutRouteRequest`.

Known cases:

- **`entity.acquiring_providers`** — genuinely write-only; absent from every GET schema.
- **`profile.custom_settings.credentials[]`** — **readable but masked.** CAT returns the block
  with placeholder values (`mid: "0"`, `token: "0"`, `reporting_auth_key: ""`) and then rejects
  those values on create with `custom_settings_credentials_required`. So the *shape* is known
  and only the values need supplying. None of the reference entity's six profiles carries a
  credentials block, which is why the completed run never hit this — but a profile that does
  will block.
- **`payout_setting.payment_instrument` bank details** — **not** a manual-entry case. CAT
  returns account number, bank code, holder and branch in full. The earlier claim that these
  are redacted on read was wrong.

**`processor.profile_id` is NOT a manual-entry case.** It is unreadable but *derivable* via the
acquirer/scheme/MCC join, which already resolves 11 of 13 processors with the other 2 correctly
identified as direct-mode. Putting it in a text box would be a regression.

### Implementation

`clone_capture.build_plan()` gains a `manual_fields` array
(`{step_seq, kind, field, json_path, reason, required_for_apply}`), and `clone.html` renders one
labelled box per entry between Plan and Apply, blocking Apply while any `required_for_apply`
entry is empty.

**The `manual_values` plumbing only handles top-level fields.** `clone_apply.py` splits the key
on the first dot and assigns `body[field]`, so a key like
`4.custom_settings.credentials[0].mid` creates a literal flat key of that name. The credentials
that need supplying are nested inside an array, so **nested-path support is a prerequisite** —
resolve dicts and array indices properly, and fail loudly on a path that doesn't exist rather
than silently creating a key.

---

## 3. Verify currency-account delete

Currency-account delete returns `422 version_required`. `clone_cleanup.py` resolves the version
from the `e_tag` (base64 `cv=0&rv=N`), **unverified**: the currency-accounts list carries no
`e_tag` and the direct GET's response schema is unspecified. The first live cleanup settles it.

Worth doing soon — currency accounts are one of only five kinds that can be *deleted* rather
than merely deactivated, so this is the part of cleanup that actually reclaims anything.

---

## 4. Smaller items

- ~~**No tests.**~~ **Done** — `tests/test_plan.py`, 78 assertions, no token and no
  network (`python3 tests/test_plan.py`). Covers the plan document (step count, kind
  counts, dependency ordering, `requires`/`provides` integrity), dry-run substitution
  (every placeholder resolves, synthetic ids match CAT's id shape, no socket is opened),
  the five traps one test each, the derivations that must refuse rather than guess, and
  the three-part live gate. `tests/mutation_check.py` reintroduces each fixed bug into a
  throwaway copy and asserts the suite catches it — 18/18 currently caught. **Add a
  mutation whenever a new live failure is diagnosed.**

  **What the tests do not cover:** the fixtures in `tests/fixtures.py` are synthetic —
  hand-built to the reference client's *shape*, not recorded from it. **Every capture is
  now persisted** to `clone-runs/capture-<stamp>-<client>.json` (gitignored), so a real
  one exists on disk after any run — redact it and it becomes the fixture this wanted. So they prove the builder is
  self-consistent; they cannot prove CAT accepts a body. Two things would raise their
  value: **commit a real (redacted) capture** as a fixture, and **capture the
  multi-channel client from item 1** as a second one, which would turn item 1's silent
  ambiguity into a test rather than a live discovery.
- **`clean()` is top-level only.** That is what caused the `payout_setting` failure — nested
  objects kept `src_*`/`csi_*` ids, timestamps and `version`, and still named the source
  client's `ca_*` and `vact_*`. Other step kinds with nested bodies may have the same latent
  problem; audit them. `test_no_source_id_reaches_the_wire_outside_a_placeholder` now
  catches this class of bug — but only for ids the *fixture* happens to nest, so it is a
  regression net, not the audit.
- **No resume.** A partially-failed run can only be cleaned up or repeated from scratch.
  The journal has everything needed to resume from step N — the id map is reconstructible from
  the `created_id` entries.
- **Some client-level services are not cloned.** Risk settings, Flow and Compass now are
  (verified live). IA, RTAU and processing region are recorded in `skipped[]`; network
  tokens raise `network_tokens_manual` with the source's settings. A cloned client will not
  behave like the source until those are configured by hand, and the post-run report
  (item 0) is what should tell the operator so.
- **`vault_lookup` retry sizing is a guess.** 6 attempts x 3s, chosen from two observations
  where the vault account appeared ~10s after client create (it resolved on the 4th attempt).
  If provisioning is slower under load, this fails with a clear error — but raise `attempts`
  rather than rediscovering it.

---

## Notes worth keeping

**The swagger is not authoritative for writes.** `cat-api/swagger.json` is reference material.
The v2 profile POST declares its request body as `{}` — no contract at all — and the only
profile schema with properties is the **v1** `ProcessingProfileRequest`, so deriving anything
from the swagger silently applies v1 rules to a v2 endpoint. `checkout_legal_entity_code` and
`credential` appear **zero times** in all 905KB, yet CAT requires both. **A known-good live
call is the only authority for a create body.**

**CAT error codes name symptoms, not causes.** Four of the five bugs found reported a field
that was present in the request, or claimed a service was disabled when it existed:

| Error | Actual cause |
|---|---|
| `checkout_legal_entity_code_required` | wrong endpoint version — v2-shaped body POSTed to v1 |
| `custom_settings_credentials_required` | same root cause, different profile tripping a different v1 rule |
| `required_service_vault_account_has_not_been_enabled` | the vault account existed; the reference belonged to another client |
| `invalid_client_settlement_currency_accounts` | nested `currency_account_ids` still named the source's `ca_*` |
| `services_required` | accurate, for once — `services` is only on the sessions-channel detail |

Read the journalled request body before believing the message.

**Cleanup tiers, derived from the swagger:**

| Tier | Kinds |
|---|---|
| DELETE | currency account, payment routing rule, payout routing rule, payout setting, payout route |
| DEACTIVATE ONLY | client, entity, processing profile, sessions channel |
| NEITHER | processing channel, processor |

**Every live run leaves a permanent record**, because channels and processors have no DELETE and
no `/status` endpoint. Scope runs to one entity.

**Sandbox `salesforce_case_id` is always `12345678`.**
