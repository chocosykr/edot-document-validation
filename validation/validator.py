import logging
from typing import List, Optional

from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodStatus
from registry.repository import MethodRegistry
from validation.models import (
    TestCase,
    ValidationAttempt,
    ValidationReport,
    AttemptOutcome,
    ValidationReportStatus,
)
from validation.field_comparison import compare_response

logger = logging.getLogger(__name__)

MAX_ATTEMPTS_PER_TEST_CASE = 3


def _attempt_improvement(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    previous_attempts: list,
) -> Optional[str]:
    """
    Calls the LLM with the failure details AND the full history of
    previous attempts so it can learn from past mistakes.
    """
    from utils.llm_client import generate_json
    import json

    logger.info("Attempting LLM improvement for method %s", method.method_id)

    system_prompt = (
        "You are an expert automation engineer fixing a broken web scraper / API integration.\n"
        "You will be given:\n"
        "1. The current ValidationMethod definition.\n"
        "2. The FULL HISTORY of every previous attempt (including what was tried and what error occurred).\n"
        "3. The latest failed attempt details.\n\n"
        "IMPORTANT RULES:\n"
        "- Study the history carefully. Do NOT repeat approaches that already failed.\n"
        "- The logs contain the actual stdout/stderr from the Docker container. Read them carefully.\n"
        "- If you see 404 errors on a form action URL like '/post', the site likely uses JavaScript/AJAX.\n"
        "  In that case, look at the source_url and try to identify the real API endpoint.\n\n"
        "SUPPORTED execution_steps actions:\n"
        "  For WEB_FORM method_type:\n"
        "  - FETCH_FORM: requires 'url' field. Fetches a page and parses HTML forms.\n"
        "  - FILL: requires 'field' (input name/id attribute) and 'value' (use {{input_name}} for dynamic values).\n"
        "  - SUBMIT: POSTs the filled form. No extra fields.\n\n"
        "  For HTTP method_type:\n"
        "  - REQUEST: requires 'method' (GET/POST), 'url' (full API endpoint URL), and 'params' (key-value dict).\n"
        "    Use {{input_name}} placeholders for dynamic values.\n\n"
        "  Do NOT use actions like NAVIGATE, TYPE, CLICK, WAIT_FOR_ELEMENT, GET_TEXT — they are NOT supported.\n\n"
        "Return ONLY a JSON object. No explanation or commentary outside the JSON.\n"
        "The JSON MUST contain 'execution_steps' and 'expected_responses'.\n"
        "You MAY also include 'method_type' if you want to switch from WEB_FORM to HTTP.\n\n"
        "The response body is shown above (raw_response field). If it looks like the same\n"
        "page was returned unchanged (e.g., the idle form page, a login redirect, or a\n"
        "500/error page), the endpoint or request format is likely wrong — change\n"
        "execution_steps structurally (URL, verb, param_location, fields), not just\n"
        "expected_responses keywords.\n"
        "Schema:\n"
        "{\n"
        '  "method_type": "HTTP",  // optional: include to switch method type\n'
        '  "execution_steps": [\n'
        '    {"action": "REQUEST", "method": "POST", "url": "https://...", "params": {"key": "{{document_number}}"}}\n'
        "  ],\n"
        '  "expected_responses": {\n'
        '    "success_keywords": ["valid", "verified", "found"],\n'
        '    "failure_keywords": ["invalid", "not found", "no record"]\n'
        "  }\n"
        "}"
    )

    # Build history of what was already tried
    history = []
    for prev in previous_attempts:
        history.append({
            "attempt_number": prev.attempt_number,
            "actual_decision": prev.actual_decision,
            "error": prev.error,
            "logs_excerpt": prev.logs[-500:] if prev.logs else "",
            "raw_response_excerpt": (prev.raw_response or "")[-500:],
            "improvement_applied": prev.improvement_applied,
        })

    user_prompt = json.dumps({
        "current_method": method.model_dump(),
        "attempt_history": history,
        "latest_failure": {
            "test_case_name": attempt.test_case_name,
            "inputs": attempt.inputs,
            "expected_decision": attempt.expected_decision,
            "actual_decision": attempt.actual_decision,
            "error": attempt.error,
            "raw_response": (attempt.raw_response or "")[:2000],
            "full_logs": attempt.logs[-2000:] if attempt.logs else ""
        }
    }, indent=2, default=str)

    improved = generate_json(system_prompt, user_prompt)
    
    print(f"\n[DEBUG] Raw LLM output from self-healing:\n{json.dumps(improved, indent=2)}\n")
    
    comparison_mode = (method.expected_responses or {}).get("comparison_mode")
    if improved and "execution_steps" in improved:
        method.execution_steps = improved["execution_steps"]
        if "expected_responses" in improved:
            method.expected_responses = improved["expected_responses"]
        if comparison_mode == "field_match":
            method.expected_responses = {"comparison_mode": "field_match"}
        if "method_type" in improved:
            from registry.models import MethodType
            try:
                method.method_type = MethodType(improved["method_type"])
            except ValueError:
                pass
        note = f"[LLM Improvement Applied] Test '{attempt.test_case_name}' failed. LLM updated execution_steps and expected_responses."
        logger.info(note)
        return note

    note = f"[LLM Improvement Failed] Test '{attempt.test_case_name}' failed. LLM did not return valid steps."
    logger.warning(note)
    return note


class MethodValidator:
    """
    Validates a candidate ValidationMethod by running its test cases
    inside the Docker sandbox.  On success it can automatically promote
    the method to ACTIVE in the registry.
    """

    def __init__(
        self,
        runner: DockerMethodRunner | None = None,
        registry: MethodRegistry | None = None,
        executor_script_path: str | None = None,
    ):
        self.runner = runner or DockerMethodRunner()
        self.registry = registry
        self.executor_script_path = executor_script_path

    def validate(
        self,
        method: ValidationMethod,
        test_cases: List[TestCase],
    ) -> ValidationReport:
        """
        Run all test cases for the method with bounded retries.

        Returns a ValidationReport.  If the report status is PASSED
        and a registry was supplied, the method's status is updated to
        ACTIVE automatically.
        """
        report = ValidationReport(
            method_id=method.method_id,
            method_version=method.version,
            status=ValidationReportStatus.ERROR,   # default; overwritten below
        )

        # Guard: need at least one test case
        if not test_cases:
            report.status = ValidationReportStatus.ERROR
            report.failure_reason = "No test cases supplied."
            return report

        # REJECTED-only guard: a method without a human-confirmed success
        # marker must never gain LLM-guessed success keywords (that would
        # reintroduce false VERIFIED decisions). Strip any injected ones
        # before the attempts run.
        if any("REJECTED-only" in lim for lim in (method.limitations or [])):
            expected = method.expected_responses or {}
            if expected.get("success_keywords"):
                logger.warning(
                    "Method %s is REJECTED-only but carries success_keywords; "
                    "stripping them (no human-confirmed success marker).",
                    method.method_id,
                )
                expected["success_keywords"] = []
                method.expected_responses = expected

        all_required_passed = True

        for tc in test_cases:
            passed = self._run_test_case(method, tc, report)
            if not passed and tc.is_required:
                all_required_passed = False

        if all_required_passed:
            report.status = ValidationReportStatus.PASSED
            logger.info(
                "Method %s v%d passed validation.",
                method.method_id,
                method.version,
            )
            if self.registry:
                self.registry.update_status(method.method_id, MethodStatus.ACTIVE)
        else:
            report.status = ValidationReportStatus.FAILED
            report.failure_reason = (
                "One or more required test cases failed after "
                f"{MAX_ATTEMPTS_PER_TEST_CASE} attempts."
            )
            logger.warning(
                "Method %s v%d FAILED validation.",
                method.method_id,
                method.version,
            )
            if self.registry:
                self.registry.update_status(method.method_id, MethodStatus.UNHEALTHY)

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_test_case(
        self,
        method: ValidationMethod,
        tc: TestCase,
        report: ValidationReport,
    ) -> bool:
        """
        Execute a single test case with up to MAX_ATTEMPTS_PER_TEST_CASE
        retries.  Returns True if the test case passed.
        """
        for attempt_num in range(1, MAX_ATTEMPTS_PER_TEST_CASE + 1):
            req = ExecutionRequest(method=method, inputs=tc.inputs)

            exec_result = self.runner.execute_method(req, self.executor_script_path)

            if (method.expected_responses or {}).get("comparison_mode") == "field_match":
                exec_result = compare_response(exec_result, tc.inputs)

            actual = exec_result.decision_status.value

            if exec_result.decision_status == ExecutionDecisionStatus.TECHNICAL_FAILURE:
                outcome = AttemptOutcome.ERROR
            elif actual == tc.expected_decision:
                outcome = AttemptOutcome.PASSED
            else:
                outcome = AttemptOutcome.FAILED

            attempt = ValidationAttempt(
                attempt_number=attempt_num,
                test_case_name=tc.name,
                inputs=tc.inputs,
                expected_decision=tc.expected_decision,
                actual_decision=actual,
                outcome=outcome,
                logs=exec_result.logs,
                raw_response=(exec_result.raw_response or "")[:2000],
                error=exec_result.error,
            )

            # ---- Print detailed error info to terminal ----
            if outcome != AttemptOutcome.PASSED:
                print(f"\n  --- Attempt {attempt_num}/{MAX_ATTEMPTS_PER_TEST_CASE} Details ---")
                if exec_result.error:
                    print(f"  Error: {exec_result.error}")
                if exec_result.logs:
                    # Show last 800 chars of logs to keep output readable
                    log_excerpt = exec_result.logs.strip()[-800:]
                    print(f"  Logs:\n    {log_excerpt}")
                print(f"  Decision: {actual} (expected: {tc.expected_decision})")

            # Try improvement on failure/error (before final attempt)
            if outcome != AttemptOutcome.PASSED and attempt_num < MAX_ATTEMPTS_PER_TEST_CASE:
                attempt.improvement_applied = _attempt_improvement(
                    method, attempt, report.attempts
                )

            report.attempts.append(attempt)

            if outcome == AttemptOutcome.PASSED:
                logger.info(
                    "  [Attempt %d/%d] Test '%s' PASSED.",
                    attempt_num,
                    MAX_ATTEMPTS_PER_TEST_CASE,
                    tc.name,
                )
                return True

            logger.warning(
                "  [Attempt %d/%d] Test '%s' %s (expected=%s, got=%s).",
                attempt_num,
                MAX_ATTEMPTS_PER_TEST_CASE,
                tc.name,
                outcome.value,
                tc.expected_decision,
                actual,
            )

        return False
