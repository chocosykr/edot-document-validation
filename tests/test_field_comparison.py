import unittest
from unittest.mock import patch

from execution.models import ExecutionDecisionStatus, ExecutionResult
from validation.field_comparison import (
    AMBIGUOUS_MATCH_THRESHOLD,
    STRONG_MATCH_THRESHOLD,
    _extract_response_fields,
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
        """A not-found body rejects ONLY when the method learned its shape;
        without a learned signature it cannot ground a verdict."""
        body = "<p>Database could not find the match.</p>"
        profile = {"document_number": "ABC123", "date_of_birth": "01/01/1990"}

        unlearned = compare_response(self._result(body), profile)
        self.assertEqual(unlearned.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)

        learned = compare_response(
            self._result(body), profile,
            not_found_signatures=[{"contains": "could not find the match"}],
        )
        self.assertEqual(learned.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertEqual(learned.evidence["comparison"], "learned_not_found_signature")

    def test_service_error_response_is_technical_failure_not_rejection(self):
        """Portal error phrasing is NOT hardcoded — the response carries no
        parseable record fields, so it cannot ground a definitive verdict.

        Live case (2026-09-28): esamudra answered a valid CDC lookup with
        "Sorry ! Unable to process your request,please try later" and the
        (then-hardcoded) marker list classified it REJECTED / HIGH. Under
        the grounding rules a tiny unparseable body degrades to
        TECHNICAL_FAILURE for ANY portal, known or unknown.
        """
        result = compare_response(
            self._result(
                "<span class='newStrip'><b><font size='4' >Sorry ! Unable to "
                "process your request,please try later</b></span>"
            ),
            {"document_number": "MUM179416", "date_of_birth": "07/09/1992"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn(result.evidence["comparison"], ("tiny_unparseable_response", "framework_error_page"))

    def test_not_found_rejects_only_via_learned_signature(self):
        """A not-found verdict requires the method's OWN learned signature.

        Generic phrase matching is gone: 'could not find' with no learned
        signature and no parseable fields is a tiny unparseable body ->
        TECHNICAL_FAILURE. When the method HAS learned the shape (from its
        known-fake probe), the same body is a true REJECTED.
        """
        body = (
            "<table class='newStrip'>Our Database could not find the match "
            "of CDC No. you are looking for</table>"
        )
        profile = {"document_number": "MUM179416", "date_of_birth": "07/09/1992"}
        no_sig = compare_response(
            self._result(body), profile, field_mapping={"text_mappings": {}}
        )
        self.assertEqual(no_sig.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)

        learned = compare_response(
            self._result(body),
            profile,
            field_mapping={
                "text_mappings": {},
                "not_found_signatures": [{"contains": "could not find the match"}],
            },
        )
        self.assertEqual(learned.decision_status, ExecutionDecisionStatus.REJECTED)
        self.assertEqual(learned.evidence["comparison"], "learned_not_found_signature")

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


class TestHtmlColumnPairing(unittest.TestCase):
    """A table header row holds COLUMN labels, not label/value pairs.

    Regression (live DMAMyanmar record, 2026-09-30): the extractor paired
    adjacent cells *within* the header row, so the returned mapping read
    ``"cdc no." -> "date of birth"``, ``"80484" -> "05, mar 1993"``. The
    genuinely VALID record then scored document_number 19.0 / dob 78.3 /
    full_name 38.5 and the run reported REJECTED.
    """

    # Mirrors the real page: a banner row, a 4-column header row of <td>
    # labels, and the data row beneath it.
    @staticmethod
    def _result(body):
        return ExecutionResult(
            decision_status=ExecutionDecisionStatus.UNCERTAIN,
            evidence={},
            raw_response=body,
        )

    MYANMAR_TABLE = """
        <table><tbody>
          <tr><td colspan="4">HEIN HTET, Passport: ,
              <span class="badge">Status: VALID</span></td></tr>
          <tr>
            <td>CDC No.</td><td>Date of Birth</td>
            <td>Certificate No.</td><td>STCW Ref</td>
          </tr>
          <tr>
            <td>80484</td><td>05, Mar 1993</td>
            <td>2DK004368</td><td>II/2</td>
          </tr>
        </tbody></table>
    """

    def test_header_row_is_paired_column_wise_with_its_data_row(self):
        fields = _extract_response_fields(self.MYANMAR_TABLE)
        self.assertEqual(fields["cdc no."], "80484")
        self.assertEqual(fields["date of birth"], "05, mar 1993")
        self.assertEqual(fields["certificate no."], "2dk004368")
        self.assertEqual(fields["stcw ref"], "ii/2")
        # The off-by-one artifacts must be gone entirely.
        self.assertNotIn("80484", fields)
        self.assertNotEqual(fields.get("cdc no."), "date of birth")

    def test_thead_header_row_is_paired_column_wise(self):
        fields = _extract_response_fields(
            "<table><thead><tr><th>Name</th><th>INDoS No.</th></tr></thead>"
            "<tbody><tr><td>Anup Kamboj</td><td>09NL5250</td></tr></tbody></table>"
        )
        self.assertEqual(fields["name"], "anup kamboj")
        self.assertEqual(fields["indos no."], "09nl5250")

    def test_label_value_rows_still_read_as_label_value(self):
        """A mixed <th>label</th><td>value</td> row is NOT a header row."""
        fields = _extract_response_fields(
            "<table><tr><th>INDoS No.</th><td>ABC123</td></tr>"
            "<tr><th>Date of Birth</th><td>01/01/1990</td></tr></table>"
        )
        self.assertEqual(fields["indos no."], "abc123")
        self.assertEqual(fields["date of birth"], "01/01/1990")

    def test_nested_table_does_not_pollute_the_outer_row(self):
        fields = _extract_response_fields(
            "<table><tbody><tr>"
            "<td><img src=\"/logo.jpg\"></td>"
            "<td><table><tbody>"
            "<tr><td>CDC No.</td><td>Date of Birth</td></tr>"
            "<tr><td>80484</td><td>05, Mar 1993</td></tr>"
            "</tbody></table></td>"
            "</tr></tbody></table>"
        )
        self.assertEqual(fields["cdc no."], "80484")
        self.assertEqual(fields["date of birth"], "05, mar 1993")

    def test_comma_separated_portal_date_matches_numeric_dob(self):
        fields = _extract_response_fields(self.MYANMAR_TABLE)
        result = compare_response(
            self._result(self.MYANMAR_TABLE),
            {"document_number": "2DK004368", "date_of_birth": "05/03/1993"},
        )
        scores = result.evidence["field_scores"]
        self.assertEqual(result.evidence["extracted_fields"], fields)
        self.assertEqual(scores["document_number"], 100.0)
        self.assertEqual(scores["date_of_birth"], 100.0)

    def test_valid_record_verifies_instead_of_being_rejected(self):
        result = compare_response(
            self._result(self.MYANMAR_TABLE),
            {"document_number": "2DK004368", "date_of_birth": "05/03/1993"},
        )
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertEqual(result.evidence["comparison"], "field_match")


if __name__ == "__main__":
    unittest.main()
