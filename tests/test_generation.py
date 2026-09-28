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
        """Full-LLM path maps the model's proposal onto a valid method.

        The LLM call is mocked: a live model cannot be relied on to produce
        an executable method for an arbitrary URL (it may emit a BROWSER
        method for a page with no visible contract — that now raises
        deterministically; see test_browser_method_rejected).
        """
        profile = {
            "issuing_country": "India",
            "document_type": "CDC"
        }
        db_source = {
            "country": "India",
            "url": "https://dgma.gov.in/seafarer-certificate-verification-system"
        }
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": "", "workflow_options": []},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "method_id": "M_IND_CDC_001",
                "method_type": "HTTP",
                "execution_steps": [{
                    "action": "REQUEST", "method": "GET",
                    "url": "https://dgma.gov.in/verify",
                    "params": {"no": "{{document_number}}"},
                }],
                "required_inputs": ["document_number"],
            },
        ):
            method = generate_candidate_method(profile, db_source)

        self.assertEqual(method.country, "India")
        self.assertEqual(method.document_type, "CDC")
        self.assertEqual(method.source_url, "https://dgma.gov.in/seafarer-certificate-verification-system")
        self.assertEqual(method.status, MethodStatus.TESTING)
        self.assertEqual(method.required_inputs, ["document_number"])

    def test_browser_method_rejected(self):
        """A BROWSER proposal fails generation loudly (stub executor)."""
        profile = {"issuing_country": "India", "document_type": "CDC"}
        db_source = {"country": "India", "url": "https://example.test/checker"}
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": "", "workflow_options": []},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "method_type": "BROWSER",
                "execution_steps": [{"action": "BROWSER", "url": "https://example.test"}],
                "required_inputs": [],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "BROWSER"):
                generate_candidate_method(profile, db_source)

    def test_generate_from_discovery_result(self):
        """A discovery result maps onto a valid method (LLM mocked: hermetic).

        This test previously called the LIVE model against a fake URL — it
        could only pass with network access. The pipeline under test is the
        deterministic mapping of the model's proposal onto a method bundle,
        so the model is mocked exactly as in test_generate_from_db_source.
        """
        profile = {
            "issuing_country": "Panama",
            "document_type": "CoC"
        }
        discovery_result = {
            # The engine passes discovery_result["result"] — the flat source
            # dict — into generate_candidate_method, not the envelope.
            "url": "https://api.panama-maritime.com/verify",
            "page_url": "https://api.panama-maritime.com/verify",
            "verification_available": True,
        }
        with patch(
            "generation.generator._fetch_page_structure",
            return_value={"summary": "", "inline_js": "", "workflow_options": []},
        ), patch(
            "generation.generator.generate_json",
            return_value={
                "method_id": "M_PAN_COC_001",
                "method_type": "HTTP",
                "execution_steps": [{
                    "action": "REQUEST", "method": "GET",
                    "url": "https://api.panama-maritime.com/verify",
                    "params": {"cert": "{{document_number}}"},
                }],
                "required_inputs": ["document_number"],
            },
        ):
            method = generate_candidate_method(profile, discovery_result)

        self.assertEqual(method.country, "Panama")
        self.assertEqual(method.document_type, "CoC")
        self.assertEqual(method.source_url, "https://api.panama-maritime.com/verify")
        self.assertEqual(method.method_type, MethodType.HTTP)
        self.assertEqual(method.status, MethodStatus.TESTING)

if __name__ == "__main__":
    unittest.main()
