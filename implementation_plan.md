# Implementation Plan: Generalizable AJAX Endpoint Extraction

## Background

The INDOS investigation confirmed four bugs, all empirically grounded:

1. **WEB_FORM is wrong for AJAX sites** — Direct POST to the JSP returns the idle page unchanged.
2. **LLM guesses field names blind** — Used hallucinated `indos_no` instead of real `txtNo`.
3. **Keyword-based success/failure detection is fragile** — Static page labels cause false `VERIFIED`.
4. **Self-healing only reshuffles keywords** — Never changes structural `execution_steps`.

**Live-confirmed contract for INDOS:**
- Endpoint: `POST /esamudraUI/checkerajaxservlet`
- Params on query string, body is `null`: `txtNo`, `dob`, `processId=PPIndosCheck`, `searchType=Indos`
- `REJECTED` marker: `"Database could not find the match of INDoS No."`
- `VERIFIED` marker: `"Search Result"`

**Confirmed bugs in current codebase:**
- `executors/http_executor.py`: POST always sends params as form-encoded body via `http_post_form`. No support for query-string POST (`send(null)` pattern).
- `generation/generator.py`: `_fetch_page_structure` extracts inline JS text and passes it to the LLM as `page_structure_hint`, but the LLM is expected to guess the endpoint from the full JS dump. No deterministic extraction layer.
- `prompts/method_generation.txt`: Full method-generation prompt is used regardless of whether the JS contains extractable XHR calls. The LLM must do all the work.
- `validation/validator.py`: `_attempt_improvement` sends `full_logs` (Docker stdout/stderr) but does not include the raw response body from `output.json`. The LLM sees "expected REJECTED, got VERIFIED" without seeing *what the server actually returned*.
- `engine/validation_engine.py`: `_GENERIC_TEST_CASES` has only `document_number`. No DOB field in the structural test case, so even a correct method that requires `dob` would fail structurally.

---

## Revised Design

**The three-layer split:**

1. **Deterministic** — XHR extractor pulls endpoint, verb, param names, encoding from inline JS using regex.
2. **Empirical** — One-time probe at method onboarding with **human-supplied real inputs** derives discriminator strings and writes them to the registry (never stores PII).
3. **LLM** — Only maps redacted profile field names → dynamic request param names. Narrow, reliable task.

The XHR extractor is the critical layer. When it succeeds, the LLM's job shrinks from "guess the entire method" to "map field names to known param names." When it fails (minified JS, no inline scripts, ambiguous patterns), the existing full-LLM path runs unchanged — graceful degradation, not a hard break.

---

## Proposed Changes

### Component 1: XHR Endpoint Extractor

**File:** `generation/generator.py`

**Change:** Add `_extract_xhr_contract(inline_js: str, base_url: str) -> dict | None`.

**What it does:**
Uses regex to extract from inline JS:
- HTTP verb from `xmlHttp.open("METHOD", url, ...)` or `fetch(url, {method: ...})`
- URL from the `var url = "..."` assignment directly above the `.open()` call, or from `url + "..."` concatenation
- Param encoding: `send(null)` → params on query string; `send(data)` → form-encoded body
- Dynamic param names — variable names appended to the URL (`url + "?txtNo=" + ...`)
- Static/hardcoded params — literal string values (`processId=PPIndosCheck`)

**Strict success definition:**
All three of URL + verb + at least one dynamic param must be cleanly matched. A partial match (URL found, params ambiguous) counts as failure and returns `None` — falls through to the existing LLM path unchanged. "Code definitely breaks it" is worse than "LLM might guess wrong."

**Returns `None` on any failure or partial match.** When `None`, the existing full-LLM path runs unmodified.

**Integration point:**
In `generate_candidate_method`, after `_fetch_page_structure` returns the JS text, attempt extraction first. If it succeeds, set a flag `xhr_contract` in the user prompt payload so the narrowed prompt (Component 3) can use it.

**Regex patterns to handle (INDOS-confirmed + common variants):**

```
# xmlHttp.open pattern
xmlHttp\.open\(["'](GET|POST)["']\s*,\s*([^,]+)

# fetch pattern
fetch\s*\(\s*["']([^"']+)["']\s*,\s*\{[^}]*method:\s*["'](GET|POST)["']

# var url assignment (one line above .open)
var\s+url\s*=\s*["']([^"']+)["']

# URL concatenation in send context
url\s*\+\s*["'][^"']*["']\s*\+\s*(\w+)\s*\+\s*["'][^"']*["']

# send(null) vs send(data)
\.send\s*\(\s*null\s*\)
\.send\s*\(\s*(\w+)\s*\)
```

**Non-goals:**
- Fetching external JS files. Only inline `<script>` content is scanned. External bundles are out of scope (would require downloading and parsing, which is a different project).
- Handling heavily minified JS. The existing `_fetch_page_structure` already detects minification (`avg_line_length > 200`) and emits `[BROWSER_FALLBACK_REQUIRED]`. The extractor returns `None` when fed minified text — that's the correct fallback.

---

### Component 2: Discriminator Discovery (One-Time, Registry-Cached)

**File:** `generation/generator.py` (or a new module `generation/discriminator.py`)

**Change:** Add `_discover_discriminators(endpoint, verb, static_params, fake_inputs, real_inputs) -> dict`.

**Rules:**
- Only runs when Component 1 has already confirmed a real endpoint. Never probes an unconfirmed/guessed URL.
- Requires **both** a fake input and a human-supplied real input to confirm a **success** marker. A one-sided diff (fake only) cannot confirm a positive signal — absence of the rejection string could be a rate-limit page, maintenance banner, or malformed-request error.
- A fake-only probe **is** sufficient to confirm the **rejection** marker (the response demonstrably reports "not found"). See "No real input available" below.
- Runs **once per method at discovery time** and writes the discriminator strings to the registry. Never re-probes on subsequent validation runs.

**PII rule (hardened — no carve-outs):**
Both `document_number` AND `dob` are PII for probe purposes. The earlier idea of using `document_numbers` from the redacted profile as the "real" input is **rejected**: the extraction component itself classifies document numbers as redaction-worthy (they are treated exactly like name/DOB/phone in the local extraction redaction flow). Overriding that boundary with a plan-LLM's ad hoc "it's public" reasoning is precisely the quiet scope-creep the privacy boundary exists to prevent.

Therefore:
- Real inputs are **supplied by a human at onboarding time** via an interactive prompt. They are never read from the redacted profile and never pulled from a raw-OCR side path — a pre-redaction code path is rejected for the same reason: it widens PII exposure into an automated flow.
- Values live in memory only for the duration of the probe: never logged, never written to any file or DB, discarded immediately after marker extraction.
- Mechanism: `generate_candidate_method(...)` gains an optional `real_inputs_provider` callable. The engine wires it to a CLI prompt only for interactive runs (`main.py`); automated contexts (MCP server, tests, batch) skip it and take the REJECTED-only path below.

**No real input available — explicit policy (this resolves the previously undefined path):**
- The fake-input probe may confirm the **rejection marker**; it can never confirm a success marker.
- The method is saved with `failure_keywords = [confirmed_rejection_marker]` and **`success_keywords = []`**.
- `VERIFIED` is mechanically impossible: the executors' `decide()` returns VERIFIED only on a success-keyword or success-json-path match, both absent here. Outcomes are limited to REJECTED / UNCERTAIN. Validation asserts `success_keywords` is empty for this class of method.
- The engine assigns `EvidenceQuality.LOW` to any decision from a method with empty `success_keywords` (small addition beside `_QUALITY_MAP` in `_build_decision`).
- The method can still pass structural validation (fake input → REJECTED) and be promoted ACTIVE, with a `limitations` entry: "REJECTED-only until a human-confirmed probe supplies the success marker."
- **No fallback to LLM-guessed success keywords** for these methods. Guessed keywords survive only in the separate extractor-failure path (full-LLM generation, Component 1 fallback), which is unchanged.
- If even the fake probe yields no rejection marker (rate-limit page, etc.), the method is registered with no markers; the structural test then fails it and it is never promoted — no silent false decisions.
- A later human-confirmed probe upgrades the method in place: both markers written, limitation removed, evidence quality raised.

**LLM's role here is narrow:**
Given two short HTML fragments (fake response A, real response B), return `{"rejection_marker": "...", "success_marker": "..."}`. This is a diff-summarization task, not free-form generation — local models handle it reliably.

**What gets written to the registry:**
The `expected_responses` field on `ValidationMethod` is populated with the discovered `success_keywords` and `failure_keywords` (each a single-element array containing the confirmed marker string). Full discriminators → decision-time evidence quality stays `HIGH` for HTTP methods. REJECTED-only methods → `LOW` (see policy above).

**Real-input sourcing — resolved:** human-supplied at onboarding, in-memory only, discarded after use (see PII rule above). The former carve-out treating `document_numbers` as non-PII "public information" is rejected.

---

### Component 3: Narrowed LLM Role for Field Mapping

**File:** `prompts/method_generation.txt`

**Change:** When XHR extraction succeeds (Component 1), the LLM prompt is replaced with a narrow mapping question only.

**Narrow prompt (when xhr_contract is present):**

```
You are mapping document fields to API parameters.

Extracted API endpoint (already confirmed): POST /esamudraUI/checkerajaxservlet
Extracted dynamic param names: txtNo, dob
Extracted static params (already included): processId=PPIndosCheck, searchType=Indos

Redacted profile fields available: document_number, date_of_birth, issuing_country

Return ONLY a JSON object mapping each dynamic param to a {{placeholder}}:
{"txtNo": "{{document_number}}", "dob": "{{date_of_birth}}"}
```

**Existing full-generation prompt** is used unchanged when the XHR extractor returns `None`.

**Integration point:**
In `generate_candidate_method`, check whether `xhr_contract` is present in the payload. If yes, use the narrow prompt variant. If no, use the existing full prompt.

**Why this works:**
The hard part (finding the endpoint, verb, param names, encoding) is done deterministically. The LLM only needs to match `date_of_birth` → `dob` and `document_number` → `txtNo`, which is a trivial field-mapping task that even smaller/local models handle reliably.

---

### Component 4: HTTP Executor Query-String POST Fix

**File:** `executors/http_executor.py`

**Change:** Add an optional `"param_location": "query"` field to the `REQUEST` action. Default remains `"body"` to preserve all existing behaviour.

**Current POST logic:**
```python
elif action == "POST" and json_body:
    status_code, body = http_post_json(url, json_body)
elif action == "POST":
    status_code, body = http_post_form(url, params)
```

`http_post_form` sends params as `application/x-www-form-urlencoded` body. This is correct for most POST forms but wrong for the `send(null)` pattern where params go on the query string and the body is empty.

**New logic:**

```python
elif action == "POST":
    param_location = step.get("param_location", "body")
    if param_location == "query":
        # Confirmed wire format: real POST, params on the query string,
        # explicit empty body — the send(null) servlet pattern.
        separator = "&" if "?" in url else "?"
        status_code, body = http_post_query(
            url + separator + urllib.parse.urlencode(params)
        )
    else:
        # Default: form-encoded body (existing behaviour)
        status_code, body = http_post_form(url, params)
```

with a new dedicated helper (real POST — never `http_get`):

```python
def http_post_query(url: str) -> tuple[int, str]:
    """POST with query-string params and an explicit empty body."""
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"User-Agent": "DVS/1.0"}
    )
    # ... same urlopen / HTTPError handling as the other helpers
```

**Notes:**
- **The verb must stay POST.** What was verified live is `requests.post(endpoint, params=..., timeout=15)` — a real POST whose params happen to land on the query string with an empty body. GET was never tested against this endpoint. Swapping in `http_get` would silently change the verb on the untested assumption that "the servlet doesn't distinguish" — some frameworks route GET/POST to different handlers on the same path, and WAFs/proxies may enforce POST-only on servlet mappings. Implement exactly the confirmed wire format.
- The `param_location` field is optional. Existing methods without it default to `"body"` — no breaking change.
- `form_executor.py` is untouched. It is a different executor path (WEB_FORM method_type).

**Schema change for `REQUEST` action:**

```json
{
  "action": "REQUEST",
  "method": "POST",
  "url": "https://...",
  "params": {"key": "{{var}}"},
  "param_location": "query"    // optional, default "body"
}
```

---

### Component 5: Self-Healing Feedback Improvement

**File:** `validation/validator.py`

**Change:** Improve the failure feedback in `_attempt_improvement` to include the raw response body and an explicit diagnosis hint.

**Current feedback:**
The `user_prompt` dict includes `full_logs` (last 2000 chars of Docker stdout/stderr) and `error`. It does **not** include the raw response body from the executor's `output.json`.

**Improved feedback:**
- Add `"raw_response": attempt.raw_response` to the `latest_failure` dict. This field should be populated from the executor's `raw_response` field in `output.json`, bounded to the first 2000 characters.
- Add an explicit diagnosis hint to the system prompt:

```
The response body is shown above (raw_response field). If it looks like the same
page was returned unchanged (e.g., the idle form page, a login redirect, or a
500/error page), the endpoint or request format is likely wrong — change
execution_steps structurally (URL, verb, param_location, fields), not just
expected_responses keywords.
```

**Implementation detail:**
The `ValidationAttempt` model has a `logs` field but no `raw_response` field. Add a `raw_response: str = ""` field to `ValidationAttempt` (option (a) — decided, see Resolved Decisions #5). It separates executor output from Docker infrastructure logs, and the `ExecutionResult` already carries `raw_response`, so it's a matter of passing it through when constructing the `ValidationAttempt`.

**System prompt update:**
Append the diagnosis hint to the existing system prompt in `_attempt_improvement`. Do not replace the existing prompt — just extend it.

---

### Component 6: Structural Test Case DOB Input (REQUIRED)

**File:** `engine/validation_engine.py`

**Change:** `_GENERIC_TEST_CASES` gains a fake `date_of_birth` alongside the fake document number:

```python
_GENERIC_TEST_CASES = [
    TestCase(
        name="structural_check",
        inputs={
            "document_number": "TEST_STRUCTURAL_001",
            "date_of_birth": "01/01/1990"
        },
        expected_decision="REJECTED",
        is_required=True,
    ),
]
```

**Why this is required, not optional:** methods generated by Components 1–3 map `dob → {{date_of_birth}}` and list it in `required_inputs`. If the structural test omits DOB, the executor submits an empty DOB string, which can produce a third server response that neither confirmed discriminator accounts for — the structural test then fails, or worse, passes for the wrong reason. The verification plan's step 3 ("Structural test → REJECTED") only exercises the fixed code path end-to-end when all required inputs are present. The fake DOB must be well-formed for the site's expected date format (use the format indicated by the extracted contract if it differs from `DD/MM/YYYY`).

---

## Verification Plan

### Automated

```bash
.venv/bin/python main.py
# Expected outcomes:
# 1. [DEBUG] shows method_type: HTTP on first generation (no self-healing)
# 2. execution_steps URL contains /checkerajaxservlet + param_location: query
# 3. Structural test (fake number + fake DOB, Component 6) → REJECTED
# 4. Real document run with human-supplied probe inputs → VERIFIED
#    (If the onboarding probe is skipped: run still completes — the method is
#     REJECTED-only and the decision is never VERIFIED)
```

### Manual

- Confirm discriminator strings in registry DB after first successful run
- Confirm no PII persisted to any file (TSV, DB, logs) — probe inputs (document number AND DOB) are typed interactively and must not appear anywhere on disk
- Confirm second run uses cached discriminators without re-probing the live server
- Confirm REJECTED-only methods carry empty `success_keywords` and the REJECTED-only limitation

---

## Build Order

| Order | Component | Rationale |
|-------|-----------|-----------|
| 1 | Component 4 (HTTP executor: `param_location` + dedicated `http_post_query`) | Self-contained, no dependencies. Implements the verified POST wire format. Unblocks Component 2's probe. |
| 2 | Component 6 (structural test DOB input) — **required** | Small and self-contained. Hard dependency of the verification plan: without it the fixed path is never exercised end-to-end. |
| 3 | Component 1 (XHR extractor) | Needed before Components 2 and 3. Pure function, easy to unit-test in isolation. |
| 4 | Component 3 (narrowed LLM prompt) | Depends on Component 1. The branching logic in `generate_candidate_method` needs the extractor to have run first. |
| 5 | Component 2 (discriminator discovery, human-in-the-loop) | Depends on Components 1, 4 and 6. The probe needs POST-with-query-params, a confirmed endpoint, and a DOB-bearing test case. |
| 6 | Component 5 (self-healing feedback) | Independent — can be done anytime. No structural dependencies on other components. |

---

## File Change Summary

| File | Component(s) | Change type |
|------|-------------|-------------|
| `executors/http_executor.py` | 4 | Add `param_location` handling in POST branch. New dedicated `http_post_query` helper — real POST, query-string params, empty body; no `http_get` reuse. |
| `generation/generator.py` | 1, 2, 3 | Add `_extract_xhr_contract()`. Add `_discover_discriminators()` taking human-supplied probe inputs via a `real_inputs_provider` callback. Modify `generate_candidate_method()` to branch on extractor result. |
| `prompts/method_generation.txt` | 3 | Add narrow mapping prompt variant. Existing full prompt unchanged. |
| `validation/validator.py` | 5 | Add `raw_response` to `ValidationAttempt` (option (a)). Update system prompt with diagnosis hint. Pass `exec_result.raw_response` into the attempt. Assert empty `success_keywords` for REJECTED-only methods. |
| `validation/models.py` | 5 | Add `raw_response: str = ""` field to `ValidationAttempt`. |
| `engine/validation_engine.py` | 2, 6 | `_GENERIC_TEST_CASES` gains a fake `date_of_birth` (**required** — verification-plan dependency). Evidence-quality override to LOW in `_build_decision` for methods with empty `success_keywords`. Wire `real_inputs_provider` to a CLI prompt in interactive runs only. |
| `registry/models.py` | 2 | No schema change needed. `expected_responses` already supports `success_keywords`/`failure_keywords` arrays. |

---

## Resolved Decisions (was: Open Questions)

1. **Real input source (Component 2) — RESOLVED: reject the carve-out.** `document_number` is PII exactly like `dob`. The extraction pipeline itself treats document numbers as redaction-worthy, right alongside name/address/phone; reclassifying it as "public" would override our own privacy boundary based on a plan-LLM's ad hoc reasoning. Real inputs are human-supplied at onboarding (interactive, in-memory only, discarded after use) — see the PII rule in Component 2.

2. **HTTP verb (Component 4) — RESOLVED: real POST, never `http_get`.** What was verified live is POST with query-string params and an empty body. GET was never tested against the endpoint. A dedicated `http_post_query` helper is added now rather than deferred, because every other component probes through this wire format. See Component 4.

3. **No-real-input capability (Component 2) — RESOLVED: REJECTED-only.** The fake-probe confirms the rejection marker; `success_keywords` stays empty; VERIFIED is mechanically impossible until a human-confirmed probe runs; no fallback to LLM-guessed positive keywords. Full policy in Component 2.

4. **Registry schema (Component 2) — ASSUMED, unchanged:** discriminators live in the existing `expected_responses` JSON field. No dedicated table. (Trivial to revisit; not blocking.)

5. **`ValidationAttempt` change (Component 5) — ASSUMED, unchanged:** option (a), new `raw_response: str = ""` field. Backwards-compatible Pydantic default. (Trivial to revisit; not blocking.)

## Flagged for Later (Non-Blocking)

- **Cached-discriminator staleness.** Discriminators are cached permanently at discovery time with no re-probe. A portal redesigning its result page six months later would silently break a cached marker — the same "trusted forever after one confirmation" gap already raised by the canary-document lifecycle question elsewhere in the architecture notes. Health checks will eventually need to be semantically meaningful (e.g. periodic fake-input probes asserting the rejection marker still appears), not just reachability checks. Not solved in this plan; link it to that existing open problem rather than inventing a second lifecycle mechanism.

---

## Policy Revisions (post-implementation, supersedes the sections above where they conflict)

### Revision A — Real lookup values come from the raw extraction, never the redacted profile

The original plan had a hole: at final-execution time the engine built inputs from the **redacted** profile, so a live endpoint would receive the literal token `[DOCUMENT_NUMBER]` as the document number — a meaningless lookup that also normalizes token submission.

**Resolution (implemented):**
- `prompts/local_extraction.txt` gains a `raw_lookup_credentials` block: the extraction LLM outputs the REAL document number and DOB (exactly as printed) alongside the redacted profile, in that one field only. Rules in the prompt forbid tokens in this block and require `null` when a value is missing.
- `ocr/extractor.py::split_credentials()` strips that block out of the profile in memory. Downstream consumers (discovery agent, narrow-mapping prompt, reports, logs) only ever see the redacted profile.
- The engine's input builder sources `document_number`/`date_of_birth` **exclusively** from a `credential_provider` callable wired to those in-memory values. Redaction tokens (values starting with `[`) are dropped even if a buggy provider returns them.
- Guard: if a method's `required_inputs` cannot be satisfied with real values, execution is refused (`VALIDATION_UNAVAILABLE`) — never attempted with tokens. Automated contexts (MCP server, batch) leave the provider unset and therefore refuse rather than guess.
- Non-PII lookup keys (e.g. a QR `verification_url`) still come from the redacted profile — the boundary applies only to document number and DOB.

This supersedes the original "human-supplied at onboarding via interactive prompt" mechanism: the raw extraction is the source, and no interactive prompt is needed at execution time. The scratch investigation (`scratch_diff.py`, Cases A/B/C) confirmed the executor's missing-DOB submission produces a third server response that neither confirmed discriminator accounts for.

### Revision B — The diff step is deterministic (difflib), not an LLM task

The original plan assigned marker extraction to a narrow LLM "diff-summarization" call. That is replaced by a mechanical string operation.

**Resolution (implemented):**
- `_extract_discriminators_by_diff()` in `generation/generator.py` performs a line-level `difflib.SequenceMatcher` diff on normalized body text: lines unique to the fake response are rejection-marker candidates; lines unique to the real response are success-marker candidates. Static labels present in both responses can never become markers by construction.
- Candidates are ranked shortest-first and must contain a classification pattern ("could not find", "not found", … / "search result", "record found", …), appear in their own response, and be absent from the other. No candidate → no marker, never a guess.
- Two-sided mode (fake + seed-credential real response) confirms BOTH markers. Without a seed credential the method is REJECTED-only: the fake response is diffed against the idle page (excludes static labels); without the idle page it falls back to a pattern scan of the fake response alone, with the structural test as backstop.
- The LLM is entirely out of the diff step: the earlier narrow-mapping call never sees probe responses.

**Wire-format fix found during implementation:** probes previously sent `document_number=`/`date_of_birth=` as wire params, but the endpoint expects `txtNo=`/`dob=` — fake and real probes would have produced identical responses and zero markers. `_probe_wire_params()` now translates profile fields through the same resolved `param_mapping` the method carries, so the probe and the generated method share one wire format. Related: the seed credential may not need a DOB when probing (Case A/B/C evidence shows DOB present is what separates a real lookup from a malformed one) — the engine supplies whichever of the two the provider has, and `_missing_required_inputs` refuses execution when the method requires an input that has no real value.

### Revision C — Probe real-inputs come from the stored seed credential (supersedes Revision A for the PROBE path only)

Revision A made the current document's raw extraction the real-input source for the discriminator probe. That model is superseded for the probe path: probe values now come from a **seed credential** — one real, confirmed-valid record per registry/document-type, supplied **once per registry at onboarding** by a consenting person and **stored encrypted at rest**. Revision A's raw-extraction sourcing remains in force for what it was actually written for: **Priority-0 execution-time fill** (the document being validated supplies its own lookup values via `credential_provider`).

**Resolution (implemented):**
- `registry/seed_store.py`: `SeedStore` — Fernet-encrypted local store (AES-128-CBC + HMAC via the `cryptography` package), one ciphertext per `COUNTRY::DOCUMENT_TYPE` scope, SHA-256 plaintext checksum verified after decryption (fails closed on tamper/wrong key), store and key files 0600 and gitignored. The original instructions specified AWS Secrets Manager/KMS "already in the stack"; boto3 is **not** in the stack (verified), so the local encrypted store is the implemented backend and an AWS backend is a drop-in swap behind the same class interface when boto3 lands.
- `engine/onboard_seed.py`: interactive, consent-gated onboarding CLI (`python -m engine.onboard_seed --country X --document-type Y`). Refuses to overwrite an existing seed silently; refuses seeds without a real document number or with redaction-token values; `--show` lists scopes only (values are never displayed).
- Probe sourcing in `engine/validation_engine.py::_seed_provider`: the stored seed for the scope, decrypted lazily so plaintext exists in memory only during the probe. **No raw-extraction fallback whenever a seed store is wired** (including wired-but-empty or unreadable — `_seed_provider` returns `None` and the probe runs one-sided, leaving the method REJECTED-only / evidence_quality LOW). The raw-extraction fallback to `credential_provider` (token-filtered) applies ONLY when no seed store is wired at all (legacy wiring/tests). The document under validation is never used as a probe input when a seed store is in force — anti-circularity (a possibly-forged document must not calibrate the discriminator that judges it).
- `engine/upgrade_method.py` (Component 6): manual re-probe CLI (`python -m engine.upgrade_method <method_id>`), the **only** code path that re-contacts a live registry with the seed. Rebuilds the XHR contract from the method's stored `execution_steps` (no page re-extraction), rewrites cached discriminators in place, removes the REJECTED-only limitation and reactivates waiting methods when both markers confirm, demotes to INACTIVE otherwise. Rate-limited by a 1-hour cooldown (`last_probed_at` bookkeeping in `expected_responses`; no seed material in it), `--force` overrides. Not a background job; health checks must never trigger it.
- Seed values are never logged, never written to the method registry, never echoed to reports; the registry stores only derived marker strings plus the `last_probed_at` timestamp.
- `ocr/extractor.py` now defines distinct `RawLookupCredentials` / `RedactedProfile` TypedDicts (Priority 0 follow-up: wrong-profile passthrough is a type error, not a silent runtime bug).
