"""Discovery page judgment: cheap deterministic pre-checks that reject obvious
wrong pages without an LLM call, plus the model judgment (accept/next-move).

Split out of agent.py. Design choice: a page is accepted only when the model
returns verdict=ACCEPT with confidence >= PAGE_ACCEPT_CONFIDENCE AND reports
at least one of the document's required lookup fields as present. Deliberately
strict — a false accept is worse than another attempt.
"""

import json

from discovery.llm import call_llm, load_prompt, parse_json_response
from discovery.page_fetch import _required_profile_fields

PAGE_ACCEPT_CONFIDENCE = 60

_REQUIRED_FIELD_SYNONYMS = {
    "document_number": ("document number", "doc no", "docnumber", "sid", "bsid",
                        "indos", "certificate no", "cert no", "number"),
    "date_of_birth": ("date of birth", "dob", "birth date", "birthdate", "birth"),
    "full_name": ("full name", "seafarer name", "holder name", "name"),
}

_LOGIN_MARKERS = (
    "sign in", "log in", "login", "staff login", "authorized personnel",
    "forgot password", "enter your password", "employee login",
)
_ERROR_MARKERS = (
    "page not found", "not found", "404", "internal server error", "500",
    "access denied", "403 forbidden", "service unavailable",
)


def _deterministic_reject(page: dict, redacted_profile: dict) -> str | None:
    """Cheap pre-checks so obvious wrong pages never cost an LLM call."""
    if not page.get("ok"):
        status = page.get("status")
        detail = page.get("error") or (f"HTTP {status}" if status else "unreachable")
        return f"page could not be fetched ({detail})"

    text = (page.get("text") or "").lower()
    title = (page.get("title") or "").lower()
    combined = f"{title} {text}"

    required = _required_profile_fields(redacted_profile)
    haystack = " ".join(
        [
            (i.get("name") or "") + " " + (i.get("id") or "") + " "
            + (i.get("placeholder") or "")
            for i in page.get("inputs") or []
        ]
    ).lower()
    has_lookup_field = any(
        any(syn in haystack for syn in _REQUIRED_FIELD_SYNONYMS[field])
        for field in required
    )

    if any(marker in combined for marker in _ERROR_MARKERS) and not page.get("inputs"):
        return "looks like an error/placeholder page (no form inputs)"

    if len(text) < 40 and not page.get("inputs") and not page.get("links"):
        return "blank or empty page (no text, inputs, or links)"

    has_password = page.get("has_password")
    if has_password is None:
        has_password = any(
            (i.get("type") or "").lower() == "password"
            for i in page.get("inputs") or []
        )
    if has_password and not has_lookup_field:
        return "login wall: password field present with no document lookup inputs"

    login_hits = sum(1 for marker in _LOGIN_MARKERS if marker in combined)
    if login_hits >= 2 and not page.get("inputs"):
        return "login wall: login/staff sign-in text with no lookup inputs"

    # --- Country-mismatch guard ---
    # Reject pages that clearly belong to a different country than the
    # document's issuing_country. This prevents the LLM from latching onto
    # e.g. India's SID portal for a Myanmar document.
    doc_country = (redacted_profile.get("issuing_country") or "").lower()
    if doc_country:
        page_url_low = (page.get("url") or "").lower()
        # Known country → domain/TLD markers (extend as needed)
        _COUNTRY_DOMAIN_MARKERS = {
            "india": (".in/", ".gov.in", "dgshipping", "dgma.gov", "indos",
                      "esamudra", "indianmaritimeuniversity"),
            "myanmar": (".mm/", ".gov.mm", "dma.gov.mm"),
            "philippines": (".ph/", ".gov.ph", "marina.gov"),
            "indonesia": (".id/", ".go.id"),
            "bangladesh": (".bd/", ".gov.bd"),
            "pakistan": (".pk/", ".gov.pk"),
            "sri lanka": (".lk/", ".gov.lk"),
            "china": (".cn/", ".gov.cn"),
        }
        for country_name, markers in _COUNTRY_DOMAIN_MARKERS.items():
            # If the page URL matches a known country's domain markers
            # AND that country is NOT the document's country → reject.
            if any(m in page_url_low for m in markers):
                if country_name not in doc_country and doc_country not in country_name:
                    return (
                        f"country mismatch: page belongs to '{country_name}' "
                        f"but document is from '{doc_country}'"
                    )

    return None


def classify_page(
    redacted_profile: dict,
    page: dict,
    endpoint_hints: list[str],
    visited: list[dict],
) -> dict:
    """Model judgment: is this the right page, and if not, what next?"""
    prompt = load_prompt("discovery_page_check.txt")

    payload = {
        "redacted_profile": redacted_profile,
        "required_lookup_fields": _required_profile_fields(redacted_profile),
        "candidate_page": {
            "url": page.get("url"),
            "title": page.get("title"),
            "http_status": page.get("status"),
            "text_excerpt": page.get("text"),
            "inputs": page.get("inputs"),
            "same_domain_links": page.get("links"),
            "external_links": page.get("external_links"),
        },
        "js_endpoint_hints": endpoint_hints,
        "already_visited": [v.get("url") for v in visited],
    }

    prompt += "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    print(f"  Judging page: {page.get('title') or page.get('url')}")

    content = call_llm(prompt)

    return parse_json_response(content)
