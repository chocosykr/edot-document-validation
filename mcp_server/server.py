import asyncio
import json
import logging
import sys
from typing import Optional, Dict, Any

from mcp.server.mcpserver import MCPServer
from registry.repository import MethodRegistry
from registry.models import MethodStatus
from engine.validation_engine import ValidationEngine
from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest

# Setup logging to stderr so it doesn't corrupt stdout (which is used by MCP)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp_server")

# Initialize core components
registry = MethodRegistry()
runner = DockerMethodRunner()
engine = ValidationEngine(registry=registry, runner=runner)

# Create MCP server instance
server = MCPServer("document-validation-mcp")

@server.tool()
def list_active_methods(country: Optional[str] = None, document_type: Optional[str] = None) -> str:
    """
    List all ACTIVE validation methods available in the registry. You can filter by country and document_type.
    """
    try:
        methods = registry.find_methods(country=country, document_type=document_type)
        active_methods = [m for m in methods if m.status == MethodStatus.ACTIVE]
        
        result = []
        for m in active_methods:
            result.append({
                "method_id": m.method_id,
                "country": m.country,
                "document_type": m.document_type,
                "method_type": m.method_type.value,
                "required_inputs": m.required_inputs,
                "limitations": m.limitations
            })
            
        return json.dumps({"active_methods": result}, indent=2)
        
    except Exception as e:
        logger.error(f"Error listing methods: {e}")
        return json.dumps({"error": str(e)})


@server.tool()
def validate_document(method_id: str, inputs: Dict[str, Any]) -> str:
    """
    Execute a specific validation method by its method_id with the required inputs.
    """
    if not method_id:
        return json.dumps({"error": "method_id is required"})
        
    try:
        method = registry.get_method(method_id)
        if not method:
            return json.dumps({"error": f"Method {method_id} not found."})
            
        if method.status != MethodStatus.ACTIVE:
            return json.dumps({
                "error": f"Method {method_id} is not ACTIVE (current status: {method.status.value})."
            })
            
        # Verify required inputs
        missing = [k for k in method.required_inputs if k not in inputs]
        if missing:
            return json.dumps({
                "error": f"Missing required inputs: {missing}"
            })
            
        # Execute
        req = ExecutionRequest(method=method, inputs=inputs)
        exec_result = runner.execute_method(req)
        
        # Wrap in decision payload so it matches the engine output structure
        decision = engine._build_decision(exec_result, method)
        
        return json.dumps(decision.model_dump(), indent=2)
        
    except Exception as e:
        logger.error(f"Error executing method {method_id}: {e}")
        return json.dumps({"error": str(e)})


def main():
    """
    Run the MCP server over standard input/output.
    """
    logger.info("Starting DVS MCP Server over stdio...")
    server.run("stdio")


if __name__ == "__main__":
    main()
