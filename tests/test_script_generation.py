"""Tests for LLM-authored SCRIPT methods (Phase 4 authoring path).

The full-LLM generation fallback forwards the model's JSON to
``generation.post_process.finalize_method``. These tests pin the SCRIPT branch:
script_source acceptance, canonical get_input normalization/coverage, and the
healing rewrite.
"""

import unittest
from unittest.mock import patch

from generation.generator import generate_candidate_method
from generation.post_process import finalize_method
from registry.models import MethodType, ValidationMethod
from validation.healing import _llm_improve
from validation.models import AttemptOutcome, ValidationAttempt

_SCRIPT = (
    "from dvs_io import get_input, write_result\n"
    'serial = get_input("Serial")\n'
    'write_result("REJECTED", raw_response="VerificationError",\n'
    '             evidence={"serial": serial})\n'
)


def _script_llm_output(**overrides) -> dict:
    base = {
        "method_id": "M_SCRIPT_GEN",
        "method_type": "SCRIPT",
        "source_url": "http://www.dmamyanmar.org/AllInOneCertificate/SelfVerification",
        "document_type": "Certificate of Competency",
        "country": "Myanmar",
        "required_inputs": ["Serial"],
        "script_source": _SCRIPT,
        "expected_responses": {"failure_keywords": ["verificationerror"]},
    }
    base.update(overrides)
    return base


class TestFinalizeScriptMethod(unittest.TestCase):
    def test_script_method_is_built_with_script_source(self):
        method = finalize_method(_script_llm_output())
        self.assertEqual(method.method_type, MethodType.SCRIPT)
        self.assertIn("get_input", method.script_source)
        self.assertEqual(method.execution_steps, [])
        self.assertEqual(method.script_runtime, "python3")

    def test_non_canonical_get_input_is_rewritten(self):
        method = finalize_method(_script_llm_output())
        # Serial -> document_number, both declared and read.
        self.assertEqual(method.required_inputs, ["document_number"])
        self.assertIn('get_input("document_number")', method.script_source)
        self.assertNotIn("get_input(\"Serial\")", method.script_source)

    def test_undeclared_get_input_is_promoted(self):
        out = _script_llm_output(
            required_inputs=["document_number"],
            script_source=(
                "from dvs_io import get_input, write_result\n"
                'a = get_input("document_number")\n'
                'b = get_input("passport_number")\n'
                'write_result("REJECTED", raw_response="VerificationError")\n'
            ),
        )
        method = finalize_method(out)
        self.assertEqual(method.required_inputs, ["document_number", "passport_number"])

    def test_script_without_source_is_rejected(self):
        out = _script_llm_output()
        out.pop("script_source")
        with self.assertRaises(RuntimeError):
            finalize_method(out)

    def test_script_without_required_inputs_is_rejected(self):
        out = _script_llm_output(required_inputs=[])
        # The script reads Serial -> promoted, so this must NOT raise. It
        # proves promotion also satisfies the non-empty required_inputs guard.
        method = finalize_method(out)
        self.assertEqual(method.required_inputs, ["document_number"])


class TestHealingScriptRewrite(unittest.TestCase):
    def _broken_method(self) -> ValidationMethod:
        return ValidationMethod(
            method_id="M_BROKEN",
            method_type=MethodType.HTTP,
            source_url="http://www.dmamyanmar.org/AllInOneCertificate/SelfVerification",
            required_inputs=["document_number"],
            execution_steps=[
                {"action": "REQUEST", "method": "POST",
                 "url": "http://www.dmamyanmar.org/AllInOneCertificate/Verify",
                 "params": {"Serial": "{{serial}}"}},
            ],
        )

    def _attempt(self) -> ValidationAttempt:
        return ValidationAttempt(
            attempt_number=1,
            test_case_name="structural",
            inputs={"document_number": "TEST_STRUCTURAL_001"},
            expected_decision="REJECTED",
            actual_decision="TECHNICAL_FAILURE",
            outcome=AttemptOutcome.ERROR,
            error="Structural coverage failure: required_inputs ['document_number'] "
                  "never appear as {placeholder} in execution_steps.",
        )

    def test_llm_improve_can_return_a_script(self):
        rewritten = {
            "method_type": "SCRIPT",
            "script_source": _SCRIPT,
            "expected_responses": {},
        }
        with patch("utils.llm_client.generate_json", return_value=rewritten):
            improved = _llm_improve(self._broken_method(), self._attempt())

        self.assertIsNotNone(improved)
        self.assertEqual(improved.method_type, MethodType.SCRIPT)
        self.assertEqual(improved.script_source, _SCRIPT)
        self.assertEqual(improved.execution_steps, [])

    def test_llm_improve_keeps_step_rewrites_working(self):
        rewritten = {
            "method_type": "HTTP",
            "execution_steps": [
                {"action": "REQUEST", "method": "POST",
                 "url": "http://www.dmamyanmar.org/AllInOneCertificate/Verify",
                 "params": {"Serial": "{{document_number}}"}},
            ],
            "expected_responses": {},
        }
        with patch("utils.llm_client.generate_json", return_value=rewritten):
            improved = _llm_improve(self._broken_method(), self._attempt())

        self.assertEqual(improved.method_type, MethodType.HTTP)
        self.assertIsNone(improved.script_source)
        self.assertEqual(len(improved.execution_steps), 1)


class TestGeneratorScriptFallback(unittest.TestCase):
    """When declarative generation cannot consume its inputs, the generator
    asks the model once more for a SCRIPT method."""

    _PROFILE = {"document_type": "Certificate of Competency", "issuing_country": "Myanmar"}
    _SOURCE = {"source_url": "http://example.test", "issuer": "DMA"}

    _INCOHERENT = {
        "method_id": "M_FIRST",
        "method_type": "HTTP",
        "source_url": "http://example.test",
        "required_inputs": ["document_number"],
        "execution_steps": [
            {"action": "REQUEST", "method": "POST",
             "url": "http://example.test/verify",
             "params": {"txtNo": "Indus", "searchType": "Indus"}},
        ],
        "expected_responses": {"failure_keywords": ["not found"]},
    }

    _SCRIPT = {
        "method_type": "SCRIPT",
        "source_url": "http://example.test",
        "required_inputs": ["document_number"],
        "script_source": (
            "from dvs_io import get_input, write_result\n"
            'd = get_input("document_number")\n'
            'write_result("REJECTED", raw_response="x", evidence={"d": d})\n'
        ),
    }

    def test_falls_back_to_script_when_declarative_is_incoherent(self):
        with patch("generation.generator._fetch_page_structure", return_value=None), \
             patch("generation.generator.generate_json",
                   side_effect=[dict(self._INCOHERENT), dict(self._SCRIPT)]):
            method = generate_candidate_method(self._PROFILE, self._SOURCE)

        self.assertEqual(method.method_type, MethodType.SCRIPT)
        self.assertIn("get_input", method.script_source)
        self.assertEqual(method.required_inputs, ["document_number"])

    def test_keeps_declarative_when_fallback_returns_no_script(self):
        with patch("generation.generator._fetch_page_structure", return_value=None), \
             patch("generation.generator.generate_json",
                   side_effect=[dict(self._INCOHERENT), dict(self._INCOHERENT)]):
            method = generate_candidate_method(self._PROFILE, self._SOURCE)

        self.assertEqual(method.method_type, MethodType.HTTP)


if __name__ == "__main__":
    unittest.main()
