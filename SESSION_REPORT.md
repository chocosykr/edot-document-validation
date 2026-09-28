# DVS Session Report — Self-Generating Method Agent

**Period:** 2026-09-21 → 2026-09-28 · **Branch:** `main` · **Final state:** 228 tests green, 7/7 registry methods ACTIVE, all testable documents VERIFIED.

This file explains what was done across the whole effort, why it took many
steps, and what was learned live about the registries.

---

## 1. Goal

The system (DVS — seafarer document validation) must be able to **generate a
verification method for a government registry on its own**: look at a scanned
seafarer document, find the issuing authority's public verification portal
(with the search API quota-exhausted), infer the HTTP workflow from the
portal's own pages, generate a method, structurally test it against the live
endpoint, and only then use it to verify the document. No hardcoded per-registry
code. Privacy/safety focus was explicitly de-prioritized (prototype).

**Test corpus** (`InputFiles/`): 4 PDFs — `INDOS (20).pdf`, `SID Anup-1.pdf`,
`2O - HEIN HTET (AIO).pdf` (Myanmar COC), `ANUP CDC ALL PAGES.pdf` — plus two
person folders (`hein htet/`, `anup/`) for cross-document testing.

---

## 2. Phase 1 — Baseline audit and safety work (commit `1b1806a`)

- Audited every live-submission path; wrote `ARCHITECTURE.md` from scratch.
- Deleted the seed/onboard machinery (`registry/seed_store.py`,
  `engine/onboard_seed.py`, `engine/upgrade_method.py`) — decision recorded §6.7.
- Person-folder mode, MCP gating, agentic fallback loop (`agent_fallback.py`).

## 3. Phase 2 — Self-generation improvements (commit `c2bf9d3`)

- Registry lookup by canonical doc-type key (country-scoped variants).
- Executor transient-failure retries (`_with_retry`), in-run captcha redo.
- Structural healing: per-method known-fake probe inputs (`ZZ…`),
  not-found signature capture, `VALIDATION_MAX_ATTEMPTS=3`.
- Generator enforcement: rejects BROWSER-only plans, canonical input
  renaming, placeholder-resolution checks.
- Search-free discovery: the document's own printed URL hints seed the crawl.
- Result: INDOS, SID (Anup), Myanmar AIO all VERIFIED end-to-end; SID and AIO
  methods fully self-generated.

## 4. Phase 3 — Indian CDC self-generation (commits `f15f7b0`, `6bc4197`, `b5a6097`)

- Same-country source reuse (`db.lookup.lookup_country_sources`).
- `searchType` pinning by whole-word match against the page's `<select>` options
  (loose substring matching had pinned `DC` for CDC — live-confirmed bug).
- ARCHITECTURE.md §7 implementation report.

## 5. Phase 4 — OCR investigation (the "missing pages" question)

Asked: *was the CDC booklet's page 1–2 ever tested separately?* It hadn't been.
Findings (all live-tested):

- The OCR pipeline renders **each PDF page to PNG and posts it individually** —
  pages were never "skipped" by our code.
- The LAN OCR service **intermittently answers HTTP 200 with `pages: []`** for a
  perfectly readable page (same PNG: 1 page → 0 pages → 1 page on three tries).
- The cached booklet run had **silently lost 6 of 13 pages**, and the loss was
  **cached permanently** (cache keyed by file SHA-256).
- Even successful reads were rough: name "AJSP KAMBO" (→ ANUP KAMBOJ), DOB
  garbled.

## 6. Phase 5 — Wipe-and-rerun (commit `b8eda1f`)

The registry was wiped (`./reg delete ALL`) and every document rerun from
scratch. The reruns exposed **five real defects**, each fixed with live
evidence:

| # | Defect | Fix |
|---|--------|-----|
| 1 | OCR empty-page responses cached forever | `ocr/client.py`: retry empty results (`OCR_PAGE_ATTEMPTS`, default 3); **never cache a run that lost pages** |
| 2 | Bare-domain hints dropped (`www.dgshipping.gov.in` has no scheme) | `discovery/agent.py::_hint_urls` upgrades whole-hint bare domains to `https://`; extraction prompt documents the hint contract |
| 3 | Generation refusal off a DB source ended the run | `engine/validation_engine.py` falls through to discovery (narrow: TECHNICAL_FAILURE only); refusal preserved if discovery finds nothing |
| 4 | Transient service errors became definitive INVALID | esamudra answered a valid lookup with *"Sorry ! Unable to process your request,please try later"* (HTTP 200) and the classifier called it REJECTED/HIGH. Split markers: service-error → TECHNICAL_FAILURE (executor retries it at body level too); only genuine not-found phrasing rejects |
| 5 | Punted dispatch selector shipped incomplete requests | mapping LLM returned `searchType=null` → pinning loop skipped → request went out **without** `searchType` → esamudra errors (the exact 108-char "try later" body). Pinning now consults the page's `<select>` regardless of the LLM punt; generation fails loudly if it cannot pin |

Also: OCR-garbled credentials are now dropped before live submission
(`execution/safety.py::credible_credentials`) — a DOB that cannot be a real
calendar date ("07-BSF-92") or a DOB after the issue date is treated as
missing, never submitted.

**Key discovery about the CDC booklet:** it **never prints the holder's date
of birth** (verified across all 13 OCR'd pages — the earlier "07-BSF-92" read
was OCR noise). esamudra's CDC lookup requires DOB, so single-doc CDC
verification is impossible by design; in folder mode the DOB resolves
cross-document from the INDOS document. Final outcome after all fixes:

```
FOLDER SUMMARY — anup
  ANUP CDC ALL PAGES.pdf: VERIFIED / VALID   ← was "impossible" that morning
  INDOS (20).pdf:         VERIFIED / VALID
  SID Anup-1.pdf:         VERIFIED / VALID
```

Support tooling: `tools/prewarm_cdc_ocr.py` — resumable chunked OCR pre-warm
(persists per-page results, assembles the cache entry when complete) built
because full-booklet OCR exceeded the 10-minute command budget on a flaky day.
Grayscale-JPEG rendering recovered two pages the RGB PNG could not.

## 7. Phase 6 — Myanmar SID (the "is there no document?" question)

**There is a document**: `2O - HEIN HTET (SID).pdf`, SID no. `MS0074452`,
holder HEIN HTET. What was wrong and what is now known:

1. **A method existed** (`M_MYANMAR_SID_001`, self-generated from the
   dmamyanmar.org AllInOneCertificate page) but it was **mis-mapped**: it
   stuffed the SID number into *both* `CrewCDCNo` and `Serial`.
2. Live probing decoded the form's real semantics: `CrewCDCNo` = the seafarer's
   **CDC book number** (`80484` — printed on the AIO doc), `Serial` = the
   **certificate's serial** (`MS0074452` for the SID doc). With the correct
   mapping the endpoint returned a **full record** for the SID document.
3. The method in the registry was corrected: `CrewCDCNo → {{cdc_number}}`,
   `cdc_number` added to required inputs (resolved cross-document from the AIO
   document at runtime).
4. dmamyanmar's endpoint is **genuinely unstable server-side**: the *same*
   valid combo returned a full record, then the 19-byte `"VerificationError"`
   string, then a full record again; later it began answering everything
   (including previously-valid combos) with an ASP.NET
   **NullReferenceException 500** that persists even after quiet periods.
   Their controller crashes on their own data.
5. System responses, in order: `"VerificationError"` now counts as a
   retryable service error (never a one-shot REJECTED); persistent 500s and
   error strings degrade to **TECHNICAL_FAILURE** — an honest "cannot verify
   right now", never a fabricated INVALID.

Current folder state (`hein htet`): AIO **VERIFIED/VALID**; SID correctly
mapped but blocked by the registry's server-side instability (TECHNICAL_FAILURE);
the remaining documents have no verifiable public source (Liberia PEC portal
dead, bank/NOK/contract docs are not registry-verifiable) or lack required
credentials (CDC jpg missing passport/email) — reported honestly as
VALIDATION_UNAVAILABLE.

---

## 8. Live facts learned about the registries

- **esamudra** (`220.156.189.33`, India): `POST /esamudraUI/checkerajaxservlet?processId=PPIndosCheck&searchType=<CDC|Indos|…>&txtNo=&dob=` — `searchType` **must** be present (absent → "try later" error; empty → empty body); CDC number works with or without the `MUM` prefix; DOB accepts `07-sep-1992`, `07/09/1992`, `07-09-1992`; rejects ISO format; throttles pipeline bursts with the "try later" error; real not-found is a distinct 228-char message.
- **dgshipping.gov.in** (India SID portal parent): connect-timeout during this session; the SID verifier `dgshippingbsid.in/seafarer/sid/verify` stayed healthy and is remembered in `verification_sources.db` as `IN_SID`.
- **dmamyanmar.org** (Myanmar): anti-forgery token flow works; `/Verify` is a *combined certificate* verifier (CDC book no. + per-certificate serial + passport); intermittently returns `"VerificationError"` for valid queries and currently throws NullReferenceException 500s.
- **LAN OCR service**: HTTP 200 + `pages:[]` on random pages (retry + don't-cache-lost-runs); grayscale JPEG sometimes reads pages RGB PNG cannot.

## 9. Commits

| Commit | Content |
|--------|---------|
| `1b1806a` | Baseline audit, safety guard, MCP gates, person folders, ARCHITECTURE.md |
| `c2bf9d3` | Self-generation improvements; INDOS/SID/AIO verified end-to-end |
| `3af1d8d` | Remove committed test DB artifact |
| `f15f7b0` | Indian CDC self-generation (searchType pinning, source reuse) |
| `6bc4197` | Repair `.gitignore` |
| `b5a6097` | ARCHITECTURE.md §7 implementation report |
| `b8eda1f` | Wipe-and-rerun durability fixes (OCR cache, hints, fall-through, verdicts, selector pinning, credential credibility) |
| *(this commit)* | Myanmar SID mapping fix + `"VerificationError"` retry class + this report |

## 10. Remaining gaps

1. **dmamyanmar.org server instability** — the Myanmar SID (and sometimes COC) cannot be verified while their endpoint 500s; our side is correct and will verify once theirs recovers.
2. **OCR service flakiness** — mitigated (retry, no poisoned cache, grayscale fallback), but the underlying service still drops pages randomly.
3. **Tavily quota exhausted** — first contact with a brand-new registry (no printed hint) cannot be discovered until quota resets; remembered sources are unaffected.
4. **`dgshipping.gov.in` unreachable** — discovery to the SID portal currently relies on the remembered source.
5. Known audit items (unchanged): MCP `validate_document` skips `compare_response`; `_discover_discriminators` orphaned; image photo/signature comparison scores 0 ("vision_call failed") without blocking verdicts.
