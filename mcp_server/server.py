import asyncio
import json
import logging
import os
import sys
from typing import Optional, Dict, Any

# Ensure project root is in Python path so direct execution works
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mcp.server.fastmcp import FastMCP
from registry.repository import MethodRegistry
from registry.models import ValidationMethod, MethodStatus, MethodType
from engine.validation_engine import ValidationEngine
from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from ocr.client import extract_document
from ocr.extractor import extract_and_redact, split_credentials

# Setup logging to stderr so it doesn't corrupt stdout (which is used by MCP)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp_server")

# Evidence file written by engine/probe_passport.py: the only accepted proof
# that a live-verified field is cosmetic (safe to synthesize) rather than an
# identity field the portal validates. See _probe_vouches_cosmetic.
PASSPORT_PROBE_EVIDENCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "probe_evidence", "passport_probe_result.json",
)

# Initialize core components
registry = MethodRegistry()
runner = DockerMethodRunner()
engine = ValidationEngine(registry=registry, runner=runner)

# HTTP executor step actions that actually perform or prepare network I/O.
# (GET_HTML and EXTRACT_FROM_HTML are implemented in the executor since the
# September 2026 Myanmar method needed an anti-forgery token flow.)
_KNOWN_HTTP_ACTIONS = {
    "REQUEST", "GET_HTML", "EXTRACT_FROM_HTML",
    "FETCH_CAPTCHA", "SOLVE_CAPTCHA",
}


def _probe_vouches_cosmetic(field_name: str) -> bool:
    """Does the live passport probe evidence vouch that this field is cosmetic?

    ``engine/probe_passport.py`` writes ``probe_evidence/passport_probe_result.json``
    after submitting obviously-fake vs real values to the live portal and
    comparing the responses structurally. Only a COSMETIC verdict opens this
    gate — a VALIDATED or INCONCLUSIVE verdict keeps identity fields blocked.
    The evidence file may carry ``vouches_for: ["field_name", ...]`` to
    restrict which fields it covers; without it, the verdict covers the
    probed portal's field(s) generally.
    """
    try:
        with open(PASSPORT_PROBE_EVIDENCE, "r", encoding="utf-8") as f:
            evidence = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if evidence.get("verdict") != "COSMETIC":
        return False
    # Fail-closed: the evidence must explicitly name the field(s) it vouches
    # for. A verdict without a scope vouches for NOTHING — otherwise one
    # portal's probe evidence would unlock every identity-shaped field.
    vouches = evidence.get("vouches_for")
    if not isinstance(vouches, list):
        return False
    return str(field_name) in {str(v) for v in vouches}


def _validate_method_definition(method: ValidationMethod) -> list:
    """Structural checks a method must pass before it can be stored.

    The agentic fallback loop used to upsert arbitrary LLM-authored method
    bodies straight to ACTIVE (three consecutive untested guesses in the
    September 2026 incident, one of them tagged with another country's
    document_type_key). These checks catch the malformed shapes that caused
    it; they are intentionally dumb and deterministic — judgment is the
    live test's job, not this function's.
    """
    problems = []
    if not method.source_url:
        problems.append("source_url is required.")

    steps = method.execution_steps or []
    if not steps:
        problems.append("execution_steps is empty — nothing to execute.")
        return problems

    actions = [
        str(s.get("action", "")).upper()
        for s in steps if isinstance(s, dict)
    ]

    if method.method_type == MethodType.HTTP:
        if not any(a in _KNOWN_HTTP_ACTIONS for a in actions):
            problems.append(
                f"No executable HTTP step found (actions present: {actions}). "
                "HTTP methods need at least a REQUEST step."
            )
        for idx, s in enumerate(steps):
            if not isinstance(s, dict):
                continue
            if str(s.get("action", "")).upper() == "REQUEST" and not s.get("url"):
                problems.append(f"execution_steps[{idx}]: REQUEST step has no url.")

    elif method.method_type == MethodType.WEB_FORM:
        if "SUBMIT" not in actions:
            problems.append(
                f"WEB_FORM methods need a SUBMIT step (actions present: {actions})."
            )

    # Every {{placeholder}} used in execution_steps must be either a declared
    # required_input or produced by an earlier step (output_var).
    import re as _re
    produced = {
        str(s.get("output_var")) for s in steps
        if isinstance(s, dict) and s.get("output_var")
    }
    declared = set(method.required_inputs or [])
    for idx, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        blob = json.dumps(s)
        for name in _re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", blob):
            if name not in declared and name not in produced:
                problems.append(
                    f"execution_steps[{idx}] uses {{{{{name}}}}} which is neither "
                    "a required_input nor an earlier step's output_var."
                )

    # contact_only_inputs entries must actually be required inputs — a
    # declaration for a non-required field is a configuration error (it would
    # silently exempt nothing, or mask a typo in required_inputs).
    contact = (method.expected_responses or {}).get("contact_only_inputs")
    if isinstance(contact, list):
        for name in contact:
            if str(name) not in declared:
                problems.append(
                    f"contact_only_inputs lists {name!r} which is not in "
                    "required_inputs."
                )
            # An agent must never be able to launder an identity field into a
            # synthesizable one: a fake passport/serial/name could produce a
            # false verdict. Identity-shaped names are rejected outright —
            # UNLESS the live probe evidence vouches the field is cosmetic.
            lowered = str(name).lower()
            if any(m in lowered for m in (
                "passport", "document", "cdc", "serial", "dob",
                "birth", "name", "indos",
            )) and not _probe_vouches_cosmetic(str(name)):
                problems.append(
                    f"contact_only_inputs lists {name!r} which looks like an "
                    "identity field — identity fields must never be declared "
                    "synthesizable (live-probe evidence could override this; "
                    "see engine/probe_passport.py)."
                )

    return problems


def _method_content(m: ValidationMethod) -> tuple:
    """The execution-relevant parts of a method, for change detection."""
    return (
        m.method_type.value,
        json.dumps(m.required_inputs, sort_keys=True),
        json.dumps(m.execution_steps, sort_keys=True),
        json.dumps(m.expected_responses, sort_keys=True),
    )

# Create MCP server instance
server = FastMCP("document-validation-mcp")

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

    This tool IS the sandboxed live test for methods in TESTING status: the
    execution layer refuses to submit redaction tokens, placeholder values,
    or incomplete data to the live endpoint (the refusal is returned as a
    TECHNICAL_FAILURE and never reaches the registry). A TESTING method that
    executes cleanly is promoted to ACTIVE by this tool automatically; a
    failed one stays TESTING and must be fixed via upsert_method.
    """
    if not method_id:
        return json.dumps({"error": "method_id is required"})
        
    try:
        method = registry.get_method(method_id)
        if not method:
            return json.dumps({"error": f"Method {method_id} not found."})
            
        if method.status not in (MethodStatus.ACTIVE, MethodStatus.TESTING):
            return json.dumps({
                "error": f"Method {method_id} is not executable (current status: {method.status.value})."
            })
            
        # Verify required inputs. Fields declared contact_only_inputs are
        # synthesizable (the runner fills missing ones with inert values), so
        # they do not block execution here — but identity fields do.
        from execution.safety import contact_only_inputs as _contact_only
        contact_fields = _contact_only(method)
        missing = [
            k for k in method.required_inputs
            if k not in inputs and k not in contact_fields
        ]
        if missing:
            return json.dumps({
                "error": f"Missing required inputs: {missing}"
            })
            
        # Execute
        req = ExecutionRequest(method=method, inputs=inputs)
        exec_result = runner.execute_method(req)

        # Wrap in decision payload so it matches the engine output structure
        decision = engine._build_decision(exec_result, method)

        response = decision.model_dump()

        # ---- test-before-trust promotion gate ----
        # A TESTING method that just executed cleanly IS the live test.
        # Promote only then; never on an LLM's self-report. Refusals
        # (TECHNICAL_FAILURE from the input guard) and infra failures never
        # promote — the method stays TESTING and the agent must fix it.
        promoted = False
        if (
            method.status == MethodStatus.TESTING
            and exec_result.decision_status not in (
                ExecutionDecisionStatus.TECHNICAL_FAILURE,
                ExecutionDecisionStatus.VALIDATION_UNAVAILABLE,
            )
        ):
            registry.update_status(method.method_id, MethodStatus.ACTIVE)
            promoted = True
            logger.info(
                "Method %s passed a clean live execution via MCP and was "
                "promoted TESTING -> ACTIVE.", method.method_id,
            )

        response["method_status"] = (
            MethodStatus.ACTIVE.value if promoted else method.status.value
        )
        response["promoted_to_active"] = promoted

        return json.dumps(response, indent=2)
        
    except Exception as e:
        logger.error(f"Error executing method {method_id}: {e}")
        return json.dumps({"error": str(e)})


@server.tool()
def process_document(document_path: str) -> str:
    """
    Run the full end-to-end document validation pipeline on a local file.
    Performs OCR, extracts/redacts PII, matches against the method registry,
    executes the live verification (handling CAPTCHAs if needed), and returns
    the final VERIFIED/REJECTED decision along with the extracted profile.
    """
    import os
    if not os.path.exists(document_path):
        return json.dumps({"error": f"File not found: {document_path}"})

    try:
        logger.info(f"Processing document via MCP: {document_path}")
        
        # 1. OCR
        ocr_result = extract_document(document_path)
        
        # 2. Extract & Redact
        extracted = extract_and_redact(ocr_result)
        redacted_profile, credentials = split_credentials(extracted)
        
        def credential_provider():
            return dict(credentials) if credentials else None
            
        # 3. Validation Engine
        val_engine = ValidationEngine(
            registry=registry, 
            runner=runner,
            credential_provider=credential_provider
        )
        decision = val_engine.validate(redacted_profile)
        
        return json.dumps({
            "redacted_profile": redacted_profile,
            "decision": decision.model_dump()
        }, indent=2)
        
    except Exception as e:
        logger.error(f"Error processing document {document_path}: {e}")
        return json.dumps({"error": str(e)})


@server.tool()
def get_method(method_id: str) -> str:
    """
    Get the full JSON definition of a validation method (including execution_steps).
    """
    if not method_id:
        return json.dumps({"error": "method_id is required"})
    try:
        method = registry.get_method(method_id)
        if not method:
            return json.dumps({"error": f"Method {method_id} not found."})
        return method.model_dump_json(indent=2)
    except Exception as e:
        logger.error(f"Error getting method {method_id}: {e}")
        return json.dumps({"error": str(e)})


@server.tool()
def upsert_method(method_json: str) -> str:
    """
    Create or update a validation method in the registry.
    Input must be a JSON string matching the ValidationMethod schema.

    TEST-BEFORE-TRUST: a method whose execution content is new or changed is
    stored with status TESTING, never ACTIVE — regardless of what the caller
    claims. Only a clean live execution via validate_document promotes it to
    ACTIVE. Structurally invalid methods are rejected outright.
    """
    try:
        data = json.loads(method_json)
        method = ValidationMethod(**data)
    except Exception as e:
        logger.error(f"Error parsing method JSON: {e}")
        return json.dumps({"error": f"Invalid method JSON: {e}"})

    try:
        problems = _validate_method_definition(method)
        if problems:
            logger.warning(
                "Rejected structurally invalid method %s via MCP: %s",
                method.method_id, problems,
            )
            return json.dumps({
                "error": "Method definition rejected — it was not stored.",
                "problems": problems,
            })

        existing = registry.get_method(method.method_id)
        notes = []

        if existing is not None and method.version < existing.version:
            return json.dumps({
                "error": (
                    f"Version regression: existing method is v{existing.version}, "
                    f"submitted v{method.version}. Bump the version."
                )
            })

        content_changed = existing is None or (
            _method_content(method) != _method_content(existing)
        )
        if (
            existing is not None
            and content_changed
            and method.version == existing.version
        ):
            # Changed execution content with the same version number is
            # ambiguous: the caller may not realize they are overwriting a
            # previously validated method. Fail closed — force an explicit
            # version bump so the change is a stated decision.
            return json.dumps({
                "error": (
                    f"Execution content changed but version stayed at "
                    f"v{method.version}. Bump the version and resubmit. "
                    "Nothing was stored."
                )
            })

        claimed_active = method.status == MethodStatus.ACTIVE
        if claimed_active and (
            content_changed
            or existing is None
            or existing.status != MethodStatus.ACTIVE
        ):
            # Untested content must never enter the registry as ACTIVE — this
            # is how the September 2026 incident stored three untested guesses.
            method.status = MethodStatus.TESTING
            notes.append(
                "Status forced to TESTING: execution content is new or changed. "
                "Run validate_document on this method_id to live-test it; a "
                "clean execution promotes it to ACTIVE automatically."
            )

        registry.register_method(method)
        logger.info(
            "Upserted method %s v%d via MCP (status=%s).",
            method.method_id, method.version, method.status.value,
        )
        return json.dumps({
            "status": "success",
            "method_id": method.method_id,
            "version": method.version,
            "stored_status": method.status.value,
            "notes": notes,
        })
    except Exception as e:
        logger.error(f"Error upserting method: {e}")
        return json.dumps({"error": str(e)})


@server.tool()
def delete_method(method_id: str) -> str:
    """
    Delete a validation method from the registry by its method_id.
    """
    if not method_id:
        return json.dumps({"error": "method_id is required"})
    try:
        deleted = registry.delete_method(method_id)
        if deleted:
            logger.info(f"Deleted method {method_id} via MCP.")
            return json.dumps({"status": "deleted", "method_id": method_id})
        else:
            return json.dumps({"error": f"Method {method_id} not found."})
    except Exception as e:
        logger.error(f"Error deleting method {method_id}: {e}")
        return json.dumps({"error": str(e)})


def main():
    """
    Run the MCP server over standard input/output.
    """
    logger.info("Starting DVS MCP Server over stdio...")
    server.run("stdio")


if __name__ == "__main__":
    main()
