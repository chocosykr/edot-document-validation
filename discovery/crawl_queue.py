"""Discovery crawl-queue management: URL hint extraction from the document
itself, search-result enqueueing, and host skip-lists.

Split out of agent.py.
"""

import re


# Domains that are never a public registry lookup form: skipping them keeps the
# limited attempt budget for real candidates (the model still judges everything
# that is enqueued).
_SKIP_HOST_MARKERS = (
    "instagram.", "youtube.", "youtu.be", "facebook.", "linkedin.",
    "twitter.", "//x.com", "tiktok.", "pinterest.", "reddit.",
)


def _is_skippable(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in _SKIP_HOST_MARKERS)


def _enqueue(results: list[dict], queue: list[str], seen: set, front: bool = False) -> None:
    urls = []
    for group in results:
        for result in group.get("results", []) or []:
            url = result.get("url")
            if not url or url in seen or url in urls:
                continue
            if _is_skippable(url):
                print(f"  skipping non-verification host: {url}")
                continue
            urls.append(url)
    if front:
        queue[:0] = urls
    else:
        queue.extend(urls)


def _hint_urls(redacted_profile: dict) -> list[str]:
    """    Verification-portal URLs the document ITSELF carries.

    Extracted scans sometimes name the issuing authority's verification URL
    (a QR target, a "verify at" footer, a portal link). These are the
    strongest possible discovery signals — printed on the document by the
    issuer — and unlike search they need no external API. Used to seed the
    crawl queue (and as the fallback when search is unavailable).

    Documents print bare domains ("www.dgshipping.gov.in") far more often
    than scheme-full URLs; a bare-domain hint is upgraded to https so it can
    seed the crawl. (Live-observed: the Indian SID's only printed pointer
    was scheme-less and was being silently dropped, killing search-free
    discovery for that document.)
    """
    hints = redacted_profile.get("source_discovery_hints") or []
    urls: list[str] = []
    for hint in hints:
        text = str(hint or "").strip()
        if text.startswith(("http://", "https://")):
            urls.append(text)
            continue
        # A hint may embed a URL in prose — take the first http(s) substring.
        m = re.search(r"https?://[^\s,;]+", text)
        if m:
            urls.append(m.group(0))
            continue
        # Only accept a bare-domain hint when the WHOLE hint is one — never
        # pluck domains out of prose ("check dgshipping website" stays out).
        m = re.match(
            r"^(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s,;]*)?$",
            text,
            re.IGNORECASE,
        )
        if m:
            urls.append("https://" + text.lstrip("/"))
    return urls
