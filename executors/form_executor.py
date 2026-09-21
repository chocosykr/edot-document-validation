"""
Web Form Executor
=================
Runs inside Docker. Handles method_type: WEB_FORM.

Workflow:
1. Fetch the form page (GET) to discover form fields and action URL.
2. Fill in the required inputs.
3. POST the completed form.
4. Evaluate the response with keyword matching.

execution_steps schema:
[
  {
    "action": "FETCH_FORM",
    "url": "https://...",                # page containing the <form>
    "form_selector": "form",             # CSS-like hint: 'form', 'form#id', 'form.class'
  },
  {
    "action": "FILL",
    "field": "document_number",          # name/id attribute on the <input>
    "value": "{{document_number}}"
  },
  {
    "action": "SUBMIT"                   # POST the discovered form
  }
]

Uses BeautifulSoup4 + urllib.
"""

from bs4 import BeautifulSoup
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error


INPUT_PATH = "input.json"
OUTPUT_PATH = "output.json"


# ---------------------------------------------------------------------------
# Minimal HTML form parser
# ---------------------------------------------------------------------------

class FormParser:
    def __init__(self):
        self.forms = []

    def feed(self, body: str):
        soup = BeautifulSoup(body, 'html.parser')
        for form in soup.find_all("form"):
            form_data = {
                "action": form.get("action", ""),
                "method": form.get("method", "get").upper() if form.get("method") else "GET",
                "fields": {}
            }
            for tag in form.find_all(["input", "select", "textarea"]):
                name = tag.get("name") or tag.get("id")
                if name:
                    form_data["fields"][name] = tag.get("value", "")
            
            self.forms.append(form_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def substitute(value: str, inputs: dict) -> str:
    for k, v in inputs.items():
        value = value.replace(f"{{{{{k}}}}}", v)
    return value


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), url


def post_form(url: str, fields: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
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


def decide(body: str, expected: dict) -> str:
    body_lower = body.lower()
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

    form_page_url = None
    discovered_form = None
    form_fields_to_fill = {}
    final_body = ""
    final_status = None

    print(f"[FormExecutor] Steps to execute: {len(steps)}")
    print(f"[FormExecutor] Inputs: {list(inputs.keys())}")
    print(f"[FormExecutor] Expected responses config: {json.dumps(expected)}")

    for step in steps:
        action = step.get("action", "").upper()
        print(f"\n[FormExecutor] Step: {action}")

        if action == "FETCH_FORM":
            url = substitute(step.get("url", method.get("source_url", "")), inputs)
            print(f"[FormExecutor] Fetching URL: {url}")
            status, body, final_url = fetch(url)
            print(f"[FormExecutor] HTTP status: {status}, final URL: {final_url}")
            if status >= 400:
                _write_failure("TECHNICAL_FAILURE", f"Fetch failed with HTTP {status}", "", status)
                return
            # Parse the first form found
            parser = FormParser()
            parser.feed(body)
            print(f"[FormExecutor] Forms found on page: {len(parser.forms)}")
            if not parser.forms:
                print(f"[FormExecutor] Page body (first 1000 chars):\n{body[:1000]}")
                _write_failure("TECHNICAL_FAILURE", "No HTML form found on page", body[:1000], status)
                return
            discovered_form = parser.forms[0]
            print(f"[FormExecutor] Using form: action={discovered_form['action']}, method={discovered_form['method']}")
            print(f"[FormExecutor] Form fields found: {list(discovered_form['fields'].keys())}")
            # Resolve form action URL
            raw_action = discovered_form["action"].strip()
            # Detect bogus action values like "post" or "get" which are
            # actually HTTP methods, not URL paths.  Treat as self-submit.
            if raw_action.lower() in ("post", "get", ""):
                print(f"[FormExecutor] Form action '{raw_action}' looks like an HTTP method, not a URL. Using page URL instead.")
                discovered_form["action"] = final_url
            elif not raw_action.startswith("http"):
                discovered_form["action"] = urllib.parse.urljoin(
                    final_url, raw_action
                )
            form_fields_to_fill = dict(discovered_form["fields"])

        elif action == "FILL":
            field = step.get("field")
            value = substitute(step.get("value", ""), inputs)
            if field:
                form_fields_to_fill[field] = value
                print(f"[FormExecutor] Filling field '{field}' = '{value}'")

        elif action == "SUBMIT":
            if not discovered_form:
                _write_failure("TECHNICAL_FAILURE", "SUBMIT called before FETCH_FORM", "", None)
                return
            print(f"[FormExecutor] Submitting to: {discovered_form['action']}")
            print(f"[FormExecutor] Fields being submitted: {json.dumps(form_fields_to_fill)}")
            final_status, final_body = post_form(discovered_form["action"], form_fields_to_fill)
            print(f"[FormExecutor] Response HTTP status: {final_status}")
            print(f"[FormExecutor] Response body (first 1500 chars):\n{final_body[:1500]}")

    if final_status and final_status >= 400:
        decision = "TECHNICAL_FAILURE"
    else:
        decision = decide(final_body, expected)

    # Log keyword matching details
    if decision == "UNCERTAIN":
        print(f"\n[FormExecutor] UNCERTAIN: No success or failure keywords matched.")
        print(f"[FormExecutor] Success keywords checked: {expected.get('success_keywords', [])}")
        print(f"[FormExecutor] Failure keywords checked: {expected.get('failure_keywords', [])}")

    result = {
        "decision_status": decision,
        "evidence": {
            "http_status": final_status,
            "method_type": "WEB_FORM",
            "fields_submitted": list(form_fields_to_fill.keys())
        },
        "raw_response": final_body[:2000]
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print(f"\nForm executor done. Decision: {decision}")


def _write_failure(status: str, reason: str, body: str, http_code):
    result = {
        "decision_status": status,
        "evidence": {"reason": reason, "http_status": http_code},
        "raw_response": body[:2000]
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)
    print(f"Form executor: {status} — {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
