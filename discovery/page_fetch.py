"""Discovery page fetching: download a candidate page and extract the signals
the judge needs (title, visible text, form inputs, links, JS bundle sources),
plus JS endpoint harvesting shared with the generation pipeline.

Split out of agent.py.
"""

from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils.js_intel import harvest_endpoints as _shared_harvest_endpoints

PAGE_FETCH_TIMEOUT_SECONDS = 20
MAX_PAGE_TEXT_CHARS = 6000
MAX_LINKS_PER_PAGE = 30

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


def _required_profile_fields(redacted_profile: dict) -> list[str]:
    """Lookup fields the document itself carries (tokens count as presence)."""
    fields = [
        field for field in ("document_number", "date_of_birth", "full_name")
        if redacted_profile.get(field)
    ]
    return fields or ["document_number"]


def fetch_page(url: str) -> dict:
    """Fetch a candidate page and extract the signals the judge needs."""
    try:
        response = requests.get(
            url,
            timeout=PAGE_FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except Exception as e:
        return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        return {
            "url": url, "ok": False, "status": response.status_code,
            "error": f"unparsable HTML: {e}",
        }

    final_url = response.url or url
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    visible_text = " ".join(soup.get_text(" ", strip=True).split())

    inputs = []
    for el in soup.find_all(["input", "select", "textarea"]):
        inputs.append({
            "tag": el.name,
            "type": (el.get("type") or "").lower(),
            "name": el.get("name") or "",
            "id": el.get("id") or "",
            "placeholder": el.get("placeholder") or "",
        })
    has_password = any(i["type"] == "password" for i in inputs)

    js_srcs = []
    for script in soup.find_all("script"):
        if script.get("src"):
            js_srcs.append(script["src"])

    host = urlparse(final_url).netloc
    links = []
    external_links = []
    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(final_url, anchor["href"]).split("#")[0]
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        bucket = links if urlparse(candidate).netloc == host else external_links
        if candidate not in bucket and len(bucket) < MAX_LINKS_PER_PAGE:
            bucket.append(candidate)

    return {
        "url": final_url,
        "requested_url": url,
        "ok": response.ok,
        "status": response.status_code,
        "title": title,
        "text": visible_text[:MAX_PAGE_TEXT_CHARS],
        "inputs": inputs[:40],
        "has_password": has_password,
        "links": links,
        "external_links": external_links,
        "js_srcs": js_srcs,
        "html_len": len(response.text),
    }


def harvest_js_endpoints(page_url: str, js_srcs: list[str]) -> list[str]:
    """Harvest ranked endpoint-like strings from the page's JS bundles.

    This is the 'inspect the page's own JS/network calls' move. The
    implementation is SHARED with the generation pipeline
    (utils/js_intel.harvest_endpoints) so generation reuses exactly the
    inspection discovery already performs.
    """
    return _shared_harvest_endpoints(page_url, js_srcs)
