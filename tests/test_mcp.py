import json
import unittest
from unittest.mock import MagicMock, patch

from mcp_server.server import (
    list_active_methods, validate_document, upsert_method, server,
    _validate_method_definition,
)
from execution.models import ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_method(method_id: str = "M_TEST") -> ValidationMethod:
    return ValidationMethod(
        method_id=method_id,
        method_type=MethodType.HTTP,
        source_url="https://mock.example.com",
        country="India",
        document_type="CDC",
        required_inputs=["document_number"],
        status=MethodStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMCPServer(unittest.TestCase):

    def test_tools_registered(self):
        tools = server._tool_manager.list_tools()
        self.assertEqual(len(tools), 6)
        tool_names = [t.name for t in tools]
        self.assertIn("list_active_methods", tool_names)
        self.assertIn("validate_document", tool_names)
        self.assertIn("process_document", tool_names)
        self.assertIn("upsert_method", tool_names)
        self.assertIn("delete_method", tool_names)
        self.assertIn("get_method", tool_names)

    @patch("mcp_server.server.registry")
    def test_list_active_methods_tool(self, mock_registry):
        mock_registry.find_methods.return_value = [_active_method()]
        
        result = list_active_methods()
        
        data = json.loads(result)
        self.assertIn("active_methods", data)
        self.assertEqual(len(data["active_methods"]), 1)
        self.assertEqual(data["active_methods"][0]["method_id"], "M_TEST")
        self.assertEqual(data["active_methods"][0]["required_inputs"], ["document_number"])

    @patch("mcp_server.server.registry")
    @patch("mcp_server.server.runner")
    @patch("mcp_server.server.engine")
    def test_validate_document_tool_success(self, mock_engine, mock_runner, mock_registry):
        method = _active_method()
        mock_registry.get_method.return_value = method
        
        mock_exec_result = MagicMock()
        mock_runner.execute_method.return_value = mock_exec_result
        
        mock_decision = MagicMock()
        mock_decision.model_dump.return_value = {"decision_status": "VERIFIED"}
        mock_engine._build_decision.return_value = mock_decision
        
        result = validate_document("M_TEST", {"document_number": "123"})
        
        data = json.loads(result)
        self.assertEqual(data["decision_status"], "VERIFIED")

    @patch("mcp_server.server.registry")
    def test_validate_document_missing_inputs(self, mock_registry):
        method = _active_method()
        mock_registry.get_method.return_value = method
        
        # Missing 'document_number'
        result = validate_document("M_TEST", {})
        
        data = json.loads(result)
        self.assertIn("error", data)
        self.assertIn("Missing required inputs", data["error"])


# ---------------------------------------------------------------------------
# Test-before-trust gate (September 2026 incident: three untested LLM-authored
# upserts went straight to ACTIVE; one reached a live government server)
# ---------------------------------------------------------------------------

def _http_method(method_id="M_NEW", status=MethodStatus.TESTING, **kw):
    defaults = dict(
        method_id=method_id,
        method_type=MethodType.HTTP,
        source_url="https://example.com/verify",
        required_inputs=["document_number"],
        execution_steps=[
            {"action": "REQUEST", "method": "POST", "url": "https://example.com/verify",
             "params": {"No": "{{document_number}}"}},
        ],
        status=status,
    )
    defaults.update(kw)
    return ValidationMethod(**defaults)


class TestMethodDefinitionValidation(unittest.TestCase):
    def test_rejects_empty_steps(self):
        problems = _validate_method_definition(_http_method(execution_steps=[]))
        self.assertTrue(any("execution_steps is empty" in p for p in problems))

    def test_rejects_http_method_without_executable_step(self):
        problems = _validate_method_definition(
            _http_method(execution_steps=[{"action": "FILL", "field": "a", "value": "b"}])
        )
        self.assertTrue(any("No executable HTTP step" in p for p in problems))

    def test_rejects_unsatisfiable_placeholder(self):
        problems = _validate_method_definition(
            _http_method(
                required_inputs=["document_number"],
                execution_steps=[
                    {"action": "REQUEST", "method": "POST",
                     "url": "https://example.com/v", "params": {"No": "{{captcha_solution}}"}},
                ],
            )
        )
        self.assertTrue(any("captcha_solution" in p for p in problems))

    def test_accepts_wellformed_method(self):
        self.assertEqual(_validate_method_definition(_http_method()), [])

    def test_rejects_identity_field_marked_contact_only(self):
        """An agent must never be able to launder an identity field into a
        synthesizable contact-only one — a fake passport could flip a real
        verdict. (The gate is pointed away from the project's REAL probe
        evidence file, whose COSMETIC verdict for passport_number is
        legitimately vouched — see probe_evidence/.)"""
        import tempfile, os as _os
        from unittest.mock import patch
        import mcp_server.server as srv
        with tempfile.TemporaryDirectory() as td:
            with patch.object(
                srv, "PASSPORT_PROBE_EVIDENCE", _os.path.join(td, "none.json")
            ):
                problems = _validate_method_definition(
                    _http_method(
                        required_inputs=["document_number", "passport_number"],
                        expected_responses={"contact_only_inputs": ["passport_number"]},
                    )
                )
        self.assertTrue(any("identity field" in p for p in problems))

    def test_rejects_contact_only_entry_not_in_required_inputs(self):
        problems = _validate_method_definition(
            _http_method(
                required_inputs=["document_number"],
                expected_responses={"contact_only_inputs": ["reply_email"]},
            )
        )
        self.assertTrue(any("not in required_inputs" in p for p in problems))


class TestUpsertPromotionGate(unittest.TestCase):
    @patch("mcp_server.server.registry")
    def test_new_method_claiming_active_is_stored_testing(self, mock_registry):
        mock_registry.get_method.return_value = None
        method = _http_method(status=MethodStatus.ACTIVE)
        result = upsert_method(method.model_dump_json())
        data = json.loads(result)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["stored_status"], MethodStatus.TESTING.value)
        stored = mock_registry.register_method.call_args[0][0]
        self.assertEqual(stored.status, MethodStatus.TESTING)

    @patch("mcp_server.server.registry")
    def test_structurally_invalid_method_not_stored(self, mock_registry):
        method = _http_method(execution_steps=[])
        result = upsert_method(method.model_dump_json())
        data = json.loads(result)
        self.assertIn("error", data)
        mock_registry.register_method.assert_not_called()

    @patch("mcp_server.server.registry")
    def test_changed_content_without_version_bump_is_rejected(self, mock_registry):
        existing = _http_method(method_id="M_V", status=MethodStatus.ACTIVE, version=3)
        mock_registry.get_method.return_value = existing
        changed = _http_method(
            method_id="M_V", status=MethodStatus.ACTIVE, version=3,
            execution_steps=[
                {"action": "REQUEST", "method": "GET", "url": "https://example.com/other"},
            ],
        )
        result = upsert_method(changed.model_dump_json())
        data = json.loads(result)
        self.assertIn("error", data)
        self.assertIn("version stayed at", data["error"])
        self.assertIn("Nothing was stored", data["error"])
        mock_registry.register_method.assert_not_called()

    @patch("mcp_server.server.registry")
    def test_unchanged_content_keeps_active(self, mock_registry):
        existing = _http_method(method_id="M_SAME", status=MethodStatus.ACTIVE)
        mock_registry.get_method.return_value = existing
        result = upsert_method(existing.model_dump_json())
        data = json.loads(result)
        self.assertEqual(data["stored_status"], MethodStatus.ACTIVE.value)


class TestValidateDocumentPromotion(unittest.TestCase):
    def _testing_method(self):
        return _http_method(status=MethodStatus.TESTING)

    @patch("mcp_server.server.registry")
    @patch("mcp_server.server.runner")
    @patch("mcp_server.server.engine")
    def test_clean_execution_promotes_testing_to_active(
        self, mock_engine, mock_runner, mock_registry
    ):
        method = self._testing_method()
        mock_registry.get_method.return_value = method

        exec_result = MagicMock()
        exec_result.decision_status = ExecutionDecisionStatus.VERIFIED
        mock_runner.execute_method.return_value = exec_result

        mock_decision = MagicMock()
        mock_decision.model_dump.return_value = {"decision_status": "VERIFIED"}
        mock_engine._build_decision.return_value = mock_decision

        result = validate_document("M_NEW", {"document_number": "DOC12345"})
        data = json.loads(result)

        self.assertTrue(data["promoted_to_active"])
        self.assertEqual(data["method_status"], MethodStatus.ACTIVE.value)
        mock_registry.update_status.assert_called_once_with("M_NEW", MethodStatus.ACTIVE)

    @patch("mcp_server.server.registry")
    @patch("mcp_server.server.runner")
    @patch("mcp_server.server.engine")
    def test_technical_failure_does_not_promote(
        self, mock_engine, mock_runner, mock_registry
    ):
        method = self._testing_method()
        mock_registry.get_method.return_value = method

        exec_result = MagicMock()
        exec_result.decision_status = ExecutionDecisionStatus.TECHNICAL_FAILURE
        mock_runner.execute_method.return_value = exec_result

        mock_decision = MagicMock()
        mock_decision.model_dump.return_value = {"decision_status": "TECHNICAL_FAILURE"}
        mock_engine._build_decision.return_value = mock_decision

        result = validate_document("M_NEW", {"document_number": "DOC12345"})
        data = json.loads(result)

        self.assertFalse(data["promoted_to_active"])
        self.assertEqual(data["method_status"], MethodStatus.TESTING.value)
        mock_registry.update_status.assert_not_called()

    @patch("mcp_server.server.registry")
    @patch("mcp_server.server.runner")
    @patch("mcp_server.server.engine")
    def test_already_active_method_not_repromoted(
        self, mock_engine, mock_runner, mock_registry
    ):
        method = _http_method(status=MethodStatus.ACTIVE)
        mock_registry.get_method.return_value = method

        exec_result = MagicMock()
        exec_result.decision_status = ExecutionDecisionStatus.UNCERTAIN
        mock_runner.execute_method.return_value = exec_result

        mock_decision = MagicMock()
        mock_decision.model_dump.return_value = {"decision_status": "UNCERTAIN"}
        mock_engine._build_decision.return_value = mock_decision

        result = validate_document("M_NEW", {"document_number": "DOC12345"})
        data = json.loads(result)

        self.assertFalse(data["promoted_to_active"])
        mock_registry.update_status.assert_not_called()

if __name__ == "__main__":
    unittest.main()
