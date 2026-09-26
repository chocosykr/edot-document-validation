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

logger = logging.getLogger(__name__)

# Retry budget per test case. On a failed attempt, the agentic self-healing
# loop (LLM rewrites execution_steps from the failure evidence) gets one shot
# before the next attempt. Raise via VALIDATION_MAX_ATTEMPTS (hard cap 6:
# each attempt is a Docker run + an LLM healing pass, so retries are not
# free). 1 = no healing at all — the historical default, which also made the
# improvement path dead code (the "before final attempt" condition can never
# hold when only one attempt exists).
def _max_attempts() -> int:
    raw = os.getenv("VALIDATION_MAX_ATTEMPTS", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(6, value))

MAX_ATTEMPTS_PER_TEST_CASE = _max_attempts()


def _llm_improve(
    method: ValidationMethod,
    attempt: "ValidationAttempt",
) -> Optional[ValidationMethod]:
    """One direct LLM rewrite pass for a failed structural test.

    Deterministic-first design: the agentic loop below can burn many minutes;
    this is the cheap first response to a failure. Sends the full method and
    the failure evidence, expects the standard ```json {method_type,
    execution_steps, expected_responses}``` block back. Returns a NEW method
    (never mutates the candidate) or None on any failure.
    """
    from utils.llm_client import generate_json
    from registry.models import MethodType
    import copy
    import json as _json

    system = (
        "You fix broken web-verification method definitions. You will get a "
        "ValidationMethod JSON and the failure evidence from its structural "
        "test. Return ONLY a ```json block with keys method_type, "
        "execution_steps, expected_responses — a corrected version of the "
        "method. Fix mechanical problems (wrong endpoint, wrong verb, wrong "
        "parameter names, wrong param_location, missing steps). Never invent "
        "values; inputs are provided by the runner. If the failure indicates "
        "the target site cannot be driven this way at all, return the single "
        "word UNFIXABLE."
    )
    user = (
        "METHOD:\n" + method.model_dump_json(indent=2) +
        "\n\nFAILURE EVIDENCE:\n" + _json.dumps({
            "expected_decision": attempt.expected_decision,
            "actual_decision": attempt.actual_decision,
            "error": attempt.error,
            "logs_tail": (attempt.logs or "")[-800:],
            "raw_response_head": (attempt.raw_response or "")[:1200],
        }, indent=2)
    )

    result = generate_json(system, user)
    if not result or "execution_steps" not in result:
        return None
    try:
        improved = copy.deepcopy(method)
        improved.execution_steps = result["execution_steps"]
        if result.get("expected_responses"):
            merged = dict(method.expected_responses or {})
            merged.update(result["expected_responses"])
            improved.expected_responses = merged
        if result.get("method_type"):
            improved.method_type = MethodType(result["method_type"])
        return improved
    except Exception as e:
        logger.warning("LLM improvement produced invalid method: %s", e)
        return None


def _attempt_improvement(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    tc: TestCase,
    runner: DockerMethodRunner,
    executor_script_path: Optional[str]
) -> Optional[str]:
    """
    Spins up an Agentic Loop to iteratively test and fix the method until it works.
    """
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool
    from utils.llm_client import get_llm_config
    import json
    import copy
    
    logger.info("Starting Agentic Self-Healing Loop for method %s", method.method_id)
    
    config = get_llm_config()
    llm = ChatOpenAI(
        model=config.get("model", "AI_Local"),
        api_key=config.get("api_key", "dummy"),
        base_url=config.get("url", "https://ai.edot-solutions.com/v1").replace("/chat/completions", ""),
    )
    
    @tool
    def test_method(execution_steps_json: str, expected_responses_json: str, method_type: str) -> str:
        """
        Tests the modified method payload against the live API.
        You MUST pass valid JSON strings for execution_steps and expected_responses.
        Returns the execution logs, raw response, and whether it passed or failed.
        """
        test_m = copy.deepcopy(method)
        try:
            test_m.execution_steps = json.loads(execution_steps_json)
            test_m.expected_responses = json.loads(expected_responses_json)
            from registry.models import MethodType
            test_m.method_type = MethodType(method_type)
        except Exception as e:
            return f"Error parsing JSON inputs: {e}"
            
        req = ExecutionRequest(method=test_m, inputs=tc.inputs)
        exec_result = runner.execute_method(
            req, executor_script_path, allow_structural_test_values=True
        )
        
        # compare
        if (test_m.expected_responses or {}).get("comparison_mode") == "field_match":
            from validation.field_comparison import compare_response
            field_mapping = (test_m.expected_responses or {}).get("field_mapping")
            exec_result = compare_response(exec_result, tc.inputs, field_mapping=field_mapping)
            
        actual = exec_result.decision_status.value
        passed = actual == tc.expected_decision
        
        res = {
            "passed": passed,
            "actual_decision": actual,
            "expected_decision": tc.expected_decision,
            "error": exec_result.error,
            "logs": exec_result.logs[-1000:] if exec_result.logs else "",
            "raw_response": (exec_result.raw_response or "")[:1000]
        }
        return json.dumps(res, indent=2)

    # Cap the loop so a confused agent cannot burn unlimited LLM calls +
    # Docker runs. (recursion_limit moved from create_react_agent() into
    # invoke() — the installed LangGraph rejects it as a constructor kwarg,
    # which silently killed this whole path at import-free runtime.)
    agent = create_react_agent(llm, [test_method])
    
    config = {"recursion_limit": 10}
    
    prompt = f"""
You are an expert automation engineer fixing a broken web scraper / API integration.
The generated method failed during validation.

Current Method:
{method.model_dump_json(indent=2)}

Latest Failure Details:
- Test Case: {attempt.test_case_name}
- Inputs: {attempt.inputs}
- Error: {attempt.error}
- Logs: {attempt.logs[-1000:] if attempt.logs else ""}
- Raw Response: {(attempt.raw_response or "")[:1000]}

EXECUTOR REFERENCE (execution_steps actions):

For method_type "HTTP":
  - FETCH_CAPTCHA: Fetches a CAPTCHA image from an API.
    Fields: "url" (captcha API endpoint), "response_format" ("json"), "image_field" (JSON key for base64 image), "id_field" (JSON key for captcha ID)
  - SOLVE_CAPTCHA: Solves the fetched CAPTCHA using vision. No fields needed.
    After this step, {{{{captcha_text}}}} and {{{{captcha_id}}}} are available as template variables.
  - REQUEST: Makes an HTTP request.
    Fields: "method" ("GET" or "POST"), "url" (full endpoint URL)
    For GET: use "params" dict — they become URL query parameters.
    For POST with JSON body: use "json_body" dict (Content-Type: application/json).
    For POST with form-encoded body: use "params" dict (Content-Type: application/x-www-form-urlencoded).
    For POST with query string params: use "params" dict AND set "param_location": "query".
    Use {{{{input_name}}}} placeholders for dynamic values.

For method_type "WEB_FORM":
  - FETCH_FORM: Fetches an HTML page and parses forms. Fields: "url".
  - FILL: Fills a form field. Fields: "field" (input name/id), "value" (use {{{{input_name}}}} placeholders).
  - SUBMIT: Submits the form. No fields needed.

IMPORTANT RULES:
- If the API returns HTTP 400 with a GET request, try POST with "json_body" instead.
- If POST with "params" returns 400, try "json_body" instead (many modern APIs expect JSON).
- If the page is a React/SPA app (empty HTML with just a <div id="root">), it uses XHR/fetch APIs — use HTTP method_type, not WEB_FORM.
- ALWAYS include FETCH_CAPTCHA and SOLVE_CAPTCHA steps if the API requires a CAPTCHA.

CONTACT-ONLY VS IDENTITY FIELDS (standing principle — check this FIRST when
a failure involves a missing or unsatisfiable input):
- CONTACT-ONLY fields (notification email, callback phone, requester
  reference) are used by the target site to SEND results or as metadata —
  they are never matched against a record. Evidence: the page's own form
  label says "Requester's Email" / "notify", or the field is type=email
  with no record cross-check. If a method lacks the declaration, ADD the
  field name to expected_responses.contact_only_inputs in your final JSON:
  the execution layer synthesizes a plausible-format inert value (reserved
  .invalid TLD for emails) and the missing-field refusal does not apply.
- IDENTITY fields (document numbers, serials, passport numbers, names,
  birth dates) ARE checked against records. Never declare one contact_only
  and never fabricate values for one. If an identity field genuinely does
  not exist on the document, this method cannot be fixed by reshaping
  steps — let the test fail and report that manual input is required.
- Unsure means IDENTITY. Never guess in the permissive direction.

Your task:
1. Use the test_method tool to iteratively try different execution_steps until you get "passed": true.
2. Once test_method returns passed: true, output a final JSON block enclosed in ```json ... ``` with the winning method_type, execution_steps, and expected_responses.

Only output the final JSON when you have successfully tested it and got passed: true.
    """
    
    try:
        final_state = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]}, config=config
        )
        final_msg = final_state["messages"][-1].content
    except Exception as e:
        logger.error(f"Agent failed: {e}")
        return f"[Agentic Improvement Failed] {e}"
        
    import re
    match = re.search(r'```json\s*(\{.*?\})\s*```', final_msg, re.DOTALL)
    if match:
        try:
            improved = json.loads(match.group(1))
            method.execution_steps = improved["execution_steps"]
            if "expected_responses" in improved:
                method.expected_responses = improved["expected_responses"]
            if "method_type" in improved:
                from registry.models import MethodType
                method.method_type = MethodType(improved["method_type"])
            note = f"[Agentic Improvement Applied] Test '{tc.name}' failed initially, but Agent found a working configuration."
            logger.info(note)
            return note
        except Exception as e:
            note = f"[Agentic Improvement Failed] Could not parse JSON: {e}"
            logger.warning(note)
            return note
    else:
        note = "[Agentic Improvement Failed] No final JSON block returned."
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
                exec_result = compare_response(exec_result, tc.inputs, field_mapping=field_mapping)

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

            # Try improvement on failure/error (before final attempt).
            # Gap policy: after the first failure run the CHEAP direct LLM
            # rewrite (test-before-adopt — the rewrite must itself pass the
            # structural probe before the method is modified); after later
            # failures escalate to the full agentic tool-use loop.
            if outcome != AttemptOutcome.PASSED and attempt_num < MAX_ATTEMPTS_PER_TEST_CASE:
                note = None
                if attempt_num == 1:
                    improved = _llm_improve(method, attempt)
                    if improved is not None:
                        probe_req = ExecutionRequest(method=improved, inputs=tc.inputs)
                        probe_result = self.runner.execute_method(
                            probe_req,
                            self.executor_script_path,
                            allow_structural_test_values=self.allow_structural_test_values,
                        )
                        if (improved.expected_responses or {}).get("comparison_mode") == "field_match":
                            field_mapping = (improved.expected_responses or {}).get("field_mapping")
                            probe_result = compare_response(
                                probe_result, tc.inputs, field_mapping=field_mapping
                            )
                        if probe_result.decision_status.value == tc.expected_decision:
                            method.execution_steps = improved.execution_steps
                            method.expected_responses = improved.expected_responses
                            method.method_type = improved.method_type
                            note = ("[LLM Improvement Applied] Direct rewrite "
                                    "passed the structural test and was adopted.")
                        else:
                            note = ("[LLM Improvement Rejected] Direct rewrite "
                                    f"also failed (got {probe_result.decision_status.value}).")
                    if note is None:
                        note = "[LLM Improvement Skipped] Model returned nothing usable."
                else:
                    note = _attempt_improvement(
                        method, attempt, tc, self.runner, self.executor_script_path
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
