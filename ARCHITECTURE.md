# ARCHITECTURE.md — What this system is and how it works

## 1. What this system does

Shipping companies must hire seafarers who hold genuine credentials — but
those credentials are easy to fake, and checking them means looking each one
up manually on government websites around the world. This project automates
that check: you give it a document (a scan or photo of a seafarer's
certificate), it reads the document, figures out which government registry
can confirm it, submits a lookup to that registry, and reports whether the
document's details match what the registry says. A human always makes the
final call; the system's job is to produce trustworthy evidence for that
call.

A few terms used throughout:

- **INDOS** — India's national database of registered seafarers. An "INDOS
  certificate" is the ID certificate issued from that database.
- **SID** — Seafarer's Identity Document, an ID card for seafarers.
- **CDC** — Continuous Discharge Certificate, a seafarer's employment record
  book.
- **COC** — Certificate of Competency, the license to work in a specific
  rank onboard.
- **Registry** — here, a government website where a credential can be
  checked against official records.

### Module map (file layout)

The codebase is deliberately split into small, single-purpose modules:
entry points and orchestrators stay thin, and each pipeline stage owns its
own package. When you touch a stage, edit its module — not the orchestrator.

| Package / file | Role |
|---|---|
| `main.py` | CLI entry point: single document, `--folder` person mode |
| `mcp_server/server.py` | MCP server entry point (external tool door) |
| `ocr/client.py` | OCR API upload |
| `ocr/extractor.py` | LLM extraction, classification, redaction |
| `registry/models.py` | `ValidationMethod` model and status lifecycle |
| `registry/repository.py` | SQLite method registry |
| `registry/document_types.py` | Canonical document-type keys |
| `db/lookup.py` | Fail-closed source lookup |
| `discovery/agent.py` | Discovery **orchestrator**: the iterative search → fetch → judge → follow loop |
| `discovery/llm.py` | Discovery LLM calls (rate-limit retry), prompt loading, JSON parsing |
| `discovery/search.py` | LLM query generation + Tavily search |
| `discovery/page_fetch.py` | Candidate page fetch + shared JS endpoint harvest |
| `discovery/judge.py` | Deterministic pre-checks (login wall/error/country mismatch) + model judgment |
| `discovery/crawl_queue.py` | Document-carried URL hints, queue enqueue/skip management |
| `engine/validation_engine.py` | Per-document orchestration (Steps 4–9) |
| `generation/generator.py` | Generation **orchestrator**: narrow path + full-LLM fallback, method assembly |
| `generation/page_fetch.py` | Source-page fetch + structure summary for the LLM |
| `generation/xhr_contract.py` | Component 1: deterministic XHR contract extraction from inline JS |
| `generation/bundle_contract.py` | Component 1b: same extraction over SPA bundles |
| `generation/param_mapping.py` | Component 3: narrow LLM mapping, workflow-param pinning, dispatch-selector detection |
| `generation/discriminators.py` | Component 2: known-fake probe + difflib marker extraction |
| `generation/post_process.py` | Deterministic post-generation enforcement (finalization) |
| `execution/docker_runner.py` | Docker sandbox runner; ships executor modules into the container (`_method_timeout`); SCRIPT branch writes `script_source` as `executor.py` + ships `dvs_io.py` |
| `execution/safety.py` | Input guarding, contact-only synthesis, structural test values |
| `executors/dvs_io.py` | SCRIPT SDK shim (`get_input`, cookie-aware `http_get`/`http_post`, `write_result`) — the only I/O surface an authored script gets |
| `execution/script_policy.py` | Harness-side SCRIPT verdict guardrails (downward-only): a script cannot claim VERIFIED on a refusal or with no evidence |
| `validation/coverage.py` | Shared structural coverage check (declarative `{{placeholder}}` / SCRIPT `get_input`) used by validator + generator fallback |
| `utils/log_scrubber.py` | Shape-preserving redaction (`‹CDC:10alnum›`) for logs, healing prompts and debug dumps |
| `executors/http_executor.py` | HTTP **step runner**: captcha round, GET_HTML/EXTRACT, REQUEST loop, `main()` |
| `executors/http_helpers.py` | HTTP verbs, session/cookies, retry, response compaction, substitution, captcha fetch |
| `executors/http_decider.py` | Response classification: `decide()`, learned not-found signatures (new `_json_message_reports_not_found`) |
| `executors/form_executor.py` | WEB_FORM method runner |
| `executors/qr_url_executor.py` | QR_URL method runner |
| `executors/browser_executor.py` | BROWSER method runner (Playwright) |
| `validation/validator.py` | Structural validation loop + confirmed not-found signature capture |
| `validation/healing.py` | Healing ladder: direct LLM rewrite + agentic tool-use loop |
| `validation/field_comparison.py` | Field-match verification (beats keyword guessing) |
| `validation/response_mapping.py` | LLM-discovered response field mapping |
| `utils/llm_client.py` | Shared LLM config + JSON/vision calls (local/frontier toggle) |
| `utils/js_intel.py` | Endpoint/call harvesting from external JS bundles |

Import conventions that keep this working:

- Split modules are re-exported from their orchestrator
  (`generation/generator.py` re-exports `_extract_xhr_contract`,
  `_discover_discriminators`, …) so existing callers and tests keep working.
- The Docker sandbox receives only flat files (no package tree):
  `execution/docker_runner.py` copies the executor plus its split modules
  (`http_helpers.py`, `http_decider.py`, `llm_client.py`) next to
  `executor.py`, and the executors fall back to plain module-name imports
  there (`try: from executors.http_helpers import … / except ImportError:
  from http_helpers import …`).

---

## 2. The life of one document

Follow a single uploaded document through the pipeline, step by step. Each
step names the code that actually does the work and explains why the step
exists — what would break if it were skipped.

### Step 1 — Upload and OCR

`main.py` takes a document path (or opens a file chooser via `select_document()`),
then calls `ocr/client.py::extract_document()`, which uploads the file to an
**external OCR API** — an HTTP POST to the URL in `OCR_API_URL` (in this
deployment, a server on the local network, per `.env`). The API returns raw
recognized text as JSON.

Why this step exists: nothing downstream can read a scan or photo; everything
starts from this text. Note the consequence: **raw, unredacted document text
already leaves the machine at this step** — OCR is not run locally.

### Step 2 — Extraction, classification, and redaction

`ocr/extractor.py::extract_and_redact()` sends the raw OCR output to the LLM
endpoint (`LLM_URL` from `.env`) together with the prompt in
`prompts/local_extraction.txt`. Redaction is **prompt-driven**: the prompt
instructs the model to replace names, dates of birth, and document numbers
with tokens like `[PERSON_NAME]` and `[DOCUMENT_NUMBER]`. There is no
Presidio and no other redaction library in the codebase.

The same prompt asks the model to also extract, into a separate
`raw_lookup_credentials` block, the **real** document number, date of birth,
and name — plus `passport_number` and `serial_number` when (and only when)
they are actually printed on the document (added September 2026: registries
such as Myanmar's DMA require the holder's passport and the certificate
serial as lookup inputs, and the old three-key allowlist in
`split_credentials()` silently dropped perfectly-extracted values of any
other name).
`ocr/extractor.py::split_credentials()` then splits the output in
memory into two distinct Python types:

- `RedactedProfile` — tokens and non-sensitive facts. This is the only thing
  that flows to discovery prompts, reports, and logs.
- `RawLookupCredentials` — the real values, kept in memory only, consumed by
  the engine when it actually talks to a registry.

Why the split exists: verification portals need the *real* number and birth
date to find a record, but redaction tokens must never be submitted to a live
website (an early bug literally submitted the string `[DOCUMENT_NUMBER]` to
the Indian registry). Making the two shapes distinct types turns that mistake
into a type error instead of a silent bug.

### Step 3 — Canonical document-type key

`registry/document_types.py::profile_document_type_key()` maps the
classified `document_type` string (e.g. `"SEAFARERS IDENTITY DOCUMENT"`) to a
canonical key like `IN_SID` via a fixed allowlist (`DOCUMENT_TYPE_ALIASES`).
An unrecognized type returns `None`.

Why a canonical key exists: in a real incident, an SID document was routed to
the **INDOS** registry (see §5b) because the old lookup matched on country
alone. The key binds *country + document type together* — an Indian SID
(`IN_SID`) and an Indian INDOS certificate (`IN_INDOS`) are different keys
even though both are Indian documents. The key is derived **only** from the
classified `document_type` field — never from raw OCR text (a document that
merely *mentions* "INDOS" must not be routed there) and never from a
key the extraction LLM might emit (the model was observed producing
plausible-looking but wrong labels like `INDOS_CERTIFICATE`;
`registry/document_types.py` documents that the model-emitted key is
"advisory only").

### Step 4 — Source lookup (fail-closed)

`db/lookup.py::lookup_source()` searches the source database
(`verification_sources.db`, built from `sources_data.tsv` by `init_db.py`)
for a registry that can verify this document. Matching is strict on both
axes:

- **Country**: exact match after normalization (`country_key()`), not a
  substring/LIKE match.
- **Document type**: each source row carries a `supported_doc_types` tag
  (e.g. the India INDOS endpoint is tagged `IN_INDOS` only). A source with
  no tag, or a document whose key isn't in the tag, matches nothing.

Why fail-closed: the SID incident happened precisely because the lookup
matched on country alone (`WHERE country LIKE '%INDIA%'`) and happily
returned the INDOS endpoint for an SID. The strict version deliberately
accepts "no source found" for documents we can't verify yet, rather than
risking the wrong registry again.

### Step 5 — Method registry, then discovery

Control passes to `engine/validation_engine.py::ValidationEngine.validate()`,
which tries three things in order:

1. **Registry lookup** (`_find_active_methods`): is there already a trusted,
   reusable "recipe" (a `ValidationMethod`) for this country + document type?
   A cached method is only used if it is `ACTIVE`, carries the **current
   `method_schema` tag** (`registry/models.py::CURRENT_METHOD_SCHEMA`,
   currently `"field-comparison-v3"`), and its stored `document_type_key`
   matches the document's key exactly. Why the schema tag: when generation
   logic is fixed, old cached methods generated by the old logic must not
   silently stay in service.
2. **Source lookup** (Step 4 above): if no method is cached but a confirmed
   source exists, generate a method for it (Step 6).
3. **Discovery agent** (`discovery/agent.py::run_discovery`): if neither
   exists, an LLM+Tavily-search pipeline researches candidate verification
   websites for that country/document type. Anything it finds is a
   *candidate* — a source whose official status hasn't been independently
   confirmed is not written to the source database, and a method that can't
   pass live testing is left `UNHEALTHY`/`INACTIVE`, never promoted. If
   nothing usable is found, the honest outcome is
   `VALIDATION_UNAVAILABLE` / `UNKNOWN` — not a guess.

### Step 6 — Method generation

`generation/generator.py::generate_candidate_method()` builds the "recipe."
It first fetches the registry's web page
(`generation/page_fetch.py::_fetch_page_structure`) and tries the
**deterministic XHR extractor**
(`generation/xhr_contract.py::_extract_xhr_contract`): a plain-regex
scanner (no LLM involved) that reads the page's inline JavaScript and pulls
out the real request the page makes behind the scenes — endpoint URL, HTTP
verb, whether parameters go on the query string or in a body, and the
parameter names.

Why it exists: the first INDOS method POSTed to the visible search page,
which ignores POSTs entirely — the real search happens through a hidden AJAX
servlet (`checkerajaxservlet`). An LLM asked to "find the endpoint" had
guessed a field name (`indos_no`) that doesn't exist (real names: `txtNo`,
`dob`). Regex extraction is deterministic: either it finds a complete,
self-consistent request (URL + verb + at least one per-document parameter)
or it returns nothing and the pipeline falls back to full LLM generation. A
half-confident extraction is treated as a failure, never as a partial
success.

Parameters are then classified three ways, not two:

- **Static** — a literal constant in the page's code, baked into the method.
- **Dynamic** — genuinely varies per document (the number, the birth date).
  These are the only parameters whose *meaning* is mapped by an LLM, and even
  then the LLM only picks which profile field each one comes from — it never
  supplies the endpoint or verb.
- **Workflow-fixed** — technically read from a form element at runtime (so
  naive classification would call it dynamic), but operationally a fixed
  choice the system always makes the same way (e.g. `searchType`, which a
  human would pick from a dropdown; the value is inferred from the page's own
  `<option>` entries by
  `generation/param_mapping.py::_infer_workflow_params`).

Why the third category exists: in the SID incident, `searchType` was mapped
to the document's type text and sent to a field expecting `Indos`, producing
a malformed request. Generation now refuses outright to map a workflow-fixed
parameter to a document-type field (the `RuntimeError` guard in the narrow
path of `generate_candidate_method`).

### Step 7 — Execution

`execution/docker_runner.py::DockerMethodRunner._run_in_docker()` runs the
method inside a plain **Docker** container (`docker run`, not gVisor): a
fresh container per execution, read-only root filesystem, `--tmpfs /tmp`,
memory capped at 256 MB, CPU at 0.5, and networking disabled entirely unless
the method needs to reach a registry. The per-type executor scripts in
`executors/` do the actual HTTP work — for HTTP methods,
`executors/http_executor.py` (step runner) drives the request through
`http_helpers.py` (verbs/retry) and `http_decider.py` (classification),
including the
POST-with-empty-body pattern (`http_post_query`) that the Indian servlet
requires (the verb must stay POST; substituting a GET was explicitly
rejected because only what was live-tested is implemented).

Inputs are built by `engine/validation_engine.py::_build_inputs` **only**
from the raw in-memory credentials; if a real value is missing, the engine
*refuses to run* rather than submit a redaction token.

**Contact-only vs identity fields.** One deliberate exception exists to the
refusal above. Some registries carry required form fields that have no
bearing on the identity match — a notification email the result is mailed
to, a callback phone number. A method may declare such fields in
`expected_responses.contact_only_inputs`; the execution layer
(`execution/safety.py::fill_contact_only_inputs`, called by the Docker
runner) then synthesizes a plausible-format **inert** value for missing ones
(emails use the reserved `.invalid` TLD, RFC 2606 — deliverable nowhere by
construction). Identity fields — document numbers, serials, passport
numbers, names, birth dates — are never synthesizable: a fake one could
flip a real verdict. Three enforcement layers back this: the runner
synthesizes only declared contact-only fields; the MCP `upsert_method`
gate rejects any `contact_only_inputs` entry that is not a required input
or whose *name* looks like an identity field (`passport`, `serial`, `cdc`,
`dob`, `name`, …); and both self-healing agents' prompts carry the
standing principle (classify a missing field first; unsure means identity).
The concept is general — any future registry's method can use it; nothing
is hardcoded to one site or to email.

**Identity-named fields and probe evidence.** Because a fake identity value
could flip a real verdict, the MCP upsert gate rejects
`contact_only_inputs` entries whose *name* looks like an identity field
(`passport`, `serial`, `cdc`, `dob`, `name`, `document`, `indos`). The only
key that opens this gate is live structural evidence:
`engine/probe_passport.py` submits obviously-fake vs real values to the live
portal (same anti-forgery-tokened form flow a browser uses) and writes
`probe_evidence/passport_probe_result.json` (gitignored). Only a **COSMETIC**
verdict — fake and real passport producing structurally identical responses
while a bogus CDC differs — lets `_probe_vouches_cosmetic()` accept the
declaration. VALIDATED or INCONCLUSIVE verdicts keep the field blocked, and
the probe script never writes method status itself; a human/agent acts on
the evidence through the normal upsert gate.

### Step 8 — Verification (field comparison, not keywords)

The registry's response is classified by
`validation/field_comparison.py::compare_response()`:

1. Parse name / birth-date / number-shaped values out of the response
   (`_extract_response_fields` — handles JSON, HTML tables, forms).
2. Compare them against the document's **own raw profile** — the real values
   held in memory from Step 2 — using RapidFuzz fuzzy matching with
   calendar-aware date comparison (`_field_similarity`).
3. All fields ≥ **90** → `VERIFIED`.
4. Response empty, or nothing scores ≥ **55** → `REJECTED` (or
   `TECHNICAL_FAILURE` if the response was empty — see Step 9).
5. Anything in the ambiguous band (≥ 55, < 90) → escalated to the
   "local-only" LLM client, `utils/local_llm_client.py::generate_local_json`,
   to judge the match.

**Where ambiguous cases actually go — the honest current state.** The
"local-only" client resolves its endpoint as `LOCAL_LLM_URL` **falling back
to `LLM_URL`** (`utils/local_llm_client.py`). `LOCAL_LLM_URL` is not set in
this deployment's `.env`, so ambiguous-case judgments — which carry the raw
name, birth date, document number, and the registry's response — go to the
**same external endpoint** (`ai.edot-solutions.com`, an internet HTTPS host)
that all other generation calls use. The separate provider-fallback chain to
Google Gemini / Groq was removed from `utils/llm_client.py::generate_json`
(silent escalation to a costlier model on local failure was its failure
mode); `USE_LOCAL_LLM_ONLY` is surfaced for reporting only and controls
nothing. Net effect: the
code-level separation between "local-only verification calls" and "external
generation calls" exists, but **in this deployment both resolve to the same
external endpoint, so raw PII does leave the machine.** This is risk #2 in
§6.

**Update (2026-09-30): the toggle now actually routes providers.**
`USE_LOCAL_LLM_ONLY` is no longer reporting-only:

- `true` (default, or unset) -> LOCAL: `LLM_URL` + `LLM_MODEL` (`AI_Local`).
- `false` -> FRONTIER: the **same** `LLM_URL` gateway and `LLM_API_KEY`, with
  `FRONTIER_LLM_MODEL` (default `gemini/gemini-2.5-flash`). The gateway serves
  both models behind one endpoint, so no second provider or key is required;
  `FRONTIER_LLM_URL` / `FRONTIER_LLM_API_KEY` override only when pointing at a
  genuinely different provider.

`utils/llm_client.get_llm_config()` now returns `is_frontier`, and every call
site (generation, discovery, healing, vision, and the "local" ambiguous-case
path) scrubs PII through `utils/log_scrubber.scrub` before a frontier request
leaves the machine. The Docker sandbox receives the frontier env keys
(`execution/docker_runner.py`) so captcha vision follows the same switch.

Why field comparison replaced keyword matching: the original method decided
"verified" if the response contained `"Name"` and `"Date of Birth"` — static
column headers present on *every* page load, found or not, guaranteeing a
false VERIFIED every time (§5a). Comparing the response against the document
itself needs no guessed keywords and no donated sample credential: if the
document is genuine, the registry's answer will echo its own details.

### Step 9 — Final decision: three separate axes

`engine/validation_engine.py::_build_decision()` and `engine/models.py::ValidationDecision`
produce the output. Three signals are deliberately kept separate:

- **`document_result`** — is the *document* valid, invalid, or unknown?
- **`evidence_quality`** (HIGH/MEDIUM/LOW/NONE) — how much do we trust the
  *evidence* behind that answer?
- **`decision_status`** (VERIFIED/REJECTED/UNCERTAIN/VALIDATION_UNAVAILABLE/TECHNICAL_FAILURE)
  — did the *machinery* work?

Why they're separate: in the SID incident, a broken request produced an HTTP
200 with an **empty body**, and the engine treated "empty" the same as
"registry says not found" — reporting the document `INVALID` with
`evidence_quality: HIGH`: the most dangerous kind of wrong answer, because
it looks maximally trustworthy. The fix: an empty/unparsable response is now
`TECHNICAL_FAILURE` / `UNKNOWN` / `NONE` — *we couldn't check*, never *the
document is bad*. Only a genuine, non-empty "not found" response body
produces `REJECTED` / `INVALID`. A broken method and a forged document are
different facts and must never be reported the same way.

### Step 10 — Where results actually land

- `report.py::generate_markdown_report()` writes a plaintext
  `discovery_report_<document>_<timestamp>.md` into the repo root.
- The full decision — including `raw_response` (the registry response body,
  truncated to 2000 chars by the executors) — is printed to the terminal by
  `main.py`. `ValidationDecision.raw_response` (`engine/models.py`) carries
  it in memory.
- The container's temp directory (`.docker_temp/exec_*`) is deleted after
  every run (`shutil.rmtree` in `docker_runner.py`).
- **There is no S3, no Object Lock, no SHA-256-hashed evidence store.** An
  evidence layer of that kind was in the early design and is not built (§4).

### Alternate entry point: the MCP server

There is a second way into the system that skips the entire flow above.
`mcp_server/server.py` runs a Model Context Protocol server over stdio
(`main()` → `server.run("stdio")`) exposing six tools to whatever client
attaches — typically an external LLM agent (the agentic fallback loop,
`agent_fallback.py`, is exactly such a client):

- `list_active_methods(country, document_type)` — lists ACTIVE methods from
  the registry (`registry.find_methods` filtered on `MethodStatus.ACTIVE`).
- `validate_document(method_id, inputs)` — loads a method, checks it is
  ACTIVE or TESTING and that the caller-supplied `inputs` cover
  `required_inputs`, runs it through the same Docker sandbox, and wraps the
  result with `engine._build_decision`.
- `process_document(document_path)` — runs the full Steps 1–10 pipeline
  (OCR → extraction/redaction → engine) for a local file, with a proper
  credential provider. This door DOES have the PII boundary.
- `get_method(method_id)` / `upsert_method(method_json)` /
  `delete_method(method_id)` — read, write, and remove registry entries.

This is an entry point for **already-generated, already-promoted methods** —
no OCR, no extraction, no redaction, no discovery, no credential provider.
After the September 2026 incident (§5c3), the following guardrails are in
place on this door, verified in code and pinned by tests:

- **Live-submission guard (`execution/safety.py`, enforced in
  `execution/docker_runner.py`).** Every execution through the Docker runner —
  whichever door called it — refuses inputs that are redaction tokens
  (`[DOCUMENT_NUMBER]`), recognizable placeholders (`"test"`, `"xxx123"`,
  repeated characters), or leave `required_inputs` unsatisfied. The refusal
  is a `TECHNICAL_FAILURE` returned before any container is started and
  before any network packet is sent. The generator's structural test is the
  only caller allowed to submit its known-fake marker values
  (`TEST_STRUCTURAL_001`), and only via an explicit
  `allow_structural_test_values=True` opt-in threaded through
  `MethodValidator`.
- **Test-before-trust on upsert (`mcp_server/server.py::upsert_method`).**
  A method whose execution content is new or changed is stored with status
  `TESTING`, never `ACTIVE`, no matter what the caller claims. Structural
  validation rejects malformed definitions outright (empty steps, an HTTP
  method with no executable step, `{{placeholders}}` that are neither
  required inputs nor earlier step outputs, version regressions, and
  same-version content changes — the latter must be re-submitted with an
  explicit version bump).
- **Promotion only via clean live execution
  (`mcp_server/server.py::validate_document`).** A `TESTING` method that
  executes without `TECHNICAL_FAILURE`/`VALIDATION_UNAVAILABLE` is promoted
  `TESTING → ACTIVE` by the tool itself, and the response carries
  `promoted_to_active`/`method_status` so the agent can see it. Nothing else
  — and in particular no LLM self-report — promotes a method.

Two known gaps remain on this path (deliberate, listed in §6):

- **The PII *provenance* boundary from Steps 1–2 still does not exist on the
  `validate_document` path.** The engine instance in `server.py` is
  constructed with no `credential_provider`, and `validate_document` still
  forwards the caller's `inputs` verbatim into the container. Placeholder
  garbage is now refused by the guard, but a caller submitting *real-looking*
  raw personal data gets it submitted to the live registry exactly as
  given — the MCP door still has no notion of where values came from.
- **The same method can get different answers through the two doors.** The
  main engine re-classifies responses with `compare_response` before
  deciding (`_execute_and_decide`); the MCP tool calls `_build_decision`
  directly and never runs comparison. For a `field_match` method the
  executor has no field context to compare
  (`executors/http_decider.py::decide` returns `UNCERTAIN` for a
  `field_match` method that carries discovered `text_mappings`; a
  mapping-less method falls through to its declared keywords — that
  keyword channel was added 2026-09-28 so a structural probe gets a
  definitive REJECTED the validator can capture as a learned not-found
  signature), so an
  MCP execution can never produce `VERIFIED` or `REJECTED` — it returns
  `UNCERTAIN` / `UNKNOWN` with `evidence_quality: HIGH` for an HTTP method.
  Conveniently, this also means an MCP live test can never *falsely*
  promote a `field_match` method on a mistaken VERIFIED — but it also means
  the two entry points do not agree about the same method.

### Person folders: multi-document testing mode (`main.py --folder`)

Besides a single document, `main.py` accepts `--folder <dir>`: a
**person-scoped container** for several independent document fixtures (one
person's COC, CDC, passport scan, …). The folder is NOT a merged profile:

- **Each document is extracted and persisted separately** as its own
  fixture: `person_folders/<person>/<document_stem>.json`
  (`fixtures/person_folder.py::load_or_extract_fixture`), containing the
  redacted profile, that document's raw lookup credentials, and the source
  file's SHA-256. The directory is **gitignored** — fixtures deliberately
  persist PII locally so tests re-run without re-running OCR, exactly like
  the input scans themselves.
- **Fixtures are content-addressed**: a fixture whose recorded SHA-256 no
  longer matches its source file is re-extracted, never silently reused.
- **Verification still runs one document at a time** — N documents mean N
  independent `ValidationEngine.validate()` runs, each with its own subject
  document and its own report.
- **Cross-document lookup:** while verifying a document, its credential
  provider (`load_subject_document`) serves the subject's own raw
  credentials first, then fills ONLY required identity keys the subject
  could not supply from the subject's other documents in the same folder
  (`lookup_credential_across_siblings` — placeholder-shaped sibling values
  are refused, the subject is never its own sibling, and contact-only
  fields are never served cross-document). The engine passes the current
  method's `required_inputs` into the provider (`_build_inputs` calls it
  with the list when the provider accepts an argument), so only keys a
  method actually needs are ever resolved.
- **Subject precedence is absolute:** cross-document values are lookup
  context; the subject fixture alone decides which document is being
  verified, and field comparison still runs against the SUBJECT document's
  own raw profile. A passport number borrowed from a sibling can satisfy an
  input requirement, but it can never make the COC look verified against
  the sibling's data.

This is the better general answer to the Myanmar case: if the person's
folder contains their actual passport document, `passport_number` for the
COC verification resolves from it directly — no synthesis involved. The
`contact_only` mechanism remains for fields that are genuinely never
document-sourced (like the DMA portal's ReplyEmail).

---

## 3. Design principles (each one earned by an incident)

- **Fail-closed routing.** A document reaches only sources explicitly tagged
  as compatible; unrecognized types match nothing. *Why:* the SID was routed
  to the INDOS registry because "India has one source" (§5b).
- **Three independent signal axes.** Document result, evidence quality, and
  machinery health are never collapsed into one field. *Why:* an empty
  response was once reported as a confident "document invalid" (§5b).
- **Deterministic over probabilistic.** Anything code can establish with
  certainty, code does; LLMs only do judgment. *Why:* an LLM hallucinated the
  field name `indos_no` and picked keywords present on every page (§5a); a
  regex extractor that either fully succeeds or fails beats a confident
  guess.
- **Raw values never meet prompts; tokens never meet registries.** Executor
  inputs come only from the in-memory raw credentials; redacted tokens are
  never submitted. *Why:* the engine once filled request placeholders from
  the redacted profile, literally posting `[DOCUMENT_NUMBER]` to a live
  government site.
- **Test-before-trust.** A generated method must pass a live structural test
  (known-fake input must be rejected) before promotion; nothing is trusted on
  generation alone. *Why:* stale cached methods silently bypassed fixed
  logic until an explicit schema tag was added
  (`registry/repository.py::_init_db` quarantines pre-canonical methods).
- **No LLM-guessed success keywords.** Methods without a human-confirmed
  success marker are stripped of any `success_keywords`
  (`validation/validator.py`, the REJECTED-only guard). *Why:* guessed
  keywords were the root cause of false VERIFIED (§5a).
- **A green test suite is not proof.** Behavior claims are confirmed against
  the live registry, not just passing tests. *Why:* a test in this project
  asserted a circular fallback (using the document under validation as its
  own known-good reference) as correct behavior until it was caught by hand.
- **Compliance first on access.** No VPNs or commercial proxies to defeat
  geo-blocking; use aggregator APIs, registry whitelisting, or legitimate
  multi-region cloud deployment instead. (Design rule, not an incident.)

---

## 4. What's actually running today vs. what was planned

### Actually running (verified in code)

- **Storage:** SQLite via the stdlib `sqlite3` module —
  `registry/repository.py` (method registry, `method_registry.db`) and
  `init_db.py` (source database, `verification_sources.db`).
- **LLM:** one external OpenAI-compatible endpoint (`LLM_URL` =
  `ai.edot-solutions.com` in this deployment's `.env`), called by
  `utils/llm_client.py::generate_json` and `discovery/llm.py::call_llm`.
  A Gemini→Groq fallback chain was removed from `utils/llm_client.py` — there
  is exactly one provider. The model used at that endpoint is selected by the
  `LLM_MODEL` toggle (`get_llm_config`), currently `AI_Local` in `.env`. Tavily is used for discovery search
  (`discovery/agent.py`).
- **Redaction:** LLM prompt only (`prompts/local_extraction.txt`). No
  Presidio.
- **OCR:** external HTTP API (`ocr/client.py` → `OCR_API_URL`, a LAN server
  in this deployment). No local OpenCV/PaddleOCR pipeline.
- **Sandboxing:** plain Docker (`execution/docker_runner.py`) with
  resource caps, read-only rootfs, and optional network isolation. No
  gVisor.
- **Evidence storage:** plaintext markdown reports (`report.py`) and the
  in-memory/printed decision JSON. Nothing at rest is encrypted (the former
  Fernet seed store was removed — see §6 item 7).
- **Matchers:** RapidFuzz + `difflib` (`validation/field_comparison.py`,
  `generation/discriminators.py`).
- **MCP server:** `mcp_server/server.py` exposes `list_active_methods` and
  `validate_document` over stdio (see the alternate-entry-point note in §2 —
  including its caveats).
- **Agentic fallback loop:** `agent_fallback.py` runs a LangGraph ReAct
  agent (`create_react_agent`) against the MCP tools over stdio, launched
  from `main.py`'s technical-failure prompt; the langchain/langgraph stack
  is pinned in `requirements.txt`.
- **Agentic healing:** `validation/healing.py` runs the same LangGraph
  ReAct agent (`create_react_agent`) as a validation-time self-repair pass
  for failed structural tests (§7.3), behind `DVS_AGENTIC_HEALING`.
- **Test suite:** 204 tests under `tests/` (unittest), currently passing.

### Planned, not yet implemented (verified absent from code)

- **PostgreSQL + SQLAlchemy** method registry — the registry is SQLite
  (`registry/repository.py`).
- **Presidio** redaction — redaction is prompt-driven (§2 Step 2).
- **S3 + Object Lock + SHA-256 evidence layer** — no such code exists.
- **Full LangGraph pipeline orchestration** — only the fallback agent
  (`agent_fallback.py`) uses LangGraph today; `discovery/agent.py` is a
  plain sequential pipeline.
- **Prometheus / Grafana / APScheduler** health monitoring — none present.
- **gVisor (runsc)** sandbox runtime — plain `docker run` only.
- **Local OCR (OpenCV, PaddleOCR)** — OCR is an external API call.
- **Playwright browser executor** — `executors/browser_executor.py` is an
  explicit stub that returns "a Playwright-capable Docker image is required";
  JavaScript single-page-app registries cannot be driven yet.
- **AWS deployment** (EC2/RDS/S3/IAM/Secrets Manager/KMS) — not wired; no
  AWS SDK (boto3) is in the stack.

---

## 5. Incidents (case studies)

### a. INDOS false-VERIFIED — the method that approved everything

- **Symptom:** every submission — genuine document and known-fake test alike
  — came back `VERIFIED`. The method couldn't tell a real credential from a
  fabricated one.
- **Root cause (three, stacked):** the generated method POSTed to the visible
  search page instead of the hidden AJAX servlet
  (`checkerajaxservlet`), so every response was just the unchanged idle
  page; the LLM had hallucinated a field name (`indos_no`; real names are
  `txtNo`, `dob`, `cmbSearch_by`); and "success" was decided by keywords
  (`"Name"`, `"Date of Birth"`) that are static column headers present on
  every page load. Self-healing couldn't fix it because retry feedback
  carried no response body — the LLM could only reshuffle guesses.
- **Fix:** the deterministic XHR extractor
  (`generation/xhr_contract.py::_extract_xhr_contract`) reads the real request
  shape out of the page's JavaScript; the three-way parameter
  classification keeps the LLM away from everything except dynamic-field
  mapping; verification was replaced by field comparison against the
  document's own raw profile (`validation/field_comparison.py::compare_response`).
- **Confirmed live:** genuine document → `VERIFIED` with 100/100/100 field
  scores on name, DOB, and number; altered-DOB document → a genuine ~742-byte
  "not found" response body → `REJECTED`. Re-run end-to-end without manual
  cache edits.

### b. SID misrouted to INDOS — the confident wrong answer

- **Symptom:** an SID document came back `INVALID` with
  `evidence_quality: HIGH`.
- **Root cause:** source lookup matched on country only
  (`WHERE country LIKE '%INDIA%'`), returning the INDOS endpoint for an SID
  document; because *a* source was found, discovery never ran. Generation
  then mapped the workflow-fixed `searchType` to the document-type text,
  the servlet rejected the malformed request with HTTP 200 and an empty
  body, and the engine classified "empty" the same as "not found."
- **Fix:** canonical document-type keys bound to country+type
  (`registry/document_types.py`); per-source compatibility tags and exact,
  fail-closed matching (`db/lookup.py::lookup_source`); generation-time
  refusal of workflow-fixed→profile-field mappings (`generator.py` narrow
  path); empty responses now produce `TECHNICAL_FAILURE` / `UNKNOWN` /
  `NONE`, never `INVALID` (`field_comparison.py`); cached stale methods
  quarantined via schema-tag + key checks (`validation_engine.py::_find_active_methods`,
  `registry/repository.py::_init_db`).
- **Confirmed live:** an SID run finds no compatible source → discovery runs
  → the candidate it finds (a JavaScript SPA the executor can't drive) is
  *not* written to the source database and its method stays `UNHEALTHY`;
  result honestly reported `VALIDATION_UNAVAILABLE` / `UNKNOWN`. The INDOS
  run, regenerated from scratch, still reaches genuine `VERIFIED` and
  genuine `REJECTED` directions.

### c. This audit's findings (September 2026)

**c1. The "local-only" verification LLM resolves to the same external
endpoint as generation.**
`utils/local_llm_client.py::generate_local_json` reads `LOCAL_LLM_URL`,
falling back to `LLM_URL`. `LOCAL_LLM_URL` is not set in `.env`, so
ambiguous-band judgments (which carry raw name, DOB, document number, and
the live registry response — `field_comparison.py` sends up to 10,000
characters of raw response) go to `ai.edot-solutions.com` — the same
internet endpoint every other LLM call uses. The Google/Groq fallback chain
in `utils/llm_client.py::generate_json` has been removed entirely — there is
exactly one provider — which limits provider sprawl but does **not** keep
data on the machine. Current status, plainly: **the PII boundary as
described ("raw data never leaves the local machine") does not hold in this
deployment.** It holds only in the sense that raw data goes to exactly one
configured external endpoint and no further fallbacks.

**c2. The structural test is close to tautological, and the discriminator
probe is orphaned.**
Verified in code: the narrow (extractor-succeeded) path of
`generation/generator.py::generate_candidate_method` does **not** call
`_discover_discriminators` (`generation/discriminators.py` — re-exported
through `generation.generator`, but the orchestrator never invokes it)
— the only caller would be an onboarding CLI that was deleted
(`engine/upgrade_method.py`); the probe currently has no
production caller at all, only unit tests). Meanwhile, for `field_match` methods the
validator runs `compare_response(exec_result, tc.inputs)`
(`validation/validator.py::_run_test_case`) — comparing the response to the
**known-fake input against the fake input itself**. Any non-empty response
that avoids the error-marker words ("not found", "error", "sorry", …) and
contains no near-match for the fake values scores below the ambiguous band
and is classified `REJECTED` — so a method pointed at the wrong endpoint
that returns its idle page for *every* input passes validation and is
promoted `ACTIVE`. The empty-body class of failure *is* caught (empty →
`TECHNICAL_FAILURE` fails the test), but the wrong-endpoint class is not.
Concrete consequence: a brand-new registry's first generated method could be
promoted on a meaningless pass, and its first real document would then be
judged by fuzzy matching alone against whatever the wrong endpoint returns.
(The original INDOS incident, §5a, is exactly this failure shape.)

### d. Four pipeline defects fixed (September 2026 session)

- **pinning/camelCase:** Fixed issue where parameter pinning failed on camelCase inputs.
- **captcha Docker budget:** Added specific timeout (`_method_timeout`) for captcha-bearing requests.
- **retry-burns-single-use-captcha:** Fixed issue where retrying a request burned a single-use captcha.
- **4xx bootstrap channel:** Handled 4xx errors properly in the bootstrap channel (no retry).
- **availability gate + coverage pre-check:** Added gate for method availability and structural placeholder coverage pre-check.


**c3. The agentic fallback loop bypassed the live-submission guard and
promoted three untested guesses — one reached the live registry.**
September 2026, document “2O - HEIN HTET (AIO).pdf” (Myanmar COC), method
`M_MY_DMA_001`. The main pipeline correctly refused to run: the method
demanded `serial_number`, `passport_number`, and `email`, none of which the
raw extraction could supply (“Refusing to submit redaction tokens to a live
endpoint”, `agent_debug_log.json`). The operator then ran the fallback agent
(`agent_fallback.py` via the y/n prompt in `main.py`), which drives the MCP
server's tools. Three consecutive LLM-authored method bodies were upserted —
each stored `ACTIVE` directly (version climbed 1→3), with no sandboxed or
live test of any of them, and one version even carried
`document_type_key: IN_COC` (an *India* key) on the *Myanmar* method. On the
third attempt the MCP `validate_document` call — which at the time forwarded
caller-supplied inputs verbatim past every guard — sent incomplete values
to the live `dmamyanmar.org` ASP.NET endpoint and received a **real HTTP
500 (NullReferenceException)** from a foreign government's server. Forensic
traces survived in `.docker_temp/exec_*` leftovers.

Root causes, all fixed in this audit:

1. *No shared guard on the execution layer.* The “no tokens to live
   endpoints” rule existed only in the engine's input builder. Fix:
   `execution/safety.py::guard_inputs` is now enforced inside
   `DockerMethodRunner.execute_method` — the single choke point every path
   shares (engine, MCP, fallback agent). It refuses redaction tokens,
   placeholders, and unsatisfied `required_inputs` before any container or
   network activity; refusal surfaces as `TECHNICAL_FAILURE` with the reason
   in `evidence.refused_reason`.
2. *Upsert trusted the LLM's self-reported status.* Fix:
   `upsert_method` stores changed/new content as `TESTING` only, rejects
   structurally invalid definitions and version regressions, and only a
   clean live execution through `validate_document` promotes to `ACTIVE`.
3. *No live-test-before-promotion on the MCP path.* Fix: promotion gate in
   `validate_document` (see the MCP section in §2).

Priority-3 finding on the underlying case: the DMA SelfVerification form
(live-checked) requires `CrewCDCNo`, `Serial` (the certificate serial),
`CrewPassport` (the holder's passport number), and `ReplyEmail`, labelled
**“Requester's Email”** — the portal delivers the verification result *by
email to the submitter*. `ReplyEmail` therefore can never be extracted from
a document — but it is also never *checked* against one, which makes it
**synthesizable**: it is now declared `contact_only_inputs: ["email"]` on
the method (see the contact-only paragraph in §2 Step 7), and the engine
supplies an inert `.invalid` address automatically.

`M_MY_DMA_001` current state (v4, `TESTING`): `document_type_key` corrected
from the erroneous `IN_COC` to **`MM_COC`** — country-scoped keys are now
derived generically (`registry/document_types.py::COUNTRY_SCOPED_SUFFIXES`:
a Myanmar COC is `MM_COC`, an Indian COC stays `IN_COC`, INDOS is
deliberately not country-scoped since it is intrinsically an Indian
database). `passport_number` (`CrewPassport`) remains a **hard identity
field pending a live structural probe**: unlike the reply email, a passport
number *could* be cross-checked against records — faking it blind could
produce a false REJECTED. The probe (real CDC No + Serial paired with an
obviously fake passport, response compared against the same submission with
the real passport) WAS run on 2026-09-25 with real values, and the outcome
settles the case:

- **The "Myanmar needs Burmese OCR" claim is REFUTED.** The earlier
  conclusion came from an ad-hoc local tesseract probe only. The production
  OCR service (`ocr/client.py` → `OCR_API_URL`) reads the same Myanmar COC
  cleanly: structured English-labelled fields including `Certificate No:
  2DK004368`, `SIRB No: 80484`, name, date of birth, and distinguishing
  marks (the local tesseract run had garbled exactly these). The person's
  passport and CDC scans extract equally cleanly (`Passport No: MK670211`,
  `BOOK NO.: 80484`). The Burmese-script lines are decorative
  translations; every identity field is Latin-script and extracted.
- **CrewPassport is COSMETIC (probe-evidenced).** The three-way probe
  (fake passport vs real passport, same CDC+Serial; bogus CDC as control)
  produced structurally identical responses for fake and real passport
  while the bogus CDC control differed — and a confirmation POST with the
  fake passport returned the *matched record* (holder name, CDC, certificate
  number in the response body). The portal does not cross-check
  CrewPassport against records. Evidence: `probe_evidence/`
  (gitignored), with `vouches_for: ["passport_number"]` — the upsert gate
  accepts an identity-named `contact_only` declaration ONLY when the
  evidence explicitly names that field (fail-closed: an unscoped verdict
  vouches for nothing).
- **Serial must be the certificate's own number.** Live-confirmed: POST
  with `Serial = 2DK004368` (the COC's Certificate No) returns the record;
  the page print-serial (`S/N 1416835`) or an empty Serial makes their
  controller crash (HTTP 500 — the same failure class as the original
  incident). The method therefore maps `Serial ← {{document_number}}`.
- **`M_MY_DMA_001` v6 is ACTIVE — promoted through the real gate.** Steps
  use the executor's new `GET_HTML` / `EXTRACT_FROM_HTML` actions (with
  per-run cookie persistence — an ASP.NET anti-forgery POST needs the
  session cookie paired with the token), `required_inputs` are
  `document_number` (Serial), `cdc_number` (CrewCDCNo, the lookup key), and
  the probe-evidenced contact-only `email` + `passport_number`. The MCP
  `validate_document` live test executed cleanly in the Docker sandbox and
  promoted `TESTING → ACTIVE`.
- **End-to-end person-folder run is REAL and VERIFIES.**
  `main.py --folder person_folders/hein_htet` (COC + passport + CDC scans):
  OCR → per-document fixtures (gitignored) → the COC verification resolved
  `cdc_number` cross-document from the CDC scan, executed the live DMA
  lookup, and field comparison returned **`VERIFIED` / `DocumentResult
  VALID`** with the redacted profile intact. The passport and CDC documents
  honestly report `VALIDATION_UNAVAILABLE` (no verification source for
  those document types yet) — the folder verified the one document a source
  exists for, one subject at a time, without merging profiles.

Notes for the next person: `cdc_number` was added as a first-class
credential key (extractor allowlist + prompt + sibling aliases `BOOK NO.`/
`SIRB No.`), because the DMA lookup key is the CDC book number, which lives
on the CDC document, not the COC. Probe fingerprints are coarse
(512-byte length buckets + signature flags); re-run the probe if the portal
ever changes its response shape.

**c4. The model-toggle discrepancy was an operator-side mix-up, and the
logs were right.** A run's logs consistently showed `model='AI_Local'` even
though the operator believed the frontier model was enabled. Verified
directly: `.env` contains `LLM_MODEL=AI_Local` and no frontier model name is
configured anywhere. The single toggle is `LLM_MODEL` in `.env`, read at
call time by `utils/llm_client.py::get_llm_config` — every LLM call site
(engine, discovery, validator self-healing, fallback agent, vision calls)
resolves through it, so all log lines naming `AI_Local` were accurate and no
frontier-model credits were spent. To escalate, set `LLM_MODEL` to the
frontier model name (same `LLM_URL` endpoint, different model in the
payload) and confirm with the `LLM call: model=...` log line on the next
run.

---

## 6. Known risks, ranked by severity

1. **PII is tracked in git — including history.** Despite the `.gitignore`
   rule `discovery_report_*.md`, **19 report files were added before the
   rule and are still tracked** (`git ls-files` confirms), including
   `discovery_report_SID Anup-1_20260921_165106.md`, whose content references
   the holder's document filename. `tests/test_field_comparison.py` line 49
   embeds a real person's full name in a fixture, and
   `prompts/local_extraction.txt` uses the same full name in its redaction
   example — both are in the current HEAD **and** in the initial commit
   (`d1f29c3`), so **`.gitignore` alone does not fix this: git history still
   contains the name.** Before the repo is shared anywhere: `git rm --cached`
   the tracked reports and databases (`method_registry.db`,
   `verification_sources.db`), replace the fixture and prompt example with
   synthetic values, and rewrite history (e.g. `git filter-repo`) — or treat
   the repo as permanently containing PII.
2. **Raw PII leaves the machine today.** As detailed in §5c1: extraction
   sends raw OCR output to an external LLM; ambiguous verification judgments
   send raw profile fields and registry responses to the same external
   endpoint (`LOCAL_LLM_URL` unset → `LLM_URL` fallback in
   `utils/local_llm_client.py`). Setting `LOCAL_LLM_URL` to a genuinely
   local endpoint — or making the local client hard-fail when it isn't set —
   would restore the documented boundary. A provider swap or endpoint change
   can silently re-break this; re-verify the two client files after any such
   change.
3. **The MCP server remains an unaudited, parallel PII entry point — but the
   September 2026 incident's gaps are closed.** What changed: incomplete/
   placeholder submissions are now impossible from any path (the runner-level
   guard, §5c3), methods can no longer be stored `ACTIVE` without a clean
   live execution, and the fallback agent's prompt tells it to live-test and
   report honestly. What did NOT change: `validate_document` still forwards
   caller-supplied inputs verbatim with no `credential_provider` and no
   provenance logging — a caller supplying *real-looking* raw personal data
   gets it sent to the live registry as-is. Before this transport is exposed
   further: decide who can reach the stdio transport, and whether
   `validate_document` should require a documented provenance marker or
   refuse raw-PII-shaped values. Related and smaller: MCP decisions skip
   `compare_response`, so field-comparison methods can only ever return
   `UNCERTAIN` / `UNKNOWN` (with HIGH evidence quality) through this door —
   the two entry points disagree about the same method (though this also
   means MCP live tests cannot falsely promote on a mistaken VERIFIED).
4. **Dependency drift masked by a working local venv — found and fixed, with
   a residual process gap.** `requirements.txt` was missing both `mcp` (the
   MCP server's SDK) and `beautifulsoup4` (imported host-side by
   `validation/field_comparison.py` and `generation/generator.py`; it was
   only listed in `Dockerfile.executor`). A fresh clone could not import
   `mcp_server.server` at all — the 131 passing tests ran only because the
   local venv happened to contain the packages. Fixed during this audit by
   pinning `mcp==2.2.0` and `beautifulsoup4==4.15.0`, and verified by
   installing `requirements.txt` into a brand-new venv and importing both
   modules. Residual risk: nothing (no CI) re-runs the clean-venv check, so
   the next missing pin will again be masked by a working local environment
   until someone looks.
5. **Tautological structural test / orphaned probe.** As detailed in §5c2:
   the narrow generation path never runs the discriminator probe, and the
   structural test compares a fake response against the fake input itself,
   so any non-empty, marker-free response passes. A wrong-endpoint method for
   a new registry could be promoted `ACTIVE` and produce misleading results
   for real documents. Fixes: wire the probe (or a confirmed-marker check)
   into the narrow path, or make the structural test assert on response
   *shape* (e.g. a genuine not-found signature) rather than only the final
   label.
6. **Not-found fingerprinting.** Currently, any non-empty response that
   doesn't parse as a match and contains no near-matching fields resolves to
   `REJECTED` → `INVALID` (`field_comparison.py` classifies on error-marker
   words and fuzzy scores alone). A CAPTCHA page, a WAF block page, a session
   expired page, or a 5xx-derived HTML page — none of which typically contain
   the words "not found" — would currently be treated as **a confirmed
   invalid document**, with `evidence_quality: HIGH` for HTTP methods. This
   should be narrowed to a captured not-found signature (response-length band
   + structural fingerprint from the known-fake test).
7. **Everything else, smaller and known:**
   - Only India has been exercised end-to-end; the extractor and field
     comparison are designed to generalize but unproven against a second
     registry (§5c2's failure shape makes first contact with a new registry
     riskier than it needs to be).
   - JavaScript SPA registries can't be verified yet — the browser executor
     is a stub (`executors/browser_executor.py`); the SID discovery path
     already found such a site.
   - The `method_schema` version is bumped by hand; deriving it
     automatically (e.g. a hash of generator logic) would prevent forgetting
     a bump (`registry/models.py::CURRENT_METHOD_SCHEMA`).
   - **Seed-credential machinery removed (decision, 2026-09-26).**
     `registry/seed_store.py`, `engine/onboard_seed.py`,
     `engine/upgrade_method.py` and their tests were deleted on purpose:
     confirmed markers now come only from the known-fake structural probe
     and the sanitized not-found-signature path, and no encrypted
     credential store exists anymore. Consequence to remember: the
     two-sided fake-vs-real discriminator probe
     (`generation/discriminators.py::_discover_discriminators`) is retained but
     has **no production caller**; re-introduce a caller — or delete the
     function — the next time a registry needs confirmed success/failure
     markers.
   - Fuzzy thresholds (90 / 55 in `field_comparison.py`) were tuned against
     one document's noise profile; watch the local-LLM escalation rate as
     real traffic grows.

---

## 7. Implementation report — the self-generating method agent (September 2026)

This section records what was actually implemented and verified across the
last working sessions (commits `1b1806a` → `6bc4197`). Every item lists the
file(s) changed and the evidence it rests on. All LLM calls run on the single
configured endpoint with `LLM_MODEL=AI_Local` and
`USE_LOCAL_LLM_ONLY=true` — no frontier model, no fallback chain (§5c4);
this constraint was held throughout.

### 7.1 Registry routing fixed (a method that existed but was never found)

- **`registry/repository.py::find_methods`** now matches on the CANONICAL
  document-type key (`expected_responses.document_type_key`), with country-
  scoped variants (MM_COC…) and an exact-text fallback. Before: SQL `LIKE`
  substring matching on raw text, which broke on punctuation — the ACTIVE
  INDOS method (`…(INDOS) CERTIFICATE`) was invisible to a profile saying
  `"INDos Certificate"`, so the pipeline re-generated a candidate that failed
  its structural test and reported a valid document as `UNKNOWN`.
- **`registry/document_types.py::document_type_key`** resolves
  parenthesized extraction wording (`"Continuous Discharge Certificate
  (CDC)"` → `IN_CDC`) by trying each parenthesis section.
- **`registry/repository.py::update_expected_responses`** — a granular
  persistence path for observations (field mappings, not-found signatures).
  The old path (`register_method`) wrote the whole row back, silently
  demoting an ACTIVE method to TESTING.

### 7.2 Executor resilience against environmental noise

`executors/http_executor.py` (step runner; helpers in `http_helpers.py`,
classification in `http_decider.py`):

- **Transient-error retries** around every request helper (DNS blips,
  `No route to host`, connection resets, 5xx) with jittered backoff. One
  real run lost a correct method to a DNS failure host-side AND a routing
  failure in-container, minutes before both endpoints answered HTTP 200.
- **In-run captcha redo**: a captcha-rejection response (the site refusing
  the solve — environment noise, not a method defect) triggers a bounded
  fetch→solve→request retry loop inside the same execution, mirroring what
  the target SPA itself does. Measured: dgshippingbsid.in accepts the same
  flow 6/6 host-side, so a rejection is a per-attempt event; captchas are
  single-use (resubmitting a solved one returns "expired").
- Captcha-rejection bodies are explicitly excluded from ever matching a
  not-found signature.

### 7.3 Structural-test healing (deterministic first, LLM second)

`validation/validator.py` (attempt loop, signature capture) and
`validation/healing.py` (LLM passes):

- **Confirmed not-found signature capture**: the structural probe submits a
  known-fake value, so the site's deterministic refusal of it IS the site's
  canonical "not found" response — even as HTTP 4xx (dgshippingbsid.in
  answers `400 {"message":"Invalid Input: Application ID not found."}`).
  The validator captures that response as a narrow, executor-validated
  signature (`status` + `contains`), persists it, and retests. A previously
  doomed-but-correct method now passes without any LLM involvement.
- **Whitespace-insensitive matching** (executor + dedup): the same endpoint
  was observed emitting `{"message": "…"}` and `{"message":"…"}` for
  different error classes; exact-substring matching silently failed.
- **Per-method fake inputs** (`execution/safety.py::
  structural_test_value_for`): every required input gets a deterministic
  known-fake value by field role (passport → ZZ0000000, email → …@dvs.invalid,
  …), so multi-input methods (the DMA portal needs serial + CDC + passport
  + reply email) can actually be probed instead of being refused before any
  request.
- **Attempt budget** raised 1 → 3 (`VALIDATION_MAX_ATTEMPTS`); at 1 every
  healing path was dead code. The ladder: signature capture/retest → cheap
  direct LLM rewrite (adopted ONLY if the rewrite passes its own probe) →
  agentic tool-use loop, gated behind `DVS_AGENTIC_HEALING=1` (default on,
  disabled in tests).

### 7.4 Generator: code enforces what the LLM only proposes

`generation/post_process.py` (post-generation enforcement, deterministic;
invoked by `generation/generator.py::_finalize_method`):

- **BROWSER methods rejected outright** — the browser executor is a stub;
  the model had begun emitting BROWSER as an escape hatch.
- **Canonical input-name normalization**: models name required inputs after
  the site's wire fields (`CrewCDCNo`, `CrewPassport`, `Serial`,
  `ReplyEmail`); inputs (never wire params) are renamed to canonical
  document keys (`cdc_number`, `passport_number`, `document_number`) so the
  credential provider and person-folder cross-document lookup can supply
  them.
- **Contact-only inference**: email/phone inputs are auto-declared in
  `expected_responses.contact_only_inputs` (inert `.invalid` synthesis).
- **Metadata injection**: `source_url`/`country`/`document_type` come from
  the confirmed source and classified profile, not the model's echo.
- **Placeholder resolution check**: every `{{…}}` must resolve to a required
  input, a documented runtime value, or a step output var; unknowns are
  promoted to required inputs (honest refusal at execution beats literal
  `{{name}}` junk reaching a live endpoint).
- **Null-mapping fallback**: the narrow path's well-known-name fallback now
  runs per-param on the params the model left unmapped (`txtNo`/`dob` were
  silently dropped when the model mapped another param and punted the rest).
- **Evidence-pinned workflow params**: `searchType`-style params are pinned
  to a page `<select>` option by whole-word match against the canonical
  document-type token. Loose substring matching pinned `DC` for a CDC
  document (substring of `(CDC)`) — caught live and fixed. Punting values
  (`{{document_type_key}}`) are dropped rather than pinned literally.
- `prompts/method_generation.txt` now documents the real step vocabulary
  (GET_HTML/EXTRACT_FROM_HTML anti-forgery flows, contact_only_inputs) and
  the structural-test contract so generation aligns with how promotion is
  decided.

### 7.5 Discovery that works without a search API

`discovery/agent.py`, `db/lookup.py`, `engine/validation_engine.py`:

- **Document-hint fallback**: when search fails entirely (Tavily quota:
  "This request exceeds your plan's set usage limit" — hit during testing),
  the crawl is seeded from `source_discovery_hints` — URLs printed on the
  document by the issuer. (A pre-marking bug made the loop skip the seeded
  hint: "exhausted 0 attempt(s)" — fixed.)
- **Same-host link harvest on deterministic rejects**: a portal root behind
  a login wall still links the public verification page
  (`dmamyanmar.org` → `/AllInOneCertificate/SelfVerification`, accepted at
  confidence 100 purely by crawling).
- **Cumulative routing knowledge** (`remember_source`): once discovery →
  generation → structural validation succeed, the source is tagged with the
  document-type key in `verification_sources.db`. The India source now
  carries `IN_CDC, IN_INDOS`; future documents skip discovery entirely.
- **Source reuse for sibling document types**: when no doc-type-tagged
  source exists, same-country sources are offered to the generator; the
  page-evidence gate (workflow option / API-path token, generalized to
  SID/CDC/COC) decides compatibility deterministically and fails honestly
  when the page does not name the type.

### 7.6 Pipeline robustness

- **`main.py --folder`**: one unextractable document is reported and
  skipped instead of aborting the folder (a 7-document run died mid-way on
  an empty LLM completion before this).
- **`ocr/extractor.py`**: extraction retries (3 attempts) on empty/unusable
  completions and falls back to brace-counting JSON extraction when the
  model annotates its output.
- **`.gitignore`**: repaired (a missing newline had glued two entries);
  `test_*.db` artifacts and OCR cache are ignored.

### 7.7 Verified outcomes on the four test PDFs

| Document | Outcome | Path taken |
|---|---|---|
| `INDOS (20).pdf` | **VERIFIED / VALID / HIGH** | Registry routing fix → existing ACTIVE method used (no regeneration) |
| `SID Anup-1.pdf` | **VERIFIED / VALID / HIGH** | Fully self-generated method: SPA bundle extraction → captcha steps → signature capture from the site's 400 refusal → promotion → LLM-discovered response mapping |
| `2O - HEIN HTET (AIO).pdf` | **VERIFIED / VALID / HIGH** | Fully autonomous chain: hint fallback (search quota exhausted) → link harvest → method generation (anti-forgery steps, normalized inputs, contact-only email) → cross-document passport lookup → live verification ("HEIN HTET, Passport: MK670211, Status: VALID") |
| `ANUP CDC ALL PAGES.pdf` | **Method self-generated and live-confirmed**; document run honestly refuses | Same-country source reuse → evidence-pinned `searchType=CDC` → structural test passed → ACTIVE. Engine pass with real credentials: VERIFIED / HIGH (scores 94.7 / 100 / 100). The booklet's own identity pages are SKIPPED by the OCR service (pages 3,6–12 extracted; page 1/2 with CDC number absent), so the single-document run correctly refused rather than fabricate inputs — an extraction gap, not a method gap |

Also verified in passing: the Myanmar COC and Myanmar SID methods were
independently self-generated and promoted to ACTIVE during folder runs
(later deleted from the registry to re-prove regeneration through the
final code paths — the AIO run above is the evidence).

Test suite: 206 tests, all passing, at every commit in this series.

### 7.8 Modularization pass (2026-09-28)

The four largest working files were broken into small, single-purpose
modules at existing section boundaries — a pure refactor: every function
moved verbatim, behavior unchanged, all 228 tests passing without test-logic
changes (only mock patch-targets moved with their functions).

| Was | Now |
|---|---|
| `generation/generator.py` (1379 lines) | Orchestrator (~360) + `page_fetch.py` + `xhr_contract.py` + `param_mapping.py` + `discriminators.py` |
| `validation/validator.py` (594 lines) | Attempt loop + signature capture (~340) + `validation/healing.py` (LLM rewrite pass + agentic tool-use loop) |
| `executors/http_executor.py` (829 lines) | Step runner (~360) + `http_helpers.py` (HTTP verbs, retry, compaction, captcha fetch) + `http_decider.py` (`decide()`, not-found signatures) |
| `discovery/agent.py` (838 lines) | Orchestrator (~295) + `llm.py` (chat + parsing) + `search.py` (queries + Tavily) + `page_fetch.py` (fetch + JS harvest) + `judge.py` (pre-checks + model judgment) + `crawl_queue.py` (hints + queue) |

Constraints the split had to respect (they are part of the design now):

- **Docker sandbox imports.** The runner copies flat files, not a package
  tree, so `docker_runner.py` ships `http_helpers.py`/`http_decider.py`
  next to the executor when the HTTP executor runs, and the executor uses
  the dual import (`try: from executors.http_helpers import …` / `except
  ImportError: from http_helpers import …`) — the same pattern the
  browser executor already used for `llm_client`.
- **Re-export facades.** `generation/generator.py` re-exports every moved
  function, so `engine/`, the CLI, and unit tests import from one place.
  Mock patch-targets in tests point at the module that now *owns* the
  function (e.g. `generation.discriminators._probe_endpoint`), because
  patching only works where the name is looked up, not where it is
  re-exported.
- **Related bug fixed in the same pass (2026-09-28):** a newly generated
  `field_match` method's structural probe previously could never reach a
  definitive verdict. Three gaps compounded: the generator's rebuilt
  `expected_responses` silently dropped the model's declared
  `success_keywords`/`failure_keywords`; `decide()` short-circuited to
  `UNCERTAIN` for every `field_match` method regardless of keywords; and
  the validator captured not-found signatures only on HTTP 4xx. With the
  keywords preserved, the keyword channel reachable in `decide()`
  (`executors/http_decider.py`), and signature capture extended to a
  REJECTED keyword verdict on any status (e.g. DMA's HTTP 200 + 19-char
  error body), the probe → capture → retest ladder completes and this
  class of registries validates instead of dying UNHEALTHY.
