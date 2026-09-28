import unittest
from unittest.mock import patch

from execution.models import ExecutionDecisionStatus, ExecutionResult
from validation.field_comparison import (
    AMBIGUOUS_MATCH_THRESHOLD,
    STRONG_MATCH_THRESHOLD,
    compare_response,
)


class TestFieldComparison(unittest.TestCase):
    def _result(self, body):
        return ExecutionResult(
            decision_status=ExecutionDecisionStatus.UNCERTAIN,
            evidence={},
            raw_response=body,
        )

    def test_matching_labeled_fields_are_verified(self):
        result = compare_response(
            self._result("""
                <table><tr><th>INDoS No.</th><td>ABC123</td></tr>
                <tr><th>Date of Birth</th><td>01/01/1990</td></tr></table>
            """),
            {"document_number": "ABC123", "date_of_birth": "01/01/1990"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertEqual(result.evidence["comparison"], "field_match")

    def test_empty_or_not_found_response_is_rejected(self):
        result = compare_response(
            self._result("<p>Database could not find the match.</p>"),
            {"document_number": "ABC123", "date_of_birth": "01/01/1990"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED)

    def test_service_error_response_is_technical_failure_not_rejection(self):
        """A transient portal outage must never become a definitive INVALID.

        Live case (2026-09-28): esamudra answered a valid CDC lookup with
        "Sorry ! Unable to process your request,please try later" and the
        shared marker list classified it REJECTED / HIGH confidence — a
        false negative. Service errors degrade to TECHNICAL_FAILURE.
        """
        result = compare_response(
            self._result(
                "<span class='newStrip'><b><font size='4' >Sorry ! Unable to "
                "process your request,please try later</b></span>"
            ),
            {"document_number": "MUM179416", "date_of_birth": "07/09/1992"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertEqual(result.evidence["comparison"], "service_error_in_response")

    def test_not_found_still_rejects(self):
        """Genuine not-found phrasing keeps its definitive REJECTED verdict."""
        result = compare_response(
            self._result(
                "<table class='newStrip'>Our Database could not find the match "
                "of CDC No. you are looking for</table>"
            ),
            {"document_number": "MUM179416", "date_of_birth": "07/09/1992"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertEqual(result.evidence["comparison"], "not_found_marker_in_response")

    def test_empty_response_is_technical_failure_not_rejection(self):
        result = compare_response(
            self._result(""),
            {"document_number": "ABC123"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("Empty response", result.error)

    def test_alternate_date_format_and_minor_name_noise_are_verified(self):
        result = compare_response(
            self._result("""
                <table><tr><th>Name</th><td>Anup Kamboj</td></tr>
                <tr><th>Date of Birth</th><td>1992-09-07</td></tr>
                <tr><th>INDoS No.</th><td>09NL5250</td></tr></table>
            """),
            {"full_name": "ANUP KAMBOJ", "date_of_birth": "07/09/1992", "document_number": "09NL5250"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertEqual(result.evidence["comparison"], "field_match")
        self.assertEqual(result.evidence["field_scores"]["date_of_birth"], 100.0)

    def test_ocr_zero_for_letter_o_stays_above_strong_threshold(self):
        result = compare_response(
            self._result("<table><tr><th>Name</th><td>ANUP KAMB0J</td></tr></table>"),
            {"full_name": "ANUP KAMBOJ"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertGreaterEqual(
            result.evidence["field_scores"]["full_name"], STRONG_MATCH_THRESHOLD
        )

    def test_partial_match_escalates_to_local_judge(self):
        with patch("utils.local_llm_client.generate_local_json", return_value={"decision": "NO_MATCH"}) as local_judge:
            result = compare_response(
                self._result("""
                    <table><tr><th>Name</th><td>ANUP KAMBOJ</td></tr>
                    <tr><th>Date of Birth</th><td>01/01/1990</td></tr></table>
                """),
                {"full_name": "ANUP KAMBOJ", "date_of_birth": "02/02/1991"},
            )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertEqual(result.evidence["comparison"], "local_llm")
        self.assertGreaterEqual(result.evidence["field_scores"]["date_of_birth"], AMBIGUOUS_MATCH_THRESHOLD)
        local_judge.assert_called_once()

    @patch("utils.local_llm_client.generate_local_json", return_value={"decision": "MATCH"})
    def test_ambiguous_match_uses_local_model(self, local_judge):
        result = compare_response(
            self._result("<table><tr><th>INDoS No.</th><td>ABC12X</td></tr></table>"),
            {"document_number": "ABC123", "date_of_birth": "01/01/1990"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertEqual(result.evidence["comparison"], "local_llm")
        local_judge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
