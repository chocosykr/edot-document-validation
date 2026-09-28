# What a "Method" Is in the DVS — A Complete Explanation

*Written to be handed to a web-based LLM (ChatGPT, Claude, Gemini, …) as a
standalone explanation. It assumes no prior knowledge of this codebase.
"Method" here is a domain term of the art — by the end you will know exactly
what it means, what one looks like, and how the system creates them by itself.*

---

## Part 1 — The big picture: what problem does a "method" solve?

The Document Validation System (DVS) checks whether a scanned seafarer
document (a sailor's licence, certificate, or seamanship booklet) is genuine.
The way you check that in the real world is:

1. Read the document with OCR (who it belongs to, what its number is, which
   country issued it).
2. Find the **official government verification portal** for that document type.
3. Fill in the portal's lookup form (document number, date of birth, …) and
   submit it.
4. Compare what the portal says with what the document says. If the registry
   returns a record whose number/name/birth-date match — the document is
   genuine. If the registry says "no such record" — it is fake.

Steps 1 and 4 are generic and work for every country. But step 3 is different
for every portal in the world: one government site wants a `GET` request with
query parameters, another wants a `POST` form, another needs an anti-forgery
token fetched first, another needs a `searchType` dropdown value, and they all
name their form fields differently (`txtNo`, `CrewCDCNo`, `app_id`, …).

A **method** is the system's solution to that variation: a small, structured,
self-contained **recipe for verifying one document type at one portal** —
written as data (JSON), not code. Think of it as a saved "how to query this
one registry" card.

Because methods are data:

- they can be stored in a database (the **method registry**) and reused,
- they can be **generated automatically** by looking at a portal's web pages,
- they can be **tested** before use, and
- they can be **improved/repaired** by the system when they stop working.

---

## Part 2 — What a method actually looks like

A method is a JSON bundle. Here is a **real one** from the registry — the
method that verifies Indian INDOS certificates against the government's
eSamudra service:

```json
{
  "method_id": "M_F7F62490",
  "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDOS) CERTIFICATE",
  "country": "INDIA",
  "issuer": null,
  "method_type": "HTTP",
  "version": 1,
  "source_url": "http://220.156.189.33/esamudraUI/checkerajaxservlet",
  "required_inputs": [
    "document_number",
    "date_of_birth"
  ],
  "execution_steps": [
    {
      "action": "REQUEST",
      "method": "POST",
      "url": "http://220.156.189.33/esamudraUI/checkerajaxservlet",
      "params": {
        "processId": "PPIndosCheck",
        "searchType": "Indos",
        "txtNo": "{{document_number}}",
        "dob": "{{date_of_birth}}"
      },
      "param_location": "query"
    }
  ],
  "expected_responses": {
    "comparison_mode": "field_match",
    "method_schema": "field-comparison-v3",
    "document_type_key": "IN_INDOS"
  },
  "limitations": [],
  "status": "ACTIVE"
}
```

Reading it in plain English:

> *"To verify an Indian INDOS certificate: make a POST request to this URL
> with four query parameters. Two of them are fixed values the portal expects
> (`processId=PPIndosCheck`, `searchType=Indos`). The other two are the
> document's own facts — `txtNo` gets the document number and `dob` gets the
> date of birth, wherever the document being checked came from. Whatever comes
> back should be compared field-by-field against the document. That's it."*

### The `{{double_braces}}` convention

Any value wrapped in double braces is a **placeholder**, filled in at runtime
with a real value extracted from the document being verified:

| Placeholder | Meaning | Source |
|---|---|---|
| `{{document_number}}` | the number printed on the document | OCR of the document (kept in memory only) |
| `{{date_of_birth}}` | the holder's birth date | OCR, or resolved from the person's *other* documents in folder mode |
| `{{full_name}}` | the holder's name | OCR |
| `{{cdc_number}}`, `{{passport_number}}`, `{{serial_number}}` | other credential numbers | OCR / cross-document resolution |
| `{{email}}` | a notification address | synthesized inert value (the portal never reads it) |
| `{{__RequestVerificationToken}}` | anti-forgery token | fetched by an earlier step of the same method |

Everything **not** in double braces is a constant the portal requires
(`processId`, `searchType`, fixed form fields, …).

### A more complex real method — multi-step with an anti-forgery token

Some portals won't accept a bare form post; they demand a session token
fetched from the page first. This method (Myanmar's maritime authority,
dmamyanmar.org) has three steps:

```json
{
  "method_id": "M_MYANMAR_SID_001",
  "document_type": "Seafarers' Identity Document",
  "method_type": "HTTP",
  "required_inputs": ["document_number", "email", "passport_number", "cdc_number"],
  "execution_steps": [
    { "action": "GET_HTML",
      "url": "http://www.dmamyanmar.org/AllInOneCertificate/SelfVerification",
      "output_var": "page_html" },
    { "action": "EXTRACT_FROM_HTML",
      "html": "{{page_html}}",
      "selector": "input[name='__RequestVerificationToken']",
      "output_var": "__RequestVerificationToken" },
    { "action": "REQUEST",
      "method": "POST",
      "url": "http://www.dmamyanmar.org/AllInOneCertificate/Verify",
      "params": {
        "CrewCDCNo": "{{cdc_number}}",
        "Serial": "{{document_number}}",
        "CrewPassport": "{{passport_number}}",
        "ReplyEmail": "{{email}}",
        "__RequestVerificationToken": "{{__RequestVerificationToken}}" } }
  ]
}
```

Step 1 downloads the page; step 2 pulls the token out of its HTML with a CSS
selector; step 3 submits the form with both the document's facts and the
token. The `output_var` of one step becomes the `{{placeholder}}` of a later
step — that's how a method chains requests.

### The field reference

| Field | Purpose |
|---|---|
| `method_id` | Unique ID (hash-based, e.g. `M_F7F62490`) — the registry's primary key. |
| `document_type` / `country` | **What** this method verifies and **where**. The router matches incoming documents to methods on these. |
| `method_type` | `HTTP` (plain web requests) or `BROWSER` (headless browser). BROWSER methods are deliberately not generated — HTTP-first, deterministic. |
| `source_url` | The portal page the method was derived from (provenance). |
| `required_inputs` | Placeholders that MUST have real values before anything is submitted. If the document can't supply them, the system refuses to run — it never submits blanks, placeholders, or guesses. |
| `execution_steps` | The recipe itself — an ordered list of HTTP/HTML operations. |
| `expected_responses` | How to judge the answer (see below). |
| `limitations` | Free-text caveats. |
| `status` | Lifecycle: `TESTING` → `ACTIVE` → (`UNHEALTHY`/`INACTIVE`). Only `ACTIVE` methods are ever used on real documents. |

### `expected_responses` — how the answer is judged

```json
"expected_responses": {
  "comparison_mode": "field_match",
  "method_schema": "field-comparison-v3",
  "document_type_key": "IN_INDOS",
  "field_mapping": { "text_mappings": { "name": "full_name", "dob": "date_of_birth", "indosNo": "document_number" } },
  "not_found_signatures": [ { "status": 400, "contains": "Application ID not found" } ]
}
```

- `comparison_mode: "field_match"` — the response is parsed into fields and
  compared against the document's fields (fuzzy text match, calendar-aware
  date comparison, image comparison for photos/signatures).
- `field_mapping` — translates the portal's response keys into the system's
  canonical field names. Each method carries its own, because portals name
  fields differently (`indosNo` vs `certificate_no` vs `DocNum`).
- `not_found_signatures` — what "no such record" looks like **at this
  specific portal**, learned automatically (see Part 4). This is what allows
  a legitimate REJECTED verdict. Without a learned signature, an
  unparseable/error-ish response is NEVER treated as "not found".

---

## Part 3 — How a method is generated (the pipeline)

This is the core capability: for a document type the system has never seen
before, it builds the method itself. The pipeline has six stages.

### Stage 0 — What the system starts with

A document has been OCR'd and a redacted profile extracted:

```json
{
  "document_type": "Continuous Discharge Certificate",
  "issuing_country": "India",
  "document_holder": { "name": "[PERSON_NAME]", "date_of_birth": "[DATE_OF_BIRTH]" },
  "source_discovery_hints": ["www.dgshipping.gov.in"]
}
```

(Real credential values are kept in a separate in-memory channel; nothing
PII-heavy is needed until execution time.)

### Stage 1 — Find the portal (discovery)

Order of preference, cheapest first:

1. **Registry router**: an ACTIVE method for this country + document type?
   Done — no generation needed.
2. **Confirmed-sources DB**: has the system *learned* this portal before?
   (Every successful generation is remembered here, keyed by country and
   document-type key — so a portal is only ever "discovered" once.)
3. **The document's own hints**: documents often print the issuer's URL
   (letterhead, "verify at …", QR codes). These are first-party seeds.
4. **Web search** (if quota allows): query generation → fetch candidates.

Then an iterative crawl: fetch a page → cheap deterministic checks (login
wall? error page?) → judge whether this page is a lookup form for THIS
document type → if not, follow the model's proposed next move (a same-domain
link, a JS-discovered API endpoint, or a refined search) up to a hard attempt
cap. The output is a `source_url` — or an honest "no source found".

### Stage 2 — Read the portal's machinery (page structure extraction)

The generator downloads the portal page and extracts, **deterministically**
(no model yet):

- all `<form>`s and their inputs (names, types, ids),
- all `<select>` dropdowns and their `<option>` values (the "workflow options"),
- all inline JavaScript, and the external JS bundles,
- **XHR contract extraction**: parsing the JS to find the actual AJAX call the
  page makes — endpoint URL, HTTP verb, parameter names, and whether params go
  in the query string or body.

For esamudra that produces:

```json
{
  "endpoint": "http://220.156.189.33/esamudraUI/checkerajaxservlet",
  "verb": "POST",
  "param_location": "query",
  "dynamic_params": ["txtNo", "dob", "searchType"],
  "static_params": { "processId": "PPIndosCheck" }
}
```

This is the portal's own machinery, read from its own code — not guessed.

### Stage 3 — Map document fields onto portal parameters (the LLM step)

The one place an LLM is used: given the extracted contract + the available
document fields, a local LLM proposes a mapping (which parameter should carry
the document number, which the birth date, …).

The model's answer is then **validated and completed deterministically**:

- **Placeholder/punting check**: if the model answers `{{document_type_key}}`
  or `null` instead of a concrete value, that answer is discarded.
- **Well-known-name fallback**: for any parameter the model punted on, the
  name itself is matched against *generic seafarer-credential vocabulary*
  (`txtNo/docNo/certNo…` → document_number; `dob/birth…` → date_of_birth;
  `cdc/sirb…` → cdc_number; `serial…` → serial_number; `passport…` →
  passport_number). This is document-domain language, not portal-specific.
- **Dispatch-selector pinning**: if a request parameter corresponds to a
  `<select>` on the page (detected structurally — the param and the select
  share a vocabulary word, e.g. servlet param `searchType` ↔ select
  `cmbSearch_by`), the system pins it to the option whose text/value names
  THIS document type, with whole-word matching so `DC` never matches inside
  `(CDC)`. If it cannot be pinned, **generation fails loudly** — a method
  that would submit an incomplete request must never ship.

### Stage 4 — Assemble the method bundle

The validated mapping is assembled into the JSON bundle (Part 2): steps,
placeholders, required_inputs, expected_responses with the canonical
`document_type_key`. Anything the evidence does not support is left out
rather than invented.

### Stage 5 — Prove it works before trusting it (test-before-trust)

The bundle is registered with status `TESTING` and put through a **structural
test** executed inside a sandboxed Docker container (no host network access,
no credentials, resource-capped):

- A **known-fake probe** is submitted: a deliberately fake document number
  (format-plausible, e.g. `ZZ…`-style values). A real registry must refuse
  it. Passing means the method actually reaches a real lookup endpoint.
- The refusal response is **captured as the method's not-found signature** —
  the learned shape of "no such record" for this portal (Part 2's
  `not_found_signatures`).
- The method is then retested with the signature in place: the refusal now
  classifies as a genuine REJECTED, and the test passes without any human
  or model involvement.

Only after this does the method become `ACTIVE`.

### Stage 6 — Remember

On the first real, verified run the portal URL is written to the
confirmed-sources DB (Stage 1's option 2). Next time a document of this type
arrives, generation is skipped entirely.

### Failure semantics (what the agent refuses to do)

- No page evidence of a workflow → refuse (never fabricate).
- Portal's page doesn't mention this document type → refuse (a CDC form
  cannot verify an SID, even on the same site).
- Dispatch selector can't be pinned → refuse.
- The model proposes a BROWSER-only method → refuse.
- Required inputs unavailable from the document → refuse at run time.
- **Any failure is a verdict-free outcome** (`TECHNICAL_FAILURE` /
  `VALIDATION_UNAVAILABLE`), never a guess dressed up as an answer.

---

## Part 4 — Runtime: how a method is used and how answers are judged

1. **Route**: document → country + document type → find ACTIVE method.
2. **Feed**: fill placeholders with real values (credentials in memory only).
   Garbage credentials are dropped before submission (a birth date that
   cannot be a real calendar date is treated as missing, never submitted).
3. **Guard**: the required-inputs check runs at the last choke point before
   any network traffic — tokens/placeholders/blanks are refused.
4. **Execute**: the recipe runs inside the Docker sandbox; transient network
   errors and 5xx are retried with backoff; so are tiny/error-shaped bodies
   (retrying an idempotent lookup is always safe).
5. **Classify the answer by grounding**, in this order:
   - empty body → TECHNICAL_FAILURE;
   - matches the method's **learned** not-found signature → REJECTED (genuine);
   - framework error page (ASP.NET/Java/PHP crash pages — platform-universal
     shapes) → TECHNICAL_FAILURE;
   - tiny body with no parseable record fields → TECHNICAL_FAILURE (no proof
     a lookup happened — this is the rule that prevents the classic failure
     of reporting INVALID with HIGH confidence off a portal outage);
   - otherwise parse the record fields, map them through the method's
     field_mapping, compare with the document: all strong matches → VERIFIED;
     nothing matches → REJECTED; in between → a local LLM judge decides
     MATCH / NO_MATCH / UNCLEAR.
6. **Report**: VERIFIED / REJECTED / UNCERTAIN / TECHNICAL_FAILURE /
   VALIDATION_UNAVAILABLE, with evidence (scores, raw response) attached.

The two rules that matter most:

- **A definitive INVALID requires positive evidence** — either a learned
  not-found signature or parsed record fields that mismatch. Anything
  ambiguous degrades to a verdict-free outcome.
- **Every portal-specific fact lives in data** (methods, the sources DB),
  not in code. The code implements only generic mechanisms. A brand-new
  country's registry requires no code change — just a new document that
  points at it.

---

## Part 5 — One-paragraph summary (for the impatient)

A **method** is a small JSON recipe that tells the system exactly how to look
up one document type at one government portal: which URL to hit, which
parameters carry the document's facts (written as `{{placeholders}}`), any
fixed values or tokens the portal needs, and how to compare the answer with
the document. The system **generates methods itself** by reading a portal's
pages (forms, dropdowns, JavaScript) to extract the real AJAX contract, using
an LLM only to propose the field mapping and then deterministically
validating/completing that mapping from the page's own evidence, assembling
the bundle, and proving it works by submitting a known-fake lookup inside a
sandbox (which also teaches it what "not found" looks like at that portal)
before marking it ACTIVE. At runtime the recipe is filled with the document's
real values and executed in the sandbox; the response is judged by grounding
— learned not-found shapes reject, framework crashes and unparseable
fragments refuse to judge — and the whole portal-specific knowledge base
grows as data, so the same code works for any country's registry without
modification.
