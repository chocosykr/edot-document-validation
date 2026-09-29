"""LLM-powered healing passes for failed validation attempts.

Two escalation levels:
1. _llm_improve: cheap direct LLM rewrite (test-before-adopt)
2. _attempt_improvement: full agentic tool-use loop
"""

import copy
import json
import logging
from typing import Optional

from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import MethodType, ValidationMethod
from validation.field_comparison import compare_response
from validation.models import TestCase, ValidationAttempt

logger = logging.getLogger(__name__)


def _llm_improve(
    method: ValidationMethod,
    attempt: ValidationAttempt,
) -> Optional[ValidationMethod]:
    """One direct LLM rewrite pass for a failed structural test.

    Deterministic-first design: the agentic loop below can burn many minutes;
    this is the cheap first response to a failure. Sends the full method and
    the failure evidence, expects the standard ```json {method_type,
    execution_steps, expected_responses}``` block back. Returns a NEW method
    (never mutates the candidate) or None on any failure.
    """
    from utils.llm_client import generate_json
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


def _build_agent_test_tool(
    method: ValidationMethod,
    tc: TestCase,
    runner: DockerMethodRunner,
    executor_script_path: Optional[str],
):
    """Build the test_method tool for the agentic healing agent."""
    from langchain_core.tools import tool
    import json

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
            test_m.method_type = MethodType(method_type)
        except Exception as e:
            return f"Error parsing JSON inputs: {e}"

        req = ExecutionRequest(method=test_m, inputs=tc.inputs)
        exec_result = runner.execute_method(
            req, executor_script_path, allow_structural_test_values=True
        )

        # compare
        if (test_m.expected_responses or {}).get("comparison_mode") == "field_match":
            field_mapping = (test_m.expected_responses or {}).get("field_mapping")
            exec_result = compare_response(
                exec_result, tc.inputs, field_mapping=field_mapping,
                not_found_signatures=(test_m.expected_responses or {}).get("not_found_signatures"),
            )

        actual = exec_result.decision_status.value
        passed = actual == tc.expected_decision

        res = {
            "passed": passed,
            "actual_decision": actual,
            "expected_decision": tc.expected_decision,
            "error": exec_result.error,
            "logs": (exec_result.logs or "")[-1000:] if exec_result.logs else "",
            "raw_response": (exec_result.raw_response or "")[:1000]
        }
        return json.dumps(res, indent=2)

    return test_method


def _build_agent_prompt(
    method: ValidationMethod,
    attempt: ValidationAttempt,
) -> str:
    """Build the prompt for the agentic healing agent."""
    return f"""
You are an expert automation engineer fixing a broken web scraper / API integration.
The generated method failed during validation.

Current Method:
{method.model_dump_json(indent=2)}

Latest Failure Details:
- Test Case: {attempt.test_case_name}
- Inputs: {attempt.inputs}
- Error: {attempt.error}
- Logs: {(attempt.logs or "")[-1000:] if attempt.logs else ""}
- Raw Response: {(attempt.raw_response or "")[:1000]}

EXECUTOR REFERENCE (execution_steps actions):

For method_type "HTTP":
  - FETCH_CAPTCHA: Fetches a CAPTCHA image from an API.
    Fields: "url" (captcha API endpoint), "response_format" ("json"), "image_field" (JSON key for base64 image), "id_field" (JSON key for captcha ID)
  - SOLVE_CAPTCHA: Solves the fetched CAPTCHA using vision. No fields needed.
    After this step, {{captcha_text}} and {{captcha_id}} are available as template variables.
  - REQUEST: Makes an HTTP request.
    Fields: "method" ("GET" or "POST"), "url" (full endpoint URL)
    For GET: use "params" dict — they become URL query parameters.
    For POST with JSON body: use "json_body" dict (Content-Type: application/json).
    For POST with form-encoded body: use "params" dict (Content-Type: application/x-www-form-urlencoded).
    For POST with query string params: use "params" dict AND set "param_location": "query".
    Use {{{{{{input_name}}}}}} placeholders for dynamic values.

For method_type "WEB_FORM":
  - FETCH_FORM: Fetches an HTML page and parses forms. Fields: "url".
  - FILL: Fills a form field. Fields: "field" (input name/id), "value" (use {{{{{{input_name}}}}}} placeholders).
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
    """.strip()


def _run_agentic_loop(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    tc: TestCase,
    runner: DockerMethodRunner,
    executor_script_path: Optional[str],
) -> Optional[str]:
    """
    Spins up an Agentic Loop to iteratively test and fix the method until it works.
    """
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import HumanMessage
    from utils.llm_client import get_llm_config

    logger.info("Starting Agentic Self-Healing Loop for method %s", method.method_id)

    config = get_llm_config()
    llm = ChatOpenAI(
        model=config.get("model", "AI_Local"),
        api_key=config.get("api_key", "dummy"),
        base_url=config.get("url", "https://ai.edot-solutions.com/v1").replace("/chat/completions", ""),
    )

    test_tool = _build_agent_test_tool(method, tc, runner, executor_script_path)
    agent = create_react_agent(llm, [test_tool])

    prompt = _build_agent_prompt(method, attempt)

    try:
        final_state = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]}, config={"recursion_limit": 10}
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


def escalate_improvement(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    tc: TestCase,
    runner: DockerMethodRunner,
    executor_script_path: Optional[str],
    attempt_num: int,
    max_attempts: int,
    agentic_healing: bool = True,
) -> Optional[str]:
    """Run the appropriate healing pass for the current attempt number.

    Attempt 1: cheap direct LLM rewrite (test-before-adopt).
    Later attempts: full agentic tool-use loop (if enabled).
    Returns a note describing what happened, or None.
    """
    note = None
    if attempt_num == 1:
        improved = _llm_improve(method, attempt)
        if improved is not None:
            probe_req = ExecutionRequest(method=improved, inputs=tc.inputs)
            probe_result = runner.execute_method(
                probe_req,
                executor_script_path,
                allow_structural_test_values=True,
            )
            if (improved.expected_responses or {}).get("comparison_mode") == "field_match":
                field_mapping = (improved.expected_responses or {}).get("field_mapping")
                probe_result = compare_response(
                    probe_result, tc.inputs, field_mapping=field_mapping,
                    not_found_signatures=(improved.expected_responses or {}).get("not_found_signatures"),
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
    elif not agentic_healing:
        note = "[Agentic Improvement Disabled] DVS_AGENTIC_HEALING=0."
    else:
        note = _run_agentic_loop(
            method, attempt, tc, runner, executor_script_path
        )
    return note
