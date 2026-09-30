"""Harness-side verdict policy for SCRIPT methods.

A SCRIPT method is arbitrary LLM-authored code. It may PROPOSE a decision, but
the final verdict is not its to give: the harness re-reads the script's
``raw_response`` + ``http_status`` through the same keyword / not-found logic
the HTTP executor uses and applies downward-only guardrails. Script freedom
(transport) is not verdict freedom:

  * a script cannot claim VERIFIED on a body the harness reads as a refusal
    (declared failure/not-found signal) — that becomes REJECTED;
  * a script cannot claim VERIFIED with no response body at all — that becomes
    UNCERTAIN (no evidence, no verdict);
  * a script cannot claim REJECTED when the body carries a declared success
    signal — that becomes UNCERTAIN (never reject on contradictory evidence);
  * infrastructure statuses (TECHNICAL_FAILURE / VALIDATION_UNAVAILABLE) are
    passed through untouched.

The script may still legitimately classify a body the harness has no opinion on
(no declared keywords, no signature) — e.g. a site-specific parse — because
only the script knows that site. The guardrails only remove confidence the
evidence does not support; they never upgrade it.
"""

import logging

from execution.models import ExecutionDecisionStatus, ExecutionResult
from registry.models import MethodType

logger = logging.getLogger(__name__)

_INFRA = {
    ExecutionDecisionStatus.TECHNICAL_FAILURE,
    ExecutionDecisionStatus.VALIDATION_UNAVAILABLE,
}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _harness_signal(body: str, expected: dict, http_status) -> str:
    """The harness's own read of the body: VERIFIED / REJECTED / UNCERTAIN."""
    # Imported lazily so this host-side module never drags the executors
    # package into an import cycle at module load.
    from executors.http_decider import _matches_not_found_signature, decide

    if _matches_not_found_signature(http_status, body, expected):
        return "REJECTED"
    return decide(body, expected)


def apply_script_policy(result: ExecutionResult, method) -> ExecutionResult:
    if method.method_type != MethodType.SCRIPT:
        return result
    if result.decision_status in _INFRA:
        return result

    proposed = result.decision_status.value
    body = (result.raw_response or "").strip()
    expected = method.expected_responses or {}
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    http_status = _as_int(evidence.get("http_status"))

    signal = _harness_signal(body, expected, http_status)

    new_status = None
    reason = None
    if proposed == "VERIFIED":
        if signal == "REJECTED":
            new_status, reason = (
                "REJECTED",
                "harness read a declared failure/not-found signal in the body",
            )
        elif not body:
            new_status, reason = (
                "UNCERTAIN",
                "script claimed VERIFIED with no response body as evidence",
            )
    elif proposed == "REJECTED" and signal == "VERIFIED":
        new_status, reason = (
            "UNCERTAIN",
            "script claimed REJECTED but the body carries a declared success signal",
        )

    if new_status is None:
        return result

    logger.warning(
        "SCRIPT policy overrode %s -> %s for %s: %s",
        proposed, new_status, method.method_id, reason,
    )
    evidence = dict(evidence)
    evidence["script_proposed_status"] = proposed
    evidence["script_policy_reason"] = reason
    return result.model_copy(
        update={
            "decision_status": ExecutionDecisionStatus(new_status),
            "evidence": evidence,
        }
    )
