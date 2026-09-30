"""Escalation from HTTP/WEB_FORM/SCRIPT to BROWSER in the healing ladder.

A non-browser method that keeps failing is usually failing on page structure
that a plain request cannot observe. The escalation re-authors the method as a
BROWSER method so the executor can read the real page — and it must PERSIST,
or the next run regenerates the same broken method.
"""

import json
import os
import tempfile
import unittest

from execution.models import ExecutionDecisionStatus, ExecutionResult
from registry.models import MethodStatus, MethodType, ValidationMethod
from registry.repository import MethodRegistry
from validation.healing import (
    _escalate_to_browser,
    _escalation_block,
    _should_escalate_to_browser,
)


def _method(method_type=MethodType.HTTP, steps=None, script=None):
    return ValidationMethod(
        method_id="M_TEST",
        method_type=method_type,
        source_url="http://220.156.189.33/esamudraUI/checkerajaxservlet",
        execution_steps=steps if steps is not None else [
            {"action": "REQUEST", "method": "POST", "url": "http://x/check",
             "params": {"txtNo": "{{document_number}}"}}
        ],
        script_source=script,
        expected_responses={"comparison_mode": "field_match",
                            "field_mapping": {"cdc no.": "document_number"}},
    )


class TestEscalationPolicy(unittest.TestCase):
    def test_first_healing_pass_stays_cheap(self):
        """Attempt 1 is the mechanical rewrite — do not burn it on a browser."""
        for mt in (MethodType.HTTP, MethodType.WEB_FORM, MethodType.SCRIPT):
            self.assertFalse(_should_escalate_to_browser(_method(mt), attempt_num=1))

    def test_later_passes_escalate_non_browser_types(self):
        for mt in (MethodType.HTTP, MethodType.WEB_FORM, MethodType.QR_URL,
                   MethodType.SCRIPT):
            self.assertTrue(_should_escalate_to_browser(_method(mt), attempt_num=2))
            self.assertTrue(_should_escalate_to_browser(_method(mt), attempt_num=3))

    def test_a_browser_method_is_never_re_escalated(self):
        for attempt in (1, 2, 3):
            self.assertFalse(
                _should_escalate_to_browser(_method(MethodType.BROWSER), attempt)
            )

    def test_manual_is_not_escalated(self):
        """MANUAL has no executor at all; a browser would not help."""
        self.assertFalse(
            _should_escalate_to_browser(_method(MethodType.MANUAL), attempt_num=2)
        )


class TestEscalationRewrite(unittest.TestCase):
    def test_switches_type_and_hands_back_the_old_steps(self):
        method = _method(MethodType.HTTP, script="print('unused')")

        note, previous_steps = _escalate_to_browser(method)

        self.assertEqual(method.method_type, MethodType.BROWSER)
        self.assertIsNone(method.script_source)
        # HTTP actions are unknown to the browser executor: submitting them
        # would fail for a reason that has nothing to do with the site.
        self.assertEqual(method.execution_steps, [])
        self.assertEqual(len(previous_steps), 1)
        self.assertIn("HTTP", note)
        self.assertIn("SCRIPT", note)

    def test_keeps_transport_agnostic_response_config(self):
        """comparison_mode / field_mapping describe the RESPONSE, not the
        transport — dropping them would break scoring on the next attempt."""
        method = _method(MethodType.SCRIPT, steps=[], script="print(1)")
        _escalate_to_browser(method)
        self.assertEqual(method.expected_responses["comparison_mode"], "field_match")
        self.assertIn("field_mapping", method.expected_responses)

    def test_prompt_handles_both_languages(self):
        """The agent must be told the BROWSER vocabulary, or it re-emits the
        same failing HTTP steps."""
        from validation.healing import _build_agent_prompt
        from validation.models import ValidationAttempt, AttemptOutcome

        attempt = ValidationAttempt(
            attempt_number=2,
            test_case_name="structural",
            inputs={"document_number": "TEST_STRUCTURAL_001"},
            expected_decision="VERIFIED",
            actual_decision="TECHNICAL_FAILURE",
            outcome=AttemptOutcome.FAILED,
            error="field 'sid_number' not found on page (unexpected structure)",
        )
        method = _method(MethodType.HTTP)

        plain = _build_agent_prompt(method, attempt)
        self.assertIn('method_type "BROWSER"', plain)
        self.assertNotIn("ESCALATION — READ FIRST", plain)

        note, steps = _escalate_to_browser(method)
        escalated = _build_agent_prompt(
            method, attempt, escalated_from=note, context_steps=steps,
        )
        self.assertIn("ESCALATION — READ FIRST", escalated)
        self.assertIn('method_type "BROWSER"', escalated)
        # the old steps appear as context, and are labelled as unusable
        self.assertIn('"REQUEST"', escalated)
        self.assertIn("Do NOT return the steps", escalated)

    def test_escalation_block_is_empty_without_escalation(self):
        self.assertEqual(_escalation_block(None, None), "")


class TestEscalatedBodyPersists(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.registry = MethodRegistry(db_path=self.db_path)
        self.addCleanup(os.remove, self.db_path)

    def test_update_execution_persists_without_touching_status(self):
        method = _method(MethodType.HTTP)
        method.status = MethodStatus.TESTING
        self.registry.register_method(method)
        # the validator promotes the row once it has a passing run
        self.registry.update_status(method.method_id, MethodStatus.ACTIVE)

        _escalate_to_browser(method)
        # the agent then re-authors the body for the browser
        browser_steps = [{"action": "FETCH_FORM", "url": "http://x"}]
        method.execution_steps = browser_steps

        from validation.validator import MethodValidator

        validator = MethodValidator(registry=self.registry)
        validator._persist_execution_body(method)

        stored = self.registry.get_method(method.method_id)
        self.assertEqual(stored.method_type, MethodType.BROWSER)
        self.assertEqual(stored.execution_steps, browser_steps)
        self.assertIsNone(stored.script_source)
        # not demoted, not version-bumped
        self.assertEqual(stored.status, MethodStatus.ACTIVE)
        self.assertEqual(stored.version, method.version)

    def test_a_step_less_escalation_is_not_persisted(self):
        """The escalation empties the steps before the agent re-authors them.
        If the agent produced nothing, do not overwrite the row with an
        unrunnable method."""
        method = _method(MethodType.HTTP)
        self.registry.register_method(method)
        self.registry.update_status(method.method_id, MethodStatus.ACTIVE)

        _escalate_to_browser(method)  # clears the steps

        from validation.validator import MethodValidator

        MethodValidator(registry=self.registry)._persist_execution_body(method)

        stored = self.registry.get_method(method.method_id)
        self.assertEqual(stored.method_type, MethodType.HTTP)
        self.assertTrue(stored.execution_steps)

    def test_no_change_is_not_persisted(self):
        from validation.validator import _execution_body

        method = _method(MethodType.HTTP)
        before = _execution_body(method)
        note, _ = _escalate_to_browser(method)
        self.assertNotEqual(_execution_body(method), before)

    def test_execution_body_is_stable_for_an_unchanged_method(self):
        from validation.validator import _execution_body

        method = _method(MethodType.HTTP)
        method_dict = json.loads(method.model_dump_json())
        other = ValidationMethod(**method_dict)
        self.assertEqual(_execution_body(method), _execution_body(other))


class TestEscalationFiresInTheValidator(unittest.TestCase):
    """End-to-end through MethodValidator: a method that keeps failing on a
    non-browser executor must come out of the ladder as a persisted BROWSER
    method, with the escalation handed to the agent."""

    def setUp(self):
        from unittest.mock import MagicMock

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.registry = MethodRegistry(db_path=self.db_path)
        self.addCleanup(os.remove, self.db_path)
        self.runner = MagicMock()
        self.runner.execute_method.return_value = ExecutionResult(
            decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
            evidence={"method_type": "HTTP"},
            logs="field 'sid_number' not found on page",
            error="field 'sid_number' not found on page (unexpected structure)",
        )

    def test_ladder_escalates_and_persists(self):
        from unittest.mock import patch

        from validation.models import TestCase
        from validation.validator import MAX_ATTEMPTS_PER_TEST_CASE, MethodValidator

        method = _method(MethodType.HTTP)
        self.registry.register_method(method)
        validator = MethodValidator(
            runner=self.runner,
            registry=self.registry,
            executor_script_path="tests/dummy_executor.py",
        )
        test_cases = [TestCase(
            name="structural", inputs={"document_number": "X"},
            expected_decision="VERIFIED",
        )]

        captured = {}

        def fake_agentic(method, attempt, tc, runner, path, **kwargs):
            captured.update(kwargs)
            # simulate the agent authoring browser steps
            method.execution_steps = [{"action": "FETCH_FORM", "url": "http://x"}]
            return "[Agentic Improvement Applied]"

        # attempt 1's cheap rewrite must not be the escalation
        with patch("validation.healing._llm_improve", return_value=None), \
             patch("validation.healing._run_agentic_loop", side_effect=fake_agentic):
            validator.validate(method, test_cases)

        self.assertIn("Browser Escalation", captured.get("escalated_from") or "")
        self.assertTrue(captured.get("context_steps"))
        self.assertEqual(method.method_type, MethodType.BROWSER)

        stored = self.registry.get_method(method.method_id)
        self.assertEqual(stored.method_type, MethodType.BROWSER)
        self.assertEqual(
            stored.execution_steps, [{"action": "FETCH_FORM", "url": "http://x"}]
        )
        # the row must not be demoted back to TESTING by the persistence
        self.assertNotEqual(stored.status, MethodStatus.TESTING)

    def test_only_one_attempt_is_available_when_capped(self):
        """Escalation must not fire on a run with no healing budget."""
        from unittest.mock import patch

        from validation.models import TestCase
        from validation.validator import MethodValidator

        method = _method(MethodType.HTTP)
        validator = MethodValidator(
            runner=self.runner, executor_script_path="tests/dummy_executor.py",
        )
        test_cases = [TestCase(
            name="structural", inputs={"document_number": "X"},
            expected_decision="VERIFIED",
        )]
        with patch("validation.healing._run_agentic_loop") as agentic, \
             patch("validation.healing._llm_improve", return_value=None), \
             patch("validation.validator.MAX_ATTEMPTS_PER_TEST_CASE", 1):
            validator.validate(method, test_cases)

        agentic.assert_not_called()
        self.assertEqual(method.method_type, MethodType.HTTP)


if __name__ == "__main__":
    unittest.main()
