"""Tests for the field-named log scrubber."""

import unittest

from utils.log_scrubber import scrub, scrub_pii, set_active_profile


class TestScrubFieldNamed(unittest.TestCase):
    def test_name_dob_document_shapes(self):
        secrets = {
            "full_name": "ANUP KAMBOJ",
            "date_of_birth": "07-SEP-1992",
            "document_number": "MUM 179416",
        }
        out = scrub(
            "Name: ANUP KAMBOJ DOB: 07-SEP-1992 CDC: MUM 179416", secrets
        )
        self.assertNotIn("ANUP KAMBOJ", out)
        self.assertNotIn("07-SEP-1992", out)
        self.assertNotIn("MUM 179416", out)
        self.assertIn("‹PERSON_NAME›", out)
        self.assertIn("‹DATE_OF_BIRTH›", out)
        self.assertIn("‹DOCUMENT_NUMBER›", out)

    def test_placeholder_names_are_self_describing(self):
        """Regression: placeholders used to read ``‹DOC:10text›`` — a short
        label plus a shape token that carried no meaning to the model or to a
        reviewer. Every placeholder must now name the field in full words."""
        out = scrub(
            "c=MUM 179416 n=ANUP KAMBOJ e=a@b.com p=+91 98765 43210",
            {
                "cdc_number": "MUM 179416",
                "full_name": "ANUP KAMBOJ",
                "email": "a@b.com",
                "phone": "+91 98765 43210",
            },
        )
        self.assertIn("‹CDC_NUMBER›", out)
        self.assertIn("‹PERSON_NAME›", out)
        self.assertIn("‹EMAIL_ADDRESS›", out)
        self.assertIn("‹PHONE_NUMBER›", out)
        # No shape tokens, no abbreviations.
        self.assertNotRegex(out, r"\d+(text|alnum|digit)\b")

    def test_unknown_field_uses_generic_descriptive_name(self):
        out = scrub("x=ZZZ999", {"some_token": "ZZZ999"})
        self.assertNotIn("ZZZ999", out)
        self.assertIn("‹SECRET_VALUE›", out)

    def test_value_inside_url_path(self):
        out = scrub("GET http://x/verify/MUM179416/detail", {"cdc_number": "MUM179416"})
        self.assertNotIn("MUM179416", out)
        self.assertIn("‹CDC_NUMBER›", out)

    def test_value_inside_json_body_structure_preserved(self):
        body = '{"cdc":"MUM179416","ok":true}'
        out = scrub(body, {"cdc_number": "MUM179416"})
        self.assertNotIn("MUM179416", out)
        # JSON structure must survive: keys and quotes intact.
        self.assertIn('"cdc":', out)
        self.assertIn('"ok":true', out)

    def test_value_inside_query_string_and_url_encoded(self):
        out = scrub(
            "POST /check?doc=MUM%20179416&x=1 body=MUM 179416",
            {"document_number": "MUM 179416"},
        )
        self.assertNotIn("MUM%20179416", out)
        self.assertNotIn("MUM 179416", out)
        self.assertIn("‹DOCUMENT_NUMBER›", out)

    def test_repeated_occurrences_all_replaced(self):
        out = scrub("X X X", {"document_number": "ABC123"})
        # no-op for absent value
        self.assertEqual(out, "X X X")
        out = scrub("ABC123 then ABC123", {"document_number": "ABC123"})
        self.assertEqual(out.count("ABC123"), 0)

    def test_already_redacted_values_are_skipped(self):
        out = scrub("Name: [REDACTED]", {"full_name": "[REDACTED]"})
        self.assertEqual(out, "Name: [REDACTED]")

    def test_longest_value_wins(self):
        out = scrub(
            "ANUP KAMBOJ / ANUP",
            {"full_name": "ANUP KAMBOJ", "other": "ANUP"},
        )
        self.assertNotIn("ANUP", out)

    def test_no_secrets_is_noop(self):
        self.assertEqual(scrub("plain text", None), "plain text")
        self.assertEqual(scrub("plain text", {}), "plain text")

    def test_scrub_pii_uses_active_profile(self):
        set_active_profile({"document_number": "XYZ789"})
        try:
            self.assertNotIn("XYZ789", scrub_pii("doc=XYZ789"))
        finally:
            set_active_profile(None)


if __name__ == "__main__":
    unittest.main()
