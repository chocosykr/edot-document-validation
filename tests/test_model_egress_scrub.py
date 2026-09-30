"""Model-EGRESS scrubbing.

The frontier model must only ever see the KIND of credential, never a value —
including inside error messages. Two channels bypassed the central scrub in
``utils.llm_client``:

* the healing agent's ``test_method`` tool result, which is fed straight back
  to the model as a tool message from the raw executor output, and
* discovery's ``call_llm``, which has its own ``requests.post`` egress and
  whose prompts carry live page text.

Live evidence (2026-09-30): portal result pages echo the value that was
searched for back into the page body, so an unscrubbed page excerpt or error
string is a real credential leak, not a theoretical one.
"""

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from execution.models import ExecutionDecisionStatus, ExecutionResult
from registry.models import MethodType, ValidationMethod
from utils.log_scrubber import set_active_profile
from validation.models import TestCase

CREDENTIALS = {
    "full_name": "ANUP KAMBOJ",
    "date_of_birth": "07-SEP-1992",
    "document_number": "MUM 179416",
}


def _method():
    return ValidationMethod(
        method_id="M_TEST",
        method_type=MethodType.HTTP,
        source_url="https://mock.example.com",
        country="India",
        document_type="Continuous Discharge Certificate",
    )


class TestHealingToolEgressIsScrubbed(unittest.TestCase):
    """The agentic healing loop's tool result goes back to the model."""

    def setUp(self):
        set_active_profile(CREDENTIALS)
        self.addCleanup(set_active_profile, None)

    def test_tool_result_hides_real_values_even_in_error_text(self):
        from validation.healing import _build_agent_test_tool

        runner = MagicMock()
        runner.execute_method.return_value = ExecutionResult(
            decision_status=ExecutionDecisionStatus.REJECTED,
            evidence={},
            error="lookup failed for MUM 179416 (ANUP KAMBOJ)",
            logs="posting document_number=MUM 179416",
            raw_response="<td>CDC No.</td><td>MUM 179416</td>",
        )
        tc = TestCase(
            name="live",
            inputs={"document_number": "MUM 179416"},
            expected_decision="VERIFIED",
        )

        tool_obj = _build_agent_test_tool(_method(), tc, runner, None)
        call = getattr(tool_obj, "func", None) or tool_obj
        payload = call(
            execution_steps_json="[]",
            expected_responses_json="{}",
            method_type="HTTP",
            script_source="",
        )

        for secret in CREDENTIALS.values():
            self.assertNotIn(secret, payload)
        self.assertIn("\u2039DOCUMENT_NUMBER\u203a", payload)
        self.assertIn("\u2039PERSON_NAME\u203a", payload)


class TestDiscoveryEgressIsScrubbed(unittest.TestCase):
    """Discovery prompts embed live page text that can echo the credential."""

    PAGE_PROMPT = "candidate_page text_excerpt: document_number=MUM 179416 holder ANUP KAMBOJ"

    def setUp(self):
        set_active_profile(CREDENTIALS)
        self.addCleanup(set_active_profile, None)

    def _call(self, frontier: bool) -> str:
        import discovery.llm as discovery_llm

        captured = {}

        class _Resp:
            status_code = 200
            headers = {}

            def json(self):
                return {"choices": [{"message": {"content": "{}"}}]}

            def raise_for_status(self):
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Resp()

        env = {"USE_LOCAL_LLM_ONLY": "false" if frontier else "true"}
        discovery_llm.last_llm_request_at = time.monotonic()
        with patch.dict(os.environ, env, clear=False), patch(
            "discovery.llm.requests.post", side_effect=fake_post
        ):
            discovery_llm.call_llm(self.PAGE_PROMPT)

        return captured["payload"]["messages"][0]["content"]

    def test_frontier_prompt_only_carries_the_kind_of_credential(self):
        sent = self._call(frontier=True)
        for secret in CREDENTIALS.values():
            self.assertNotIn(secret, sent)
        self.assertIn("\u2039DOCUMENT_NUMBER\u203a", sent)
        self.assertIn("\u2039PERSON_NAME\u203a", sent)

    def test_local_prompt_is_not_scrubbed(self):
        """Local runs keep the real values — they never leave the machine."""
        sent = self._call(frontier=False)
        self.assertIn("MUM 179416", sent)


if __name__ == "__main__":
    unittest.main()
