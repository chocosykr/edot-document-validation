import base64
import unittest
from unittest.mock import patch

from execution.models import ExecutionDecisionStatus, ExecutionResult
from validation.field_comparison import compare_response
from validation.response_mapping import (
    _flatten_response,
    _is_base64_image,
    compare_image_field,
    discover_field_mapping,
)


class TestResponseMapping(unittest.TestCase):
    def test_is_base64_image(self):
        # 2x2 white BMP padded to > 200 chars for _MIN_B64_IMAGE_LEN
        bmp_bytes = (
            b"BM:\x00\x00\x00\x00\x00\x00\x006\x00\x00\x00(\x00\x00\x00"
            b"\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
            b"\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\xff\xff\xff\x00" + (b"\x00" * 200)
        )
        b64_bmp = base64.b64encode(bmp_bytes).decode("ascii")
        self.assertEqual(_is_base64_image(b64_bmp), "bmp")

        # Plain text (short or non-image)
        self.assertIsNone(_is_base64_image("Hello World"))
        self.assertIsNone(_is_base64_image("SCAR ON RIGHT CHEEK"))

    def test_flatten_response(self):
        nested = {
            "dateOfExpiry": "19-04-2031",
            "details": {"mark": "SCAR ON CHEEK"},
            "items": [{"id": 1}],
        }
        flat = _flatten_response(nested)
        self.assertEqual(flat["dateOfExpiry"], "19-04-2031")
        self.assertEqual(flat["details.mark"], "SCAR ON CHEEK")
        self.assertEqual(flat["items[0].id"], 1)

    @patch("validation.response_mapping.generate_json")
    def test_discover_field_mapping(self, mock_gen_json):
        mock_gen_json.return_value = {
            "mappings": [
                {
                    "response_field": "dateOfExpiry",
                    "profile_field": "expiry_date",
                    "confidence": "HIGH",
                },
                {
                    "response_field": "randomField",
                    "profile_field": "unknown",
                    "confidence": "LOW",
                },
            ]
        }

        # Long enough b64 bmp so it classifies as image field
        bmp_bytes = b"BM" + (b"\x00" * 200)
        b64_bmp = base64.b64encode(bmp_bytes).decode("ascii")

        response = {
            "dateOfExpiry": "19-04-2031",
            "signature": b64_bmp,
            "randomField": "abc",
        }
        extracted_profile = {
            "expiry_date": "19/04/2031",
            "document_number": "N123456",
        }

        mapping = discover_field_mapping(response, extracted_profile)

        self.assertIsNotNone(mapping)
        self.assertIn("text_mappings", mapping)
        self.assertIn("image_fields", mapping)

        # LOW confidence is filtered out, HIGH is kept
        self.assertEqual(mapping["text_mappings"].get("dateOfExpiry"), "expiry_date")
        self.assertNotIn("randomField", mapping["text_mappings"])
        self.assertIn("signature", mapping["image_fields"])

    @patch("validation.response_mapping.vision_call")
    def test_compare_image_field_genuine(self, mock_vision):
        mock_vision.return_value = "TYPE: signature\nGENUINE: YES\nBRIEF: Valid seafarer signature image"
        bmp_bytes = b"BM" + (b"\x00" * 200)
        b64_bmp = base64.b64encode(bmp_bytes).decode("ascii")

        profile = {"document_number": "N123456"}
        res = compare_image_field(b64_bmp, "bmp", profile, field_name="signature")
        self.assertEqual(res["score"], 75.0)
        self.assertTrue(res["plausible"])
        mock_vision.assert_called_once()

    @patch("validation.response_mapping.vision_call")
    def test_compare_image_field_mismatch(self, mock_vision):
        mock_vision.return_value = "TYPE: noise\nGENUINE: NO\nBRIEF: Corrupted noise pixels"
        bmp_bytes = b"BM" + (b"\x00" * 200)
        b64_bmp = base64.b64encode(bmp_bytes).decode("ascii")

        profile = {"document_number": "N123456"}
        res = compare_image_field(b64_bmp, "bmp", profile, field_name="signature")
        self.assertEqual(res["score"], 25.0)
        self.assertFalse(res["plausible"])

    def test_compare_response_with_discovered_mapping(self):
        result = ExecutionResult(
            decision_status=ExecutionDecisionStatus.UNCERTAIN,
            evidence={},
            raw_response='{"dateOfExpiry":"19-04-2031", "identificationMark":"SCAR ON RIGHT CHEEK"}',
        )
        extracted_profile = {
            "expiry_date": "19/04/2031",
            "identifying_fields": {"mark": "SCAR ON RIGHT CHEEK"},
        }
        field_mapping = {
            "text_mappings": {
                "dateOfExpiry": "expiry_date",
                "identificationMark": "mark",
            },
            "image_fields": {},
            "unmapped_response_fields": [],
        }

        evaluated = compare_response(result, extracted_profile, field_mapping=field_mapping)
        self.assertEqual(evaluated.decision_status, ExecutionDecisionStatus.VERIFIED)
        self.assertEqual(evaluated.evidence["comparison"], "discovered_field_match")
        self.assertGreaterEqual(evaluated.evidence["field_scores"]["expiry_date←dateOfExpiry"], 90.0)

    def test_compare_response_with_empty_mapping_returns_uncertain(self):
        result = ExecutionResult(
            decision_status=ExecutionDecisionStatus.UNCERTAIN,
            evidence={},
            raw_response='{"unknownKey": "someValue"}',
        )
        extracted_profile = {"document_number": "12345"}
        field_mapping = {
            "text_mappings": {},
            "image_fields": {},
            "unmapped_response_fields": ["unknownKey"],
        }

        evaluated = compare_response(result, extracted_profile, field_mapping=field_mapping)
        self.assertEqual(evaluated.decision_status, ExecutionDecisionStatus.UNCERTAIN)
        self.assertEqual(evaluated.evidence["comparison"], "no_comparable_fields")


if __name__ == "__main__":
    unittest.main()
