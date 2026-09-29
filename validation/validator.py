import logging
import os
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
from validation.healing import escalate_improvement

logger = logging.getLogger(__name__)

# Retry budget per test case. On a failed attempt, the healing ladder gets
# one shot before the next attempt: first the mechanical not-found-signature
# capture/retest (no LLM involved), then the cheap direct LLM rewrite
# (test-before-adopt), then the full agentic tool-use loop. Raise or lower
# via VALIDATION_MAX_ATTEMPTS (hard cap 6: each attempt is a Docker run and
# possibly an LLM pass). The historical default of 1 made every healing path
# dead code — the "before final attempt" condition can never hold when only
# one attempt exists.
def _max_attempts() -> int:
    raw = os.getenv("VALIDATION_MAX_ATTEMPTS", "3")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(6, value))

MAX_ATTEMPTS_PER_TEST_CASE = _max_attempts()




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
        allow_structural_test_values: bool = False,
    ):
        """...

        allow_structural_test_values: forwarded to the Docker runner so the
        generator's structural test may submit the known-fake marker values
        (e.g. TEST_STRUCTURAL_001) to a live endpoint. Only this validator —
        the code path that exists to run deliberate known-fake probes — may
        enable it. The engine constructs its validator with the default
        (False); MCP and the fallback agent never enable it.
        """
        self.runner = runner or DockerMethodRunner()
        self.registry = registry
        self.executor_script_path = executor_script_path
        self.allow_structural_test_values = allow_structural_test_values

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

            exec_result = self.runner.execute_method(
                req,
                self.executor_script_path,
                allow_structural_test_values=self.allow_structural_test_values,
            )

            if (method.expected_responses or {}).get("comparison_mode") == "field_match":
                field_mapping = (method.expected_responses or {}).get("field_mapping")
                exec_result = compare_response(
                    exec_result, tc.inputs, field_mapping=field_mapping,
                    not_found_signatures=(method.expected_responses or {}).get("not_found_signatures"),
                )

            actual = exec_result.decision_status.value

            if exec_result.decision_status == ExecutionDecisionStatus.TECHNICAL_FAILURE:
                outcome = AttemptOutcome.ERROR
            elif actual == tc.expected_decision:
                outcome = AttemptOutcome.PASSED
            else:
                outcome = AttemptOutcome.FAILED

            # HTTP status extraction: the executor's evidence carries it for
            # HTTP methods; pull it out once for the attempt record and for
            # the signature-capture path below.
            http_status = None
            try:
                http_status = int((exec_result.evidence or {}).get("http_status"))
            except (TypeError, ValueError):
                http_status = None

            attempt = ValidationAttempt(
                attempt_number=attempt_num,
                test_case_name=tc.name,
                inputs=tc.inputs,
                expected_decision=tc.expected_decision,
                actual_decision=actual,
                outcome=outcome,
                logs=exec_result.logs,
                raw_response=(exec_result.raw_response or "")[:2000],
                http_status=http_status,
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

            # -----------------------------------------------------------------
            # Confirmed not-found signature capture.
            #
            # The structural probe submitted a KNOWN-FAKE value, so the
            # site's deterministic refusal of it IS the site's canonical
            # "not found" response — even when it arrives as an HTTP 4xx
            # (e.g. dgshippingbsid.in answers 400
            # {"message":"Invalid Input: Application ID not found."}). A
            # field_match method without a signature for that response
            # classifies it TECHNICAL_FAILURE, fails its own structural
            # test, and a mechanically-correct method dies at birth. Capture
            # the response as a narrow, executor-validated signature
            # (status + substring), persist it on the method, and retest.
            # The executor's _matches_not_found_signature re-validates both
            # fields at execution time, so only genuinely narrow signatures
            # take effect.
            # -----------------------------------------------------------------
            if (
                outcome != AttemptOutcome.PASSED
                and attempt_num < MAX_ATTEMPTS_PER_TEST_CASE
                and http_status is not None
                and (exec_result.raw_response or "").strip()
                and (method.expected_responses or {}).get("comparison_mode") == "field_match"
                # A captcha-rejection body is NEVER a not-found signature:
                # capturing it would classify a future captcha hiccup as a
                # confirmed document rejection. The executor redoes the
                # captcha round in-run; the validator must not enshrine it.
                and "captcha" not in (exec_result.raw_response or "").lower()
                and (
                    # The executor returned REJECTED via its keyword channel
                    # (the body carried a declared failure keyword). That is
                    # the site's deterministic refusal of the known-fake
                    # structural-probe values --- the canonical "not found"
                    # shape. Capture it as a narrow learned signature
                    # regardless of HTTP status, because many registries
                    # answer "not found" as HTTP 200 + an error token
                    # (e.g. DMA returns 200 + "VerificationError").
                    exec_result.decision_status.value == "REJECTED"
                    # Legacy path: a 4xx response with a body and no matching
                    # signature (the executor classifies it TECHNICAL_FAILURE).
                    # The known-fake probe's refusal is still the canonical
                    # not-found shape --- capture it so the retest can use the
                    # learned signature.
                    or (400 <= http_status < 500)
                )
            ):
                body_head = exec_result.raw_response.strip()[:200]
                expected = dict(method.expected_responses or {})
                signatures = expected.get("not_found_signatures")
                if not isinstance(signatures, list):
                    signatures = []
                    expected["not_found_signatures"] = signatures

                def _sig_norm(text: str) -> str:
                    # Whitespace-insensitive comparison — the same server has
                    # been observed emitting '{"message": "..."}' and
                    # '{"message":"..."}' for different error classes, so
                    # exact-substring dedup would store drift duplicates.
                    return "".join(str(text).lower().split())

                already = any(
                    s.get("status") == http_status
                    and _sig_norm(s.get("contains", "")) == _sig_norm(body_head)
                    for s in signatures
                    if isinstance(s, dict)
                )
                if not already:
                    signatures.append({"status": http_status, "contains": body_head})
                    method.expected_responses = expected
                    logger.info(
                        "Captured confirmed not-found signature for %s: "
                        "HTTP %s %r — retesting.",
                        method.method_id, http_status, body_head[:120],
                    )
                    if self.registry:
                        try:
                            self.registry.update_expected_responses(
                                method.method_id, expected
                            )
                            logger.info(
                                "Persisted not-found signature to registry for %s",
                                method.method_id,
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to persist signature for %s: %s",
                                method.method_id, e,
                            )
                    continue  # immediate retest with the signature in place

            # Try improvement on failure/error (before final attempt).
            # Gap policy: after the first failure run the CHEAP direct LLM
            # rewrite (test-before-adopt — the rewrite must itself pass the
            # structural probe before the method is modified); after later
            # failures escalate to the full agentic tool-use loop.
            if outcome != AttemptOutcome.PASSED and attempt_num < MAX_ATTEMPTS_PER_TEST_CASE:
                note = None
                note = escalate_improvement(
                    method, attempt, tc, self.runner, self.executor_script_path,
                    attempt_num, MAX_ATTEMPTS_PER_TEST_CASE,
                )
                attempt.improvement_applied = note

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
