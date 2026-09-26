"""
Person-folder fixtures: per-document persistence, staleness detection, and
cross-document credential lookup. All tests use fake data and a redirected
person_folders directory — nothing touches the real OCR service or git.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fixtures import person_folder as pf
from execution.safety import is_placeholder_value


def _fake_extraction(prefix=""):
    return {
        "document_type": "CERTIFICATE OF COMPETENCY",
        "issuing_country": "Myanmar",
        "document_holder": {"name": "[PERSON_NAME]"},
    }, {
        "document_number": prefix + "CDC12345",
        "date_of_birth": "01/01/1990",
        "passport_number": prefix + "MA7654321",
    }


class PersonFolderTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(pf, "PERSON_FOLDERS_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Fake the OCR+extraction pipeline: no network, no LLM.
        self.extract_calls = []

        def fake_extract(path):
            self.extract_calls.append(path)
            return {"raw": "ocr text"}

        def fake_extract_and_redact(raw):
            prefix = "SIBLING" if "sibling" in str(self.current_doc) else ""
            red, creds = _fake_extraction(prefix)
            return {"redacted_profile": red, "raw_lookup_credentials": creds}

        self.current_doc = ""
        p1 = patch.object(pf, "extract_document", side_effect=fake_extract)
        p2 = patch.object(pf, "extract_and_redact", side_effect=fake_extract_and_redact)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def _make_source(self, name, content=b"fake scan bytes"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(content)
        return path


class TestFixturePersistence(PersonFolderTestBase):
    def test_fixture_created_with_hash_and_credentials(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        fixture = pf.load_or_extract_fixture(doc, "personA")
        self.assertEqual(fixture["source_file"], "subject.pdf")
        self.assertTrue(len(fixture["source_sha256"]) == 64)
        self.assertEqual(fixture["raw_lookup_credentials"]["document_number"], "CDC12345")
        fx_file = os.path.join(self.tmp.name, "personA", "subject.json")
        self.assertTrue(os.path.exists(fx_file))
        self.assertEqual(self.extract_calls, [doc])  # extracted exactly once

    def test_second_load_is_cache_hit(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        pf.load_or_extract_fixture(doc, "personA")
        pf.load_or_extract_fixture(doc, "personA")
        self.assertEqual(self.extract_calls, [doc])  # no re-extraction

    def test_changed_source_invalidates_fixture(self):
        doc = self._make_source("subject.pdf", b"v1 bytes")
        self.current_doc = doc
        pf.load_or_extract_fixture(doc, "personA")
        with open(doc, "wb") as f:
            f.write(b"v2 bytes - rescanned document")
        pf.load_or_extract_fixture(doc, "personA")
        self.assertEqual(len(self.extract_calls), 2)  # stale -> re-extracted

    def test_force_refresh_bypasses_cache(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        pf.load_or_extract_fixture(doc, "personA")
        pf.load_or_extract_fixture(doc, "personA", force_refresh=True)
        self.assertEqual(len(self.extract_calls), 2)


class TestCrossDocumentLookup(PersonFolderTestBase):
    def _write_sibling(self, person, stem, creds):
        d = os.path.join(self.tmp.name, person)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, stem + ".json"), "w") as f:
            json.dump({
                "source_file": stem + ".pdf",
                "source_sha256": "0" * 64,
                "redacted_profile": {},
                "raw_lookup_credentials": creds,
            }, f)

    def test_passport_found_in_sibling(self):
        self._write_sibling("personA", "passport_scan",
                            {"passport_number": "P1234567"})
        hit = pf.lookup_credential_across_siblings(
            "personA", "subject.pdf", "passport_number"
        )
        self.assertEqual(hit, ("P1234567", "passport_scan"))

    def test_subject_document_excluded(self):
        self._write_sibling("personA", "subject",
                            {"passport_number": "OWN"})
        self.assertIsNone(
            pf.lookup_credential_across_siblings("personA", "subject.pdf", "passport_number")
        )

    def test_unknown_key_not_lookupable(self):
        self._write_sibling("personA", "sibling", {"magic_field": "X"})
        self.assertIsNone(
            pf.lookup_credential_across_siblings("personA", "subject.pdf", "magic_field")
        )

    def test_placeholder_sibling_value_not_served(self):
        self._write_sibling("personA", "sibling", {"passport_number": "[PASSPORT_NUMBER]"})
        self.assertIsNone(
            pf.lookup_credential_across_siblings("personA", "subject.pdf", "passport_number")
        )

    def test_missing_folder_is_none(self):
        self.assertIsNone(
            pf.lookup_credential_across_siblings("nobody", "subject.pdf", "passport_number")
        )


class TestFolderCredentialProvider(PersonFolderTestBase):
    def test_own_credentials_win_over_siblings(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        subject, provider = pf.load_subject_document(
            doc, "personA", required_inputs=["passport_number"]
        )
        # sibling has a DIFFERENT passport; subject's own must win
        d = os.path.join(self.tmp.name, "personA")
        with open(os.path.join(d, "sibling.json"), "w") as f:
            json.dump({"raw_lookup_credentials": {"passport_number": "SIBLING_VALUE"}}, f)
        creds = provider(["passport_number"])
        self.assertEqual(creds["passport_number"], "MA7654321")

    def test_missing_required_key_filled_from_sibling(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        # subject fixture has NO serial_number; sibling does.
        # Use the REAL source hash so the loader treats the fixture as fresh
        # (otherwise the mock re-extraction would inject mock credentials).
        d = os.path.join(self.tmp.name, "personA")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "subject.json"), "w") as f:
            json.dump({
                "source_file": "subject.pdf",
                "source_sha256": pf._sha256_file(doc),
                "redacted_profile": {},
                "raw_lookup_credentials": {"document_number": "CDC12345"},
            }, f)
        with open(os.path.join(d, "coc_page2.json"), "w") as f:
            json.dump({
                "source_file": "coc_page2.pdf", "source_sha256": "1" * 64,
                "redacted_profile": {},
                "raw_lookup_credentials": {"serial": "S/2024/0042"},
            }, f)
        _, provider = pf.load_subject_document(
            doc, "personA", required_inputs=["serial_number"]
        )
        creds = provider(["serial_number"])
        self.assertEqual(creds["serial_number"], "S/2024/0042")

    def test_non_required_keys_not_filled(self):
        doc = self._make_source("subject.pdf")
        self.current_doc = doc
        d = os.path.join(self.tmp.name, "personA")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "subject.json"), "w") as f:
            json.dump({
                "source_file": "subject.pdf",
                "source_sha256": pf._sha256_file(doc),
                "redacted_profile": {}, "raw_lookup_credentials": {},
            }, f)
        with open(os.path.join(d, "sib.json"), "w") as f:
            json.dump({
                "source_file": "sib.pdf", "source_sha256": "1" * 64,
                "redacted_profile": {},
                "raw_lookup_credentials": {"passport_number": "X"},
            }, f)
        _, provider = pf.load_subject_document(doc, "personA", required_inputs=[])
        creds = provider([]) or {}  # nothing required -> nothing injected
        self.assertNotIn("passport_number", creds)


class TestEngineProviderSeam(unittest.TestCase):
    def test_engine_passes_required_inputs_to_provider(self):
        """_build_inputs must call a folder-aware provider WITH the method's
        required_inputs — that is the hook that activates cross-document
        lookup at validation time."""
        from engine.validation_engine import ValidationEngine
        from registry.models import ValidationMethod, MethodType

        seen = {}

        def provider(required_inputs=None):
            seen["required_inputs"] = required_inputs
            return {"document_number": "CDC12345"}

        engine = ValidationEngine(
            registry=None, runner=None, credential_provider=provider,
            enable_discovery=False,
        )
        method = ValidationMethod(
            method_id="M_X", method_type=MethodType.HTTP,
            source_url="https://example.com",
            required_inputs=["document_number", "serial_number"],
        )
        inputs = engine._build_inputs({}, method=method)
        self.assertEqual(seen["required_inputs"], ["document_number", "serial_number"])
        self.assertEqual(inputs["document_number"], "CDC12345")

    def test_engine_tolerates_legacy_zero_arg_provider(self):
        from engine.validation_engine import ValidationEngine
        from registry.models import ValidationMethod, MethodType

        engine = ValidationEngine(
            registry=None, runner=None,
            credential_provider=lambda: {"document_number": "D1"},
            enable_discovery=False,
        )
        method = ValidationMethod(
            method_id="M_X", method_type=MethodType.HTTP,
            source_url="https://example.com", required_inputs=["document_number"],
        )
        inputs = engine._build_inputs({}, method=method)
        self.assertEqual(inputs["document_number"], "D1")


class TestContactOnlyEvidenceEscape(unittest.TestCase):
    """The upsert gate rejects identity-named contact_only declarations —
    unless a probe evidence file vouches for the field being cosmetic."""

    def setUp(self):
        import mcp_server.server as srv
        self.server = srv
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _evidence_file(self, verdict="COSMETIC", vouches_for=("passport_number",)):
        path = os.path.join(self.tmp.name, "probe_result.json")
        payload = {"verdict": verdict, "timestamp": "2026-09-25T00:00:00+00:00"}
        if vouches_for is not None:
            payload["vouches_for"] = list(vouches_for)
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def _method(self, contact, required_inputs=None):
        from registry.models import ValidationMethod, MethodType, MethodStatus
        return ValidationMethod(
            method_id="M_EVID", method_type=MethodType.HTTP,
            source_url="https://example.com",
            required_inputs=required_inputs or ["document_number", "passport_number"],
            execution_steps=[{"action": "REQUEST", "method": "POST",
                              "url": "https://example.com/v"}],
            expected_responses={"contact_only_inputs": contact},
            status=MethodStatus.TESTING,
        )

    def test_identity_name_rejected_without_evidence(self):
        # Point the gate at a path with no evidence file — the REAL project
        # evidence (probe_evidence/passport_probe_result.json) must not leak
        # into unit tests.
        with patch.object(
            self.server, "PASSPORT_PROBE_EVIDENCE",
            os.path.join(self.tmp.name, "does_not_exist.json"),
        ):
            problems = self.server._validate_method_definition(
                self._method(["passport_number"])
            )
        self.assertTrue(any("identity field" in p for p in problems))

    def test_identity_name_accepted_with_cosmetic_evidence(self):
        evidence = self._evidence_file("COSMETIC")
        with patch.object(self.server, "PASSPORT_PROBE_EVIDENCE", evidence):
            problems = self.server._validate_method_definition(
                self._method(["passport_number"])
            )
        self.assertEqual(problems, [])

    def test_rejected_verdict_still_blocks(self):
        evidence = self._evidence_file("VALIDATED")
        with patch.object(self.server, "PASSPORT_PROBE_EVIDENCE", evidence):
            problems = self.server._validate_method_definition(
                self._method(["passport_number"])
            )
        self.assertTrue(any("identity field" in p for p in problems))

    def test_cosmetic_verdict_without_scope_vouches_nothing(self):
        """Fail-closed: evidence without an explicit vouches_for list must
        unlock nothing — otherwise one portal's probe evidence would mark
        every identity-shaped field synthesizable."""
        evidence = self._evidence_file("COSMETIC", vouches_for=None)
        with patch.object(self.server, "PASSPORT_PROBE_EVIDENCE", evidence):
            problems = self.server._validate_method_definition(
                self._method(["passport_number"])
            )
        self.assertTrue(any("identity field" in p for p in problems))

    def test_vouches_for_is_field_scoped(self):
        """Evidence scoped to passport_number must not unlock serial_number."""
        evidence = self._evidence_file("COSMETIC", vouches_for=("passport_number",))
        with patch.object(self.server, "PASSPORT_PROBE_EVIDENCE", evidence):
            passport_ok = not self.server._validate_method_definition(
                self._method(["passport_number"])
            )
            serial_problems = self.server._validate_method_definition(
                self._method(
                    ["serial_number"],
                    required_inputs=["document_number", "serial_number"],
                )
            )
        self.assertTrue(passport_ok)
        self.assertTrue(any("identity field" in p for p in serial_problems))


if __name__ == "__main__":
    unittest.main()
