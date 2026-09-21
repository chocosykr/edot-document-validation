import unittest
from unittest.mock import patch
from registry.models import MethodStatus, MethodType
from generation.generator import generate_candidate_method
from registry.document_types import document_type_key, profile_document_type_key

class TestMethodGeneration(unittest.TestCase):
    def test_document_type_key_uses_allowlist_and_ignores_model_key(self):
        self.assertEqual(document_type_key("Seafarers' Identity Document (SID)"), "IN_SID")
        self.assertIsNone(document_type_key("INDOS-like credential"))
        self.assertEqual(
            profile_document_type_key({
                "document_type": "Seafarers' Identity Document",
                "document_type_key": "INDOS_CERTIFICATE",
            }),
            "IN_SID",
        )
    def test_search_type_document_mapping_is_rejected(self):
        js = '''
        var url = "/checker";
        var xhr = new XMLHttpRequest();
        xhr.open("POST", url + "?txtNo=" + txtNo + "&dob=" + dob + "&searchType=" + searchType, true);
        xhr.send(null);
        '''
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": js, "workflow_options": []},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "param_mapping": {
                    "txtNo": "{{document_number}}",
                    "dob": "{{date_of_birth}}",
                    "searchType": "{{document_type}}",
                },
                "required_inputs": ["document_number", "date_of_birth", "document_type"],
                "workflow_params": {},
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "searchType"):
                generate_candidate_method(
                    {"issuing_country": "India", "document_type": "INDOS"},
                    {"source_url": "https://example.test/checker"},
                )

    def test_generate_from_db_source(self):
        profile = {
            "issuing_country": "India",
            "document_type": "CDC"
        }
        db_source = {
            "country": "India",
            "url": "https://dgma.gov.in/seafarer-certificate-verification-system"
        }
        
        method = generate_candidate_method(profile, db_source)
        
        self.assertEqual(method.country, "India")
        self.assertEqual(method.document_type, "CDC")
        self.assertEqual(method.source_url, "https://dgma.gov.in/seafarer-certificate-verification-system")
        self.assertEqual(method.status, MethodStatus.TESTING)
        self.assertTrue(len(method.required_inputs) > 0)
        self.assertTrue(len(method.execution_steps) > 0)

    def test_generate_from_discovery_result(self):
        profile = {
            "issuing_country": "Panama",
            "document_type": "CoC"
        }
        discovery_result = {
            "query": "Panama CoC verification",
            "source": {
                "title": "Panama Verification API",
                "url": "https://api.panama-maritime.com/verify"
            },
            "analysis": {
                "summary": "This is an API for Panama CoC."
            }
        }
        
        method = generate_candidate_method(profile, discovery_result)
        
        self.assertEqual(method.country, "Panama")
        self.assertEqual(method.document_type, "CoC")
        self.assertEqual(method.source_url, "https://api.panama-maritime.com/verify")
        # Should be detected as HTTP based on our mock logic
        self.assertEqual(method.method_type, MethodType.HTTP)
        self.assertEqual(method.status, MethodStatus.TESTING)

if __name__ == "__main__":
    unittest.main()
