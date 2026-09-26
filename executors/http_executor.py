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
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Response compaction: preserve JSON structure, truncate only large base64
# ---------------------------------------------------------------------------

_B64_RE = re.compile(r'^[A-Za-z0-9+/=\s]{200,}$')
_B64_KEEP = 200

def _compact_response(body: str, limit: int = 50_000) -> str:
    if not body:
        return body

    json_str = body.strip()
    if json_str.startswith('<') or json_str.startswith('<!'):
        first_brace = json_str.find('{')
        last_brace = json_str.rfind('}')
        if first_brace >= 0 and last_brace > first_brace:
            candidate = json_str[first_brace:last_brace + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    json_str = candidate
            except (json.JSONDecodeError, ValueError):
                pass

    # Iterative parse for double-encoded JSON
    try:
        data = json_str
        while isinstance(data, str):
            parsed = json.loads(data)
            if parsed == data:
                break
            data = parsed
            
        if isinstance(data, dict):
            compacted = _compact_dict(data)
            return json.dumps(compacted)
    except (json.JSONDecodeError, ValueError, TypeError):
        # Try replacing escaped quotes if standard loads fails on double-encoded string
        if isinstance(json_str, str) and '\\"' in json_str:
            try:
                clean_str = json_str.replace('\\"', '"').replace('\\\\', '\\')
                data = json.loads(clean_str)
                while isinstance(data, str):
                    parsed = json.loads(data)
                    if parsed == data:
                        break
                    data = parsed
                if isinstance(data, dict):
                    compacted = _compact_dict(data)
                    return json.dumps(compacted)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    return body[:limit]

def _compact_dict(d: dict) -> dict:
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _compact_dict(value)
        elif isinstance(value, list):
            result[key] = [_compact_dict(item) if isinstance(item, dict) else item
                          for item in value]
        elif isinstance(value, str) and len(value) > _B64_KEEP:
            if _B64_RE.match(value[:500]):
                result[key] = value[:_B64_KEEP]
            else:
                result[key] = value
        else:
            result[key] = value
    return result

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
# Template substitution
# ---------------------------------------------------------------------------

def substitute(value: str, inputs: dict) -> str:
    for k, v in inputs.items():
        value = value.replace(f"{{{{{k}}}}}", v)
    return value


def substitute_dict(d: dict, inputs: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = substitute(v, inputs)
        elif isinstance(v, dict):
            result[k] = substitute_dict(v, inputs)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# HTTP helpers (pure stdlib — no requests required)
# ---------------------------------------------------------------------------

# One cookie-aware opener per executor process = one session per method run.
# ASP.NET anti-forgery flows (e.g. dmamyanmar.org) issue a session cookie
# together with the __RequestVerificationToken and require BOTH back on the
# POST; a cookieless client gets 400 even with a valid token.
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def http_get(url: str, params: dict) -> tuple[int, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_post_json(url: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_post_form(url: str, params: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_get_bytes(url: str) -> tuple[int, bytes]:
    """GET returning raw bytes (for captcha images served as a URL)."""
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _extract_regex_from_html(html: str, selector: str) -> str | None:
    """Minimal EXTRACT_FROM_HTML: a CSS-ish selector reduced to attribute
    extraction by regex. Supports the one shape the executor actually needs
    for anti-forgery flows:

        input[name='__RequestVerificationToken']  +  attribute=value

    Unknown selector shapes are a hard error for the step (fail loudly, not
    silently-skip): generation must only emit supported selectors.
    """
    m = re.match(
        r"^\s*input\[\s*name\s*=\s*['\"]([^'\"]+)['\"]\s*\]\s*$", selector or ""
    )
    if not m:
        raise ValueError(f"EXTRACT_FROM_HTML: unsupported selector: {selector!r}")
    name = m.group(1)
    tag = re.search(
        r"<input\b[^>]*\bname\s*=\s*['\"]" + re.escape(name)
        + r"['\"][^>]*>", html or "", re.I,
    )
    if not tag:
        return None
    vm = re.search(r"\bvalue\s*=\s*['\"]([^'\"]*)['\"]", tag.group(0), re.I)
    return vm.group(1) if vm else None


# ---------------------------------------------------------------------------
# CAPTCHA steps
# ---------------------------------------------------------------------------

def _sanitize_captcha_text(raw: str) -> str:
    """Keep only plausible captcha characters (empty string => unusable)."""
    text = re.sub(r"```[a-z]*", "", raw or "").replace("```", "").strip()
    text = text.strip('"\'`')
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _first_key(parsed: dict, *keys) -> str:
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_captcha(url: str, step: dict) -> tuple[str, str, str]:
    """Fetch a CAPTCHA image + id. Returns (image_b64, captcha_id, error)."""
    fmt = str(step.get("response_format") or "json").lower()
    image_field = step.get("image_field") or "captchaImage"
    id_field = step.get("id_field") or "captchaId"

    status, body = http_get(url, {})
    if status and status >= 400:
        return "", "", f"FETCH_CAPTCHA returned HTTP {status}"

    if fmt == "json":
        try:
            parsed = json.loads(body)
        except Exception as e:
            return "", "", f"FETCH_CAPTCHA response was not JSON ({e})"
        if not isinstance(parsed, dict):
            return "", "", "FETCH_CAPTCHA JSON was not an object"
        image_val = _first_key(
            parsed, image_field, "captchaImage", "captcha_image", "image", "img", "captcha"
        )
        id_val = _first_key(
            parsed, id_field, "captchaId", "captcha_id", "id", "key", "token"
        )
    else:
        image_val = body.strip()
        id_val = ""

    if not image_val:
        return "", "", "FETCH_CAPTCHA: no image found in the response"

    if image_val.lower().startswith("data:image"):
        image_val = image_val.split(",", 1)[-1]
    elif image_val.lower().startswith(("http://", "https://")):
        status2, raw = http_get_bytes(image_val)
        if status2 and status2 >= 400:
            return "", "", f"CAPTCHA image URL returned HTTP {status2}"
        image_val = base64.b64encode(raw).decode("ascii")

    return image_val, id_val, ""


def http_post_query(url: str) -> tuple[int, str]:
    """
    POST with parameters on the query string and an explicit empty body.

    This is the legacy XHR `send(null)` servlet pattern: params are appended
    to the URL by the caller, and the request body is empty. The verb stays
    POST — that is the wire format confirmed live (a GET must NOT be
    substituted here; some frameworks route GET/POST to different handlers
    and WAFs may enforce POST-only on servlet mappings).
    """
    req = urllib.request.Request(
        url, data=b"",
        headers={"User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def _matches_not_found_signature(status_code, body: str, expected: dict) -> bool:
    """Does this response match a method-declared not-found signature?

    A method may declare, in its own expected_responses:
        "not_found_signatures": [{"status": 400, "contains": "Application ID not found"}]

    BOTH fields are required and must match (status exactly, message as a
    case-insensitive substring). This is deliberately narrow and grounded in a
    specific endpoint's confirmed response: it is NOT a generic "any non-2xx
    with a JSON message = not found" rule. A CAPTCHA rejection, a rate limit,
    or a server error will not match unless the method explicitly names its
    exact message — so those stay TECHNICAL_FAILURE.
    """
    if not status_code or status_code < 400 or not body:
        return False
    signatures = expected.get("not_found_signatures")
    if not isinstance(signatures, list):
        return False
    body_lower = body.lower()
    for signature in signatures:
        if not isinstance(signature, dict):
            continue
        try:
            signature_status = int(signature.get("status"))
        except (TypeError, ValueError):
            continue
        contains = signature.get("contains")
        if not isinstance(contains, str) or not contains.strip():
            continue  # a status-only rule is too broad — never honoured
        if signature_status == status_code and contains.strip().lower() in body_lower:
            return True
    return False


def decide(body: str, expected: dict) -> str:
    if expected.get("comparison_mode") == "field_match":
        return "UNCERTAIN"

    # 1. JSON path check
    json_path = expected.get("success_json_path")
    json_val = expected.get("success_json_value")
    if json_path and json_val:
        try:
            parsed = json.loads(body)
            if str(parsed.get(json_path)) == str(json_val):
                return "VERIFIED"
        except (json.JSONDecodeError, AttributeError):
            pass

    body_lower = body.lower()

    # 2. Success keywords
    for kw in expected.get("success_keywords", []):
        if kw.lower() in body_lower:
            return "VERIFIED"

    # 3. Failure keywords
    for kw in expected.get("failure_keywords", []):
        if kw.lower() in body_lower:
            return "REJECTED"

    return "UNCERTAIN"


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

    for step in steps:
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
                technical_error = err
                break
            inputs["captcha_id"] = captcha_id
            print(f"[HTTP] FETCH_CAPTCHA ok: image_b64_len={len(captcha_image_b64)} "
                  f"captcha_id={'set' if captcha_id else 'absent'}")
        elif action == "SOLVE_CAPTCHA":
            if _vision_call is None:
                technical_error = "SOLVE_CAPTCHA unavailable: shared vision client not importable"
                break
            if not captcha_image_b64:
                technical_error = "SOLVE_CAPTCHA called before a CAPTCHA image was fetched"
                break
            reply = _vision_call(
                CAPTCHA_VISION_PROMPT, captcha_image_b64,
                timeout_s=CAPTCHA_VISION_TIMEOUT_S,
            )
            solved = _sanitize_captcha_text(reply)
            if not solved:
                technical_error = "SOLVE_CAPTCHA: vision model returned no usable characters"
                break
            inputs["captcha_text"] = solved
            print(f"[HTTP] SOLVE_CAPTCHA ok ({len(solved)} chars)")
        elif action == "GET_HTML":
            # Fetch an HTML page into a template variable (e.g. the page
            # carrying an anti-forgery token). Uses the shared session so
            # the cookie pairs with the token extracted from this response.
            status_code, body = http_get(url, {})
            inputs[str(step.get("output_var") or "page_html")] = body
            print(f"[HTTP] GET_HTML ok: {len(body)} chars -> "
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
                technical_error = str(e)
                break
            out_var = str(step.get("output_var") or "")
            if not out_var:
                technical_error = "EXTRACT_FROM_HTML: output_var is required"
                break
            if not value:
                technical_error = (
                    f"EXTRACT_FROM_HTML: selector {step.get('selector')!r} "
                    "matched nothing in the fetched HTML"
                )
                break
            inputs[out_var] = value
            print(f"[HTTP] EXTRACT_FROM_HTML ok: {out_var} ({len(value)} chars)")
        elif action == "GET":
            status_code, body = http_get(url, params)
        elif action == "POST" and json_body:
            status_code, body = http_post_json(url, json_body)
        elif action == "POST":
            param_location = step.get("param_location", "body")
            if param_location == "query":
                # send(null) pattern: real POST, params on the query string,
                # explicit empty body.
                separator = "&" if "?" in url else "?"
                status_code, body = http_post_query(
                    url + separator + urllib.parse.urlencode(params)
                )
            else:
                # Default: form-encoded body (existing behaviour)
                status_code, body = http_post_form(url, params)
        else:
            body = f"Unknown action: {action}"
            status_code = 0

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
            "evidence": {"http_status": status_code},
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
