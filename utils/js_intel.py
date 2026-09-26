"""Shared JavaScript endpoint harvesting.

SPA bundles frequently reference the real public API (a CAPTCHA-fetch endpoint
plus a verify endpoint, for example) even when the visible page is a login wall
or an empty shell. This module fetches same-origin JS bundles and harvests
endpoint-like strings, ranked so the most promising survive the cap.

Used by BOTH:
  - discovery/agent.py — the "inspect the page's own JS" move, and
  - generation/generator.py — so the LLM-fallback generation path can derive the
    request contract from real JS/network calls instead of guessing from the
    visible form.
Keeping one implementation means generation reuses exactly the inspection that
already works in discovery.

Only `requests` (an existing dependency) is used.
"""

import re
from typing import List
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

MAX_JS_BUNDLES = 4
MAX_JS_BUNDLE_CHARS = 6_000_000
MAX_ENDPOINT_HINTS = 60
JS_FETCH_TIMEOUT_SECONDS = 20

ENDPOINT_HINT_KEYWORDS = (
    "api", "captcha", "verif", "search", "check", "lookup", "public",
    "sid", "indos", "status", "query", "validate",
)
# Higher-signal words used to RANK harvested paths.
HIGH_VALUE_KEYWORDS = (
    "captcha", "verify", "verif", "lookup", "validate", "check", "search",
    "query", "public",
)

_PATH_RE = re.compile(r"/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-{}.:$]+){1,6}")
_ABS_URL_RE = re.compile(r"https?://[A-Za-z0-9_\-./]+")

# --- Concrete call-pattern extraction (verb + path + query param names) -----
# A bare path list is not enough for generation: the model still has to know
# the HTTP verb and the parameter names. These patterns pull the whole call.
# Backtick template literals may contain quotes (e.g. ${f(q||"")}), so each
# delimiter gets its own pattern instead of a shared character class.
_FETCH_TPL_PATTERNS = (
    re.compile(r"fetch\(\s*`([^`]{1,500})`\s*,?\s*\{([^{}]{0,240})\}", re.S),
    re.compile(r"fetch\(\s*'([^']{1,500})'\s*,?\s*\{([^{}]{0,240})\}", re.S),
    re.compile(r'fetch\(\s*"([^"]{1,500})"\s*,?\s*\{([^{}]{0,240})\}', re.S),
)
_FETCH_BARE_PATTERNS = (
    re.compile(r"fetch\(\s*`([^`]{1,500})`", re.S),
    re.compile(r"fetch\(\s*'([^']{1,500})'", re.S),
    re.compile(r'fetch\(\s*"([^"]{1,500})"', re.S),
)
_AXIOS_RE = re.compile(
    r"axios\.(get|post|put|delete|patch)\(\s*[`\"']([^`\"']{1,400})[`\"']", re.I
)
_OPEN_RE = re.compile(r"\.open\(\s*[\"'](GET|POST|PUT|DELETE)[\"']\s*,\s*([^,)]{1,300})")
_METHOD_IN_OPTS_RE = re.compile(r"method\s*:\s*[\"']([A-Za-z]{3,7})[\"']")
_QUERY_PARAM_RE = re.compile(r"[?&]([A-Za-z0-9_\-]+)=")
_PATH_IN_URL_RE = re.compile(r"(/[A-Za-z0-9_\-./]{2,80})")
_CALL_RELEVANCE = (
    "captcha", "verify", "verif", "lookup", "check", "search", "query",
    "public", "validate", "login", "otp", "session", "token", "api/",
)


def _call_score(line: str) -> int:
    low = line.lower()
    score = sum(1 for kw in _CALL_RELEVANCE if kw in low)
    if "captcha" in low:
        score += 3
    if "params=" in low:            # a call that carries parameters is richer
        score += 1
    return score


def harvest_call_hints(js_text: str, max_hints: int = 30) -> List[str]:
    """Extract concrete API call patterns: verb, path, and query param names.

    Example output line:  ``GET /seafarer/sid/verify  params=[inputvalue, captcha, captchaId]``
    This is what lets the generation step copy the site's REAL request contract
    instead of guessing a verb/param name from the visible form.
    """
    seen: dict = {}

    def _add(verb: str, url_template: str) -> None:
        if not url_template:
            return
        path_match = _PATH_IN_URL_RE.search(url_template)
        path = path_match.group(1) if path_match else url_template.strip()
        params = list(dict.fromkeys(_QUERY_PARAM_RE.findall(url_template)))
        line = f"{(verb or '').upper()} {path}".strip()
        if params:
            line += "  params=[" + ", ".join(params) + "]"
        if line and line not in seen:
            seen[line] = _call_score(line)

    with_opts = set()
    for pattern in _FETCH_TPL_PATTERNS:
        for match in pattern.finditer(js_text):
            url_template, opts = match.group(1), match.group(2) or ""
            verb_match = _METHOD_IN_OPTS_RE.search(opts)
            _add(verb_match.group(1) if verb_match else "", url_template)
            with_opts.add(url_template)
    for pattern in _FETCH_BARE_PATTERNS:
        for match in pattern.finditer(js_text):
            if match.group(1) not in with_opts:
                _add("", match.group(1))
    for match in _AXIOS_RE.finditer(js_text):
        _add(match.group(1), match.group(2))
    for match in _OPEN_RE.finditer(js_text):
        _add(match.group(1), match.group(2))

    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    return [line for line, _ in ranked[:max_hints]]


def _endpoint_score(path: str) -> int:
    """Rank a harvested path by how likely it is a real lookup/verify call."""
    low = path.lower()
    score = sum(1 for kw in HIGH_VALUE_KEYWORDS if kw in low)
    if "captcha" in low:
        score += 3
    if "/api/" in low:
        score += 2
    if low.rstrip("/").endswith(("verify", "generate", "refresh", "validate",
                                 "check", "search", "lookup", "query", "status")):
        score += 2
    if "{" in path or "$" in path:
        score -= 1  # template placeholders are less directly callable
    return score


def fetch_bundles(page_url: str, script_srcs: List[str]) -> List[str]:
    """Download same-origin JS bundles (up to the caps). Returns their text."""
    page_host = urlparse(page_url).netloc
    texts: List[str] = []
    for src in script_srcs[:MAX_JS_BUNDLES]:
        abs_src = urljoin(page_url, src)
        if urlparse(abs_src).netloc != page_host:
            continue
        try:
            response = requests.get(
                abs_src, timeout=JS_FETCH_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            if not response.ok:
                continue
            texts.append(response.text[:MAX_JS_BUNDLE_CHARS])
        except Exception:
            continue
    return texts


def harvest_endpoints(
    page_url: str,
    script_srcs: List[str],
    max_hints: int = MAX_ENDPOINT_HINTS,
) -> List[str]:
    """Harvest ranked endpoint-like strings from a page's same-origin bundles."""
    page_host = urlparse(page_url).netloc
    scored: dict = {}

    for js in fetch_bundles(page_url, script_srcs):
        for match in _PATH_RE.finditer(js):
            path = match.group(0)
            low = path.lower()
            if low.startswith("/assets/"):
                continue  # static bundles/images, never the verification API
            if any(kw in low for kw in ENDPOINT_HINT_KEYWORDS) and path not in scored:
                scored[path] = _endpoint_score(path)
        for match in _ABS_URL_RE.finditer(js):
            url = match.group(0)
            if page_host in url and url not in scored:
                scored[url] = _endpoint_score(url) + 1  # explicit absolute URL

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    return [path for path, _ in ranked[:max_hints]]


def harvest_js_intel(
    page_url: str,
    script_srcs: List[str],
    max_endpoints: int = MAX_ENDPOINT_HINTS,
    max_calls: int = 30,
) -> tuple:
    """Fetch the bundles ONCE and return both harvests.

    Returns (endpoint_paths, call_hints) where call_hints are concrete
    "VERB /path params=[...]" lines — the real request contract.
    """
    endpoints, calls, _ = harvest_js_intel_with_text(
        page_url, script_srcs, max_endpoints=max_endpoints, max_calls=max_calls,
    )
    return endpoints, calls


def harvest_js_intel_with_text(
    page_url: str,
    script_srcs: List[str],
    max_endpoints: int = MAX_ENDPOINT_HINTS,
    max_calls: int = 30,
) -> tuple:
    """Same single fetch, but also return the raw bundle text.

    Returns (endpoint_paths, call_hints, bundle_js). The raw text feeds the
    deterministic bundle contract extractor (generation/bundle_contract.py)
    so the SPA fetch() calls are parsed without a second download.
    """
    js = "\n".join(fetch_bundles(page_url, script_srcs))
    endpoints = harvest_endpoints_from_text(js, page_host=urlparse(page_url).netloc,
                                            max_hints=max_endpoints)
    calls = harvest_call_hints(js, max_hints=max_calls)
    return endpoints, calls, js


def harvest_endpoints_from_text(js_text: str, page_host: str = "",
                                max_hints: int = MAX_ENDPOINT_HINTS) -> List[str]:
    """Same ranking, but over already-fetched JS text (no network)."""
    scored: dict = {}
    for match in _PATH_RE.finditer(js_text or ""):
        path = match.group(0)
        low = path.lower()
        if low.startswith("/assets/"):
            continue
        if any(kw in low for kw in ENDPOINT_HINT_KEYWORDS) and path not in scored:
            scored[path] = _endpoint_score(path)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    return [path for path, _ in ranked[:max_hints]]
