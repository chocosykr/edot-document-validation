import os
import unittest
from unittest.mock import MagicMock, patch

from execution.models import ExecutionResult, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus
from registry.repository import MethodRegistry
from validation.models import TestCase, ValidationReportStatus, AttemptOutcome
from validation.validator import MethodValidator, MAX_ATTEMPTS_PER_TEST_CASE

# Keep the expensive agentic self-healing escalation (real LLM + tool loop)
# out of unit tests; the direct LLM rewrite pass is mocked per-test instead.
OS_ENV = patch.dict(os.environ, {"DVS_AGENTIC_HEALING": "0"})
OS_ENV.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_method(method_id: str = "M_TEST") -> ValidationMethod:
    return ValidationMethod(
        method_id=method_id,
        method_type=MethodType.HTTP,
        source_url="https://mock.example.com",
        country="India",
        document_type="CDC",
    )


def _mock_runner(decision: str, error: str = None) -> MagicMock:
    """Return a DockerMethodRunner mock that always returns `decision`."""
    runner = MagicMock()
    runner.execute_method.return_value = ExecutionResult(
        decision_status=ExecutionDecisionStatus(decision),
        evidence={"mock": True},
        logs="mock log",
        error=error,
    )
    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMethodValidator(unittest.TestCase):

    # -- passing scenario ------------------------------------------------

    def test_all_required_cases_pass(self):
        method = _make_method()
        runner = _mock_runner("VERIFIED")
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        test_cases = [
            TestCase(name="valid_doc", inputs={"document_number": "OK"}, expected_decision="VERIFIED"),
        ]
        report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.PASSED)
        self.assertEqual(len(report.attempts), 1)
        self.assertEqual(report.attempts[0].outcome, AttemptOutcome.PASSED)

    # -- failing scenario ------------------------------------------------

    def test_required_case_exhausts_attempts(self):
        method = _make_method()
        # Always returns REJECTED, but test expects VERIFIED
        runner = _mock_runner("REJECTED")
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        test_cases = [
            TestCase(name="valid_doc", inputs={"document_number": "OK"}, expected_decision="VERIFIED"),
        ]
        with patch("utils.llm_client.generate_json", return_value={"execution_steps": [{"action": "mock_improved"}]}):
            report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.FAILED)
        # Should have MAX_ATTEMPTS_PER_TEST_CASE attempts recorded
        self.assertEqual(len(report.attempts), MAX_ATTEMPTS_PER_TEST_CASE)
        for attempt in report.attempts:
            self.assertEqual(attempt.outcome, AttemptOutcome.FAILED)

    # -- non-required case doesn't block pass ----------------------------

    def test_non_required_failure_does_not_block_pass(self):
        method = _make_method()

        # First call VERIFIED (required), second call REJECTED (non-required expected VERIFIED)
        runner = MagicMock()
        runner.execute_method.side_effect = [
            ExecutionResult(decision_status=ExecutionDecisionStatus.VERIFIED, evidence={}, logs=""),
        ] + [ExecutionResult(decision_status=ExecutionDecisionStatus.REJECTED, evidence={}, logs="")] * 20
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        test_cases = [
            TestCase(name="required", inputs={"document_number": "OK"}, expected_decision="VERIFIED", is_required=True),
            TestCase(name="optional", inputs={"document_number": "X"}, expected_decision="VERIFIED", is_required=False),
        ]
        with patch("utils.llm_client.generate_json", return_value={"execution_steps": [{"action": "mock_improved"}]}):
            report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.PASSED)

    # -- technical failure -----------------------------------------------

    def test_technical_failure_recorded_as_error(self):
        method = _make_method()
        runner = _mock_runner("TECHNICAL_FAILURE", error="Container crashed")
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        test_cases = [
            TestCase(name="valid_doc", inputs={"document_number": "OK"}, expected_decision="VERIFIED"),
        ]
        with patch("utils.llm_client.generate_json", return_value={"execution_steps": [{"action": "mock_improved"}]}):
            report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.FAILED)
        for attempt in report.attempts:
            self.assertEqual(attempt.outcome, AttemptOutcome.ERROR)

    # -- no test cases guard ---------------------------------------------

    def test_no_test_cases_returns_error(self):
        method = _make_method()
        runner = _mock_runner("VERIFIED")
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        report = validator.validate(method, [])

        self.assertEqual(report.status, ValidationReportStatus.ERROR)
        self.assertIn("No test cases", report.failure_reason)

    # -- registry integration: status updated on pass --------------------

    def test_registry_updated_to_active_on_pass(self):
        method = _make_method("M_REG_TEST")
        runner = _mock_runner("VERIFIED")

        db_path = "test_validator_registry.db"
        registry = MethodRegistry(db_path=db_path)
        registry.register_method(method)

        try:
            validator = MethodValidator(runner=runner, registry=registry, executor_script_path="tests/dummy_executor.py")
            test_cases = [
                TestCase(name="valid_doc", inputs={"document_number": "OK"}, expected_decision="VERIFIED"),
            ]
            report = validator.validate(method, test_cases)

            self.assertEqual(report.status, ValidationReportStatus.PASSED)
            updated = registry.get_method("M_REG_TEST")
            self.assertEqual(updated.status, MethodStatus.ACTIVE)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # -- improvement ladder runs on failure ------------------------------

    def test_improvement_note_recorded_on_failure(self):
        """A failing non-signature attempt runs the healing ladder.

        Attempt 1 → cheap direct LLM rewrite (test-before-adopt: with a
        runner that always returns REJECTED, the rewrite's probe also fails,
        so the rewrite is NOT adopted). Attempt 2 → the agentic escalation
        (disabled via DVS_AGENTIC_HEALING=0 in tests).
        """
        method = _make_method()
        runner = _mock_runner("REJECTED")
        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")

        test_cases = [
            TestCase(name="valid_doc", inputs={"document_number": "OK"}, expected_decision="VERIFIED"),
        ]
        
        with patch("utils.llm_client.generate_json", return_value={"execution_steps": [{"action": "mock_improved"}]}):
            report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.FAILED)
        self.assertLessEqual(len(report.attempts), MAX_ATTEMPTS_PER_TEST_CASE)
        # Attempt 1: the direct rewrite ran but its probe failed the same way.
        self.assertIsNotNone(report.attempts[0].improvement_applied)
        self.assertIn("[LLM Improvement Rejected]", report.attempts[0].improvement_applied)
        # Any later attempt records its (disabled-in-tests) escalation note.
        for attempt in report.attempts[1:-1]:
            self.assertIsNotNone(attempt.improvement_applied)

    # -- not-found signature capture --------------------------------------

    def test_not_found_signature_captured_and_retest_passes(self):
        """A 4xx-with-body refusal of the known-fake probe becomes a confirmed
        not-found signature; the method is retested and passes without any
        LLM involvement.
        """
        method = _make_method()
        method.expected_responses = {"comparison_mode": "field_match"}
        method.execution_steps = [{"action": "GET", "url": "https://x.example/verify"}]

        runner = MagicMock()

        def executor_simulation(request, *args, **kwargs):
            """Mimic the real executor: without a matching declared signature
            the site's 400 refusal is TECHNICAL_FAILURE; once the method
            carries the captured signature it classifies REJECTED."""
            raw = '{"message":"Invalid Input: Application ID not found."}'
            sigs = (request.method.expected_responses or {}).get("not_found_signatures") or []
            if any(
                s.get("status") == 400 and "application id not found" in str(s.get("contains", "")).lower()
                for s in sigs if isinstance(s, dict)
            ):
                return ExecutionResult(
                    decision_status=ExecutionDecisionStatus.REJECTED,
                    evidence={"http_status": 400, "not_found_signature": True},
                    raw_response=raw,
                )
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={"http_status": 400},
                raw_response=raw,
            )

        runner.execute_method.side_effect = executor_simulation
        validator = MethodValidator(
            runner=runner,
            registry=MethodRegistry(db_path="test_validator_sig.db"),
            executor_script_path="tests/dummy_executor.py",
        )
        # The real compare_response classifies: TECHNICAL_FAILURE passes
        # through untouched; the retest's response has no near-match for the
        # fake input, so the executor's REJECTED survives comparison.
        report = validator.validate(
            method,
            [TestCase(name="structural_check", inputs={"document_number": "TEST_STRUCTURAL_001"}, expected_decision="REJECTED", is_required=True)],
        )
        self.assertEqual(report.status, ValidationReportStatus.PASSED)
        # The capture attempt `continue`s unrecorded; the only recorded
        # attempt is the retest, which passed via the captured signature.
        self.assertEqual(len(report.attempts), 1)
        self.assertEqual(report.attempts[0].outcome, AttemptOutcome.PASSED)
        sigs = method.expected_responses["not_found_signatures"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["status"], 400)
        self.assertIn("Application ID not found", sigs[0]["contains"])


if __name__ == "__main__":
    unittest.main()
