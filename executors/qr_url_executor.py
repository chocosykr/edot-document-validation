"""
QR / URL Executor
=================
Runs inside Docker. Handles method_type: QR_URL.

Workflow:
1. Follow a verification URL (extracted from the document's QR code or
   directly provided as an input).
2. Follow any redirects.
3. Evaluate the final page content with keyword/JSON matching.

execution_steps schema:
[
  {
    "action": "FOLLOW_URL",
    "url": "{{verification_url}}"    # the URL to follow
  }
]

expected_responses schema: same as HTTP executor.
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
# Helpers
# ---------------------------------------------------------------------------

def substitute(value: str, inputs: dict) -> str:
    for k, v in inputs.items():
        value = value.replace(f"{{{{{k}}}}}", v)
    return value


def follow_url(url: str, max_redirects: int = 5) -> tuple[int, str, str]:
    """Follow URL, return (status_code, body, final_url)."""
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), url


def decide(body: str, final_url: str, expected: dict) -> str:
    # JSON path check first
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

    # URL-based check (some portals redirect to a known "success" path)
    for kw in expected.get("success_url_fragments", []):
        if kw.lower() in final_url.lower():
            return "VERIFIED"

    for kw in expected.get("success_keywords", []):
        if kw.lower() in body_lower:
            return "VERIFIED"

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

    # Resolve verification URL — first from inputs, then from steps, then source_url
    verification_url = inputs.get("verification_url") or method.get("source_url", "")

    for step in steps:
        action = step.get("action", "").upper()
        if action == "FOLLOW_URL":
            verification_url = substitute(step.get("url", verification_url), inputs)

    if not verification_url:
        result = {
            "decision_status": "TECHNICAL_FAILURE",
            "evidence": {"reason": "No verification URL provided"},
            "raw_response": None
        }
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f)
        return

    status, body, final_url = follow_url(verification_url)

    if status >= 400:
        result = {
            "decision_status": "TECHNICAL_FAILURE",
            "evidence": {"http_status": status, "final_url": final_url},
            "raw_response": body[:2000]
        }
    else:
        decision = decide(body, final_url, expected)
        result = {
            "decision_status": decision,
            "evidence": {
                "http_status": status,
                "final_url": final_url,
                "response_length": len(body),
                "method_type": "QR_URL"
            },
            "raw_response": body[:2000]
        }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print(f"QR/URL executor done. Decision: {result['decision_status']}")


if __name__ == "__main__":
    main()
