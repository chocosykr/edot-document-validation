import json
import os
import re
import time
import logging
import tempfile
import subprocess
import shutil
from typing import List, Optional

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
from execution.script_policy import apply_script_policy
from registry.models import MethodType

logger = logging.getLogger(__name__)

# Directory where executor scripts live (relative to project root)
_EXECUTORS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "executors")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Durable home for screenshots an executor produced. The executor's workspace
# is a temp dir that is deleted in this function's `finally`, so anything worth
# keeping must be moved out first. `probe_evidence/` is the project's existing
# (gitignored) evidence directory, already used by engine/probe_passport.py.
_EVIDENCE_DIR = os.path.join(_PROJECT_ROOT, "probe_evidence", "browser")


def _salvage_screenshots(temp_dir: str, method_id: str) -> List[str]:
    """Move executor screenshots out of the doomed temp dir.

    Returns project-root-relative paths of the files kept. Never raises: a
    missing or unreadable artifact must not turn an execution into a failure.
    """
    try:
        names = sorted(
            n for n in os.listdir(temp_dir)
            if n.startswith("screenshot") and n.endswith(".png")
        )
    except OSError:
        return []
    if not names:
        return []

    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(method_id or "method"))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved: List[str] = []
    for name in names:
        dest = os.path.join(_EVIDENCE_DIR, f"{safe_id}_{stamp}_{name}")
        try:
            os.makedirs(_EVIDENCE_DIR, exist_ok=True)
            shutil.copy2(os.path.join(temp_dir, name), dest)
        except OSError as e:
            logger.warning("Could not salvage screenshot %s: %s", name, e)
            continue
        saved.append(os.path.relpath(dest, _PROJECT_ROOT))
    if saved:
        logger.info("Saved %d browser screenshot(s): %s", len(saved), saved)
    return saved

# Map MethodType → executor filename
_EXECUTOR_MAP = {
    MethodType.HTTP:      "http_executor.py",
    MethodType.WEB_FORM:  "form_executor.py",
    MethodType.QR_URL:    "qr_url_executor.py",
    MethodType.BROWSER:   "browser_executor.py",
    MethodType.MANUAL:    None,   # cannot be automated
    MethodType.SCRIPT:    "__SCRIPT__",
}

# Method types that need outbound network access
_NETWORK_REQUIRED = {MethodType.HTTP, MethodType.WEB_FORM, MethodType.QR_URL, MethodType.BROWSER, MethodType.SCRIPT}


DEFAULT_TIMEOUT_SECONDS = 30
BROWSER_TIMEOUT_SECONDS = 120
# Captcha-bearing HTTP methods run FETCH_CAPTCHA -> SOLVE_CAPTCHA (a remote
# vision-LLM call, ~CAPTCHA_VISION_TIMEOUT_S on its own) before the actual
# lookup request. The plain 30s HTTP budget times them out before the site
# is even asked (live-confirmed 2026-09-29 on dgshippingbsid.in, which
# itself answers in ~0.2s). One vision call + retries fits in 90s with
# margin.
CAPTCHA_HTTP_TIMEOUT_SECONDS = 90
# SCRIPT methods drive multi-step handshakes in code and may legitimately
# poll/retry; the plain 30s HTTP budget is too tight for that.
SCRIPT_TIMEOUT_SECONDS = 90

_METHOD_TIMEOUTS = {
    MethodType.HTTP: DEFAULT_TIMEOUT_SECONDS,
    MethodType.WEB_FORM: DEFAULT_TIMEOUT_SECONDS,
    MethodType.QR_URL: DEFAULT_TIMEOUT_SECONDS,
    MethodType.BROWSER: BROWSER_TIMEOUT_SECONDS,
    MethodType.SCRIPT: SCRIPT_TIMEOUT_SECONDS,
}


def _method_timeout(method) -> int:
    """Container budget for a method; captcha steps widen the HTTP budget."""
    base = _METHOD_TIMEOUTS.get(method.method_type, DEFAULT_TIMEOUT_SECONDS)
    if base == DEFAULT_TIMEOUT_SECONDS:
        for step in (method.execution_steps or []):
            action = str((step or {}).get("action", "")).upper()
            if action in ("FETCH_CAPTCHA", "SOLVE_CAPTCHA"):
                return CAPTCHA_HTTP_TIMEOUT_SECONDS
    return base

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

        if executor_script_path != "__SCRIPT__" and not os.path.exists(executor_script_path):
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

        result = self._run_in_docker(effective_request, executor_script_path, needs_network)
        # Container logs can echo real credential values (a SCRIPT can print
        # anything); scrub before they reach any caller, dump or healing prompt.
        if result.logs:
            from utils.log_scrubber import scrub_pii
            result = result.model_copy(update={"logs": scrub_pii(result.logs)})
        # SCRIPT verdicts are governed by the harness, not the authored code.
        return apply_script_policy(result, method)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_executor(self, method_type: MethodType) -> Optional[str]:
        filename = _EXECUTOR_MAP.get(method_type)
        if filename is None:
            return None
        if filename == "__SCRIPT__":
            return "__SCRIPT__"
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

            if executor_script_path == "__SCRIPT__":
                # Authored code lives on the method; fall back to the early
                # prototype's execution_steps[0]["code"] for old rows.
                script_code = request.method.script_source or ""
                if not script_code and request.method.execution_steps:
                    script_code = str(
                        (request.method.execution_steps[0] or {}).get("code", "")
                    )
                if not script_code.strip():
                    return ExecutionResult(
                        decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                        evidence={},
                        logs="",
                        error="SCRIPT method carries no script_source to execute.",
                    )
                with open(script_dest, "w", encoding="utf-8") as f:
                    f.write(script_code)
                # Inject the SDK shim flat next to the script so it can
                # `from dvs_io import get_input, http_get, write_result`.
                dvs_io_src = os.path.join(_EXECUTORS_DIR, "dvs_io.py")
                if os.path.exists(dvs_io_src):
                    shutil.copy2(dvs_io_src, os.path.join(temp_dir, "dvs_io.py"))
            else:
                shutil.copy2(executor_script_path, script_dest)

            # Split HTTP-executor modules: copy them flat next to executor.py
            # so the sandbox can import them by plain module name (the
            # executor falls back to non-package imports inside Docker).
            # Only shipped when the HTTP executor is the one being run.
            if executor_script_path != "__SCRIPT__" and os.path.basename(executor_script_path) == "http_executor.py":
                for module_name in ("http_helpers.py", "http_decider.py"):
                    module_src = os.path.join(_EXECUTORS_DIR, module_name)
                    if os.path.exists(module_src):
                        shutil.copy2(module_src, os.path.join(temp_dir, module_name))

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
            # Frontier mode reuses LLM_URL/LLM_API_KEY and only swaps the model
            # name, so no extra provider keys are forwarded.
            for env_key in ("LLM_URL", "LLM_MODEL", "LLM_API_KEY",
                            "USE_LOCAL_LLM_ONLY",
                            "FRONTIER_LLM_URL", "FRONTIER_LLM_MODEL",
                            "FRONTIER_LLM_API_KEY", "DVS_SCRUB_FRONTIER",
                            "BROWSER_EXECUTOR_DEADLINE"):
                env_val = os.getenv(env_key, "")
                if env_val:
                    docker_opts += ["-e", f"{env_key}={env_val}"]

            cmd = ["docker"] + docker_opts + [
                self.docker_image,
                "python", "executor.py",
            ]

            _SECRET_ENV = {"LLM_API_KEY", "FRONTIER_LLM_API_KEY"}
            safe_cmd = [
                (a.split("=", 1)[0] + "=<redacted>"
                 if ("=" in a and a.split("=", 1)[0] in _SECRET_ENV) else a)
                for a in cmd
            ]
            logger.debug("Running: %s", " ".join(safe_cmd))

            timeout = (
                self.timeout_seconds
                if self.timeout_seconds is not None
                else _method_timeout(request.method)
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

            evidence = dict(out_data.get("evidence", {}) or {})
            screenshots = _salvage_screenshots(
                temp_dir, request.method.method_id
            )
            if screenshots:
                evidence["screenshot_paths"] = screenshots

            return ExecutionResult(
                decision_status=ExecutionDecisionStatus(
                    out_data.get("decision_status", "TECHNICAL_FAILURE")
                ),
                evidence=evidence,
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
            # A timeout is precisely when the last rendered page is worth
            # keeping — salvage whatever the container wrote before the kill.
            screenshots = _salvage_screenshots(
                temp_dir, request.method.method_id
            )
            return ExecutionResult(
                decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
                evidence={"screenshot_paths": screenshots} if screenshots else {},
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
