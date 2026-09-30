# DVS — Next Session Work Plan

Context: 2026-09-29 session fixed four pipeline defects (details in ARCHITECTURE.md §7
once updated; raw evidence in `run*.log` files). Registry currently: INDOS + SID
methods ACTIVE and VERIFIED against live sites; CDC method works but the esamudra
CDC checker does not carry the test CDC (`MUM 179416`) in any format — site-side
data gap, report externally, nothing to fix in code. 239 tests passing.

---

## Phase 0 — Close the open item: Myanmar reconciliation fix (~30 min)

The fix `_reconcile_wire_param_assignments` in `generation/post_process.py` is
written but untested and unrerun.

1. `python -m pytest tests/ -q` → expect 239 passing.
2. Add unit test (new `tests/test_post_process_reconcile.py` or into an existing
   post-process test file): reproduce the exact M_MM_COC_001 shape —
   - declared inputs: `["cdc_number", "document_number", "passport_number"]`
   - steps params: `CrewCDCNo: "{{document_number}}"`, `Serial: "{{serial}}"`,
     `CrewPassport: "{{passport}}"`, `ReplyEmail: "structural.probe@dvs.invalid"`,
     `"__RequestVerificationToken": "{{__RequestVerificationToken}}"`
   - after `finalize_method`: `CrewCDCNo→{{cdc_number}}`, `CrewPassport→{{passport_number}}`,
     `Serial→{{document_number}}`; token/email untouched; no orphan `{{serial}}`/
     `{{passport}}` left; coverage pre-check passes.
3. Rerun `InputFiles/2O - HEIN HTET (AIO).pdf`:
   ```bash
   setsid nohup .venv/bin/python main.py "InputFiles/2O - HEIN HTET (AIO).pdf" \
     > run_mm.log 2>&1 < /dev/null & echo "PID: $!"
   ```
   (Always `setsid nohup … < /dev/null` — the terminal sandbox reaps plain
   background jobs, which caused the earlier "silent crashes".)
4. Acceptance: method validates via bootstrap/signature path, or healing ladder
   converges. Expected live outcome even when the method works: the site answers
   `"VerificationError"` (19 chars) for the fake probe — the bootstrap channel
   does NOT match it (correct), so signature capture or healing must handle it.
   If the run ends VALIDATION_UNAVAILABLE with a *coherent* method, that is a
   partial pass: the remaining problem is the evidence policy (Phase 4.5).

## Phase 1 — Lock in today's work (~30 min)

5. Delete `.diag_sandbox/` and `run*.log` (or gitignore them: `.diag_sandbox/`,
   `run*.log`). Do NOT commit `method_registry.db`, `filestructure.txt`.
6. ARCHITECTURE.md: add incident entries for the four defects (pinning/camelCase,
   captcha Docker budget, retry-burns-single-use-captcha, 4xx bootstrap channel)
   + the availability gate + coverage pre-check. Update the module map notes for
   `executors/http_decider.py` (new `_json_message_reports_not_found`) and
   `execution/docker_runner.py` (`_method_timeout`).
7. Commit. Staged set: `generation/param_mapping.py`, `generation/post_process.py`,
   `generation/generator.py`, `validation/validator.py`,
   `executors/http_helpers.py`, `executors/http_decider.py`,
   `executors/http_executor.py`, `execution/docker_runner.py`,
   `tests/test_param_pinning.py`, `tests/test_validator.py`,
   `tests/test_docker_runner.py`, `tests/test_generation.py`, ARCHITECTURE.md.

## Phase 2 — Self-healing experiment (~1–2 h) — proves the design thesis

Question: *can the system fix a generation-level coherence bug on its own now
that the error messages are actionable?*

8. Put the reconciliation fix behind `DVS_WIRE_RECONCILE=0` env flag (default on).
9. Wipe registry, rerun Myanmar with the flag off, watch the healing ladder.
10. Verify the coverage pre-check's message actually reaches the healing prompt
    (`validation/healing.py` — check what evidence the agent tool exposes).
11. Record honestly: converged (attempts, time, LLM calls) or not, and why.
    Either result is a finding for the mentor discussion.

## Phase 3 — Redaction/scrubber (~1–2 h) — prerequisite for SCRIPT

Generated scripts can `print()` anything; logs flow into healing prompts raw.

12. New `utils/log_scrubber.py`: `scrub(text, secret_values) -> str` replacing
    each secret with a shape-preserving placeholder, e.g. `‹CDC:10 alnum›`,
    `‹DOB:date›` — keep structure visible, values gone. Handle creds embedded
    in URLs, JSON bodies, query strings.
13. Apply at: validator → healing prompt assembly; `main.py` debug dump
    (reuse/extend the existing scrub block); docker log capture.
14. Unit tests with nasty cases (value inside URL path, JSON, repeated).

## Phase 4 — SCRIPT method type (the big one; half a day+)

Goal: per-site transport becomes LLM-authored code stored in the registry;
verification/probe/evidence policy stays deterministic harness code.

15. **Model**: `registry/models.py` — add `MethodType.SCRIPT`; optional fields
    `script_source: str`, `script_runtime: str = "python3"`; keep
    `execution_steps` optional for this type. Old methods untouched.
16. **Sandbox**: `execution/docker_runner.py` — SCRIPT branch: write
    `script.py` (+ shipped `dvs_io.py` shim) + `input.json`; same docker flags;
    per-method timeout field (default 90s); parse same `output.json` contract:
    `{"decision_status", "evidence", "raw_response"}`.
17. **Shim** `executors/dvs_io.py` (flat-copied into sandbox):
    `get_input(name)` (only declared inputs), `http_get/http_post` helpers
    (cookie-aware, use the fixed 4xx-never-retried transport), `write_result(...)`.
    Coverage check for SCRIPT = every declared input read via `get_input`
    (greppable in `validation/validator.py` pre-check).
18. **Validator**: pre-check branch for SCRIPT (grep shim calls instead of
    placeholders); probe unchanged; healing ladder rewrites `script_source`
    (direct LLM pass → agentic loop), version bumps, test-before-adopt.
19. **Decider policy stays in harness**: script returns raw evidence + MAY
    propose a decision; classification minimums (4xx business answers, captcha
    noise, tiny-body rule) live in `http_decider.py`-equivalent logic the
    script's output flows through. Script freedom ≠ verdict freedom.
20. **Pilot**: hand-convert the Myanmar method to a SCRIPT method; run the
    probe; then test a generation run that emits SCRIPT for a fresh site.
21. **Tests**: sandbox exec (dummy script), probe pass, heal-and-bump-version,
    redaction integration (script prints a cred → scrubber removes it).

### Phase 4.5 — evidence policy (parallel, small)
22. Extend decider: learned per-method "response shape" notes (e.g. Myanmar's
    `"VerificationError"` string) captured by signature capture like HTTP;
    keep the refusal-to-guess guard intact (it saved us twice).

## Phase 5 — per-phase model tiers (optional, ~1–2 h)

23. `utils/llm_client.py`: authoring calls accept a model-tier override
    (frontier via existing GOOGLE/GROQ fallback chain) — authoring sees only
    redacted page evidence, so no PII leaves the machine. Execution stays local.
    Healing: local first; frontier escalation only with scrubbed logs.
24. Measure: captcha vision solve-rate local vs frontier on dgshippingbsid.in
    (was ~50%/round locally, needs 2–3 rounds per probe).

---

## Standing environment notes

- Long runs: `setsid nohup .venv/bin/python main.py "InputFiles/X.pdf" > run.log 2>&1 < /dev/null &`
- Docker mounts must be inside the project tree (`/tmp` is namespace-private);
  the runner already uses `.docker_temp/`.
- Docker budget: plain HTTP 30s, captcha-bearing 90s (`_method_timeout`).
- Site facts learned: esamudra `searchType` ∈ {GMDSS, DC, COP, IGF, WK, DCPOLAR,
  CRSE, PP, Indos, CDC, SMY}; INDOS lookup ignores dob; dgshippingbsid.in
  captchas are single-use, ~0.2s responses, vision solve ≈50%/round;
  dmamyanmar.org = ASP.NET MVC form + anti-forgery token, answers
  `"VerificationError"` for any bad input.
