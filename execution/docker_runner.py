import json
import os
import logging
import tempfile
import subprocess
import shutil
from typing import Optional

from execution.models import ExecutionRequest, ExecutionResult, ExecutionDecisionStatus
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


class DockerMethodRunner:
    """
    Executes validation methods inside isolated Docker containers.

    The interface is intentionally thin so a future sandbox implementation
    can replace it without touching the validation engine.
    """

    def __init__(
        self,
        docker_image: str = "dvs-executor:latest",
        timeout_seconds: int = 30,
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
    ) -> ExecutionResult:
        """
        Execute a validation method inside Docker.

        executor_script_path: override the auto-selected executor.
                              Pass None to auto-select by method type.
        """
        method = request.method

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

        return self._run_in_docker(request, executor_script_path, needs_network)

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

            # Build docker run command
            cmd = [
                "docker", "run", "--rm",
                "--network", "bridge" if needs_network else "none",
                "--memory", "256m",
                "--cpus", "0.5",
                "-v", f"{temp_dir}:/workspace",
                "-w", "/workspace",
                "--read-only",
                "--tmpfs", "/tmp",
                self.docker_image,
                "python", "executor.py",
            ]

            logger.debug("Running: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
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
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={},
                logs=str(e),
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
