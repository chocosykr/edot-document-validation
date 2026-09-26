import json
import os
import logging
import tempfile
import subprocess
import shutil
from typing import Optional

try:
    from dotenv import load_dotenv
    # Load the project .env so LLM credentials can be forwarded to executor
    # containers regardless of the entry point (main.py, MCP server, scripts).
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    ), override=False)
except ImportError:
    pass

from execution.models import ExecutionRequest, ExecutionResult, ExecutionDecisionStatus
from execution.safety import (
    guard_inputs,
    UnsafeInputError,
    contact_only_inputs,
    fill_contact_only_inputs,
)
from registry.models import MethodType

logger = logging.getLogger(__name__)

# Directory where executor scripts live (relative to project root)
_EXECUTORS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "executors")

# Map MethodType → executor filename
_EXECUTOR_MAP = {
    MethodType.HTTP:      "http_executor.py",
    MethodType.WEB_FORM:  "form_executor.py",
    MethodType.QR_URL:    "qr_url_executor.py",
    MethodType.BROWSER:   "browser_executor.py",
    MethodType.MANUAL:    None,   # cannot be automated
}

# Method types that need outbound network access
_NETWORK_REQUIRED = {MethodType.HTTP, MethodType.WEB_FORM, MethodType.QR_URL, MethodType.BROWSER}


DEFAULT_TIMEOUT_SECONDS = 30
BROWSER_TIMEOUT_SECONDS = 120

_METHOD_TIMEOUTS = {
    MethodType.HTTP: DEFAULT_TIMEOUT_SECONDS,
    MethodType.WEB_FORM: DEFAULT_TIMEOUT_SECONDS,
    MethodType.QR_URL: DEFAULT_TIMEOUT_SECONDS,
    MethodType.BROWSER: BROWSER_TIMEOUT_SECONDS,
}

# Chromium (BROWSER executor) crashes its renderer under a 256m cgroup limit
# on JS-heavy pages ("Target crashed"). Browsers need their own budget; other
# executors are plain HTTP clients.
_DEFAULT_MEMORY = "256m"
_METHOD_MEMORY = {
    MethodType.BROWSER: "1g",
}
_DEFAULT_CPUS = "0.5"
_METHOD_CPUS = {
    MethodType.BROWSER: "1.0",
}


class DockerMethodRunner:
    """
    Executes validation methods inside isolated Docker containers.

    The interface is intentionally thin so a future sandbox implementation
    can replace it without touching the validation engine.
    """

    def __init__(
        self,
        docker_image: str = "dvs-executor:latest",
        timeout_seconds: Optional[int] = None,
    ):
        self.docker_image = docker_image
        self.timeout_seconds = timeout_seconds

        # Project-local temp directory so Docker can bind-mount it
        self.base_temp_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), ".docker_temp"
        )
        os.makedirs(self.base_temp_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_method(
        self,
        request: ExecutionRequest,
        executor_script_path: Optional[str] = None,
        allow_structural_test_values: bool = False,
    ) -> ExecutionResult:
        """
        Execute a validation method inside Docker.

        executor_script_path: override the auto-selected executor.
                              Pass None to auto-select by method type.
        allow_structural_test_values: only the generator's structural test
                              (validation/validator.py) may submit the
                              known-fake marker values to a live endpoint;
                              every other caller — including the MCP server
                              and the agentic fallback agent — is refused
                              placeholder/incomplete input by default.
        """
        method = request.method

        # ---- live-submission safety guard ----
        # Single choke point for the "no redaction tokens / placeholder /
        # incomplete data to a live endpoint" rule. The MCP server and the
        # agentic fallback agent forward caller-supplied inputs verbatim; this
        # guard makes the rule apply on every path, not just the main engine.
        # (See execution/safety.py — added after the September 2026 incident
        # where an MCP tool call submitted incomplete values to the live
        # dmamyanmar.org endpoint and received a real HTTP 500.)
        #
        # A refusal is a TECHNICAL_FAILURE, not an exception: callers (the
        # engine's decision builder, the MCP tool wrapper) already classify
        # TECHNICAL_FAILURE as "the machinery refused to run — never the
        # document is bad", and no container/network side effect occurs.
        # Contact-only fields (declared in expected_responses) are
        # synthesizable: fill any MISSING ones with inert plausible-format
        # values before the guard. Identity fields are never touched here.
        effective_inputs = fill_contact_only_inputs(request.inputs, method)

        try:
            guard_inputs(
                effective_inputs,
                method.required_inputs,
                allow_structural_test_values=allow_structural_test_values,
                contact_only_inputs=contact_only_inputs(method),
            )
        except UnsafeInputError as e:
            logger.error(
                "Refusing live submission for method %s: %s", method.method_id, e
            )
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={"refused_reason": str(e)},
                logs="",
                error=f"Refusing to submit incomplete or placeholder data to a "
                      f"live endpoint: {e}",
            )

        # ---- resolve executor script ----
        if executor_script_path is None:
            executor_script_path = self._resolve_executor(method.method_type)

        if executor_script_path is None:
            # MANUAL or unknown — cannot be automated
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.VALIDATION_UNAVAILABLE,
                evidence={"reason": f"Method type {method.method_type} cannot be automated."},
                logs="",
            )

        if not os.path.exists(executor_script_path):
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={},
                logs="",
                error=f"Executor script not found: {executor_script_path}",
            )

        # ---- network policy ----
        needs_network = method.method_type in _NETWORK_REQUIRED

        # Execute with the EFFECTIVE inputs (missing contact-only fields
        # synthesized) so the value actually reaches the executor, not just
        # the guard.
        effective_request = request
        if effective_inputs != request.inputs:
            effective_request = ExecutionRequest(method=method, inputs=effective_inputs)

        return self._run_in_docker(effective_request, executor_script_path, needs_network)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_executor(self, method_type: MethodType) -> Optional[str]:
        filename = _EXECUTOR_MAP.get(method_type)
        if filename is None:
            return None
        path = os.path.join(_EXECUTORS_DIR, filename)
        return path if os.path.exists(path) else None

    def _run_in_docker(
        self,
        request: ExecutionRequest,
        executor_script_path: str,
        needs_network: bool,
    ) -> ExecutionResult:
        temp_dir = tempfile.mkdtemp(prefix="exec_", dir=self.base_temp_dir)
        input_path  = os.path.join(temp_dir, "input.json")
        output_path = os.path.join(temp_dir, "output.json")
        script_dest = os.path.join(temp_dir, "executor.py")

        try:
            # Write inputs
            with open(input_path, "w", encoding="utf-8") as f:
                f.write(request.model_dump_json())

            shutil.copy2(executor_script_path, script_dest)

            # The BROWSER executor resolves its model through the shared
            # local/frontier toggle. Copy the shared module next to the
            # executor so the sandbox calls the same function rather than
            # reimplementing the switch (imported as top-level `llm_client`).
            shared_llm_module = os.path.join(
                os.path.dirname(_EXECUTORS_DIR), "utils", "llm_client.py"
            )
            if os.path.exists(shared_llm_module):
                shutil.copy2(shared_llm_module, os.path.join(temp_dir, "llm_client.py"))

            # Build docker run command (options first, image + command last)
            docker_opts = [
                "run", "--rm",
                "--network", "bridge" if needs_network else "none",
                "--memory", _METHOD_MEMORY.get(request.method.method_type, _DEFAULT_MEMORY),
                "--cpus", _METHOD_CPUS.get(request.method.method_type, _DEFAULT_CPUS),
                "-v", f"{temp_dir}:/workspace",
                "-w", "/workspace",
                "--read-only",
                "--tmpfs", "/tmp",
            ]

            # Pass LLM configuration through to the sandbox (needed by the
            # BROWSER executor's vision calls). Only LLM-related keys are
            # forwarded — never document credentials or other host secrets.
            # Values are also scrubbed from the logged command line.
            for env_key in ("LLM_URL", "LLM_MODEL", "LLM_API_KEY",
                            "USE_LOCAL_LLM_ONLY",
                            "BROWSER_EXECUTOR_DEADLINE"):
                env_val = os.getenv(env_key, "")
                if env_val:
                    docker_opts += ["-e", f"{env_key}={env_val}"]

            cmd = ["docker"] + docker_opts + [
                self.docker_image,
                "python", "executor.py",
            ]

            safe_cmd = [
                ("<redacted>" if ("=" in a and a.split("=", 1)[0] in
                 ("LLM_API_KEY", "GOOGLE_API_KEY")) else a)
                for a in cmd
            ]
            logger.debug("Running: %s", " ".join(safe_cmd))

            timeout = (
                self.timeout_seconds
                if self.timeout_seconds is not None
                else _METHOD_TIMEOUTS.get(request.method.method_type, DEFAULT_TIMEOUT_SECONDS)
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            logs = result.stdout + "\n" + result.stderr

            if result.returncode != 0:
                return ExecutionResult(
                    decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                    evidence={},
                    logs=logs,
                    error=f"Container exited with code {result.returncode}",
                )

            if not os.path.exists(output_path):
                return ExecutionResult(
                    decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                    evidence={},
                    logs=logs,
                    error="output.json was not produced by the executor",
                )

            with open(output_path, "r", encoding="utf-8") as f:
                out_data = json.load(f)

            return ExecutionResult(
                decision_status=ExecutionDecisionStatus(
                    out_data.get("decision_status", "TECHNICAL_FAILURE")
                ),
                evidence=out_data.get("evidence", {}),
                raw_response=out_data.get("raw_response"),
                logs=logs,
            )

        except subprocess.TimeoutExpired as e:
            # Keep whatever the container printed before the kill — this is
            # often the only diagnostic for a hung execution.
            partial_logs = ""
            if getattr(e, "stdout", None):
                partial_logs += e.stdout.decode("utf-8", errors="replace") \
                    if isinstance(e.stdout, bytes) else e.stdout
            if getattr(e, "stderr", None):
                partial_logs += "\n" + (e.stderr.decode("utf-8", errors="replace") \
                    if isinstance(e.stderr, bytes) else e.stderr)
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={},
                logs=partial_logs,
                error="Execution timed out.",
            )
        except Exception as e:
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={},
                logs="",
                error=str(e),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
