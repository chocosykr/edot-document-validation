"""
Tests for Component 2 — discriminator discovery (one-time probe,
registry-cached), the deterministic difflib extraction (no LLM in the diff
step), and the REJECTED-only policy enforced around it.

All network and LLM calls are mocked. The PII rule is structural: real
inputs exist only as return values of a provider callable and are never
written anywhere.
"""

import unittest
from unittest.mock import MagicMock, patch

from generation.generator import (
    _discover_discriminators,
    _extract_discriminators_by_diff,
    _probe_wire_params,
    generate_candidate_method,
    FAKE_PROBE_INPUTS,
)
from registry.models import MethodStatus
from validation.models import TestCase, ValidationReportStatus
from validation.validator import MethodValidator

FAKE_HTML = '<html><body>Fake page<br>Database could not find the match of INDoS No.</body></html>'
REAL_HTML = '<html><body>Real page<br>Search Result</body></html>'
REJECTION_MARKER = "Database could not find the match of INDoS No."
SUCCESS_MARKER = "Search Result"

# Wire param → profile field (as the narrow LLM would return it)
PARAM_MAPPING = {"txtNo": "{{document_number}}", "dob": "{{date_of_birth}}"}


def _contract() -> dict:
    return {
        "endpoint": "http://220.156.189.33/esamudraUI/checkerajaxservlet",
        "verb": "POST",
        "param_location": "query",
        "dynamic_params": ["txtNo", "dob"],
        "static_params": {"processId": "PPIndosCheck", "searchType": "Indos"},
    }


class TestProbeWireParams(unittest.TestCase):
    """Probes must translate profile fields → wire param names."""

    def test_translates_profile_fields_to_wire_names(self):
        params = _probe_wire_params(
            {"document_number": "X1", "date_of_birth": "01/01/1990"},
            PARAM_MAPPING,
            {"processId": "PPIndosCheck"},
        )
        self.assertEqual(
            params,
            {
                "processId": "PPIndosCheck",
                "txtNo": "X1",
                "dob": "01/01/1990",
            },
        )

    def test_missing_field_is_omitted_not_tokenized(self):
        params = _probe_wire_params({}, PARAM_MAPPING, {})
        self.assertEqual(params, {})

    def test_redaction_token_field_is_still_passed_through_here(self):
        # Token filtering happens in the engine's _build_inputs; this layer
        # only translates names. Guard documents the responsibility split.
        params = _probe_wire_params(
            {"document_number": "[DOCUMENT_NUMBER]"}, PARAM_MAPPING, {}
        )
        self.assertEqual(params.get("txtNo"), "[DOCUMENT_NUMBER]")


class TestDiffExtraction(unittest.TestCase):
    """Deterministic difflib discriminator extraction — no LLM involved."""

    def test_two_sided_diff_finds_both_markers(self):
        result = _extract_discriminators_by_diff(FAKE_HTML, REAL_HTML)
        self.assertEqual(result["rejection_marker"], REJECTION_MARKER)
        self.assertEqual(result["success_marker"], SUCCESS_MARKER)

    def test_one_sided_diff_returns_no_markers(self):
        result = _extract_discriminators_by_diff(FAKE_HTML, None)
        self.assertEqual(result, {"rejection_marker": "", "success_marker": ""})

    def test_static_labels_are_not_markers(self):
        # The label appears in BOTH responses → not unique → never a marker.
        # Varying lines must live INSIDE <body> (the parser drops the rest).
        fake = (
            '<html><body><div>Search Result</div>'
            'Database could not find the match of INDoS No.</body></html>'
        )
        real = (
            '<html><body><div>Search Result</div>'
            'Record found for the holder.</body></html>'
        )
        result = _extract_discriminators_by_diff(fake, real)
        self.assertEqual(result["rejection_marker"], REJECTION_MARKER)
        self.assertNotEqual(result["success_marker"], "Search Result")

    def test_no_marker_without_classification_pattern(self):
        # Unique lines with no "not found"-ish / "found"-ish wording
        fake = "<html><body>zzqq wwzz</body></html>"
        real = "<html><body>aabb ccdd</body></html>"
        result = _extract_discriminators_by_diff(fake, real)
        self.assertEqual(result, {"rejection_marker": "", "success_marker": ""})


class TestDiscriminatorDiscovery(unittest.TestCase):
    """Seed-credential probe via difflib; REJECTED-only without real inputs."""

    @patch("generation.generator._fetch_idle_text", return_value="")
    @patch("generation.generator._probe_endpoint")
    def test_no_credential_means_rejected_only_or_nothing(self, mock_probe, _mock_idle):
        mock_probe.return_value = FAKE_HTML

        result = _discover_discriminators(
            xhr_contract=_contract(),
            source_url="http://example.com/page.jsp",
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=PARAM_MAPPING,
            real_inputs_provider=None,
        )

        # Without the idle page the fallback is a pattern scan; with our
        # fixture the rejection line matches the "could not find" pattern.
        self.assertEqual(result["success_keywords"], [])
        self.assertIn(result["failure_keywords"], ([], [REJECTION_MARKER]))

    @patch("generation.generator._fetch_idle_text", return_value="")
    @patch("generation.generator._probe_endpoint")
    def test_seed_credential_confirms_both_markers(self, mock_probe, _mock_idle):
        mock_probe.side_effect = [FAKE_HTML, REAL_HTML]

        provider = MagicMock(
            return_value={"document_number": "REAL123", "date_of_birth": "07/09/1992"}
        )

        result = _discover_discriminators(
            xhr_contract=_contract(),
            source_url="http://example.com/page.jsp",
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=PARAM_MAPPING,
            real_inputs_provider=provider,
        )

        self.assertEqual(result["failure_keywords"], [REJECTION_MARKER])
        self.assertEqual(result["success_keywords"], [SUCCESS_MARKER])
        provider.assert_called_once()

        # Probe #2 must use WIRE param names carrying the real values
        second_kwargs = mock_probe.call_args_list[1].kwargs
        self.assertEqual(
            second_kwargs["params"],
            {
                "processId": "PPIndosCheck",
                "searchType": "Indos",
                "txtNo": "REAL123",
                "dob": "07/09/1992",
            },
        )

    @patch("generation.generator._fetch_idle_text", return_value="")
    @patch("generation.generator._probe_endpoint")
    def test_probe_failure_yields_no_markers(self, mock_probe, _mock_idle):
        mock_probe.return_value = None
        result = _discover_discriminators(
            xhr_contract=_contract(),
            source_url="http://example.com/page.jsp",
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=PARAM_MAPPING,
        )
        self.assertEqual(
            result, {"success_keywords": [], "failure_keywords": []}
        )

    @patch("generation.generator._fetch_idle_text", return_value="")
    @patch("generation.generator._probe_endpoint")
    def test_provider_exception_does_not_crash(self, mock_probe, _mock_idle):
        mock_probe.return_value = FAKE_HTML

        def boom():
            raise RuntimeError("no raw profile available")

        result = _discover_discriminators(
            xhr_contract=_contract(),
            source_url="http://example.com/page.jsp",
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=PARAM_MAPPING,
            real_inputs_provider=boom,
        )
        self.assertEqual(result["success_keywords"], [])

    @patch("generation.generator._fetch_idle_text", return_value="")
    @patch("generation.generator._probe_endpoint")
    def test_credential_without_document_number_is_ignored(self, mock_probe, _mock_idle):
        """A provider result without a document number is not a usable seed."""
        mock_probe.return_value = FAKE_HTML
        result = _discover_discriminators(
            xhr_contract=_contract(),
            source_url="http://example.com/page.jsp",
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=PARAM_MAPPING,
            real_inputs_provider=lambda: {"date_of_birth": "01/01/1990"},
        )
        # Only the fake probe fired
        self.assertEqual(mock_probe.call_count, 1)
        self.assertEqual(result["success_keywords"], [])


class TestNarrowPathLimitation(unittest.TestCase):
    """generate_candidate_method tags the method REJECTED-only exactly when
    no success marker was confirmed (regression test for the inverted
    condition — the limitation must key off success_keywords, not
    failure_keywords)."""

    INDOS_JS = """
    function checkIndos() {
        var txtNoVal = document.getElementById('txtNo').value;
        var dobVal = document.getElementById('dob').value;
        var url = "/esamudraUI/checkerajaxservlet";
        xmlHttp.open("POST", url + "?txtNo=" + txtNoVal + "&dob=" + dobVal + "&processId=PPIndosCheck&searchType=Indos", true);
        xmlHttp.send(null);
    }
    """

    def _generate(self, expected_responses: dict):
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": self.INDOS_JS},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "param_mapping": {"txtNo": "{{document_number}}", "dob": "{{date_of_birth}}"},
                "required_inputs": ["document_number", "date_of_birth"],
            },
        ), patch(
            "generation.generator._discover_discriminators",
            return_value=expected_responses,
        ):
            return generate_candidate_method(
                {"document_type": "INDOS", "issuing_country": "India"},
                {"source_url": "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp"},
            )

    def test_rejected_only_limitation_without_success_marker(self):
        method = self._generate({"success_keywords": [], "failure_keywords": [REJECTION_MARKER]})
        self.assertEqual(method.expected_responses.get("comparison_mode"), "field_match")
        self.assertFalse(any("REJECTED-only" in lim for lim in method.limitations))

    def test_no_limitation_when_success_marker_confirmed(self):
        method = self._generate(
            {"success_keywords": [SUCCESS_MARKER], "failure_keywords": [REJECTION_MARKER]}
        )
        self.assertEqual(method.expected_responses.get("comparison_mode"), "field_match")
        self.assertFalse(any("REJECTED-only" in lim for lim in method.limitations))

    def test_rejected_only_even_when_probe_confirmed_nothing(self):
        method = self._generate({"success_keywords": [], "failure_keywords": []})
        self.assertEqual(method.expected_responses.get("comparison_mode"), "field_match")
        self.assertFalse(any("REJECTED-only" in lim for lim in method.limitations))
        self.assertEqual(method.status, MethodStatus.TESTING)

    def test_probe_receives_wire_format_params(self):
        """Integration: the probe must receive WIRE-format params built from
        the same resolved mapping the method carries — otherwise the probe
        sends document_number= to an endpoint expecting txtNo= and fake vs
        real responses become indistinguishable."""
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": self.INDOS_JS},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "param_mapping": {"txtNo": "{{document_number}}", "dob": "{{date_of_birth}}"},
                "required_inputs": ["document_number", "date_of_birth"],
            },
        ), patch(
            "generation.generator._fetch_idle_text", return_value=""
        ), patch(
            "generation.generator._probe_endpoint", return_value=FAKE_HTML
        ) as mock_probe:
            method = generate_candidate_method(
                {"document_type": "INDOS", "issuing_country": "India"},
                {"source_url": "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp"},
            )

        # Seed/discriminator probing is no longer part of generation.
        self.assertEqual(mock_probe.call_count, 0)

        # The method's execution steps share the same wire param names
        step_params = method.execution_steps[0]["params"]
        self.assertEqual(step_params["txtNo"], "{{document_number}}")
        self.assertEqual(step_params["dob"], "{{date_of_birth}}")


class TestValidatorStripGuard(unittest.TestCase):
    """Self-healing must not reintroduce success keywords into a
    REJECTED-only method (implementation plan, Component 2 policy)."""

    def test_injected_success_keywords_are_stripped(self):
        from registry.models import ValidationMethod, MethodType
        from execution.models import ExecutionResult, ExecutionDecisionStatus

        method = ValidationMethod(
            method_id="M_REJ_GUARD",
            method_type=MethodType.HTTP,
            source_url="https://mock.example.com",
            country="India",
            document_type="INDOS",
            expected_responses={
                "success_keywords": ["guessed-by-llm"],
                "failure_keywords": [REJECTION_MARKER],
            },
            limitations=[
                "REJECTED-only until a human-confirmed probe supplies the success marker."
            ],
        )

        runner = MagicMock()
        runner.execute_method.return_value = ExecutionResult(
            decision_status=ExecutionDecisionStatus.REJECTED,
            evidence={"mock": True},
            logs="mock log",
        )

        validator = MethodValidator(runner=runner, executor_script_path="tests/dummy_executor.py")
        test_cases = [
            TestCase(
                name="structural_check",
                inputs=dict(FAKE_PROBE_INPUTS),
                expected_decision="REJECTED",
            ),
        ]
        report = validator.validate(method, test_cases)

        self.assertEqual(report.status, ValidationReportStatus.PASSED)
        self.assertEqual(method.expected_responses.get("success_keywords"), [])


if __name__ == "__main__":
    unittest.main()
