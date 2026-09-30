"""Tests for the harness-side SCRIPT verdict guardrails."""

import unittest

from execution.models import ExecutionDecisionStatus, ExecutionResult
from execution.script_policy import apply_script_policy
from registry.models import MethodType, ValidationMethod


def _method(expected=None, method_type=MethodType.SCRIPT) -> ValidationMethod:
    return ValidationMethod(
        method_id="M_SCRIPT_POLICY",
        method_type=method_type,
        source_url="http://example.test",
        required_inputs=["document_number"],
        script_source="from dvs_io import write_result\n",
        expected_responses=expected if expected is not None else {},
    )


def _result(status, body="", http_status=None, evidence=None) -> ExecutionResult:
    ev = dict(evidence or {})
    if http_status is not None:
        ev["http_status"] = http_status
    return ExecutionResult(
        decision_status=ExecutionDecisionStatus(status),
        raw_response=body, evidence=ev,
    )


class TestScriptPolicy(unittest.TestCase):
    def test_non_script_is_untouched(self):
        m = _method(method_type=MethodType.HTTP)
        r = _result("VERIFIED", body="")
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.VERIFIED)

    def test_infra_status_passthrough(self):
        m = _method()
        r = _result("TECHNICAL_FAILURE", body="whatever")
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)

    def test_verified_on_declared_failure_signal_becomes_rejected(self):
        m = _method({"comparison_mode": "field_match",
                     "failure_keywords": ["verificationerror"]})
        r = _result("VERIFIED", body="VerificationError", http_status=200)
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertEqual(out.evidence["script_proposed_status"], "VERIFIED")
        self.assertIn("script_policy_reason", out.evidence)

    def test_verified_with_no_body_becomes_uncertain(self):
        m = _method({"comparison_mode": "field_match"})
        out = apply_script_policy(_result("VERIFIED", body=""), m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.UNCERTAIN)

    def test_rejected_on_success_signal_becomes_uncertain(self):
        m = _method({"comparison_mode": "field_match",
                     "success_keywords": ["record verified"]})
        r = _result("REJECTED", body="Record VERIFIED for this seafarer")
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.UNCERTAIN)

    def test_rejected_with_no_signal_is_honoured(self):
        # The script knows a site-specific parse the harness has no keyword for.
        m = _method({"comparison_mode": "field_match"})
        r = _result("REJECTED", body="opaque-site-specific-marker")
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.REJECTED)

    def test_matching_failure_signal_keeps_rejected(self):
        m = _method({"comparison_mode": "field_match",
                     "failure_keywords": ["verificationerror"]})
        r = _result("REJECTED", body="VerificationError", http_status=200)
        out = apply_script_policy(r, m)
        self.assertEqual(out.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertNotIn("script_policy_reason", out.evidence)


if __name__ == "__main__":
    unittest.main()
