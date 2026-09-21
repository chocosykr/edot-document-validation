"""Tests for the profile/credentials split in ocr/extractor.py."""

import unittest

from ocr.extractor import split_credentials, CREDENTIALS_KEY


class TestSplitCredentials(unittest.TestCase):

    def test_split_separates_profile_from_credentials(self):
        extracted = {
            "document_type": "INDOS",
            "document_holder": {"name": "[PERSON_NAME]", "date_of_birth": "[DATE_OF_BIRTH]"},
            "document_numbers": ["[DOCUMENT_NUMBER]"],
            CREDENTIALS_KEY: {
                "document_number": "09NL5250",
                "date_of_birth": "07/09/1992",
            },
        }

        profile, creds = split_credentials(extracted)

        self.assertEqual(
            creds,
            {"document_number": "09NL5250", "date_of_birth": "07/09/1992"},
        )
        self.assertNotIn(CREDENTIALS_KEY, profile)
        self.assertEqual(profile["document_type"], "INDOS")

    def test_missing_credentials_block_yields_empty(self):
        profile, creds = split_credentials({"document_type": "INDOS"})
        self.assertEqual(creds, {})
        self.assertEqual(profile, {"document_type": "INDOS"})

    def test_redaction_tokens_are_never_treated_as_credentials(self):
        extracted = {
            "document_type": "INDOS",
            CREDENTIALS_KEY: {
                "document_number": "[DOCUMENT_NUMBER]",
                "date_of_birth": "[DATE_OF_BIRTH]",
            },
        }
        profile, creds = split_credentials(extracted)
        self.assertEqual(creds, {})
        self.assertNotIn(CREDENTIALS_KEY, profile)

    def test_non_string_and_empty_values_dropped(self):
        extracted = {
            CREDENTIALS_KEY: {
                "document_number": 12345,
                "date_of_birth": "",
                "extra_field": "ignored",
            },
        }
        _, creds = split_credentials(extracted)
        self.assertEqual(creds, {})

    def test_credentials_block_is_not_a_dict(self):
        extracted = {CREDENTIALS_KEY: "09NL5250"}
        _, creds = split_credentials(extracted)
        self.assertEqual(creds, {})


if __name__ == "__main__":
    unittest.main()
