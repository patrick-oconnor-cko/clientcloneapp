# Project instructions

Copies a Checkout.com client's CAT configuration into a brand-new client. See
[README.md](README.md) for how it works and [TODO.md](TODO.md) for open work — **read TODO.md
before starting anything substantial**; it carries current priorities and several API quirks
that are expensive to rediscover.

## This application writes, and CAT has no rollback

- **`dry_run=True` is the default everywhere.** Keep it that way. In dry-run no socket is
  opened at all.
- **A live run requires three independent things**: `dry_run` explicitly false,
  `confirm == "CLONE"` (or `"CLEANUP"`), and a token. Never collapse those into fewer checks.
- **Verbs are allowlisted to `{GET, POST, PUT}`** in `clone_apply.ALLOWED_METHODS`. Do not add
  `DELETE` — removal belongs to `clone_cleanup.py` behind its own gate.
- **Sandbox API keys are manual, optional and sandbox-only.** `server.sandbox_keys` refuses
  anything not prefixed `sk_sbox_`/`pk_sbox_`. Two secret keys with two roles: the
  **source** key (`sandbox_sk`) only ever reads (webhooks at capture); the **destination**
  key (`dest_sandbox_sk`) is what `auth: "sandbox_secret"` steps are sent with. Without the
  destination key such a step is blocked (optional → flagged, run continues), never sent
  with the CAT token or the source key. **The CAT token comes from Okta SSO in the browser**
  (OAuth 2.0 implicit flow against the same Okta apps the CAT UI uses; `server.OKTA` holds
  the public issuer/client-id values, the page does the redirect and reads the fragment) or
  is pasted — either way it lands in `cat_token`, is never persisted, and the server never
  talks to Okta. Nothing else obtains one.
- **The clone's API keys are minted in the run and never journalled.** CAT returns a key's
  secret RSA-encrypted with a public crypto key on the client; `clone_keys` (stdlib) makes
  the keypair at apply time, `clone_apply` decrypts, feeds the secret to the webhook steps
  and returns both plaintexts ONCE in `run["destination_keys"]`. Response excerpts redact
  `temporary_secret`/`secret`; the plan carries the literal `<<RUN_PUBLIC_KEY_PEM>>`
  marker, never key material. Keep it that way.
- **Prod → Sandbox reads from production and writes to sandbox — never the other way.**
  `server.CAT_BASES[source_env]` picks the read host; `TARGET_CAT_BASE` is the only write
  host and apply/verify/cleanup never see the prod token. The prod token and the source
  production secret key are read-only inputs accepted only in that mode. What leaves prod is
  governed by the `PROD_*` tables in `clone_capture.py` (strip / blank / block-until-manual /
  flag) — extend the table, do not special-case a body. A prod capture and a prod-source
  journal are written through `redact_for_disk`; the in-memory copies are not. Sandbox →
  Sandbox behaviour must stay byte-identical (a test pins the reference plan).
- **Webhooks are Workflows in the Checkout sandbox API, not CAT.** `clean()` does not reach
  their nested ids; `workflow_create_body` strips `id`/`_links` at every level and remaps
  entity/channel conditions. A condition left empty by scoping means skip + flag, never a
  widened match.
- **Processing channels and processors can be neither deleted nor deactivated.** Every live run
  leaves a permanent record in sandbox. Prefer scoping a run to one entity.
- **Never run a live apply or cleanup without being asked explicitly.** A dry run is always
  fine and needs no permission.
- **Don't start the server yourself** — Patrick runs it. Give the command:
  `python3 app/server.py` (port 8788)
- **Commit only when asked.** Work locally; don't prompt for it.

## Working rules

**A known-good live call is the only authority for a create body.** `cat-api/swagger.json` is
reference material and is **incomplete for writes**: the v2 profile POST declares its request
body as `{}`, and `checkout_legal_entity_code` and `credential` appear zero times in all 905KB
despite CAT requiring both. Deriving drop-lists from the swagger silently applies v1 rules to a
v2 endpoint. When a create fails, diff the sent body against a call known to work.

**CAT's error codes name symptoms, not causes.** Four of the five bugs found so far reported a
field that was present in the request, or claimed a service was disabled when it existed.
Read the journalled request body before believing the message.

**An ambiguous derivation must refuse, not guess.** `resolve_profile_id` and
`resolve_sessions_processor_link` return a confidence label and drop the step into `skipped[]`
when the join is ambiguous. Emitting a step that cannot resolve fails mid-run and leaves a
partial clone.

**`clean()` strips only the top level.** Nested objects keep their own server-assigned ids,
timestamps and `version`, and can still carry *source-client* references. That caused the
`payout_setting` failure. Check nested bodies when adding a step kind.

**Don't conclude from a truncated dump.** Print all keys, or say you haven't checked.

**Sandbox `salesforce_case_id` is always `12345678`.** Never carry the source's real case id
into a clone, and never invent one. It stays in `DROP_ALWAYS` and is re-stamped from
`SANDBOX_SALESFORCE_CASE_ID` on the bodies that need it.

## Domain facts that are easy to get wrong

- **A vault account is client-level and no two clients share one.** The source's `vact_*` is
  never valid on the clone. CAT provisions the target's asynchronously, ~10s after client
  create, which is why `vault_lookup` retries.
- **Profiles are created via `/entities/{id}/processing-profiles/v2`.** Send
  `checkout_legal_entity_codes` (plural array); the singular is a response-only echo, and
  `banking_partner_code` is server-derived — send neither.
- **A channel's prism service key is usually `client|entity` — but not always.** A legacy
  opaque id (32 hex) exists on real channels and can never be carried: `prism_service_check`
  reads the clone's `prism.prism_key` back after the prism PUT and any non-composite source
  key references that captured value (`prism_key_normalised`). Never pass a prism key
  through unless it is the composite for the source client and entity.
- **A sessions channel shares its gateway channel's id**, so it must not be registered in the
  id map (`provides=None`), or later references bind to the wrong object.
- **Sessions-channel `services` exists only on the detail endpoint**, and spells the service
  type `value`, not `type` — unlike a gateway channel's `services`.
- **The catch-all payment routing rule must be created first.** Payout routing has no such
  constraint (verified live).
- **A profile's MCCs live in `business_settings[]`**, one entry per MCC. The top-level
  `merchant_category_code` is null on both the list and the v2 detail.
- **List endpoints omit fields creates require.** Almost everything is fetched twice: list for
  ids, detail for the body. Assume the list is insufficient until proven otherwise.

## Config source

CAT: `client-admin.cko-sbox.ckotech.co/api`, with one CAT bearer token (short-lived, ~1h
— ask for a fresh one rather than working around an expired one). **One exception:** the
network-tokens configuration is read from the NT portal
(`nt-portal.sbox.checkout.internal/vault-nt-portal/cat/configurations/{id}`, same token),
because CAT's own endpoint returns only the blank form template. This application does not
read the reporting-profile, network-token, Intelligent Acceptance or RTAU services, and a
cloned client does **not** inherit those client-level services. `build_plan` records that in
`skipped[]`.

## Documentation lookups

For Checkout API schemas, field names and error codes, use the **`checkout-mcp-sandbox`** MCP
server — not `checkout-mcp` (production). This project is sandbox, and the two servers index
different API surfaces. For CAT itself, `cat-api/swagger.json` is the contract — subject to the
caveat above.
