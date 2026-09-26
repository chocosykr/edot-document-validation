"""Discovery agent.

Iterative source discovery for a document type with no confirmed source.

Flow (prototype-quality, but genuinely iterative):

  1. LLM generates search queries from the redacted profile.
  2. Tavily performs the searches; candidate URLs form a work queue.
  3. For up to MAX_DISCOVERY_ATTEMPTS pages: fetch the page, run cheap
     deterministic pre-checks (login wall / error / blank), then ask the
     model's own judgment whether the page (or an API endpoint its JavaScript
     exposes) is the right public lookup/verification page for the document
     type. If it is wrong, the model proposes the next move — follow a
     promising same-domain link, open a JS-discovered API endpoint, or refine
     the search query — and the loop continues.
  4. If the attempt budget is exhausted without a valid page, discovery returns
     an EMPTY ``result`` so the engine degrades to VALIDATION_UNAVAILABLE
     (never a crash, never a hallucinated method). Whatever source is accepted
     still goes through the engine's existing test-before-trust validation.

Every LLM call here resolves its model through the shared local/frontier
toggle (utils.llm_client.get_llm_config) — no hardcoded model name.
"""

import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from tavily import TavilyClient

from config import TAVILY_API_KEY
from utils.llm_client import get_llm_config
from utils.js_intel import harvest_endpoints as _shared_harvest_endpoints


# --------------------------------------------------
# Configuration
# --------------------------------------------------

MAX_SEARCH_QUERIES = 8
MAX_RESULTS_PER_QUERY = 5
LLM_MAX_RETRIES = 3
LLM_MIN_INTERVAL_SECONDS = 1.0

# --- Iterative-discovery budget (hard caps: no run-away loops/cost) ----------
MAX_DISCOVERY_ATTEMPTS = int(os.getenv("DISCOVERY_MAX_ATTEMPTS", "10"))
PAGE_FETCH_TIMEOUT_SECONDS = 20
MAX_PAGE_TEXT_CHARS = 6000
MAX_LINKS_PER_PAGE = 30

# Design choice (prototype): a page is accepted only when the model returns
# verdict=ACCEPT with confidence >= this threshold AND reports at least one of
# the document's required lookup fields as present. Anything less is treated as
# a rejection and the proposed next move is taken instead. Deliberately strict:
# a false accept is worse than another attempt.
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
# Domains that are never a public registry lookup form: skipping them keeps the
# limited attempt budget for real candidates (the model still judges everything
# that is enqueued).
_SKIP_HOST_MARKERS = (
    "instagram.", "youtube.", "youtu.be", "facebook.", "linkedin.",
    "twitter.", "//x.com", "tiktok.", "pinterest.", "reddit.",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

last_llm_request_at = 0.0


if not get_llm_config()["api_key"]:
    raise ValueError(
        "LLM_API_KEY is not set in .env"
    )

if not TAVILY_API_KEY:
    raise ValueError(
        "TAVILY_API_KEY is not set in .env"
    )


tavily_client = TavilyClient(
    api_key=TAVILY_API_KEY
)


# --------------------------------------------------
# Prompt loading
# --------------------------------------------------

def load_prompt(filename: str) -> str:
    with open(
        f"prompts/{filename}",
        "r",
        encoding="utf-8"
    ) as file:
        return file.read()


# --------------------------------------------------
# LLM API
# --------------------------------------------------

def call_llm(prompt: str) -> str:

    global last_llm_request_at

    # Shared toggle: endpoint is always LLM_URL; LLM_MODEL is the switch.
    config = get_llm_config()
    url = config["url"]
    model = config["model"]
    api_key = config["api_key"]

    print(f"[discovery] LLM call using model={model!r}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0
    }

    for attempt in range(LLM_MAX_RETRIES + 1):

        elapsed = time.monotonic() - last_llm_request_at
        if elapsed < LLM_MIN_INTERVAL_SECONDS:
            time.sleep(
                LLM_MIN_INTERVAL_SECONDS - elapsed
            )

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=120
        )

        last_llm_request_at = time.monotonic()

        if response.status_code != 429:
            break

        if attempt == LLM_MAX_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get(
            "Retry-After"
        )

        try:
            delay = float(retry_after) if retry_after else 0
        except (TypeError, ValueError):
            delay = 2 ** (attempt + 1)

        print(
            f"LLM rate limit reached; retrying in "
            f"{delay:.0f}s..."
        )
        time.sleep(delay)

    response.raise_for_status()

    result = response.json()

    return result["choices"][0]["message"]["content"]


# --------------------------------------------------
# Generate search queries
# --------------------------------------------------

def generate_search_queries(
    redacted_profile: dict
) -> list[str]:

    prompt = load_prompt(
        "discovery_queries.txt"
    )

    prompt += "\n\n"

    prompt += json.dumps(
        redacted_profile,
        ensure_ascii=False,
        indent=2
    )

    print("\nGenerating search queries with LLM...")

    content = call_llm(prompt)

    return parse_json_array(content)[:MAX_SEARCH_QUERIES]


# --------------------------------------------------
# Tavily searches
# --------------------------------------------------

def perform_searches(
    queries: list[str]
) -> list[dict]:

    search_results = []

    for query in queries:

        print(f"\nSearching: {query}")

        try:

            response = tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=MAX_RESULTS_PER_QUERY,
                include_answer=False
            )

            results = []

            for result in response.get(
                "results",
                []
            ):

                results.append({
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "content": result.get(
                        "content",
                        ""
                    ),
                    "score": result.get("score")
                })

            search_results.append({
                "query": query,
                "results": results
            })

        except Exception as e:

            print(f"Search failed: {e}")

            search_results.append({
                "query": query,
                "results": [],
                "error": str(e)
            })

    return search_results


# --------------------------------------------------
# Page fetching + relevance signals
# --------------------------------------------------

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


# --------------------------------------------------
# Iterative discovery loop
# --------------------------------------------------

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
    """Verification-portal URLs the document ITSELF carries.

    Extracted scans sometimes name the issuing authority's verification URL
    (a QR target, a "verify at" footer, a portal link). These are the
    strongest possible discovery signals — printed on the document by the
    issuer — and unlike search they need no external API. Used to seed the
    crawl queue (and as the fallback when search is unavailable).
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
    return urls


def run_discovery(redacted_profile: dict) -> dict:
    """Search, fetch candidates, judge each, and iterate up to the cap.

    Returns a dict. On success ``result`` carries a ``source_url`` the
    generator can use. On exhaustion ``result`` is empty so the engine returns
    VALIDATION_UNAVAILABLE (graceful, never a hallucinated method).
    """
    queries = generate_search_queries(redacted_profile)
    search_results = perform_searches(queries)

    queue: list[str] = []
    seen: set = set()
    _enqueue(search_results, queue, seen)

    # Search can be entirely unavailable (quota exhausted, API outage). The
    # document's own hints — URLs printed on the document by the issuer —
    # then seed the crawl: they are first-party evidence, better than any
    # search result, and they keep discovery functional without Tavily.
    hints = _hint_urls(redacted_profile)
    if hints:
        total_results = sum(len(r.get("results") or []) for r in search_results)
        if total_results == 0:
            print(
                f"[discovery] search returned nothing "
                f"({sum(1 for r in search_results if r.get('error'))} error(s) "
                f"across {len(search_results)} queries) — falling back to the "
                "document's own source hints."
            )
        for url in hints:
            # NOTE: not added to `seen` here — the crawl loop below marks a
            # URL as seen when it POPS it; pre-marking made the loop skip
            # the hint immediately (observed: "exhausted 0 attempt(s)").
            if url not in seen and not _is_skippable(url):
                queue.insert(0, url)

    visited: list[dict] = []
    attempts = 0

    while queue and attempts < MAX_DISCOVERY_ATTEMPTS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        attempts += 1

        print(
            f"\n[discovery] attempt {attempts}/{MAX_DISCOVERY_ATTEMPTS}: {url}"
        )

        page = fetch_page(url)

        # Harvest JS endpoints FIRST: an SPA whose visible page is a login
        # wall or near-empty shell may still expose the real public lookup API
        # in its bundle. Only take the cheap deterministic rejection when
        # there is nothing left to inspect.
        endpoint_hints = (
            harvest_js_endpoints(page["url"], page.get("js_srcs") or [])
            if page.get("js_srcs") else []
        )
        if endpoint_hints:
            print(
                f"[discovery] harvested {len(endpoint_hints)} JS endpoint "
                f"hint(s) from {page['url']}"
            )

        reject_reason = _deterministic_reject(page, redacted_profile)
        if reject_reason and not endpoint_hints:
            print(f"[discovery] rejected (deterministic): {reject_reason}")
            visited.append({
                "url": url,
                "verdict": "REJECT",
                "confidence": 0,
                "page_type": "login_wall" if "login" in reject_reason else "error",
                "reasoning": reject_reason,
            })
            # A deterministic reject (login wall, error page) kills THIS page,
            # not its links: a portal root behind a login may still link the
            # public verification page (dmamyanmar.org behaves exactly so).
            # Same-host links are cheap to try and never leave the source.
            base_netloc = urlparse(url).netloc
            for link in (page.get("links") or []) + (page.get("external_links") or []):
                if (
                    link not in seen
                    and not _is_skippable(link)
                    and urlparse(link).netloc == base_netloc
                ):
                    queue.append(link)
            continue

        try:
            decision = classify_page(
                redacted_profile, page, endpoint_hints, visited
            )
        except Exception as e:
            print(f"[discovery] page judgment failed: {e}")
            decision = {
                "verdict": "REJECT",
                "confidence": 0,
                "page_type": "unrelated",
                "next_action": "refine_search",
                "reasoning": f"page judgment failed: {e}",
            }

        verdict = str(decision.get("verdict") or "REJECT").upper()
        try:
            confidence = int(decision.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        matched = [
            f for f in (decision.get("matched_fields") or [])
            if f in _required_profile_fields(redacted_profile)
        ]
        required_fields = _required_profile_fields(redacted_profile)

        # Design choice: accept only on ACCEPT + confidence >= threshold +
        # positive evidence of a lookup mechanism — either a required field
        # was matched, or the model identified a concrete API endpoint.
        accepted = (
            verdict == "ACCEPT"
            and confidence >= PAGE_ACCEPT_CONFIDENCE
            and (
                bool(matched)
                or (str(decision.get("page_type") or "").lower() == "api"
                    and decision.get("endpoint_url"))
            )
        )

        visited.append({
            "url": page.get("url"),
            "verdict": "ACCEPT" if accepted else "REJECT",
            "confidence": confidence,
            "page_type": decision.get("page_type"),
            "matched_fields": matched,
            "next_action": decision.get("next_action"),
            "reasoning": (decision.get("reasoning") or "")[:500],
        })

        if accepted:
            # We must always hand the base webpage URL (not a bare API endpoint)
            # to the deterministic generator so it can boot the headless browser
            # and intercept the network request organically.
            selected_url = page.get("url")
            print(
                f"[discovery] ACCEPTED {page.get('url')} "
                f"(confidence={confidence}, fields={matched}) "
                f"-> source {selected_url}"
            )
            return {
                "result": {
                    "source_url": selected_url,
                    "url": selected_url,
                    "page_url": page.get("url"),
                    "verification_available": True,
                    "verification_method": decision.get("page_type"),
                    "required_information": matched or required_fields,
                    "access": {"public": True, "login_required": False},
                    "confidence": confidence,
                    "reasoning": decision.get("reasoning"),
                    "js_endpoint_hints": endpoint_hints,
                },
                "attempts": attempts,
                "visited": visited,
                "search_queries": queries,
                "raw_search_results": search_results,
            }

        # Not accepted -> take the model's proposed next move.
        move = str(decision.get("next_action") or "").lower()
        print(f"[discovery] rejected (model): {decision.get('reasoning')}")
        print(f"[discovery] next move: {move or 'unspecified'}")

        if move == "follow_link" and decision.get("next_url"):
            candidate = urljoin(page.get("url") or "", decision["next_url"])
            known = set(page.get("links") or []) | set(page.get("external_links") or [])
            same_host = any(
                urlparse(candidate).netloc == urlparse(k).netloc for k in known
            )
            if (candidate in known or same_host) and candidate not in seen and not _is_skippable(candidate):
                queue.insert(0, candidate)
            else:
                # The model named a URL we did not actually see on the page:
                # never invent it — fall back to the page's own links.
                for link in (page.get("links") or []) + (page.get("external_links") or []):
                    if link not in seen:
                        queue.append(link)
        elif move == "inspect_endpoint" and decision.get("endpoint_url"):
            candidate = urljoin(page.get("url") or "", decision["endpoint_url"])
            if candidate not in seen:
                queue.insert(0, candidate)
        elif move == "refine_search" and decision.get("next_query"):
            # The model's refined query is the freshest signal: act on its
            # results before the stale initial list.
            _enqueue(perform_searches([decision["next_query"]]), queue, seen, front=True)
        else:
            # No usable move: fall back to the page's own links.
            for link in (page.get("links") or []) + (page.get("external_links") or []):
                if link not in seen:
                    queue.append(link)

    print(
        f"\n[discovery] exhausted {attempts} attempt(s) without a valid page; "
        "returning no source (engine will report VALIDATION_UNAVAILABLE)."
    )
    return {
        "result": {},
        "attempts": attempts,
        "visited": visited,
        "search_queries": queries,
        "raw_search_results": search_results,
        "reason": (
            "Discovery exhausted its attempt budget without finding a valid "
            "public verification page for this document type."
        ),
    }


# --------------------------------------------------
# JSON parsing
# --------------------------------------------------

def parse_json_response(
    content: str
) -> dict:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, dict):

        raise ValueError(
            "LLM did not return a JSON object."
        )

    return result


def parse_json_array(
    content: str
) -> list[str]:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, list):

        raise ValueError(
            "LLM did not return a JSON array."
        )

    if not all(
        isinstance(query, str)
        for query in result
    ):

        raise ValueError(
            "All search queries must be strings."
        )

    return result
