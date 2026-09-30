"""dvs_io — the SDK shim injected next to an LLM-authored SCRIPT executor.

A SCRIPT method is arbitrary Python authored by the model to drive a site that
does not fit the declarative step schema (stateful cookies, multi-step
handshakes, bespoke parsing). The script runs in exactly the same Docker
sandbox as every other executor and must speak the same contract:

    /workspace/input.json   <- {"method": {...}, "inputs": {...}}
    /workspace/output.json  -> {"decision_status", "raw_response", "evidence"}

This module gives the script safe primitives so it does not have to
re-implement transport, timeouts, cookies, or retry policy:

    from dvs_io import get_input, http_get, http_post, write_result

    cdc = get_input("cdc_number")
    status, body = http_post(url, data={"CDC": cdc},
                             headers={"RequestVerificationToken": tok})
    write_result("REJECTED" if "VerificationError" in body else "UNCERTAIN",
                 raw_response=body, evidence={"http_status": status})

Design note (Phase 4): the script MAY propose a decision, but the harness
still owns the verdict/evidence policy — the runner records the script's
status, raw_response and evidence, and the validator/decider layer classifies
on top of them. Script freedom ≠ verdict freedom.
"""

import json
import os
import sys
import time

INPUT_PATH = os.environ.get("DVS_INPUT_PATH", "input.json")
OUTPUT_PATH = os.environ.get("DVS_OUTPUT_PATH", "output.json")

# One cookie-aware session per script process. Anti-forgery flows (ASP.NET
# MVC, servlet sessions) require the session cookie AND the token back on the
# POST; a cookieless client gets refused even with a valid token.
_SESSION = None

# Retry policy mirrors executors/http_helpers.py: transient network errors and
# 5xx are retried; a 4xx is a DEFINITIVE application-level answer and is never
# retried (retrying it burns single-use captchas and hides real rejections).
_MAX_ATTEMPTS = 3
_TIMEOUT_S = 20
_TRANSIENT_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "no route to host",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "timed out",
    "remote end closed connection",
    "max retries exceeded",
)


def _load_inputs() -> dict:
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Tolerate both the full ExecutionRequest envelope and a bare {inputs:...}
    if isinstance(data, dict) and "inputs" in data:
        return data.get("inputs") or {}
    return data if isinstance(data, dict) else {}


def get_inputs() -> dict:
    """All declared input values supplied by the execution engine."""
    return _load_inputs()


def get_input(name: str, default=None):
    """A single declared input by name (raising is safer than a wrong lookup)."""
    return _load_inputs().get(name, default)


def _session():
    global _SESSION
    if _SESSION is None:
        import requests  # provided by the executor image
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": "DVS-SCRIPT/1.0"})
    return _SESSION


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _request(method: str, url: str, params=None, data=None, json_body=None,
             headers=None, cookies=None, timeout=None):
    """One retried HTTP call. Returns (status_code, body_text).

    Accepts the ``requests`` keyword names an authored script naturally
    reaches for (``headers``, ``cookies``, ``json``, ``timeout``); only the
    retry policy and cookie session are opinionated.
    """
    import requests

    last_exc = None
    per_call_headers = dict(headers) if headers else None
    per_call_cookies = dict(cookies) if cookies else None
    per_call_timeout = timeout if timeout else _TIMEOUT_S
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _session().request(
                method, url, params=params, data=data, json=json_body,
                headers=per_call_headers, cookies=per_call_cookies,
                timeout=per_call_timeout, allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == _MAX_ATTEMPTS:
                raise
            time.sleep(1.5 * (2 ** (attempt - 1)))
            continue
        # 4xx is definitive — never retry. 5xx / connection issues retry.
        if 500 <= resp.status_code < 600 and attempt < _MAX_ATTEMPTS:
            time.sleep(1.5 * (2 ** (attempt - 1)))
            continue
        return resp.status_code, resp.text
    if last_exc:
        raise last_exc
    return 0, ""


def http_get(url: str, params=None, headers=None, cookies=None, timeout=None):
    """GET returning (status, text); cookies persist across calls."""
    return _request(
        "GET", url, params=params,
        headers=headers, cookies=cookies, timeout=timeout,
    )


def http_post(url: str, data=None, json_body=None, params=None, headers=None,
              cookies=None, timeout=None, **kwargs):
    """POST returning (status, text).

    Pass ``data`` for a form body or ``json_body`` (aliased as ``json``) for a
    JSON body. ``headers``, ``cookies`` and ``timeout`` are honoured when the
    authored script supplies them; unknown kwargs are ignored rather than
    crashing the sandbox on a naming difference.
    """
    # `json` is accepted as an alias for `json_body`, matching requests.
    json_alt = kwargs.pop("json", None)
    if json_body is None:
        json_body = json_alt
    return _request(
        "POST", url, params=params, data=data, json_body=json_body,
        headers=headers, cookies=cookies, timeout=timeout,
    )


def write_result(status: str = None, raw_response: str = None, evidence: dict = None):
    """Write the standard output.json contract and exit 0.

    `status` is one of VERIFIED / REJECTED / UNCERTAIN /
    VALIDATION_UNAVAILABLE / TECHNICAL_FAILURE. Defaults to UNCERTAIN so an
    author that forgets the verdict never silently claims success.
    """
    out = {
        "decision_status": (status or "UNCERTAIN").upper(),
        "raw_response": raw_response,
        "evidence": evidence or {},
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    sys.exit(0)


# Backwards-compatible alias (early prototype name).
report_decision = write_result
