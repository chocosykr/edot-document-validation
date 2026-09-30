"""
HTTP helper primitives for the HTTP executor: response compaction, template
substitution, session-cookie-aware HTTP verbs, transient-failure retry, and
CAPTCHA fetching.

Split out of http_executor.py so the decision logic (http_decider.py) and the
step runner (http_executor.py) stay small. In the Docker sandbox these files
are copied flat next to executor.py (see execution/docker_runner.py), so
imports fall back to plain module names there — same pattern as llm_client.
"""

import base64
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Response compaction: preserve JSON structure, truncate only large base64
# ---------------------------------------------------------------------------

_B64_RE = re.compile(r'^[A-Za-z0-9+/=\s]{200,}$')
_B64_KEEP = 200

def _compact_response(body: str, limit: int = 50_000) -> str:
    if not body:
        return body

    json_str = body.strip()
    if json_str.startswith('<') or json_str.startswith('<!'):
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

    return body[:limit]

def _compact_dict(d: dict) -> dict:
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _compact_dict(value)
        elif isinstance(value, list):
            result[key] = [_compact_dict(item) if isinstance(item, dict) else item
                          for item in value]
        elif isinstance(value, str) and len(value) > _B64_KEEP:
            if _B64_RE.match(value[:500]):
                result[key] = value[:_B64_KEEP]
            else:
                result[key] = value
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------

def substitute(value: str, inputs: dict) -> str:
    for k, v in inputs.items():
        value = value.replace(f"{{{{{k}}}}}", v)
    return value


def substitute_dict(d: dict, inputs: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = substitute(v, inputs)
        elif isinstance(v, dict):
            result[k] = substitute_dict(v, inputs)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# HTTP helpers (pure stdlib — no requests required)
# ---------------------------------------------------------------------------

# One cookie-aware opener per executor process = one session per method run.
# ASP.NET anti-forgery flows (e.g. dmamyanmar.org) issue a session cookie
# together with the __RequestVerificationToken and require BOTH back on the
# POST; a cookieless client gets 400 even with a valid token.
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

# Transient network failures — DNS blips, "No route to host", connection
# resets — killed otherwise-correct runs during testing (one run saw BOTH a
# host-side DNS resolution failure and an in-container "No route to host" on
# a registry that answered HTTP 200 seconds later). These are environmental,
# not method defects, so a short bounded retry with jittered backoff is
# applied around every request below. A 5xx response is retried the same
# way: the server was there, the answer just failed.
_TRANSIENT_ERRNO = {99, 101, 102, 104, 110, 111, 113}  # ECONNRESET, EHOSTUNREACH, ETIMEDOUT, ECONNREFUSED, EHOSTDOWN, ENETUNREACH, EADDRNOTAVAIL... (the Errno values actually observed in sandbox logs)
_TRANSIENT_MARKERS = (
    "temporary failure in name resolution",   # DNS (Errno -3)
    "name or service not known",
    "no route to host",
    "connection reset by peer",
    "connection refused",
    "network is unreachable",
    "timed out",
    "remote end closed connection",
    "certificate verify failed",
)


def _is_transient(err: Exception) -> bool:
    text = str(err).lower()
    if any(m in text for m in _TRANSIENT_MARKERS):
        return True
    # URLError wraps an underlying socket.error carrying the errno.
    errno = getattr(getattr(err, "reason", None), "errno", None) or getattr(err, "errno", None)
    return errno in _TRANSIENT_ERRNO


def _is_5xx(status: int) -> bool:
    return 500 <= status <= 599


# Body-level retry rules, deliberately GENERIC (no portal vocabulary): some
# registries answer throttled/mid-outage requests with HTTP 200 and an error
# fragment in the body. We retry when the body is evidence of a server-side
# problem — framework error-page titles (ASP.NET/Java/PHP universals) or HTTP
# reason phrases — or when it is TOO SMALL to be a record lookup result at
# all (retrying an idempotent lookup is always safe; the worst case is a few
# seconds of latency). Anything ambiguous that survives retries is classified
# host-side as TECHNICAL_FAILURE, never a definitive verdict. Portal-specific
# error shapes belong in the method's own learned not_found_signatures.
_FRAMEWORK_ERROR_MARKERS = (
    "object reference not set",          # ASP.NET NullReferenceException page
    "server error in '/' application",   # ASP.NET yellow-screen-of-death
    "exception",                         # Java/PHP/ASP error pages
    "stack trace",
)
_HTTP_REASON_PHRASES = (
    "service unavailable", "internal server error", "bad gateway",
    "gateway timeout", "gateway time-out",
)
_TINY_BODY_RETRY_LIMIT = 200


def _body_worth_retrying(result) -> bool:
    if not (isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], str)):
        return False
    body = result[1].strip()
    if not body:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in _FRAMEWORK_ERROR_MARKERS + _HTTP_REASON_PHRASES):
        return True
    return len(body) < _TINY_BODY_RETRY_LIMIT


def _with_retry(fn, *args, **kwargs):
    """Wrapper combining exception-retry with 5xx/body-service-error retry."""
    attempts = 3
    result = None
    for attempt in range(1, attempts + 1):
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            if not _is_transient(e) or attempt == attempts:
                raise
            time.sleep(1.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
            continue
        if (
            isinstance(result, tuple) and result
            and isinstance(result[0], int)
            and attempt < attempts
            and (
                _is_5xx(result[0])
                # 4xx is a DEFINITIVE application-level answer — never retry
                # it. (Live case, 2026-09-29: dgshippingbsid.in's verify
                # endpoint answers tiny 400 JSON bodies — captcha errors AND
                # business rejections like "Application ID not found" — and
                # the tiny-body rule re-submitted them, burning single-use
                # captchas so the executor only ever saw the final "expired
                # captcha" response and could never reach a real verdict.)
                or (result[0] < 400 and _body_worth_retrying(result))
            )
        ):
            time.sleep(1.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
            continue
        return result
    return result


def http_get(url: str, params: dict) -> tuple[int, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_get_retry(url: str, params: dict) -> tuple[int, str]:
    return _with_retry(http_get, url, params)


def http_post_json_retry(url: str, body: dict) -> tuple[int, str]:
    return _with_retry(http_post_json, url, body)


def http_post_form_retry(url: str, params: dict) -> tuple[int, str]:
    return _with_retry(http_post_form, url, params)


def http_post_query_retry(url: str) -> tuple[int, str]:
    return _with_retry(http_post_query, url)


def http_get_bytes_retry(url: str) -> tuple[int, bytes]:
    return _with_retry(http_get_bytes, url)


def http_post_json(url: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_post_form(url: str, params: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def http_get_bytes(url: str) -> tuple[int, bytes]:
    """GET returning raw bytes (for captcha images served as a URL)."""
    req = urllib.request.Request(url, headers={"User-Agent": "DVS/1.0"})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post_query(url: str) -> tuple[int, str]:
    """
    POST with parameters on the query string and an explicit empty body.

    This is the legacy XHR `send(null)` servlet pattern: params are appended
    to the URL by the caller, and the request body is empty. The verb stays
    POST — that is the wire format confirmed live (a GET must NOT be
    substituted here; some frameworks route GET/POST to different handlers
    and WAFs may enforce POST-only on servlet mappings).
    """
    req = urllib.request.Request(
        url, data=b"",
        headers={"User-Agent": "DVS/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _extract_regex_from_html(html: str, selector: str) -> str | None:
    """Minimal EXTRACT_FROM_HTML: a CSS-ish selector reduced to attribute
    extraction by regex. Supports the one shape the executor actually needs
    for anti-forgery flows:

        input[name='__RequestVerificationToken']  +  attribute=value

    Unknown selector shapes are a hard error for the step (fail loudly, not
    silently-skip): generation must only emit supported selectors.
    """
    m = re.match(
        r"^\s*input\[\s*name\s*=\s*['\"]([^'\"]+)['\"]\s*\]\s*$", selector or ""
    )
    if not m:
        raise ValueError(f"EXTRACT_FROM_HTML: unsupported selector: {selector!r}")
    name = m.group(1)
    tag = re.search(
        r"<input\b[^>]*\bname\s*=\s*['\"]" + re.escape(name)
        + r"['\"][^>]*>", html or "", re.I,
    )
    if not tag:
        return None
    vm = re.search(r"\bvalue\s*=\s*['\"]([^'\"]*)['\"]", tag.group(0), re.I)
    return vm.group(1) if vm else None


# ---------------------------------------------------------------------------
# CAPTCHA steps
# ---------------------------------------------------------------------------

def _sanitize_captcha_text(raw: str) -> str:
    """Keep only plausible captcha characters (empty string => unusable)."""
    text = re.sub(r"```[a-z]*", "", raw or "").replace("```", "").strip()
    text = text.strip('"\'`')
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _first_key(parsed: dict, *keys) -> str:
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_captcha(url: str, step: dict) -> tuple[str, str, str]:
    """Fetch a CAPTCHA image + id. Returns (image_b64, captcha_id, error)."""
    fmt = str(step.get("response_format") or "json").lower()
    image_field = step.get("image_field") or "captchaImage"
    id_field = step.get("id_field") or "captchaId"

    status, body = http_get_retry(url, {})
    if status and status >= 400:
        return "", "", f"FETCH_CAPTCHA returned HTTP {status}"

    if fmt == "json":
        try:
            parsed = json.loads(body)
        except Exception as e:
            return "", "", f"FETCH_CAPTCHA response was not JSON ({e})"
        if not isinstance(parsed, dict):
            return "", "", "FETCH_CAPTCHA JSON was not an object"
        image_val = _first_key(
            parsed, image_field, "captchaImage", "captcha_image", "image", "img", "captcha"
        )
        id_val = _first_key(
            parsed, id_field, "captchaId", "captcha_id", "id", "key", "token"
        )
    else:
        image_val = body.strip()
        id_val = ""

    if not image_val:
        return "", "", "FETCH_CAPTCHA: no image found in the response"

    if image_val.lower().startswith("data:image"):
        image_val = image_val.split(",", 1)[-1]
    elif image_val.lower().startswith(("http://", "https://")):
        status2, raw = http_get_bytes_retry(image_val)
        if status2 and status2 >= 400:
            return "", "", f"CAPTCHA image URL returned HTTP {status2}"
        image_val = base64.b64encode(raw).decode("ascii")

    return image_val, id_val, ""
