# Instructions for GLM 5.3 — Method Generation Fixes (Consolidated)

> Saved 2026-09-18 by request ("keep these instructions saved for the next session").
> The instruction set below is verbatim. A status annotation follows at the bottom
> — it is NOT part of the instructions.

---

## Priority 0 — fix first, independent of everything else below:

`generator.py` / wherever `execution_steps` placeholders get filled at *execution* time: `{{document_number}}` and `{{date_of_birth}}` are currently being populated from the **redacted** profile (`"[DOCUMENT_NUMBER]"`, `"[DATE_OF_BIRTH]"` literal strings), not the raw locally-extracted values. This means every real document is being submitted to the live registry as a literal placeholder string, which never matches, which correctly triggers the REJECTED discriminator — so real, valid documents are silently reported INVALID with HIGH evidence quality.

Fix: the executor's fill step must source values from the raw, locally-held extracted profile, never the redacted one. If feasible, make `RawProfile` and `RedactedProfile` distinct types (not just two variables of the same shape) so that passing the wrong one into the executor is a type error, not a silent runtime bug.

## Component 1 — XHR Endpoint Extractor (unchanged from before):

- New function `_extract_xhr_contract(inline_js, base_url)` in `generation/generator.py`.
- Regex-extracts HTTP verb, URL, param encoding (`send(null)` = query string; `send(data)` = body), dynamic param names, static params from inline `<script>` JS.
- Strict success = URL + verb + ≥1 dynamic param all cleanly matched. Any partial match returns `None` and falls through to the existing full-LLM path unchanged.
- Out of scope: external `<script src>` bundles, minified JS (already flagged upstream via `[BROWSER_FALLBACK_REQUIRED]`).

## Component 2 — Discriminator Discovery (revised: no LLM, seed-credential based):

- Only runs once Component 1 has confirmed a real endpoint. Never probes an unconfirmed/guessed URL.
- Requires two inputs: a synthetic known-fake value, and a **pre-supplied seed credential** — a real, confirmed-valid record for that specific registry, supplied **once per registry at onboarding time** (not per document, not per run) by a consenting person, stored encrypted (AWS Secrets Manager/KMS — already in your stack), never in a TSV/plaintext DB column. One seed record per registry/document-type; a seed valid for INDOS proves nothing about any other registry.
- Submit both inputs, capture both raw responses, **diff with stdlib `difflib`** — not an LLM, local or otherwise. This is a mechanical string-diff task; routing real registry response content (which can contain a real name/DOB on the "found" page) through any LLM is an unnecessary and unguarded PII exposure path, especially once the generation LLM is swapped to a frontier API.
- Runs once per method at discovery time. Discriminator strings get cached in the registry (`expected_responses.success_keywords` / `failure_keywords`). No LLM step in this component at all.
- **Never persist the seed credential's actual response content or the fake/real input values anywhere** — only the derived marker strings are written to the registry. Discard the raw input/response pair immediately after diffing.
- If no seed credential exists yet for a registry, save the method with `evidence_quality: LOW`, confirmed rejection marker only (if found), and disallow `VERIFIED` outcomes until a seed is supplied and a probe run.
- Rate-limit re-probes against a seed credential — don't hit the live registry repeatedly on every health check; only re-probe on a signal that the cached discriminator may be stale (see Component 6).

## Component 3 — Narrowed LLM Role (unchanged):

- When `xhr_contract` is present, LLM prompt narrows to only mapping redacted profile field names → extracted dynamic param names (e.g. `document_number` → `txtNo`). No endpoint, verb, param-encoding, or discriminator decisions left to the LLM.
- Full existing prompt used unchanged when extraction returns `None`.

## Component 4 — HTTP Executor Fix (corrected):

- Add `param_location: "query"` (default `"body"`) to the `REQUEST` action schema.
- **Do not substitute a GET request.** What was empirically confirmed was a real **POST** with query-string params and an empty body (`requests.post(url, params=params, data=None)` or equivalent) — not a GET. Implement exactly that; don't assume the server treats GET/POST interchangeably just because it happened to work once — that was never tested.

## Component 5 — Self-Healing Feedback (unchanged):

- Add `raw_response` field to `ValidationAttempt`, bounded to ~2000 chars, sourced from `ExecutionResult.raw_response`.
- Extend (don't replace) the self-healing system prompt with a diagnosis hint: an unchanged/idle-looking response means the endpoint or request format is wrong — fix `execution_steps` structurally, not `expected_responses` keywords.

## Component 6 — Re-probe entry point (new):

- CLI/tool entry point (e.g. `python -m engine.upgrade_method <method_id>`) that re-runs the discovery probe against a currently-`LOW`-evidence method, or a method flagged as possibly stale by a health check, and rewrites its cached discriminators in place. Manual/on-demand trigger only — not an automatic background job re-probing live government servers on its own schedule.

## Fix required alongside this, not optional:

- `engine/validation_engine.py`: `_GENERIC_TEST_CASES` must include a fake `dob` value for any document type that has a DOB field, not just `document_number` — otherwise the structural test never actually exercises the fixed field-filling path end-to-end.

## Explicitly rejected from prior plan versions — do not reintroduce:

- Treating `document_number` as non-PII/"public info" to justify using the redacted profile's document number directly in probes. Your own redaction pipeline already classifies it as PII (see `redaction_summary.fields_redacted`); don't override that classification in this component.
- Reusing `http_get` as a substitute for POST-with-empty-body. Only what was actually tested should be implemented.
- Any LLM-based diffing of raw registry response content, local or frontier model.

---

# IMPLEMENTATION STATUS — annotation, added 2026-09-18 (not part of the instructions)

> UPDATED 2026-09-18 (second session): all five deltas from the first status
> note are now implemented. Full test suite: 113 tests, all passing.

## Already implemented (do not redo)

| Instruction item | Where |
|---|---|
| Priority 0 — execution-time fill from raw values, tokens refused | `ocr/extractor.py::split_credentials` (strips `raw_lookup_credentials` out of the profile in memory), `engine/validation_engine.py::_build_inputs` (sources `document_number`/`date_of_birth` **exclusively** from a `credential_provider` callable wired in `main.py`), `_missing_required_inputs` guard → `VALIDATION_UNAVAILABLE` instead of submitting tokens. Distinct `RawLookupCredentials`/`RedactedProfile` TypedDicts now defined in `ocr/extractor.py` (delta 3 closed). |
| Component 1 — XHR extractor | `generation/generator.py::_extract_xhr_contract` + helpers; strict None-on-partial; tests in `tests/test_xhr_extractor.py`. |
| Component 2 — difflib diff, no LLM | `_extract_discriminators_by_diff` (line-level difflib on normalized body text, pattern-classified, shortest-first, no-candidate → no marker). REJECTED-only policy (empty `success_keywords`, limitation tag, validator strip-guard, engine LOW-cap) in place. |
| Component 2 — encrypted seed storage + onboarding (delta 1, CLOSED) | `registry/seed_store.py::SeedStore` — Fernet-encrypted local store (cryptography pkg; see dependency note below), one ciphertext per `COUNTRY::DOCUMENT_TYPE` scope, SHA-256 plaintext checksum verified after decryption (fails closed), 0600 perms, gitignored. Onboarding CLI `engine/onboard_seed.py` (interactive, consent-gated, refuses silent overwrite/token seeds; `--show` lists scopes only). Probe real-input source switched to the stored seed via `ValidationEngine._seed_provider` (seed store wired → seed only; missing/unreadable seed → probe skipped one-sided, method stays REJECTED-only, NO substitution of the document under validation; raw-extraction fallback kept ONLY for legacy wiring without any seed store); `generate_candidate_method` takes `seed_provider`. |
| Component 3 — narrow mapping prompt | `prompts/narrow_mapping.txt`, `_build_narrow_mapping_payload`, branching in `generate_candidate_method`. |
| Component 4 — POST + query params, empty body | `executors/http_executor.py`: `param_location: "query"`, dedicated `http_post_query` (real POST, never GET). |
| Component 5 — raw_response + diagnosis hint | `validation/models.py` (`raw_response: str = ""`), `validation/validator.py` (bounded 2000 chars, history includes raw_response excerpt, system prompt extended). |
| Component 6 — re-probe entry point (delta 2, CLOSED) | `engine/upgrade_method.py` — `python -m engine.upgrade_method <method_id> [--force]`. Manual-only; rebuilds the contract from the method's stored `execution_steps` (no page re-extraction); rewrites cached discriminators in place; removes the REJECTED-only limitation + reactivates INACTIVE/DEGRADED/UNHEALTHY methods when both markers confirm, demotes to INACTIVE otherwise. Rate-limit: 1-hour cooldown via `last_probed_at` in `expected_responses` (timestamp only — no seed material ever reaches the registry); `--force` overrides. |
| `_GENERIC_TEST_CASES` fake DOB | `engine/validation_engine.py`; sync-tested against `FAKE_PROBE_INPUTS` in `tests/test_xhr_extractor.py`. |
| Explicitly-rejected items — current code complies | document_number stays PII (`split_credentials` drops token-valued credentials); `http_post_query` is a real POST; diff step is pure difflib (LLM sees no probe responses). |
| Wire-format probe fix (found during impl) | `_probe_wire_params` translates profile fields → wire names (`txtNo=`/`dob=`) via the same resolved mapping the method carries; probes and generated method share one wire format. |
| Dependency note (delta 4, CLOSED) | boto3 confirmed NOT in the stack. User decision (2026-09-18): local Fernet-encrypted store only; AWS Secrets Manager deferred until boto3 actually lands. `cryptography` added to `requirements.txt` (it was already installed transitively). An AWS backend is a drop-in swap behind `SeedStore`'s interface. |
| Plan caveat (delta 5, CLOSED) | `implementation_plan.md` gains **Revision C**: the seed-credential model supersedes Revision A for the PROBE path only; Revision A raw-extraction sourcing remains in force for Priority-0 execution-time fill (the document being validated). |

## Remaining gaps / next steps (nothing below blocks the current design)

1. **Health-check staleness signal** — the instructions' Component 6 mentions re-probing "a method flagged as possibly stale by a health check". Health checks are currently reachability-only; wiring a staleness flag to surface in `upgrade_method` output is future work (must stay manual-trigger, never scheduled).
2. **Per-executor `last_probed_at` visibility** — stored in `expected_responses`; harmless to executors' keyword matching, but a future registry schema could give probe bookkeeping a dedicated column.
3. **`RedactedProfile` enforcement depth** — the TypedDicts are a static-typing boundary (no runtime type check at function boundaries). If stronger guarantees are wanted, add a light runtime guard where `redacted_profile` enters the engine.

## First-session status (superseded, kept for reference)

Full test suite was 86 tests, all passing, with these deltas open — all closed as marked above: seed storage model (→ `SeedStore` + onboarding CLI), Component 6 (→ `engine/upgrade_method.py`), distinct profile types (→ TypedDicts), boto3 verification (→ local store per user decision), Revision A wording (→ Revision C).
