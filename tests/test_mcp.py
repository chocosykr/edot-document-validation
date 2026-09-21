import json
import unittest
from unittest.mock import MagicMock, patch

from mcp_server.server import list_active_methods, validate_document, server
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
        self.assertEqual(len(tools), 2)
        tool_names = [t.name for t in tools]
        self.assertIn("list_active_methods", tool_names)
        self.assertIn("validate_document", tool_names)

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

if __name__ == "__main__":
    unittest.main()
