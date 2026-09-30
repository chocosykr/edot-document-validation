"""Browser executor evidence bundle for the healing loop.

The healing agent can only fix what it can see. These tests pin two things:

* the PRE-FILL screenshot gate — once a credential has been typed into the
  page, no image may be captured, because an image cannot be scrubbed the way
  text can; and
* the plumbing that carries the bundle from the executor, through the runner's
  temp-dir cleanup, to the model-facing tool result (where it is scrubbed).
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from execution.models import ExecutionDecisionStatus, ExecutionResult
from registry.models import MethodType, ValidationMethod
from utils.log_scrubber import set_active_profile
from validation.models import TestCase

import executors.browser_executor as browser


class _FakePage:
    """Minimal stand-in for a Playwright Page."""

    def __init__(self, html="<html><input name='sid_number'></html>", png=b"PNGDATA"):
        self._html = html
        self._png = png
        self.screenshot_calls = 0

    def content(self):
        return self._html

    def screenshot(self, **kwargs):
        self.screenshot_calls += 1
        return self._png


class TestScreenshotGate(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="dvs_shot_")
        os.chdir(self._tmp)
        self.addCleanup(os.chdir, self._cwd)

    def test_pre_fill_screenshot_is_written_to_disk(self):
        page = _FakePage()
        diagnostics = browser._new_diagnostics()

        browser._snapshot_page(page, diagnostics, attempt=1, allow_screenshot=True)

        self.assertEqual(page.screenshot_calls, 1)
        self.assertEqual(diagnostics["screenshots"], ["screenshot_a1.png"])
        with open("screenshot_a1.png", "rb") as f:
            self.assertEqual(f.read(), b"PNGDATA")
        self.assertIn("sid_number", diagnostics["dom"])

    def test_no_screenshot_once_credentials_are_on_the_page(self):
        """The gate: after a FILL, only text is recorded."""
        page = _FakePage()
        diagnostics = browser._new_diagnostics()

        browser._snapshot_page(page, diagnostics, attempt=2, allow_screenshot=False)

        self.assertEqual(page.screenshot_calls, 0)
        self.assertEqual(diagnostics["screenshots"], [])
        self.assertFalse(os.path.exists("screenshot_a2.png"))
        # the DOM is still captured — it is text, and the caller scrubs it
        self.assertIn("sid_number", diagnostics["dom"])


class TestThrowawayTempDirDoesNotLoseScreenshots(unittest.TestCase):
    def test_runner_salvages_screenshots_before_deleting_the_workspace(self):
        from execution.docker_runner import _salvage_screenshots

        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, "screenshot_a1.png"), "wb") as f:
                f.write(b"PNGDATA")
            with open(os.path.join(temp_dir, "output.json"), "w") as f:
                f.write("{}")

            saved = _salvage_screenshots(temp_dir, "M_TEST")

        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].endswith(".png"))
        self.assertIn("probe_evidence", saved[0])

        # The file must really exist at the path that was returned, and the
        # temp dir must be gone (the runner's `finally` does that).
        from execution.docker_runner import _PROJECT_ROOT

        self.assertTrue(os.path.exists(os.path.join(_PROJECT_ROOT, saved[0])))
        os.remove(os.path.join(_PROJECT_ROOT, saved[0]))

    def test_no_screenshots_is_not_an_error(self):
        from execution.docker_runner import _salvage_screenshots

        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, "output.json"), "w") as f:
                f.write("{}")
            self.assertEqual(_salvage_screenshots(temp_dir, "M_TEST"), [])

        self.assertEqual(_salvage_screenshots("/nonexistent/path", "M_TEST"), [])


class TestHealingToolSeesTheBundle(unittest.TestCase):
    CREDENTIALS = {"document_number": "MUM 179416", "full_name": "ANUP KAMBOJ"}

    def _tool(self, evidence):
        from validation.healing import _build_agent_test_tool

        runner = MagicMock()
        runner.execute_method.return_value = ExecutionResult(
            decision_status=ExecutionDecisionStatus.TECHNICAL_FAILURE,
            evidence=evidence,
            error="field 'sid_number' not found on page (unexpected structure)",
        )
        method = ValidationMethod(
            method_id="M_TEST",
            method_type=MethodType.BROWSER,
            source_url="https://mock.example.com",
        )
        tc = TestCase(
            name="live",
            inputs={"document_number": "MUM 179416"},
            expected_decision="VERIFIED",
        )
        return _build_agent_test_tool(method, tc, runner, None)

    def _call(self, tool_obj):
        call = getattr(tool_obj, "func", None) or tool_obj
        return call(
            execution_steps_json="[]",
            expected_responses_json="{}",
            method_type="BROWSER",
            script_source="",
        )

    def test_bundle_reaches_the_model_and_is_scrubbed(self):
        set_active_profile(self.CREDENTIALS)
        self.addCleanup(set_active_profile, None)

        tool_obj = self._tool({
            "browser_diagnostics": {
                # The live page echoes the searched value back into its DOM.
                "dom": "<td>CDC No.</td><td>MUM 179416</td><td>ANUP KAMBOJ</td>",
                "console": ["error: TypeError: cannot read 'value' of null"],
                "network": [{
                    "method": "POST",
                    "url": "http://x/check?txtNo=MUM%20179416",
                    "status": 200,
                    "resource_type": "xhr",
                    "content_type": "application/json",
                }],
                "screenshots": ["screenshot_a1.png"],
            },
            "screenshot_paths": ["probe_evidence/browser/M_TEST_1_screenshot_a1.png"],
        })
        payload = self._call(tool_obj)

        # scrubbed before the model sees it, in every nested string
        self.assertNotIn("MUM 179416", payload)
        self.assertNotIn("MUM%20179416", payload)
        self.assertNotIn("ANUP KAMBOJ", payload)
        self.assertIn("\u2039DOCUMENT_NUMBER\u203a", payload)
        self.assertIn("\u2039PERSON_NAME\u203a", payload)

        parsed = json.loads(payload)
        bundle = parsed["browser_evidence"]
        self.assertEqual(
            bundle["screenshots_saved"],
            ["probe_evidence/browser/M_TEST_1_screenshot_a1.png"],
        )
        self.assertEqual(bundle["network"][0]["status"], 200)
        self.assertTrue(any("TypeError" in line for line in bundle["console"]))

    def test_no_bundle_for_methods_without_one(self):
        tool_obj = self._tool({})
        parsed = json.loads(self._call(tool_obj))
        self.assertNotIn("browser_evidence", parsed)


class TestSnapshotOnFailure(unittest.TestCase):
    """A failed attempt's page state must not be thrown away."""

    def test_shared_diagnostics_survive_a_failing_attempt(self):
        import inspect

        sig = inspect.signature(browser.run_attempt)
        # The caller owns the bundle so a later attempt cannot drop a failed
        # attempt's evidence.
        self.assertIn("diagnostics", sig.parameters)
        self.assertIn("attempt", sig.parameters)

        source = inspect.getsource(browser.run_attempt)
        self.assertIn("sys.exc_info()", source)
        self.assertIn("allow_screenshot=not _credentials_filled", source)


if __name__ == "__main__":
    unittest.main()
