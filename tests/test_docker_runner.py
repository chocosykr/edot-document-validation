import unittest
import os
import shutil
from execution.docker_runner import (
    DockerMethodRunner,
    _method_timeout,
    CAPTCHA_HTTP_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus

class TestDockerMethodRunner(unittest.TestCase):
    def setUp(self):
        # We need a dummy method
        self.method = ValidationMethod(
            method_id="M_TEST_DOCKER",
            method_type=MethodType.HTTP,
            source_url="https://mock",
            required_inputs=["document_number"]
        )
        self.dummy_script_path = os.path.join(os.path.dirname(__file__), "dummy_executor.py")
        
        # Test if docker is available, otherwise skip tests?
        # Let's assume docker is available since it's a hard requirement.
        self.runner = DockerMethodRunner(docker_image="python:3.12-alpine", timeout_seconds=5)

    def test_successful_execution(self):
        req = ExecutionRequest(
            method=self.method,
            inputs={"document_number": "VALID123"}
        )
        result = self.runner.execute_method(req, self.dummy_script_path)
        
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertTrue(result.evidence.get("valid"))
        self.assertIn("Execution completed successfully.", result.logs)
        self.assertIsNone(result.error)

    def test_rejected_execution(self):
        req = ExecutionRequest(
            method=self.method,
            inputs={"document_number": "INVALID999"}
        )
        result = self.runner.execute_method(req, self.dummy_script_path)
        
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertFalse(result.evidence.get("valid"))
        self.assertIsNone(result.error)

    def test_timeout_execution(self):
        # We set runner timeout to 2 seconds for this test to speed it up
        runner = DockerMethodRunner(docker_image="python:3.12-alpine", timeout_seconds=2)
        req = ExecutionRequest(
            method=self.method,
            inputs={"document_number": "VALID123", "trigger_timeout": "true"}
        )
        result = runner.execute_method(req, self.dummy_script_path)
        
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("timed out", result.error)


class TestMethodTimeouts(unittest.TestCase):
    """Captcha-bearing HTTP methods need a wider container budget than plain
    HTTP: FETCH_CAPTCHA -> SOLVE_CAPTCHA (remote vision-LLM) runs before the
    lookup request (2026-09-29: dgshippingbsid.in timed out at 30s while the
    site itself answered in 0.2s)."""

    def test_plain_http_keeps_default_budget(self):
        method = ValidationMethod(
            method_id="M_T", method_type=MethodType.HTTP, source_url="https://x",
            execution_steps=[{"action": "REQUEST", "method": "GET", "url": "https://x"}],
        )
        self.assertEqual(_method_timeout(method), DEFAULT_TIMEOUT_SECONDS)

    def test_captcha_http_gets_widened_budget(self):
        method = ValidationMethod(
            method_id="M_T", method_type=MethodType.HTTP, source_url="https://x",
            execution_steps=[
                {"action": "FETCH_CAPTCHA", "url": "https://x/captcha"},
                {"action": "SOLVE_CAPTCHA"},
                {"action": "REQUEST", "method": "GET", "url": "https://x/v"},
            ],
        )
        self.assertEqual(_method_timeout(method), CAPTCHA_HTTP_TIMEOUT_SECONDS)
        self.assertGreater(CAPTCHA_HTTP_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)

    def test_browser_budget_untouched(self):
        method = ValidationMethod(
            method_id="M_T", method_type=MethodType.BROWSER, source_url="https://x",
            execution_steps=[{"action": "SOLVE_CAPTCHA"}],
        )
        self.assertEqual(_method_timeout(method), 120)


if __name__ == "__main__":
    unittest.main()
