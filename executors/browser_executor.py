"""
Browser Executor (Playwright)
==============================
Handles method_type: BROWSER. Runs inside the Docker sandbox (playwright image)
or directly on the host.

Contract (identical to the other executors):
- Reads  input.json  (ExecutionRequest schema)
- Writes output.json (ExecutionResult contract: decision_status, evidence, raw_response)

execution_steps schema:
  {"action": "FETCH_FORM", "url": "https://..."}   # navigate ({{var}} supported)
  {"action": "SOLVE_CAPTCHA"}                      # detect + solve, see below
  {"action": "FILL", "field": "sid_number", "value": "{{document_number}}"}
  {"action": "SUBMIT"}

CAPTCHA policy (scope: simple, non-distorted text CAPTCHAs only):
- HTML-first detection (img/inputs with captcha-ish hints, incl. inline data-URI
  images with alt="CAPTCHA").
- If HTML is inconclusive -> ONE viewport screenshot (in memory only) is sent to
  the vision LLM to ask whether a CAPTCHA is present at all.
- Complex challenge frameworks (reCAPTCHA / hCaptcha / Turnstile / GeeTest /
  FunCaptcha / Arkose) are NEVER attempted: they immediately produce
  TECHNICAL_FAILURE, not a solve attempt.

Non-negotiable sequencing (PII protection):
  CAPTCHA detection/solving MUST complete before any FILL step runs, so the
  screenshot sent to the vision LLM can never contain credential values.
  Enforced with an explicit gate in the step loop:
    - SOLVE_CAPTCHA asserts that no FILL has run yet.
    - FILL refuses to run until the captcha gate is open (either the method has
      no SOLVE_CAPTCHA step, or SOLVE_CAPTCHA completed successfully).
  This is enforced per attempt, so retries can never loop past a FILL.

Retry counters (kept separate, never merged):
  - captcha_failures:    failed solve attempts (unreadable text, solve rejected,
                         CAPTCHA-error shown after submit). Limit 3.
  - execution_failures:  everything else (timeout, connection error, unexpected
                         page structure, missing fields). Limit 3.
  Either limit reached -> TECHNICAL_FAILURE. A complex challenge framework is a
  terminal TECHNICAL_FAILURE with no retries.

Signal-axis rule: an empty/failed/HTTP-error response is TECHNICAL_FAILURE or
UNCERTAIN — never a genuine INVALID document. Keyword VERIFIED is only returned
from real response content (field_match methods stay UNCERTAIN here so the
engine's compare_response() decides).

Privacy:
- Screenshots live in memory only and are never written to disk.
- Logs never contain credential values (FILL logs field names only).
- LLM config comes from the shared local/frontier toggle
  (utils/llm_client.get_llm_config): endpoint is always LLM_URL, and LLM_MODEL
  is the switch. No second endpoint and no hardcoded model name here.
- A hard internal deadline (DEADLINE_SECONDS) keeps total runtime under the
  runner's BROWSER container timeout so retries can never hang past it.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Response compaction: preserve JSON structure, truncate only large base64
# ---------------------------------------------------------------------------

_B64_RE = re.compile(r'^[A-Za-z0-9+/=\s]{200,}$')

# Keep the first 200 chars of base64 values — enough for image magic-byte
# detection in _is_base64_image() downstream, but avoids writing 400KB+ of
# BMP signature data into output.json.
_B64_KEEP = 200


def _compact_response(body: str, limit: int = 50_000) -> str:
    """Intelligently compact a large HTML/JSON response body.

    1. If the body is HTML wrapping a JSON blob (e.g. ``<pre>{...}</pre>``),
       extract the JSON, truncate large base64 values, and return the
       compacted JSON string so downstream parsing keeps working.
    2. If the body is already JSON, do the same.
    3. Otherwise, fall back to simple string truncation.

    This replaces the previous ``body[:2000]`` approach which broke JSON
    parsing when the response contained large base64 image fields.
    """
    if not body:
        return body

    # Try to extract a JSON object from the body (handles HTML wrapping)
    json_str = body.strip()

    # Strip HTML wrapper if present (common pattern: <html>...<pre>{...}</pre>...)
    if json_str.startswith('<') or json_str.startswith('<!') :
        # Quick extraction: find the first '{' and last '}' that might be JSON
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

    # Fallback: plain truncation (but with a more generous limit than 2000)
    return body[:limit]


def _compact_dict(d: dict) -> dict:
    """Recursively compact a dict, truncating large base64-looking values."""
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _compact_dict(value)
        elif isinstance(value, list):
            result[key] = [_compact_dict(item) if isinstance(item, dict) else item
                          for item in value]
        elif isinstance(value, str) and len(value) > _B64_KEEP:
            # Check if this looks like base64 image data
            if _B64_RE.match(value[:500]):
                result[key] = value[:_B64_KEEP]
            else:
                result[key] = value  # keep non-base64 strings intact
        else:
            result[key] = value
    return result

try:
    from dotenv import load_dotenv
    # Host runs: pick up the project .env. In Docker the values are injected
    # via `docker run -e` flags by the runner, so this is a no-op there.
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_BASE_DIR, ".env"), override=False)
except ImportError:
    pass

try:
    from playwright.sync_api import (
        Page,
        ElementHandle,
        TimeoutError as PlaywrightTimeoutError,
    )
    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    # Minimal containers (e.g. the python:3.12-alpine used in runner unit
    # tests) have no playwright. Import must not crash: the executor still
    # has to read input.json and write a contract-conformant output.json.
    from typing import Any as _Any
    Page = _Any
    ElementHandle = _Any

    class PlaywrightTimeoutError(Exception):
        pass

    _PLAYWRIGHT_IMPORTABLE = False

INPUT_PATH = "input.json"
OUTPUT_PATH = "output.json"

# --- LLM configuration -----------------------------------------------------------------
# Vision calls go through the SHARED implementation in
# utils/llm_client.vision_call: the same single endpoint (LLM_URL) and the same
# local/frontier toggle (LLM_MODEL) as every other call site — no hardcoded
# model and no second endpoint. This executor can run standalone inside the
# Docker sandbox — docker_runner copies utils/llm_client.py next to
# executor.py — so try both import paths. If neither is importable the vision
# call fails loudly; it is never reimplemented here.
try:  # host / project-root runs
    from utils.llm_client import vision_call as _shared_vision_call
except Exception:  # pragma: no cover - sandbox path
    try:
        from llm_client import vision_call as _shared_vision_call
    except Exception:  # pragma: no cover - last-resort
        _shared_vision_call = None

# --- Limits / timing ---
MAX_CAPTCHA_FAILURES = 3
MAX_EXECUTION_FAILURES = 3
DEADLINE_SECONDS = float(os.getenv("BROWSER_EXECUTOR_DEADLINE", "100"))  # runner kills at 120s
PAGE_LOAD_TIMEOUT_MS = 30_000
STEP_TIMEOUT_MS = 8_000
SETTLE_WAIT_MS = 2_500          # SPA render settle after domcontentloaded
SUBMIT_SETTLE_MS = 3_000
LLM_CALL_TIMEOUT_S = 30

# --- CAPTCHA element detection -------------------------------------------------
# Simple text CAPTCHAs: an image of characters + a text input to type them into.
CAPTCHA_IMG_SELECTORS = [
    "img[alt*='captcha' i]",
    "img[id*='captcha' i]",
    "img[class*='captcha' i]",
    "img[src*='captcha' i]",
    "img[src*='vcode' i]",
    "img[id*='vcode' i]",
]
CAPTCHA_INPUT_SELECTORS = [
    "input[name*='captcha' i]:not([type='hidden'])",
    "input[id*='captcha' i]:not([type='hidden'])",
    "input[class*='captcha' i]:not([type='hidden'])",
    "input[name*='vcode' i]:not([type='hidden'])",
    "input[placeholder*='captcha' i]:not([type='hidden'])",
    "input[aria-label*='captcha' i]:not([type='hidden'])",
]
# Complex challenge frameworks: detection of these is terminal, never solved.
COMPLEX_CAPTCHA_MARKERS = [
    "recaptcha", "hcaptcha", "turnstile", "geetest", "funcaptcha", "arkose",
]
# Post-submit error signals that the solved text was rejected.
CAPTCHA_ERROR_SELECTORS = [
    "[class*='captcha' i][class*='error' i]",
    "[id*='captcha' i][id*='error' i]",
    "[class*='captcha' i][class*='invalid' i]",
]
CAPTCHA_ERROR_TEXT_RE = re.compile(
    r"captcha[\s\S]{0,40}?(invalid|incorrect|wrong|expired|mismatch|does not match)"
    r"|(invalid|incorrect|wrong|expired)\s+captcha",
    re.I,
)
# Credential-ish markers used to EXCLUDE fields from captcha-input guessing.
CREDENTIAL_FIELD_RE = re.compile(
    r"user|pass|dob|birth|sid\b|bsid|document|indos|email|phone|mobile|aadhar|pan|name",
    re.I,
)


# ---------------------------------------------------------------------------
# Exceptions driving the two counters
# ---------------------------------------------------------------------------

class CaptchaRetry(Exception):
    """A solve attempt failed -> increment captcha_failures, retry fresh."""


class ExecutionRetry(Exception):
    """Non-captcha problem (structure, timeout, connection) -> execution_failures."""


class TerminalFailure(Exception):
    """Unrecoverable (e.g. complex CAPTCHA framework) -> TECHNICAL_FAILURE now."""


# ---------------------------------------------------------------------------
# Vision LLM helpers
# ---------------------------------------------------------------------------

def _remaining(deadline: float) -> float:
    return max(1.0, deadline - time.monotonic())


def call_vision_llm(prompt: str, img_b64: str, deadline: float) -> str:
    """Send an in-memory screenshot to the vision LLM.

    Delegates to the shared implementation (utils.llm_client.vision_call), so
    the CAPTCHA solver uses the same single endpoint and the same
    local/frontier toggle as every other call site — no hardcoded model, no
    second endpoint, and no duplicate vision implementation. The images sent
    here are guaranteed PII-free by the sequencing gate.
    """
    if _shared_vision_call is None:
        print("[browser] vision unavailable: shared llm_client not importable", file=sys.stderr)
        return ""
    timeout_s = int(min(LLM_CALL_TIMEOUT_S, _remaining(deadline)))
    return _shared_vision_call(prompt, img_b64, timeout_s=timeout_s)


def sanitize_captcha_text(raw: str) -> Optional[str]:
    """Keep only plausible captcha characters; None if the reply is unusable."""
    if not raw:
        return None
    text = raw.strip()
    # strip markdown fences / quotes some models add
    text = re.sub(r"```[a-z]*", "", text).replace("```", "").strip().strip('"\'`')
    text = re.sub(r"[^A-Za-z0-9]", "", text)
    if not (3 <= len(text) <= 12):
        return None
    return text


# ---------------------------------------------------------------------------
# Generic step helpers
# ---------------------------------------------------------------------------

def substitute(value: str, inputs: Dict[str, str]) -> str:
    for k, v in inputs.items():
        value = value.replace("{{" + k + "}}", str(v))
    return value


def _visible(handle: ElementHandle) -> bool:
    """is_visible() OR a non-empty bounding box.

    Some SPA frameworks (e.g. MUI on older chromium builds) leave elements
    technically "not visible" to Playwright's actionability heuristics while
    fully rendered with valid layout boxes. A bounding box means a human can
    see it — and it also means element screenshots will work.
    """
    try:
        if handle and handle.is_visible():
            return True
        return _has_box(handle)
    except Exception:
        return False


def _has_box(handle: ElementHandle) -> bool:
    try:
        box = handle.bounding_box() if handle else None
        return bool(box and box.get("width", 0) > 0 and box.get("height", 0) > 0)
    except Exception:
        return False


def _first_visible(page: Page, selectors: List[str]) -> Optional[ElementHandle]:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() and _visible(loc.first.element_handle()):
                return loc.first.element_handle()
        except Exception:
            continue
    return None


def _input_haystack(el: ElementHandle) -> str:
    """name/id/placeholder/aria-label/label text of an input, for matching."""
    try:
        return el.evaluate(
            """(el) => {
                const label = el.labels && el.labels.length
                    ? Array.from(el.labels).map(l => l.textContent).join(' ') : '';
                return [
                    el.name, el.id, el.placeholder || '',
                    el.getAttribute('aria-label') || '', label
                ].join(' ').toLowerCase();
            }"""
        )
    except Exception:
        return ""


def _format_value_for_input(handle: ElementHandle, value: str) -> str:
    """Adapt the value to the input type (e.g. date inputs need ISO format)."""
    try:
        itype = (handle.evaluate("el => el.type") or "").lower()
    except Exception:
        itype = ""
    if itype == "date":
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", value.strip())
        if m:
            d, mo, y = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return value


# ---------------------------------------------------------------------------
# Decision logic (mirrors http_executor semantics; empty/broken != INVALID)
# ---------------------------------------------------------------------------

def decide(body: str, expected: dict) -> str:
    if not body or not body.strip():
        return "TECHNICAL_FAILURE"  # empty response is never a genuine INVALID
    if expected.get("comparison_mode") == "field_match":
        return "UNCERTAIN"  # engine's compare_response() decides on real content
    body_lower = body.lower()
    for kw in expected.get("success_keywords", []):
        if kw.lower() in body_lower:
            return "VERIFIED"
    for kw in expected.get("failure_keywords", []):
        if kw.lower() in body_lower:
            return "REJECTED"
    return "UNCERTAIN"


# ---------------------------------------------------------------------------
# CAPTCHA detection & solving
# ---------------------------------------------------------------------------

def find_complex_markers(page: Page) -> List[str]:
    """Detect complex challenge frameworks in HTML. These are never solved."""
    try:
        html = page.content().lower()
    except Exception:
        return []
    return [m for m in COMPLEX_CAPTCHA_MARKERS if m in html]


def find_captcha_elements(page: Page) -> Tuple[Optional[ElementHandle], Optional[ElementHandle], str]:
    """HTML-first detection of a simple text captcha (image + input)."""
    img = _first_visible(page, CAPTCHA_IMG_SELECTORS)
    if img is None:
        # Inline data-URI images labelled CAPTCHA (SPA pattern, e.g. MUI forms)
        try:
            loc = page.locator(
                "img[alt*='captcha' i][src^='data:image'], "
                "img[src^='data:image/png'][width]:below(:text('captcha'))"
            )
            if not loc.count():
                loc = page.locator("img[alt*='captcha' i]")
            if loc.count() and loc.first.is_visible():
                img = loc.first.element_handle()
        except Exception:
            pass
    if img is None:
        return None, None, "none"

    inp = _first_visible(page, CAPTCHA_INPUT_SELECTORS)
    if inp is None:
        inp = _guess_captcha_input(page, img)
    return img, inp, "html"


def _guess_captcha_input(page: Page, img: ElementHandle) -> Optional[ElementHandle]:
    """No captcha-named input found: pick the last visible empty text input in
    the captcha image's form, excluding credential-ish fields."""
    try:
        handle = page.evaluate_handle(
            """(img) => {
                const scope = img.closest('form') || img.closest('div') || document;
                const inputs = Array.from(scope.querySelectorAll('input[type="text"], input:not([type])'))
                    .filter(i => (i.offsetParent || i.offsetHeight))
                    .filter(i => !i.value)
                    .filter(i => {
                        const hay = [i.name, i.id, i.placeholder || '',
                            i.getAttribute('aria-label') || ''].join(' ').toLowerCase();
                        return !/(user|pass|dob|birth|sid|bsid|document|indos|email|phone|mobile|aadhar|pan|name)/.test(hay);
                    });
                return inputs.length ? inputs[inputs.length - 1] : null;
            }""",
            img,
        )
        el = handle.as_element()
        return el if _visible(el) else None
    except Exception:
        return None


def vision_detect_captcha(page: Page, deadline: float) -> bool:
    """HTML inconclusive: ask the vision LLM whether a captcha is present."""
    try:
        png = page.screenshot(type="png", full_page=False, timeout=8_000,
                              animations="disabled")  # in memory only
    except Exception as e:
        print(f"[browser] screenshot failed: {e}", file=sys.stderr)
        return False
    b64 = base64.b64encode(png).decode("ascii")
    prompt = (
        "Look at this screenshot of a web page. Is there a CAPTCHA challenge "
        "visible (an image of distorted text/characters a human must type)? "
        "Answer with exactly one word: YES or NO."
    )
    reply = call_vision_llm(prompt, b64, deadline)
    print("[browser] vision captcha-presence reply received")
    return bool(reply) and "yes" in reply.lower()


def _captcha_image_b64(page: Page, img: ElementHandle, deadline: float) -> Optional[str]:
    """Acquire the captcha image as base64 PNG, best method first.

    1. data: URI src -> bytes are already in the DOM, no rendering needed.
    2. http(s) src -> fetch via the page's request context (same cookies).
    3. element screenshot (waits for actionability; may fail on SPAs).
    Screenshots stay in memory only.
    """
    try:
        src = img.evaluate("el => el.src") or ""
    except Exception:
        src = ""
    if src.startswith("data:image"):
        comma = src.find(",")
        if comma != -1:
            return src[comma + 1:]
    if src.startswith("http"):
        try:
            resp = page.context.request.get(src, timeout=10_000)
            if resp.ok:
                return base64.b64encode(resp.body()).decode("ascii")
        except Exception as e:
            print(f"[browser] captcha image fetch failed: {e}", file=sys.stderr)
    try:
        png = img.screenshot(type="png", timeout=6_000,
                             animations="disabled")  # in memory only
        return base64.b64encode(png).decode("ascii")
    except Exception:
        pass
    try:
        png = page.screenshot(type="png", timeout=6_000,
                              animations="disabled")
        return base64.b64encode(png).decode("ascii")
    except Exception as e:
        print(f"[browser] captcha image acquisition failed: {e}", file=sys.stderr)
        return None


def solve_simple_captcha(page: Page, img: ElementHandle, inp: ElementHandle,
                         deadline: float) -> bool:
    """Read the captcha image, OCR via vision LLM, fill, sanity-check.
    Images stay in memory; the caller's error handling covers rejection."""
    b64 = _captcha_image_b64(page, img, deadline)
    if not b64:
        return False

    prompt = (
        "Type the characters shown in this CAPTCHA image. "
        "Respond with ONLY the characters, nothing else."
    )
    raw = call_vision_llm(prompt, b64, deadline)
    text = sanitize_captcha_text(raw)
    if not text:
        print(f"[browser] vision OCR unusable (len={len(raw or '')} raw chars)")
        return False
    print(f"[browser] vision OCR returned {len(text)} captcha characters")

    try:
        inp.fill(text)
    except Exception:
        # Rendered-but-not-actionable inputs (MUI/animations on older
        # chromium): inject the value the way React controlled inputs need.
        try:
            inp.evaluate(
                """(el, v) => {
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                text,
            )
        except Exception as e:
            print(f"[browser] failed to fill captcha input: {e}", file=sys.stderr)
            return False

    # Pre-submit sanity: a visible captcha error right after filling means the
    # page already validated it (some SPAs do) -> fail this attempt.
    for sel in CAPTCHA_ERROR_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return False
        except Exception:
            continue
    return True


def captcha_error_visible(page: Page) -> bool:
    """Post-submit check: did the site reject the captcha text?"""
    for sel in CAPTCHA_ERROR_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:
            continue
    try:
        if page.get_by_text(CAPTCHA_ERROR_TEXT_RE).first.is_visible():
            return True
    except Exception:
        pass
    return False


def handle_solve_captcha(page: Page, deadline: float) -> str:
    """Full SOLVE_CAPTCHA action. Returns 'solved' | 'not_present'.
    Raises TerminalFailure (complex framework) or CaptchaRetry (solve failed)."""
    assert not _credentials_filled, "PII guard: FILL ran before SOLVE_CAPTCHA"

    complex_markers = find_complex_markers(page)
    if complex_markers:
        raise TerminalFailure(
            "complex CAPTCHA framework detected "
            f"({', '.join(complex_markers)}) — not solvable in current scope"
        )

    img, inp, detection = find_captcha_elements(page)
    if img is None:
        # HTML inconclusive -> one vision presence check
        if vision_detect_captcha(page, deadline):
            raise ExecutionRetry(
                "vision detection reports a CAPTCHA but no solvable "
                "image/input pair was found in the DOM (unexpected structure)"
            )
        print("[browser] no CAPTCHA detected (HTML + vision)")
        return "not_present"

    if inp is None:
        raise ExecutionRetry(
            "CAPTCHA image found but no input field to answer it "
            "(unexpected page structure)"
        )

    print(f"[browser] simple CAPTCHA detected via {detection}; attempting solve")
    if solve_simple_captcha(page, img, inp, deadline):
        print("[browser] CAPTCHA solve filled without error signal")
        return "solved"
    raise CaptchaRetry("solve attempt failed (unreadable or immediately rejected)")


# ---------------------------------------------------------------------------
# Step execution for one attempt
# ---------------------------------------------------------------------------

_credentials_filled = False  # module-level so SOLVE_CAPTCHA's assert sees it


def run_attempt(playwright, method: dict, inputs: Dict[str, str],
                deadline: float, info: Dict[str, Any]) -> Tuple[str, str]:
    """Run the full step list once on a fresh browser.

    Mutates `info` in place as the attempt progresses, so diagnostics survive
    exception paths. Returns (decision, raw_response). Raises CaptchaRetry /
    ExecutionRetry / TerminalFailure; the caller owns the counters.
    """
    global _credentials_filled
    _credentials_filled = False

    steps: List[dict] = method.get("execution_steps", []) or []
    expected: dict = method.get("expected_responses", {}) or {}
    source_url = method.get("source_url", "")
    has_captcha_step = any(
        (s.get("action", "") or "").upper() == "SOLVE_CAPTCHA" for s in steps
    )
    captcha_gate_open = not has_captcha_step  # no captcha step -> gate open

    info.setdefault("captcha_detected", False)
    info.setdefault("detection", "none")
    info.setdefault("solve", "skipped")
    last_status: Optional[int] = None

    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    try:
        context = browser.new_context(user_agent="DVS-Browser/1.0")
        page = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)

        def _on_response(resp):
            # Track only document/xhr/fetch responses: a static-asset 404 must
            # not be mistaken for a failed verification submission.
            try:
                if resp.request.resource_type in ("document", "xhr", "fetch"):
                    nonlocal last_status
                    last_status = resp.status
            except Exception:
                pass
        page.on("response", _on_response)

        final_body = ""

        for idx, step in enumerate(steps):
            if time.monotonic() > deadline:
                raise ExecutionRetry("attempt deadline exceeded before step completion")
            action = (step.get("action", "") or "").upper()

            if action in ("FETCH_FORM", "NAVIGATE"):
                url = substitute(step.get("url") or source_url, inputs)
                if not url:
                    raise ExecutionRetry("FETCH_FORM step has no url")
                print(f"[browser] navigating to {url}")
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=PAGE_LOAD_TIMEOUT_MS)
                status = resp.status if resp else None
                if status and status >= 400:
                    raise ExecutionRetry(f"FETCH_FORM returned HTTP {status}")
                try:
                    page.wait_for_selector("input", state="attached", timeout=8_000)
                except Exception:
                    pass  # not all pages have inputs; content() decides later
                page.wait_for_timeout(SETTLE_WAIT_MS)

            elif action == "SOLVE_CAPTCHA":
                # Before solving: no FILL may have run (assert), and the gate
                # is by definition still closed — solving it is what opens it.
                assert not _credentials_filled, \
                    "PII guard violated: FILL executed before SOLVE_CAPTCHA"
                assert not captcha_gate_open, \
                    "captcha gate already open before SOLVE_CAPTCHA ran"
                outcome = handle_solve_captcha(page, deadline)
                info["captcha_detected"] = outcome == "solved"
                info["detection"] = "html" if outcome == "solved" else "none"
                info["solve"] = outcome
                captcha_gate_open = True

            elif action == "FILL":
                if not captcha_gate_open:
                    raise RuntimeError(
                        "PII guard: FILL reached before CAPTCHA resolution confirmed"
                    )
                field = step.get("field", "")
                value = substitute(step.get("value", ""), inputs)
                handle = _find_input_for_field(page, field)
                if handle is None:
                    raise ExecutionRetry(
                        f"field '{field}' not found on page (unexpected structure)"
                    )
                formatted = _format_value_for_input(handle, value)
                try:
                    handle.fill(formatted)
                except Exception:
                    # Rendered-but-not-actionable input: inject via JS the way
                    # React controlled inputs require (native setter + events).
                    try:
                        handle.evaluate(
                            """(el, v) => {
                                const proto = el instanceof HTMLTextAreaElement
                                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                                setter.call(el, v);
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }""",
                            formatted,
                        )
                    except Exception as e:
                        raise ExecutionRetry(
                            f"field '{field}' present but could not be filled: {e}"
                        )
                _credentials_filled = True  # screenshots are now forbidden
                print(f"[browser] filled field '{field}' (value withheld)")

            elif action == "SUBMIT":
                print("[browser] submitting form")
                submitted = _click_submit(page)
                if submitted:
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    except Exception:
                        pass
                page.wait_for_timeout(SUBMIT_SETTLE_MS)
                final_body = page.content()

                if captcha_error_visible(page):
                    raise CaptchaRetry("CAPTCHA-error signal visible after submit")

                if not final_body or not final_body.strip():
                    raise ExecutionRetry("empty response after submit")
                if last_status is not None and last_status >= 400:
                    raise ExecutionRetry(f"submit response HTTP {last_status}")

                decision = decide(final_body, expected)
                info["http_status"] = last_status
                return decision, _compact_response(final_body)
            else:
                print(f"[browser] unknown step action '{action}' ignored")

        # No SUBMIT step (or steps ended early): evaluate what we have.
        final_body = page.content()
        if captcha_error_visible(page):
            raise CaptchaRetry("CAPTCHA-error signal visible (pre-submit check)")
        return decide(final_body, expected), _compact_response(final_body)
    finally:
        browser.close()


def _click_submit(page: Page) -> bool:
    for sel in (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Verify')",
        "button:has-text('Submit')",
        "input[value*='Submit' i]",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() and _visible(loc.first.element_handle()):
                try:
                    loc.first.click()
                except Exception:
                    # Rendered-but-not-actionable: dispatch a DOM click.
                    loc.first.evaluate("el => el.click()")
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _find_input_for_field(page: Page, field: str) -> Optional[ElementHandle]:
    """Locate a form field for a logical field name via a synonym table."""
    field_lc = (field or "").lower()
    if "dob" in field_lc or "birth" in field_lc:
        needles = ["dob", "date of birth", "birth date", "birthdate", "d.o.b"]
    elif "sid" in field_lc or "document" in field_lc or "number" in field_lc:
        needles = [field_lc.replace("_", " "), "sid", "bsid", "document number",
                   "doc number", "inputvalue"]
    else:
        needles = [field_lc.replace("_", " ")]
    try:
        handle = page.evaluate_handle(
            """(needles) => {
                const inputs = Array.from(document.querySelectorAll('input, select'))
                    .filter(i => (i.offsetParent || i.offsetHeight));
                const hay = (el) => {
                    const label = el.labels && el.labels.length
                        ? Array.from(el.labels).map(l => l.textContent).join(' ') : '';
                    return [
                        el.name, el.id, el.placeholder || '',
                        el.getAttribute('aria-label') || '', label
                    ].join(' ').toLowerCase();
                };
                for (const n of needles) {
                    const hit = inputs.find(i => hay(i).includes(n));
                    if (hit) return hit;
                }
                return null;
            }""",
            needles,
        )
        el = handle.as_element()
        return el if _visible(el) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main: retry loop with the two counters + deadline
# ---------------------------------------------------------------------------

def _write_result(decision: str, evidence: dict, raw_response: Optional[str]):
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"decision_status": decision,
                   "evidence": evidence,
                   "raw_response": raw_response}, f)


def main():
    if not os.path.exists(INPUT_PATH):
        print(f"ERROR: {INPUT_PATH} not found", file=sys.stderr)
        sys.exit(1)

    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        request_data = json.load(f)

    method = request_data.get("method", {})
    inputs = request_data.get("inputs", {})
    method_id = method.get("method_id", "?")
    print(f"[browser] execution started method_id={method_id}")

    # A method with no steps is malformed and cannot produce evidence.
    # TECHNICAL_FAILURE (never UNCERTAIN/INVALID) without launching a browser.
    if not (method.get("execution_steps") or []):
        _write_result("TECHNICAL_FAILURE", {
            "method_type": "BROWSER",
            "attempts": 0,
            "captcha_failures": 0,
            "execution_failures": 0,
            "reason": "method has no execution_steps",
        }, None)
        print("[browser] no execution_steps -> TECHNICAL_FAILURE")
        return

    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS

    captcha_failures = 0
    execution_failures = 0
    attempts = 0
    decision = "TECHNICAL_FAILURE"
    final_body = ""
    info: Dict[str, Any] = {}

    if not _PLAYWRIGHT_IMPORTABLE:
        # Playwright missing -> no browser execution is possible. This is a
        # sandbox capability problem, never a document verdict.
        _write_result("TECHNICAL_FAILURE", {
            "method_type": "BROWSER",
            "attempts": 0,
            "captcha_failures": 0,
            "execution_failures": 0,
            "reason": "playwright not available in execution environment",
        }, None)
        print("[browser] playwright not importable -> TECHNICAL_FAILURE")
        return

    from playwright.sync_api import sync_playwright  # lazy: needs the package

    with sync_playwright() as playwright:
        while (captcha_failures < MAX_CAPTCHA_FAILURES
               and execution_failures < MAX_EXECUTION_FAILURES):
            attempts += 1
            print(f"\n[browser] attempt {attempts} "
                  f"(captcha_failures={captcha_failures}, "
                  f"execution_failures={execution_failures})")
            counter = "none"
            attempt_info: Dict[str, Any] = {
                "captcha_detected": False, "detection": "none", "solve": "skipped"
            }
            try:
                decision, final_body = run_attempt(
                    playwright, method, inputs, deadline, attempt_info)
                info = attempt_info
                counter = "none"
                print(f"[browser] attempt {attempts} completed: decision={decision}")
                break
            except CaptchaRetry as e:
                captcha_failures += 1
                counter = "captcha_failures"
                print(f"[browser] captcha failure: {e}")
            except ExecutionRetry as e:
                execution_failures += 1
                counter = "execution_failures"
                print(f"[browser] execution failure: {e}")
            except TerminalFailure as e:
                print(f"[browser] terminal failure: {e}")
                info["terminal_reason"] = str(e)
                counter = "terminal"
                break
            except PlaywrightTimeoutError as e:
                execution_failures += 1
                counter = "execution_failures"
                print(f"[browser] playwright timeout: {e}", file=sys.stderr)
            except Exception as e:
                execution_failures += 1
                counter = "execution_failures"
                print(f"[browser] unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
            finally:
                print(f"[browser] attempt {attempts} summary: "
                      f"captcha_detected={attempt_info.get('captcha_detected', False)} "
                      f"detection={attempt_info.get('detection', 'none')} "
                      f"solve={attempt_info.get('solve', 'skipped')} "
                      f"counter_incremented={counter}")

            if time.monotonic() > deadline:
                print("[browser] deadline reached — stopping retries", file=sys.stderr)
                info["deadline_exceeded"] = True
                break

    elapsed = round(time.monotonic() - started, 1)

    result = {
        "decision_status": decision,
        "evidence": {
            "method_type": "BROWSER",
            "attempts": attempts,
            "captcha_failures": captcha_failures,
            "execution_failures": execution_failures,
            "captcha_detected": info.get("captcha_detected", False),
            "captcha_detection_method": info.get("detection", "none"),
            "solve_outcome": info.get("solve", "skipped"),
            "response_length": len(final_body),
            "elapsed_seconds": elapsed,
            **({"terminal_reason": info["terminal_reason"]} if info.get("terminal_reason") else {}),
            **({"deadline_exceeded": True} if info.get("deadline_exceeded") else {}),
        },
        "raw_response": _compact_response(final_body) if final_body else None,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print(f"\n[browser] execution completed decision={decision} elapsed={elapsed}s "
          f"captcha_failures={captcha_failures} execution_failures={execution_failures}")


if __name__ == "__main__":
    main()
