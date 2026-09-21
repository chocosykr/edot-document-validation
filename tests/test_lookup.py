import unittest
from db.lookup import lookup_source

class TestLookup(unittest.TestCase):

    def test_matching_source_exists(self):
        mock_profile = {
            "issuing_country": "India",
            "document_type": "INDOS Certificate",
        }
        results = lookup_source(mock_profile)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["country"], "India")

    def test_no_matching_source_exists(self):
        mock_profile = {
            "issuing_country": "UnknownLand"
        }
        results = lookup_source(mock_profile)
        self.assertEqual(len(results), 0)

    def test_multiple_potential_sources(self):
        mock_profile = {"issuing_country": "Bahamas", "document_type": "CDC"}
        results = lookup_source(mock_profile)
        self.assertEqual(results, [])

    def test_sid_does_not_match_indos_source(self):
        results = lookup_source({
            "issuing_country": "INDIA",
            "document_type": "Seafarers' Identity Document (SID)",
        })
        self.assertEqual(results, [])

    def test_indos_matches_india_source(self):
        results = lookup_source({
            "issuing_country": "INDIA",
            "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDOS) Certificate",
        })
        self.assertEqual(len(results), 1)
        self.assertIn("indos", results[0]["url"].lower())

    def test_fallback_to_flag_state(self):
        mock_profile = {
            "issuing_country": None,
            "authorities": {
                "flag_state": "Cyprus"
            }
        }
        results = lookup_source(mock_profile)
        self.assertEqual(results, [])

    def test_incomplete_info(self):
        mock_profile = {
            "document_type": "CDC"
            # Missing country entirely
        }
        results = lookup_source(mock_profile)
        self.assertEqual(len(results), 0)

    def test_unknown_document_type_is_fail_closed(self):
        results = lookup_source({"issuing_country": "INDIA", "document_type": "Unrecognized Credential"})
        self.assertEqual(results, [])

if __name__ == "__main__":
    unittest.main()
