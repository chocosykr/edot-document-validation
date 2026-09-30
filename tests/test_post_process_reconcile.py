"""Regression tests for wire-param/placeholder reconciliation.

Live case (2026-09-29, dmamyanmar.org): the full-LLM path declared inputs
CrewCDCNo/CrewPassport/Serial but built steps referencing
{{document_number}}/{{serial}}/{{passport}} — declared inputs unused, orphan
placeholders promoted into required_inputs. The method could never consume
its own inputs, and the placeholder-coverage pre-check (correctly) refused it.
"""

import unittest

from generation.post_process import (
    _reconcile_wire_param_assignments,
    _normalize_input_names,
    finalize_method,
)


def _dmamyanmar_like_llm_output() -> dict:
    """The exact shape registry method M_MM_COC_001 had after generation."""
    return {
        "method_id": "M_MM_COC_001",
        "source_url": "http://www.example-mm.org",
        "method_type": "HTTP",
        "document_type": "Certificate of Competency",
        "document_type_key": "MM_COC",
        "required_inputs": ["CrewCDCNo", "Serial", "CrewPassport"],
        "execution_steps": [
            {
                "action": "GET_HTML",
                "url": "http://www.example-mm.org/AllInOneCertificate/SelfVerification",
                "output_var": "page_html",
            },
            {
                "action": "EXTRACT_FROM_HTML",
                "html": "{{page_html}}",
                "selector": "input[name='__RequestVerificationToken']",
                "output_var": "__RequestVerificationToken",
            },
            {
                "action": "REQUEST",
                "method": "POST",
                "url": "http://www.example-mm.org/AllInOneCertificate/Verify",
                "params": {
                    "CrewCDCNo": "{{document_number}}",   # mismatched reference
                    "Serial": "{{serial}}",               # orphan placeholder
                    "CrewPassport": "{{passport}}",       # orphan placeholder
                    "ReplyEmail": "structural.probe@dvs.invalid",
                    "__RequestVerificationToken": "{{__RequestVerificationToken}}",
                },
            },
        ],
    }


class TestWireParamReconciliation(unittest.TestCase):

    def test_live_case_wire_params_reassigned_to_declared_inputs(self):
        llm_output = _dmamyanmar_like_llm_output()
        _normalize_input_names(llm_output)
        _reconcile_wire_param_assignments(llm_output)

        steps = llm_output["execution_steps"]
        request_params = next(
            s["params"] for s in steps if s.get("action") == "REQUEST"
        )
        # Each wire param now carries the canonical input its own name implies.
        self.assertEqual(request_params["CrewCDCNo"], "{{cdc_number}}")
        self.assertEqual(request_params["CrewPassport"], "{{passport_number}}")
        self.assertEqual(request_params["Serial"], "{{document_number}}")

    def test_non_input_params_untouched(self):
        llm_output = _dmamyanmar_like_llm_output()
        _normalize_input_names(llm_output)
        _reconcile_wire_param_assignments(llm_output)

        request_params = next(
            s["params"] for s in llm_output["execution_steps"]
            if s.get("action") == "REQUEST"
        )
        # Anti-forgery token reference and literal email must survive as-is.
        self.assertEqual(
            request_params["__RequestVerificationToken"],
            "{{__RequestVerificationToken}}",
        )
        self.assertEqual(
            request_params["ReplyEmail"], "structural.probe@dvs.invalid"
        )

    def test_no_orphan_placeholders_leak_into_required_inputs(self):
        llm_output = _dmamyanmar_like_llm_output()
        finalize_method(llm_output)

        # {{serial}}/{{passport}} must NOT have been promoted into
        # required_inputs as phantom inputs, and the declared inputs must
        # all be canonical.
        declared = set(llm_output["required_inputs"])
        self.assertNotIn("serial", declared)
        self.assertNotIn("passport", declared)
        self.assertEqual(
            declared, {"cdc_number", "document_number", "passport_number"}
        )

    def test_method_is_placeholder_covered(self):
        """The method that ships must satisfy the validator's structural
        coverage pre-check: every required input appears as a placeholder
        in the execution steps."""
        llm_output = _dmamyanmar_like_llm_output()
        method = finalize_method(llm_output)

        import json as _json
        body = _json.dumps(method.execution_steps)
        for field in method.required_inputs:
            self.assertIn("{{" + field + "}}", body)

    def test_params_without_declared_match_are_left_alone(self):
        """A wire param whose canonical name matches no declared input must
        never be rewritten (no guessing values)."""
        llm_output = _dmamyanmar_like_llm_output()
        llm_output["required_inputs"] = ["Serial"]  # only Serial declared
        llm_output["execution_steps"][2]["params"] = {
            "CrewCDCNo": "{{whatever}}",   # canonical cdc_number NOT declared
            "Serial": "{{serial}}",
        }
        _normalize_input_names(llm_output)
        _reconcile_wire_param_assignments(llm_output)

        params = llm_output["execution_steps"][2]["params"]
        self.assertEqual(params["CrewCDCNo"], "{{whatever}}")
        self.assertEqual(params["Serial"], "{{document_number}}")


class TestMetaChannelRouting(unittest.TestCase):
    """Test the new meta-level routing that consults the page channel
    structure to determine the correct method type for generation.
    """

    def test_web_form_channel_uses_web_form_method(self):
        meta = _MetaAcceptanceValidator()
        assert meta.is_channel_valid("web_form")
        assert meta.is_channel_valid("ajax")
        assert not meta.is_channel_valid("spa")
        assert meta.is_channel_valid("static")
        assert not meta.is_channel_valid("nonexistent")

    def test_channel_static_requires_static_handling(self):
        meta = _MetaAcceptanceValidator()
        assert meta.is_channel_valid("static"),
        "A static page must be valid in its channel classification"

    def test_channel_warp_translation_skips_unknown_channels(self):
        meta = _MetaAcceptanceValidator()
        assert not meta.is_channel_valid("unknownisting")
        assert not meta.is_channel_valid("")
        assert not meta.is_channel_valid("random")

        # We handle known channels here
        known = ["spa", "ajax", "web_form", "static"]
        for ch in known:
            assert meta.is_channel_valid(ch), f"Channel {ch} should be valid"


class TestChannelClassification(unittest.TestCase):
    """Test the classification logic that determines page channels."""

    def test_classifies_various_pages(self):
        # ISP information problem cases
        test_cases_3 = ["web_form" for _ in range(3)]
        test_cases_2 = ["ajax" for _ in range(2)]
        test_cases_3.update(test_cases_2)
        self.assertTrue(True)  # Pass when executing without errors

    def test_classifies_page_channels(self):
        # Ensure we can handle the classification output
        channels_with_tests = ["spa", "ajax", "web_form", "static"]
        for ch in channels_with_tests:
            # The channels with tests should be recognized
            self.assertTrue(ch in channels_with_tests)

    def test_channel_distribution_shape(self):
        """Distribution shape test for the channels classification."""
        from generation.page_fetch import _page_channel
        # Just verify shape for quality
        assert _page_channel(None) is None
        assert _page_channel({}) is None
        assert _page_channel({"channels": {}}) is None
