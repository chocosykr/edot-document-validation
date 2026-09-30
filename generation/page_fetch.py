"""
Page fetching and structure summarization for the generator: pulls the source
page HTML, summarizes forms/inputs/selects and inline JavaScript for the LLM,
and returns the raw inline JS + external-bundle text for the deterministic
XHR extractors (Components 1/1b).

Split out of generator.py so the LLM-facing orchestration stays small.
"""

import logging
from typing import List

import requests
from bs4 import BeautifulSoup

from utils.js_intel import harvest_js_intel_with_text

logger = logging.getLogger(__name__)


def _collect_inline_js(soup) -> tuple[str, List[str]]:
    """
    Collect inline <script> text AND the src of every external, non-library
    script. Returns (inline_js_text, external_custom_js_srcs).

    External bundles used to be counted and then ignored; they are now returned
    so ``_fetch_page_structure`` can harvest API endpoints from them. For SPAs
    the real request contract lives in the bundle, not in the visible page.
    """
    inline_js = []
    external_custom_js = []

    for script in soup.find_all("script"):
        src = script.get("src", "")
        if src:
            src_lower = src.lower()
            if not any(lib in src_lower for lib in ["jquery", "bootstrap", "react", "vue", "angular", "cdn"]):
                external_custom_js.append(src)
        else:
            text = script.string or ""
            if text.strip():
                inline_js.append(text.strip())

    return "\n\n".join(inline_js), external_custom_js


def _fetch_page_structure(url: str) -> dict:
    """
    Fetches the HTML from the given URL and returns a dict with:
      - "summary": human-readable summary of forms, inputs, buttons AND
        JavaScript logic for the LLM (same content as before the refactor).
      - "inline_js": the raw inline <script> text, for the deterministic
        XHR extractor (Component 1).
    """
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"Failed to fetch page structure for {url}: {e}")
        return {
            "summary": f"Could not fetch HTML (error: {e}). Proceed with blind generation.",
            "inline_js": "",
        }

    summary = []
    workflow_options = []
    bundle_js = ""  # raw external-bundle text, for Component 1b (may stay empty)

    # Extract form structure
    forms = soup.find_all("form")
    if not forms:
        summary.append("No <form> tags found on the page.")
    else:
        for i, form in enumerate(forms):
            form_id = form.get("id", "")
            form_name = form.get("name", "")
            form_action = form.get("action", "")
            form_method = form.get("method", "")
            summary.append(f"Form {i+1} (id='{form_id}', name='{form_name}', action='{form_action}', method='{form_method}'):")

            for tag in form.find_all(["input", "select", "textarea", "button"]):
                tag_name = tag.name
                tag_id = tag.get("id", "")
                tag_name_attr = tag.get("name", "")
                tag_type = tag.get("type", "")
                tag_class = tag.get("class", [])
                tag_onclick = tag.get("onclick", "")

                info = f"  <{tag_name}"
                if tag_type: info += f" type='{tag_type}'"
                if tag_id: info += f" id='{tag_id}'"
                if tag_name_attr: info += f" name='{tag_name_attr}'"
                if tag_class: info += f" class='{' '.join(tag_class)}'"
                if tag_onclick: info += f" onclick='{tag_onclick}'"
                info += ">"
                summary.append(info)
                if tag_name == "select":
                    for option in tag.find_all("option"):
                        workflow_options.append({
                            "select": tag_name_attr or tag_id,
                            "value": option.get("value", ""),
                            "text": option.get_text(" ", strip=True),
                        })
                        summary.append(
                            f"    <option value='{option.get('value', '')}'>{option.get_text(' ', strip=True)}</option>"
                        )

    # Extract JavaScript to find AJAX endpoints and form submission logic
    full_js_text, external_custom_js = _collect_inline_js(soup)

    if full_js_text or external_custom_js:
        summary.append("\n--- JavaScript Logic (AJAX/form submission) ---")

        lines = full_js_text.split("\n")

        # Heuristics for minification and length
        total_chars = len(full_js_text)
        avg_line_length = total_chars / len(lines) if lines else 0

        is_minified = avg_line_length > 200
        is_too_long = total_chars > 40000  # Leaves plenty of room for 15k token budget

        # External bundles: this is where an SPA keeps its real request
        # contract. Harvest ranked endpoint hints so the LLM can derive the
        # verb/url/param names from the actual API rather than the visible form.
        # The bundle TEXT is also returned so the deterministic bundle
        # extractor (Component 1b) can run over it without a second download.
        endpoint_hints: List[str] = []
        call_hints: List[str] = []
        if external_custom_js:
            try:
                endpoint_hints, call_hints, bundle_js = harvest_js_intel_with_text(url, external_custom_js)
            except Exception as e:
                logger.warning("External JS endpoint harvest failed for %s: %s", url, e)

        # Legacy sites often have 2-4 custom utility scripts (e.g., date-picker.js).
        # SPAs typically have massive bundles. We'll rely on size/minification primarily.
        if is_minified or is_too_long:
            if endpoint_hints:
                summary.append(
                    "[Modern SPA/minified bundle detected — the real API endpoints "
                    "were harvested from it and are listed below.]"
                )
            else:
                summary.append("[BROWSER_FALLBACK_REQUIRED: Modern SPA or heavily minified logic detected.]")
                summary.append("Do NOT attempt to guess the API endpoint. Generate a BROWSER method type.")
        else:
            summary.append(full_js_text)
            summary.append("\nIMPORTANT: If the JavaScript shows AJAX/XMLHttpRequest calls to a URL like a servlet or API endpoint, "
                          "the page does NOT use standard HTML form submission. "
                          "Generate an HTTP method type targeting that AJAX endpoint instead of WEB_FORM.")

        if endpoint_hints:
            summary.append("\n--- API endpoints found in external JavaScript bundles ---")
            summary.append("These come from the page's own JavaScript/network calls. Derive the request "
                           "contract (verb, url, parameter names) from THESE, not from visible form field names:")
            for path in endpoint_hints:
                summary.append(f"  {path}")

        if call_hints:
            summary.append("\n--- API call patterns found in JavaScript (verb + path + params) ---")
            summary.append("These lines show the HTTP verb, path, and query parameter names the site's own "
                           "code actually sends. COPY the verb and the parameter names EXACTLY — do not "
                           "substitute a parameter name from the visible form:")
            for line in call_hints:
                summary.append(f"  {line}")
            summary.append("If one of these calls is a CAPTCHA-fetch endpoint and another the verify/lookup "
                           "call it feeds, emit the FETCH_CAPTCHA -> SOLVE_CAPTCHA -> REQUEST sequence, using "
                           "the exact verb and parameter names shown above.")

        summary.append("--- End JavaScript ---")

    has_inline_js = bool(full_js_text and full_js_text.strip())
    has_custom_bundle = bool(external_custom_js)
    has_endpoint_hints = bool(endpoint_hints)
    has_call_hints = bool(call_hints)
    is_modern_spa = bool(is_minified or is_too_long) and not has_endpoint_hints

    result = {
        "summary": "\n".join(summary),
        "inline_js": full_js_text,
        "bundle_js": bundle_js,
        "workflow_options": workflow_options,
        "channels": {
            "has_inline_js": has_inline_js,
            "has_custom_bundle": has_custom_bundle,
            "has_endpoint_hints": has_endpoint_hints,
            "has_call_hints": has_call_hints,
            "is_modern_spa": is_modern_spa,
        },
    }
    return result
