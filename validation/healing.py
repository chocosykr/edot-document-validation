"""LLM-powered healing passes for failed validation attempts.

Two escalation levels:
1. _llm_improve: cheap direct LLM rewrite (test-before-adopt)
2. _attempt_improvement: full agentic tool-use loop
"""

import copy
import json
import logging
from typing import Optional, Tuple

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
        "values; inputs are provided by the runner. "
        "If the declarative step schema cannot express the flow (stateful "
        "token/session handshake, bespoke parsing, incoherent placeholders "
        "you cannot reconcile), you MAY return method_type \"SCRIPT\" with a "
        "`script_source` Python program instead of execution_steps. A SCRIPT "
        "imports only `from dvs_io import get_input, http_get, http_post, "
        "write_result`; it MUST read every required input via "
        "get_input(\"name\") and finish with write_result(status, "
        "raw_response=..., evidence=...). "
        "If the failure indicates the target site cannot be driven this way "
        "at all, return the single word UNFIXABLE."
    )
    from utils.log_scrubber import scrub_pii
    user = (
        "METHOD:\n" + scrub_pii(method.model_dump_json(indent=2)) +
        "\n\nFAILURE EVIDENCE:\n" + scrub_pii(_json.dumps({
            "expected_decision": attempt.expected_decision,
            "actual_decision": attempt.actual_decision,
            "error": attempt.error,
            "logs_tail": (attempt.logs or "")[-800:],
            "raw_response_head": (attempt.raw_response or "")[:1200],
        }, indent=2))
    )

    result = generate_json(system, user)
    if not result:
        return None
    script_src = str(result.get("script_source") or "").strip()
    if not script_src and "execution_steps" not in result:
        return None
    try:
        improved = copy.deepcopy(method)
        if script_src:
            improved.method_type = MethodType.SCRIPT
            improved.script_source = result["script_source"]
            improved.execution_steps = []
        else:
            improved.execution_steps = result["execution_steps"]
            improved.script_source = None
            if result.get("method_type"):
                improved.method_type = MethodType(result["method_type"])
        if result.get("expected_responses"):
            merged = dict(method.expected_responses or {})
            merged.update(result["expected_responses"])
            improved.expected_responses = merged
        return improved
    except Exception as e:
        logger.warning("LLM improvement produced invalid method: %s", e)
        return None


_BROWSER_DOM_EXCERPT = 6_000


def _browser_evidence(exec_result) -> dict:
    """Condense the browser executor's page evidence for the healing model.

    Text only: DOM, console, and the request/response trace. Any saved
    screenshots are reported by PATH — the files exist so a human (or a
    later multimodal step) can look at the real page; they are never inlined
    into this payload. Screenshots are captured pre-fill only, so a path
    never points at an image containing a credential value.
    """
    evidence = exec_result.evidence if isinstance(exec_result.evidence, dict) else {}
    diagnostics = evidence.get("browser_diagnostics") or {}
    screenshots = evidence.get("screenshot_paths") or []
    if not diagnostics and not screenshots:
        return {}

    bundle = {
        "screenshots_saved": list(screenshots),
        "dom_excerpt": str(diagnostics.get("dom") or "")[:_BROWSER_DOM_EXCERPT],
        "console": list(diagnostics.get("console") or []),
        "network": list(diagnostics.get("network") or []),
    }
    if screenshots:
        bundle["screenshots_note"] = (
            "PNG files of the page state BEFORE any credential was typed. "
            "Read the DOM/network below for the post-submit state."
        )
    return bundle


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
    def test_method(
        execution_steps_json: str = "[]",
        expected_responses_json: str = "{}",
        method_type: str = "",
        script_source: str = "",
    ) -> str:
        """
        Tests a candidate method payload against the live API.

        Provide EITHER execution_steps_json (+ method_type) for a declarative
        method, OR script_source for a SCRIPT method. A SCRIPT is Python that
        imports `from dvs_io import get_input, http_get, http_post, write_result`,
        reads each required input with get_input("..."), and ends with
        write_result(status, raw_response=..., evidence=...).
        expected_responses_json is always required. Returns the execution logs,
        raw response, and whether the candidate produced the expected decision.

        For method_type BROWSER the result also carries `browser_evidence`:
        the rendered page's DOM excerpt, its browser console entries, the
        request/response trace, and the path(s) of any PNG screenshots saved
        for this run. Use it instead of guessing why a step failed. The
        screenshots are pre-fill only, so they never show credential values.
        """
        test_m = copy.deepcopy(method)
        try:
            test_m.expected_responses = json.loads(expected_responses_json or "{}")
            if str(script_source or "").strip():
                test_m.method_type = MethodType.SCRIPT
                test_m.script_source = script_source
                test_m.execution_steps = []
            else:
                test_m.execution_steps = json.loads(execution_steps_json or "[]")
                if method_type:
                    test_m.method_type = MethodType(method_type)
        except Exception as e:
            return f"Error parsing inputs: {e}"

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

        # Browser methods return what the rendered page actually was. Without
        # it a fix is a guess at structure the DOM never revealed in the logs
        # (renamed field, cookie wall, error panel after submit).
        browser_evidence = _browser_evidence(exec_result)
        if browser_evidence:
            res["browser_evidence"] = browser_evidence

        # MODEL EGRESS: this tool result is fed straight back to the healing
        # model as a tool message, bypassing the scrubbing every other prompt
        # gets in utils.llm_client. `error` and `raw_response` are the raw
        # executor outputs, so a frontier model would otherwise receive real
        # credential values — in error text too. Scrub before returning.
        from utils.log_scrubber import scrub_pii
        return scrub_pii(json.dumps(res, indent=2))

    return test_method


def _escalation_block(
    escalated_from: Optional[str], context_steps: Optional[list]
) -> str:
    """Prompt section telling the agent it must produce a BROWSER method."""
    if not escalated_from:
        return ""
    steps = json.dumps(context_steps or [], indent=2, default=str)[:2000]
    return f"""
ESCALATION — READ FIRST:
{escalated_from}

This method MUST now be authored as method_type "BROWSER" and driven with a
real browser. Reuse the SAME source_url: load the page, fill the fields the
document provides, submit, and read the result page. Do NOT return the steps
below — they belong to the executor that already failed. They are shown only
so you do not repeat the approach:
{steps}
"""


def _build_agent_prompt(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    escalated_from: Optional[str] = None,
    context_steps: Optional[list] = None,
) -> str:
    """Build the prompt for the agentic healing agent."""
    prompt = f"""
You are an expert automation engineer fixing a broken web scraper / API integration.
The generated method failed during validation.
{_escalation_block(escalated_from, context_steps)}
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

For method_type "BROWSER" (a real Playwright browser — use when the site
cannot be driven by a plain request: JS-rendered forms, session handshakes,
SPA lookups, or when HTTP/WEB_FORM/SCRIPT has already failed):
  - FETCH_FORM (alias NAVIGATE): navigates to a URL. Fields: "url".
  - SOLVE_CAPTCHA: detects and solves a simple text CAPTCHA. It MUST come
    before any FILL; the executor enforces this ordering.
  - FILL: fills a field on the page. Fields: "field" (the input's name/id —
    read it from the page evidence, do not guess), "value" (use
    {{{{{{input_name}}}}}} placeholders).
  - SUBMIT: submits the form and reads the result page.
  A BROWSER test returns "browser_evidence" (DOM, console, network trace and
  any saved screenshots). READ IT to learn the real page structure before
  writing steps.
  - SUBMIT: Submits the form. No fields needed.

For method_type "SCRIPT" (last-resort escape hatch):
  Pass `script_source` instead of execution_steps to test_method. The program
  imports `from dvs_io import get_input, http_get, http_post, write_result`,
  reads each required input with get_input("canonical_name"), and ends with
  write_result(status, raw_response=..., evidence=...). Use SCRIPT only when
  the declarative steps genuinely cannot express the flow.

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
1. Use the test_method tool to iteratively try execution_steps (or script_source) until you get "passed": true.
2. Once test_method returns passed: true, output a final JSON block enclosed in ```json ... ``` with the winning method_type plus either execution_steps or script_source, and expected_responses.

Only output the final JSON when you have successfully tested it and got passed: true.
    """.strip()
    from utils.log_scrubber import scrub_pii
    return scrub_pii(prompt)


# Method types that drive a site without a browser. When one of them keeps
# failing, the page's real structure is usually the thing that is missing
# (a renamed field, a JS-rendered form, a cookie wall) — and only the BROWSER
# executor can observe it and report it back through browser_evidence.
_NON_BROWSER_TYPES = {
    MethodType.HTTP, MethodType.WEB_FORM, MethodType.QR_URL, MethodType.SCRIPT
}


def _should_escalate_to_browser(method: ValidationMethod, attempt_num: int) -> bool:
    """Escalate once the cheaper method types have demonstrably had their shot.

    Only from the second healing pass onward: attempt 1 is the cheap
    mechanical rewrite (wrong endpoint, wrong param name), which fixes most
    failures for a fraction of the cost. By the time the agentic pass runs,
    a non-browser method that still fails is failing on structure.
    """
    return method.method_type in _NON_BROWSER_TYPES and attempt_num >= 2


def _escalate_to_browser(method: ValidationMethod) -> Tuple[str, list]:
    """Switch a failing non-browser method to a browser method.

    Returns ``(note, previous_steps)``. The steps are REMOVED from the method
    and handed to the agent as context only: submitting HTTP/SCRIPT actions to
    the browser executor is guaranteed to fail (every action is unknown to it),
    so a clean "no steps yet" state is better than a method that looks
    configured but cannot run.

    ``expected_responses`` is deliberately kept — comparison_mode, field_mapping
    and not_found_signatures describe the RESPONSE and are transport-agnostic.
    """
    previous_type = method.method_type.value
    previous_steps = list(method.execution_steps or [])
    had_script = bool((method.script_source or "").strip())

    method.method_type = MethodType.BROWSER
    method.script_source = None
    method.execution_steps = []

    note = (
        f"[Browser Escalation] {previous_type}"
        + ("+SCRIPT" if had_script else "")
        + " could not be driven — re-authoring as a BROWSER method against "
        "the real page."
    )
    return note, previous_steps


def _run_agentic_loop(
    method: ValidationMethod,
    attempt: ValidationAttempt,
    tc: TestCase,
    runner: DockerMethodRunner,
    executor_script_path: Optional[str],
    escalated_from: Optional[str] = None,
    context_steps: Optional[list] = None,
) -> Optional[str]:
    """
    Spins up an Agentic Loop to iteratively test and fix the method until it works.

    ``escalated_from`` / ``context_steps`` describe the executor this method
    was just escalated away from, so the agent can see what was tried instead
    of re-deriving it.
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

    prompt = _build_agent_prompt(
        method, attempt, escalated_from=escalated_from,
        context_steps=context_steps,
    )

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
            if str(improved.get("method_type") or "").upper() == "SCRIPT" and str(
                improved.get("script_source") or ""
            ).strip():
                method.method_type = MethodType.SCRIPT
                method.script_source = improved["script_source"]
                method.execution_steps = []
            elif "execution_steps" in improved:
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
                method.script_source = improved.script_source
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
        escalation_note = None
        context_steps = None
        if _should_escalate_to_browser(method, attempt_num):
            escalation_note, context_steps = _escalate_to_browser(method)
            logger.info(
                "%s (method %s, attempt %d/%d)",
                escalation_note, method.method_id, attempt_num, max_attempts,
            )
        note = _run_agentic_loop(
            method, attempt, tc, runner, executor_script_path,
            escalated_from=escalation_note,
            context_steps=context_steps,
        )
        if escalation_note:
            note = f"{escalation_note} {note}"
    return note
