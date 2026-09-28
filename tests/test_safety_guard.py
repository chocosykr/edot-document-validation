"""
Live-submission safety guard (execution/safety.py) and its wiring into the
Docker runner. These tests pin the fix for the September 2026 incident where
the MCP validate_document tool submitted incomplete/placeholder values to the
live dmamyanmar.org endpoint (real HTTP 500).
"""

import unittest

from execution.safety import (
    guard_inputs,
    is_placeholder_value,
    is_credible_document_date,
    credible_credentials,
    UnsafeInputError,
)
from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus


class TestIsPlaceholderValue(unittest.TestCase):
    def test_redaction_tokens_are_placeholders(self):
        for v in ("[PERSON_NAME]", "[DOCUMENT_NUMBER]", "[EMAIL]", "[REDACTED]"):
            self.assertTrue(is_placeholder_value(v), v)

    def test_known_placeholders_are_detected(self):
        for v in ("test", "placeholder", "xxx123", "123456", "N/A", "None"):
            self.assertTrue(is_placeholder_value(v), v)

    def test_repeated_single_char_is_placeholder(self):
        self.assertTrue(is_placeholder_value("xxxx"))
        self.assertTrue(is_placeholder_value("1111"))

    def test_realistic_values_pass(self):
        for v in ("09NL5250", "MA-1234567", "07/09/1992", "24A1234567"):
            self.assertFalse(is_placeholder_value(v), v)

    def test_empty_is_placeholder(self):
        self.assertTrue(is_placeholder_value(""))
        self.assertTrue(is_placeholder_value(None))


class TestCredibleCredentials(unittest.TestCase):
    """OCR-garbage identity values must be dropped before live submission.

    Live case (2026-09-28): the CDC booklet's OCR produced DOB "07-BSF-92"
    (not a date) and "22/11/2020" (after the printed issue date). Both were
    submitted to the live esamudra registry; a genuinely missing credential
    would have been refused or resolved cross-document instead.
    """

    def test_garbled_dob_is_dropped(self):
        creds = credible_credentials({"date_of_birth": "07-BSF-92", "cdc_number": "MUM179416"})
        self.assertNotIn("date_of_birth", creds)
        self.assertEqual(creds["cdc_number"], "MUM179416")

    def test_real_dob_survives(self):
        creds = credible_credentials({"date_of_birth": "07-sep-1992"})
        self.assertEqual(creds["date_of_birth"], "07-sep-1992")

    def test_slash_format_dob_survives(self):
        creds = credible_credentials({"date_of_birth": "07/09/1992"})
        self.assertEqual(creds["date_of_birth"], "07/09/1992")

    def test_dob_after_issue_date_is_dropped(self):
        creds = credible_credentials({
            "date_of_birth": "22/11/2020",
            "issue_date": "22/01/2020",
            "document_number": "MUM179416",
        })
        self.assertNotIn("date_of_birth", creds)
        self.assertEqual(creds["document_number"], "MUM179416")

    def test_dob_before_issue_date_survives(self):
        creds = credible_credentials({
            "date_of_birth": "07/09/1992",
            "issue_date": "22/01/2020",
        })
        self.assertEqual(creds["date_of_birth"], "07/09/1992")

    def test_no_issue_date_means_no_timeline_check(self):
        creds = credible_credentials({"date_of_birth": "22/11/2020"})
        self.assertEqual(creds["date_of_birth"], "22/11/2020")

    def test_garbled_dob_fails_credibility(self):
        self.assertFalse(is_credible_document_date("07-BSF-92"))
        self.assertTrue(is_credible_document_date("07-sep-1992"))
        self.assertTrue(is_credible_document_date("22/11/2020"))

    def test_none_and_empty_are_noops(self):
        self.assertEqual(credible_credentials(None), {})
        self.assertEqual(credible_credentials({"date_of_birth": ""}), {})


class TestGuardInputs(unittest.TestCase):
    def test_refuses_missing_required_inputs(self):
        with self.assertRaises(UnsafeInputError) as ctx:
            guard_inputs({"document_number": "X1234567"}, ["serial_number"])
        self.assertIn("serial_number", str(ctx.exception))

    def test_refuses_redaction_token_in_value(self):
        # required_inputs are satisfied, but a value is a token — this is
        # exactly the shape that got past the engine's presence-only check.
        with self.assertRaises(UnsafeInputError) as ctx:
            guard_inputs(
                {"passport_number": "[PASSPORT_NUMBER]", "email": "a@b.com"},
                ["passport_number", "email"],
            )
        self.assertIn("redaction token", str(ctx.exception))

    def test_refuses_obvious_placeholder(self):
        with self.assertRaises(UnsafeInputError):
            guard_inputs(
                {"passport_number": "placeholder", "email": "a@b.com"},
                ["passport_number", "email"],
            )

    def test_accepts_plausible_complete_inputs(self):
        guard_inputs(
            {"passport_number": "MA1234567", "email": "ops@example.com"},
            ["passport_number", "email"],
        )  # must not raise


class TestRunnerGuardWiring(unittest.TestCase):
    """The runner is the single choke point: refusal = TECHNICAL_FAILURE,
    before any container spin-up or network traffic."""

    def setUp(self):
        self.runner = DockerMethodRunner()

    def _method(self, **kw):
        defaults = dict(
            method_id="M_GUARD_TEST",
            method_type=MethodType.HTTP,
            source_url="https://example.com/verify",
            required_inputs=["passport_number", "email"],
            execution_steps=[{"action": "REQUEST", "method": "POST",
                              "url": "https://example.com/verify"}],
            status=MethodStatus.TESTING,
        )
        defaults.update(kw)
        return ValidationMethod(**defaults)

    def test_incomplete_inputs_refused_without_container(self):
        req = ExecutionRequest(
            method=self._method(),
            inputs={"email": "someone@example.com"},  # passport_number missing
        )
        result = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("Refusing", result.error)
        self.assertIn("refused_reason", result.evidence)

    def test_placeholder_value_refused_without_container(self):
        req = ExecutionRequest(
            method=self._method(),
            inputs={"passport_number": "[PASSPORT_NUMBER]", "email": "someone@example.com"},
        )
        result = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("redaction token", result.error)

    def test_structural_test_values_allowed_only_on_opt_in(self):
        req = ExecutionRequest(
            method=self._method(required_inputs=["document_number"]),
            inputs={"document_number": "TEST_STRUCTURAL_001"},
        )
        # Strict (default): refused.
        r1 = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertEqual(r1.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        # Opted-in (structural test): passes the guard (then fails on the
        # missing executor script — proving it got past the guard).
        r2 = self.runner.execute_method(
            req, executor_script_path="/nonexistent.py", allow_structural_test_values=True
        )
        self.assertEqual(r2.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("Executor script not found", r2.error)

    def test_complete_inputs_reach_executor_stage(self):
        req = ExecutionRequest(
            method=self._method(required_inputs=["passport_number"]),
            inputs={"passport_number": "MA1234567"},
        )
        # /nonexistent.py ensures no container and no network is used; we only
        # assert that the guard let it through to the executor-resolution step.
        result = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertIn("Executor script not found", result.error)


if __name__ == "__main__":
    unittest.main()
