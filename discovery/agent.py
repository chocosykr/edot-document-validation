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

Module layout (split for modularity):
  llm.py         — chat calls + rate-limit retry, prompt loading, JSON parsing
  search.py      — LLM query generation + Tavily search
  page_fetch.py  — candidate page fetch + JS endpoint harvest
  judge.py       — deterministic pre-checks + model page judgment
  crawl_queue.py — document hint extraction + queue management
  agent.py       — this orchestrator (the discovery loop)

Every LLM call here resolves its model through the shared local/frontier
toggle (utils.llm_client.get_llm_config) — no hardcoded model name.
"""

import os
from urllib.parse import urljoin, urlparse

# Split modules. Everything is re-exported here so existing callers
# (engine.validation_engine imports run_discovery; tests import _hint_urls)
# and tests that patch through discovery.agent keep working.
from discovery.crawl_queue import (
    _SKIP_HOST_MARKERS,
    _enqueue,
    _hint_urls,
    _is_skippable,
)
from discovery.judge import (
    _ERROR_MARKERS,
    _LOGIN_MARKERS,
    _REQUIRED_FIELD_SYNONYMS,
    _deterministic_reject,
    classify_page,
)
from discovery.llm import (
    LLM_MAX_RETRIES,
    LLM_MIN_INTERVAL_SECONDS,
    call_llm,
    load_prompt,
    parse_json_array,
    parse_json_response,
)
from discovery.page_fetch import (
    MAX_LINKS_PER_PAGE,
    MAX_PAGE_TEXT_CHARS,
    PAGE_FETCH_TIMEOUT_SECONDS,
    USER_AGENT,
    _required_profile_fields,
    fetch_page,
    harvest_js_endpoints,
)
from discovery.search import (
    MAX_RESULTS_PER_QUERY,
    MAX_SEARCH_QUERIES,
    generate_search_queries,
    perform_searches,
    tavily_client,
)

# --- Iterative-discovery budget (hard caps: no run-away loops/cost) ----------
MAX_DISCOVERY_ATTEMPTS = int(os.getenv("DISCOVERY_MAX_ATTEMPTS", "10"))


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
