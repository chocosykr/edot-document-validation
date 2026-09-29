"""
HTTP Executor
=============
Runs inside a Docker container (python:3.12-alpine or similar).
Reads  /workspace/input.json
Writes /workspace/output.json

Supports method_type: HTTP

execution_steps schema (stored in the ValidationMethod):
[
  {
    "action": "GET" | "POST",
    "url": "https://...",            # may use {{variable}} placeholders
    "params": {"key": "{{var}}"},   # query-string params (GET or POST)
    "json_body": {"key": "{{var}}"} # JSON body (POST only)
  }
]

expected_responses schema:
{
  "success_keywords": ["valid", "verified"],   # ANY of these in body → VERIFIED
  "failure_keywords": ["invalid", "not found"], # ANY of these in body → REJECTED
  "success_json_path": "status",               # check response JSON key
  "success_json_value": "ok"                   # expected value at that key
}

REQUEST action schema:
{
  "action": "REQUEST",               # or "GET"/"POST" shorthand
  "method": "GET" | "POST",
  "url": "https://...",              # may use {{variable}} placeholders
  "params": {"key": "{{var}}"},     # query-string params (GET) or form body (POST)
  "json_body": {"key": "{{var}}"},  # JSON body (POST only, wins over params)
  "param_location": "body" | "query" # POST only, default "body". "query" = send(null)
}                                    # pattern: real POST, params on query string,
                                     # empty body. Verb stays POST.

CAPTCHA steps (for APIs that gate a lookup behind a CAPTCHA image):
{
  "action": "FETCH_CAPTCHA",         # GET the captcha endpoint
  "url": "https://.../api/captcha/generate",
  "response_format": "json" | "raw", # "json" = keys below; "raw" = body is base64 image
  "image_field": "captchaImage",     # JSON key for the base64 image (or an image URL)
  "id_field": "captchaId"            # JSON key for the captcha id
}
{
  "action": "SOLVE_CAPTCHA"          # solve the fetched image with the vision LLM
}
After SOLVE_CAPTCHA, {{captcha_text}} and {{captcha_id}} become available to
later REQUEST params (e.g. "captcha"/"captchaId"). Vision calls use the shared
local/frontier toggle (utils.llm_client.vision_call) — no hardcoded model.

Decision logic (in order):
0. CAPTCHA fetch/solve failure → TECHNICAL_FAILURE
1. Method-declared not-found signature (exact status + message) → REJECTED
2. If HTTP error (4xx/5xx) → TECHNICAL_FAILURE
2. If success_json_path + success_json_value match → VERIFIED
3. If any success_keyword in body → VERIFIED
4. If any failure_keyword in body → REJECTED
5. Otherwise → UNCERTAIN

Module layout (split for modularity):
  http_helpers.py  — HTTP verbs, compaction, substitution, retry, captcha fetch
  http_decider.py  — response classification (decide, not-found signatures)
  http_executor.py — this step runner (FETCH/SOLVE_CAPTCHA, REQUEST loop, main)
"""

import json
import os
import re
import sys
import urllib.parse

# Split modules. Host runs import the package; the Docker sandbox receives
# these files copied flat next to executor.py by execution/docker_runner.py,
# so fall back to plain module names there (same pattern as llm_client).
try:  # host / project-root runs
    from executors.http_helpers import (
        _compact_response,
        _extract_regex_from_html,
        _fetch_captcha,
        _sanitize_captcha_text,
        http_get_retry,
        http_post_json_retry,
        http_post_query_retry,
        http_post_form_retry,
        substitute,
        substitute_dict,
    )
    from executors.http_decider import decide, _matches_not_found_signature
except ImportError:  # pragma: no cover - sandbox path
    from http_helpers import (
        _compact_response,
        _extract_regex_from_html,
        _fetch_captcha,
        _sanitize_captcha_text,
        http_get_retry,
        http_post_json_retry,
        http_post_query_retry,
        http_post_form_retry,
        substitute,
        substitute_dict,
    )
    from http_decider import decide, _matches_not_found_signature

# Shared vision client, same import pattern as executors/browser_executor.py:
# host runs import it from the project; inside the Docker sandbox
# docker_runner copies utils/llm_client.py next to executor.py.
try:  # host / project-root runs
    from utils.llm_client import vision_call as _vision_call
except Exception:  # pragma: no cover - sandbox path
    try:
        from llm_client import vision_call as _vision_call
    except Exception:  # pragma: no cover - keep the executor runnable
        _vision_call = None


INPUT_PATH = "input.json"
OUTPUT_PATH = "output.json"

CAPTCHA_VISION_PROMPT = (
    "Type the characters shown in this CAPTCHA image. "
    "Respond with ONLY the characters, nothing else."
)
CAPTCHA_VISION_TIMEOUT_S = 20


# ---------------------------------------------------------------------------
# Captcha-rejection detection (environment noise, never a document verdict)
# ---------------------------------------------------------------------------

# Captcha-rejection responses (a wrong/expired captcha answer) are
# environment noise, never a document verdict — the executor redoes the
# captcha round in-run (see the REQUEST retry loop) instead of failing the
# method. Measured on dgshippingbsid.in: the same flow succeeds 6/6
# host-side, so a rejection is a per-attempt event, not a method defect.
_CAPTCHA_REJECTION_MARKERS = (
    "invalid or expired captcha",
    "invalid captcha", "expired captcha", "captcha is invalid",
    "captcha does not match", "captcha mismatch", "wrong captcha",
    "incorrect captcha", "captcha verification failed",
    "security code is invalid", "security code incorrect",
    "please enter a valid captcha",
)


def _is_captcha_rejection(status_code, body: str) -> bool:
    if not status_code or status_code < 400 or not body:
        return False
    low = body.lower()
    if any(m in low for m in _CAPTCHA_REJECTION_MARKERS):
        return True
    return "captcha" in low and "try again" in low


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_PATH):
        print("ERROR: input.json not found", file=sys.stderr)
        sys.exit(1)

    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        request_data = json.load(f)

    method = request_data.get("method", {})
    inputs = request_data.get("inputs", {})
    steps = method.get("execution_steps", [])
    expected = method.get("expected_responses", {})

    status_code = None
    body = ""
    technical_error = None
    captcha_image_b64 = ""

    # The index of the FETCH_CAPTCHA step (if any). When a request is refused
    # with a captcha-rejection response, the captcha round — fetch, solve,
    # request — is redone in-run (bounded), mirroring the site SPA's own
    # refresh-and-retry behavior, instead of failing the whole method.
    captcha_step_index = None
    for i, s in enumerate(steps):
        if str(s.get("action", "")).upper() == "FETCH_CAPTCHA":
            captcha_step_index = i
            break

    def _run_steps(seq):
        """Run a step sequence; returns (status_code, body, technical_error).

        The captcha artifacts (image/id/text) live in the shared `inputs`
        dict / captured variable, so re-running the sequence from the
        FETCH_CAPTCHA step produces a fresh captcha round.
        """
        nonlocal captcha_image_b64
        sc = None
        bd = ""
        terr = None
        for step in seq:
            action = step.get("action", "GET").upper()

            # Canonical schema: {"action": "REQUEST", "method": "GET"|"POST", ...}
            # (as documented in the generation and self-healing prompts). The bare
            # "GET"/"POST" action forms are kept as shorthand.
            if action == "REQUEST":
                action = step.get("method", "GET").upper()

            url = substitute(step.get("url", method.get("source_url", "")), inputs)
            params = substitute_dict(step.get("params", {}), inputs)
            json_body = substitute_dict(step.get("json_body", {}), inputs)

            if action == "FETCH_CAPTCHA":
                captcha_image_b64, captcha_id, err = _fetch_captcha(url, step)
                if err:
                    terr = err
                    break
                inputs["captcha_id"] = captcha_id
                print(f"[HTTP] FETCH_CAPTCHA ok: image_b64_len={len(captcha_image_b64)} "
                      f"captcha_id={'set' if captcha_id else 'absent'}")
            elif action == "SOLVE_CAPTCHA":
                if _vision_call is None:
                    terr = "SOLVE_CAPTCHA unavailable: shared vision client not importable"
                    break
                if not captcha_image_b64:
                    terr = "SOLVE_CAPTCHA called before a CAPTCHA image was fetched"
                    break
                reply = _vision_call(
                    CAPTCHA_VISION_PROMPT, captcha_image_b64,
                    timeout_s=CAPTCHA_VISION_TIMEOUT_S,
                )
                solved = _sanitize_captcha_text(reply)
                if not solved:
                    terr = "SOLVE_CAPTCHA: vision model returned no usable characters"
                    break
                inputs["captcha_text"] = solved
                print(f"[HTTP] SOLVE_CAPTCHA ok ({len(solved)} chars)")
            elif action == "GET_HTML":
                # Fetch an HTML page into a template variable (e.g. the page
                # carrying an anti-forgery token). Uses the shared session so
                # the cookie pairs with the token extracted from this response.
                sc, bd = http_get_retry(url, {})
                inputs[str(step.get("output_var") or "page_html")] = bd
                print(f"[HTTP] GET_HTML ok: {len(bd)} chars -> "
                      f"{step.get('output_var') or 'page_html'}")
            elif action == "EXTRACT_FROM_HTML":
                try:
                    html_var = step.get("html") or ""
                    m = re.match(r"^\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}$", html_var.strip())
                    source_html = inputs.get(m.group(1), "") if m else ""
                    value = _extract_regex_from_html(
                        source_html, str(step.get("selector", ""))
                    )
                except ValueError as e:
                    terr = str(e)
                    break
                out_var = str(step.get("output_var") or "")
                if not out_var:
                    terr = "EXTRACT_FROM_HTML: output_var is required"
                    break
                if not value:
                    terr = (
                        f"EXTRACT_FROM_HTML: selector {step.get('selector')!r} "
                        "matched nothing in the fetched HTML"
                    )
                    break
                inputs[out_var] = value
                print(f"[HTTP] EXTRACT_FROM_HTML ok: {out_var} ({len(value)} chars)")
            elif action == "GET":
                sc, bd = http_get_retry(url, params)
            elif action == "POST" and json_body:
                sc, bd = http_post_json_retry(url, json_body)
            elif action == "POST":
                param_location = step.get("param_location", "body")
                if param_location == "query":
                    # send(null) pattern: real POST, params on the query string,
                    # explicit empty body.
                    separator = "&" if "?" in url else "?"
                    sc, bd = http_post_query_retry(
                        url + separator + urllib.parse.urlencode(params)
                    )
                else:
                    # Default: form-encoded body (existing behaviour)
                    sc, bd = http_post_form_retry(url, params)
            else:
                bd = f"Unknown action: {action}"
                sc = 0
        return sc, bd, terr

    # First pass: the full step sequence.
    status_code, body, technical_error = _run_steps(steps)

    # Captcha-rejection retry: a wrong/expired captcha answer is environment
    # noise, not a method defect (the same flow succeeds host-side). Redo
    # fetch -> solve -> rest-of-sequence in-run, like the site SPA does on
    # its own captcha failure. Bounded: 3 fresh captcha rounds, then give up.
    if (
        technical_error is None
        and captcha_step_index is not None
        and _is_captcha_rejection(status_code, body)
    ):
        for captcha_attempt in range(1, 4):
            print(f"[HTTP] captcha rejected — retry {captcha_attempt}/3")
            status_code, body, technical_error = _run_steps(steps[captcha_step_index:])
            if technical_error is not None or not _is_captcha_rejection(status_code, body):
                break

    # CAPTCHA fetch/solve failure: never a document verdict.
    if technical_error:
        result = {
            "decision_status": "TECHNICAL_FAILURE",
            "evidence": {
                "method_type": "HTTP",
                "reason": technical_error,
                "http_status": status_code,
            },
            "raw_response": _compact_response(body) if body else None,
        }
    # Method-declared not-found signature: a genuine REJECTED verdict (the
    # registry responded that no such document exists), not a machinery
    # failure. Scoped by the method itself — see _matches_not_found_signature.
    elif _matches_not_found_signature(status_code, body, expected):
        result = {
            "decision_status": "REJECTED",
            "evidence": {
                "http_status": status_code,
                "method_type": "HTTP",
                "not_found_signature": True,
            },
            "raw_response": _compact_response(body)
        }
    # HTTP error
    elif status_code and status_code >= 400:
        result = {
            "decision_status": "TECHNICAL_FAILURE",
            "evidence": {
                "http_status": status_code,
                "signature_count": len(expected.get("not_found_signatures") or []),
            },
            "raw_response": _compact_response(body)
        }
    else:
        decision = decide(body, expected)
        result = {
            "decision_status": decision,
            "evidence": {
                "http_status": status_code,
                "response_length": len(body),
                "method_type": "HTTP"
            },
            "raw_response": _compact_response(body)
        }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print(f"HTTP executor done. Decision: {result['decision_status']}")


if __name__ == "__main__":
    main()
