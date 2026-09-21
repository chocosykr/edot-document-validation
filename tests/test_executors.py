"""
Tests for Stage 6 — Website and API Method Implementations.

Unit tests:
  - executor auto-selection by MethodType
  - MANUAL method returns VALIDATION_UNAVAILABLE immediately
  - BROWSER stub returns VALIDATION_UNAVAILABLE

Integration test (real Docker, real HTTP):
  - HTTP executor against httpbin.org/get (public echo API)
  - HTTP executor: keyword-based VERIFIED decision
  - HTTP executor: failure keyword → REJECTED
  - QR/URL executor following a redirect (httpbin redirect)
  - WEB_FORM executor against httpbin form endpoint
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from execution.docker_runner import DockerMethodRunner, _EXECUTOR_MAP
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _method(method_type: MethodType, source_url: str = "https://httpbin.org/get",
            execution_steps=None, expected_responses=None) -> ValidationMethod:
    return ValidationMethod(
        method_id=f"M_{method_type.value}",
        method_type=method_type,
        source_url=source_url,
        execution_steps=execution_steps or [],
        expected_responses=expected_responses or {},
    )


# ---------------------------------------------------------------------------
# Unit tests (no real Docker)
# ---------------------------------------------------------------------------

class TestExecutorSelection(unittest.TestCase):

    def test_manual_returns_unavailable_immediately(self):
        runner = DockerMethodRunner()
        method = _method(MethodType.MANUAL)
        req = ExecutionRequest(method=method, inputs={})

        # Patch _run_in_docker to ensure it's never called
        runner._run_in_docker = MagicMock()
        result = runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VALIDATION_UNAVAILABLE)
        runner._run_in_docker.assert_not_called()

    def test_executor_map_covers_all_method_types(self):
        """Every MethodType must have an entry in _EXECUTOR_MAP."""
        for mt in MethodType:
            self.assertIn(mt, _EXECUTOR_MAP, f"MethodType.{mt} missing from _EXECUTOR_MAP")

    def test_missing_executor_script_returns_failure(self):
        runner = DockerMethodRunner()
        method = _method(MethodType.HTTP)
        req = ExecutionRequest(method=method, inputs={})
        # Override with a non-existent path
        result = runner.execute_method(req, executor_script_path="/nonexistent/path.py")
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("not found", result.error)


# ---------------------------------------------------------------------------
# Integration tests (real Docker + real internet)
# ---------------------------------------------------------------------------

class TestHTTPExecutorIntegration(unittest.TestCase):
    """These tests make real HTTP calls inside Docker."""

    def setUp(self):
        self.runner = DockerMethodRunner(timeout_seconds=30)

    def test_http_get_verified_by_keyword(self):
        """httpbin.org/get echoes the request — 'url' key always present."""
        method = _method(
            MethodType.HTTP,
            source_url="https://httpbin.org/get",
            execution_steps=[{"action": "GET", "url": "https://httpbin.org/get", "params": {}}],
            expected_responses={"success_keywords": ["httpbin"]},
        )
        req = ExecutionRequest(method=method, inputs={})
        result = self.runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED,
                         msg=f"Logs: {result.logs}\nError: {result.error}")
        self.assertIn("http_status", result.evidence)

    def test_http_get_rejected_by_keyword(self):
        """httpbin.org/get response does NOT contain 'INVALID_DOC', so we use
        a failure keyword that IS present ('httpbin') to simulate REJECTED."""
        method = _method(
            MethodType.HTTP,
            source_url="https://httpbin.org/get",
            execution_steps=[{"action": "GET", "url": "https://httpbin.org/get", "params": {}}],
            expected_responses={
                "failure_keywords": ["httpbin"],   # will match → REJECTED
                "success_keywords": [],
            },
        )
        req = ExecutionRequest(method=method, inputs={})
        result = self.runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED,
                         msg=f"Logs: {result.logs}")

    def test_http_get_with_variable_substitution(self):
        """Ensure {{variable}} placeholders are resolved before the request."""
        method = _method(
            MethodType.HTTP,
            source_url="https://httpbin.org/get",
            execution_steps=[{
                "action": "GET",
                "url": "https://httpbin.org/get",
                "params": {"doc": "{{document_number}}"}
            }],
            expected_responses={"success_keywords": ["ABC123"]},
        )
        req = ExecutionRequest(method=method, inputs={"document_number": "ABC123"})
        result = self.runner.execute_method(req)

        # httpbin echoes query params in the response
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED,
                         msg=f"Logs: {result.logs}")

    def test_http_post_query_string_params(self):
        """param_location: 'query' must produce a real POST with params on
        the query string and an empty body — the confirmed send(null) wire
        format. httpbin.org/post echoes the verb, query args, and body."""
        method = _method(
            MethodType.HTTP,
            source_url="https://httpbin.org/post",
            execution_steps=[{
                "action": "REQUEST",
                "method": "POST",
                "url": "https://httpbin.org/post",
                "params": {"doc": "{{document_number}}"},
                "param_location": "query",
            }],
            expected_responses={"success_keywords": ["ABC123"]},
        )
        req = ExecutionRequest(method=method, inputs={"document_number": "ABC123"})
        result = self.runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED,
                         msg=f"Logs: {result.logs}")

        # Verify the exact wire format from the echo:
        # - params arrived as query args (send(null) pattern)
        # - body is empty and form fields are empty (not form-encoded)
        echo = json.loads(result.raw_response)
        self.assertEqual(echo["args"], {"doc": "ABC123"})
        self.assertEqual(echo["data"], "")
        self.assertEqual(echo["form"], {})

    def test_qr_url_executor_follow_url(self):
        """QR/URL executor: follow a URL and check for keyword."""
        method = _method(
            MethodType.QR_URL,
            source_url="https://httpbin.org/get",
            execution_steps=[{"action": "FOLLOW_URL", "url": "https://httpbin.org/get"}],
            expected_responses={"success_keywords": ["httpbin"]},
        )
        req = ExecutionRequest(method=method, inputs={})
        result = self.runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED,
                         msg=f"Logs: {result.logs}")
        self.assertEqual(result.evidence.get("method_type"), "QR_URL")

    def test_browser_executor_returns_unavailable(self):
        """Browser stub should return VALIDATION_UNAVAILABLE."""
        method = _method(MethodType.BROWSER, source_url="https://example.com")
        req = ExecutionRequest(method=method, inputs={})
        result = self.runner.execute_method(req)

        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VALIDATION_UNAVAILABLE,
                         msg=f"Logs: {result.logs}")


if __name__ == "__main__":
    unittest.main()
