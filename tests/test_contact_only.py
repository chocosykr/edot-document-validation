"""
Country-scoped document_type_key routing and the contact-only field concept.

The country-scoping tests pin the fix for the September 2026 incident where a
Myanmar method carried an India-era IN_COC key; the contact-only tests pin the
general mechanism letting methods declare a required input (notification
email/phone) as synthesizable because the target site never checks it against
the document holder's identity.
"""

import unittest

from registry.document_types import (
    profile_document_type_key,
    document_type_key,
)
from execution.safety import (
    contact_only_inputs,
    synthesize_contact_value,
    fill_contact_only_inputs,
    guard_inputs,
    UnsafeInputError,
    is_placeholder_value,
)
from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus


class TestCountryScopedKeys(unittest.TestCase):
    def test_myanmar_coc_gets_mm_coc(self):
        self.assertEqual(
            profile_document_type_key({
                "document_type": "CERTIFICATE OF COMPETENCY",
                "issuing_country": "Myanmar",
            }),
            "MM_COC",
        )

    def test_india_coc_stays_in_coc(self):
        self.assertEqual(
            profile_document_type_key({
                "document_type": "CERTIFICATE OF COMPETENCY",
                "issuing_country": "India",
            }),
            "IN_COC",
        )

    def test_country_scoping_is_country_sensitive(self):
        """A Myanmar method must NOT be routable with an India-era key."""
        mm = profile_document_type_key({
            "document_type": "CERTIFICATE OF COMPETENCY",
            "issuing_country": "Myanmar",
        })
        self.assertNotEqual(mm, "IN_COC")

    def test_indos_is_not_country_scoped(self):
        """INDOS is intrinsically an Indian database; keep IN_INDOS."""
        self.assertEqual(
            profile_document_type_key({
                "document_type": "INDOS",
                "issuing_country": "India",
            }),
            "IN_INDOS",
        )

    def test_coc_without_country_keeps_legacy_key(self):
        self.assertEqual(
            profile_document_type_key({"document_type": "COC"}),
            "IN_COC",
        )

    def test_unknown_type_still_none(self):
        self.assertIsNone(
            profile_document_type_key({"document_type": "INDOS-like credential"})
        )

    def test_document_type_key_allowlist_unchanged(self):
        self.assertEqual(document_type_key("Seafarers' Identity Document (SID)"), "IN_SID")
        self.assertIsNone(document_type_key("INDOS-like credential"))


class TestContactOnlyConcept(unittest.TestCase):
    def _method(self, required=None, contact=None):
        expected = {}
        if contact is not None:
            expected["contact_only_inputs"] = contact
        return ValidationMethod(
            method_id="M_CONTACT_TEST",
            method_type=MethodType.HTTP,
            source_url="https://example.com/verify",
            required_inputs=required or [],
            execution_steps=[{"action": "REQUEST", "method": "POST",
                              "url": "https://example.com/verify"}],
            expected_responses=expected,
            status=MethodStatus.TESTING,
        )

    def test_declared_contact_inputs_are_read(self):
        m = self._method(contact=["reply_email"])
        self.assertEqual(contact_only_inputs(m), {"reply_email"})

    def test_undeclared_is_empty(self):
        self.assertEqual(contact_only_inputs(self._method()), set())

    def test_synthesized_email_is_inert_and_valid_format(self):
        v = synthesize_contact_value("reply_email")
        self.assertTrue(v.endswith(".invalid"))          # RFC 2606 — undeliverable
        self.assertIn("@", v)                            # plausible format
        self.assertFalse(is_placeholder_value(v))        # passes the guard

    def test_synthesized_phone_is_plausible_format(self):
        v = synthesize_contact_value("contact_number")
        self.assertTrue(v.startswith("+"))
        self.assertFalse(is_placeholder_value(v))

    def test_fill_only_missing_contact_fields(self):
        m = self._method(contact=["reply_email"])
        out = fill_contact_only_inputs({"document_number": "X1"}, m)
        self.assertEqual(out["document_number"], "X1")
        self.assertIn("@", out["reply_email"])

    def test_explicit_contact_value_not_overwritten(self):
        m = self._method(contact=["reply_email"])
        out = fill_contact_only_inputs({"reply_email": "ops@example.com"}, m)
        self.assertEqual(out["reply_email"], "ops@example.com")

    def test_identity_fields_never_filled(self):
        m = self._method(contact=["reply_email"])
        out = fill_contact_only_inputs({}, m)
        self.assertNotIn("passport_number", out)
        self.assertNotIn("document_number", out)

    def test_guard_exempts_missing_contact_only_fields(self):
        # reply_email missing but contact_only -> NOT a missing-input refusal
        guard_inputs(
            {"passport_number": "MA1234567"},
            ["passport_number", "reply_email"],
            contact_only_inputs={"reply_email"},
        )  # must not raise

    def test_guard_still_refuses_missing_identity_fields(self):
        with self.assertRaises(UnsafeInputError):
            guard_inputs(
                {"reply_email": "someone@example.com"},
                ["passport_number", "reply_email"],
                contact_only_inputs={"reply_email"},
            )


class TestRunnerContactOnlyWiring(unittest.TestCase):
    """End-to-end through the runner: missing contact-only input is
    synthesized before the guard and reaches the executor request."""

    def setUp(self):
        self.runner = DockerMethodRunner()

    def _method(self, required, contact):
        return ValidationMethod(
            method_id="M_CONTACT_RUN",
            method_type=MethodType.HTTP,
            source_url="https://example.com/verify",
            required_inputs=required,
            execution_steps=[{"action": "REQUEST", "method": "POST",
                              "url": "https://example.com/verify"}],
            expected_responses={"contact_only_inputs": contact},
            status=MethodStatus.TESTING,
        )

    def test_missing_contact_only_input_is_synthesized_and_executes(self):
        m = self._method(["document_number", "reply_email"], ["reply_email"])
        req = ExecutionRequest(method=m, inputs={"document_number": "DOC12345"})
        # /nonexistent.py: proves the guard passed (error is about the
        # executor script, not a refusal) — no container, no network.
        result = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertIn("Executor script not found", result.error)
        self.assertIsNone(result.evidence.get("refused_reason"))

    def test_missing_identity_input_still_refused(self):
        m = self._method(["document_number", "reply_email"], ["reply_email"])
        req = ExecutionRequest(method=m, inputs={"reply_email": "x@example.com"})
        result = self.runner.execute_method(req, executor_script_path="/nonexistent.py")
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.TECHNICAL_FAILURE)
        self.assertIn("document_number", result.error)


if __name__ == "__main__":
    unittest.main()
