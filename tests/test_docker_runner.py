import unittest
import os
import shutil
from execution.docker_runner import DockerMethodRunner
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

if __name__ == "__main__":
    unittest.main()
