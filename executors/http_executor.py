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

Decision logic (in order):
1. If HTTP error (4xx/5xx) → TECHNICAL_FAILURE
2. If success_json_path + success_json_value match → VERIFIED
3. If any success_keyword in body → VERIFIED
4. If any failure_keyword in body → REJECTED
5. Otherwise → UNCERTAIN
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error


INPUT_PATH = "input.json"
OUTPUT_PATH = "output.json"


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

def http_get(url: str, params: dict) -> tuple[int, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


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

        if action == "GET":
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

    # HTTP error
    if status_code and status_code >= 400:
        result = {
            "decision_status": "TECHNICAL_FAILURE",
            "evidence": {"http_status": status_code},
            "raw_response": body[:2000]
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
            "raw_response": body[:2000]
        }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print(f"HTTP executor done. Decision: {result['decision_status']}")


if __name__ == "__main__":
    main()
