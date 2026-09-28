"""
Tests for Stage 7 — Production Validation Engine.

All Docker and network calls are mocked.
Tests cover every branch in ValidationEngine.validate():
  1. Active method found in registry → executed directly
  2. No active method + DB source → generate → validate → execute
  3. No active method + no DB source + discovery enabled → discovery → generate → execute
  3b. DB-source generation refuses → fall through to discovery → generate → execute
  3c. DB-source generation refuses + discovery finds nothing → refusal preserved
  4. No source anywhere → VALIDATION_UNAVAILABLE
  5. Active method execution → TECHNICAL_FAILURE preserved
  6. Generated method fails validation → VALIDATION_UNAVAILABLE
  7. Discovery disabled + no source → VALIDATION_UNAVAILABLE
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from engine.models import DecisionStatus, DocumentResult, EvidenceQuality
from engine.validation_engine import ValidationEngine
from execution.models import ExecutionResult, ExecutionDecisionStatus
from registry.models import ValidationMethod, MethodType, MethodStatus, CURRENT_METHOD_SCHEMA
from registry.repository import MethodRegistry
from validation.models import ValidationReport, ValidationReportStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_method(method_id: str = "M_ACTIVE") -> ValidationMethod:
    return ValidationMethod(
        method_id=method_id,
        method_type=MethodType.HTTP,
        source_url="https://mock.example.com",
        country="India",
        document_type="CDC",
        status=MethodStatus.ACTIVE,
        # A properly-provisioned method carries a confirmed success marker.
        expected_responses={
            "success_keywords": ["valid"],
            "method_schema": CURRENT_METHOD_SCHEMA,
            "document_type_key": "IN_CDC",
        },
    )


def _exec_result(decision: str, error: str = None) -> ExecutionResult:
    return ExecutionResult(
        decision_status=ExecutionDecisionStatus(decision),
        evidence={"mock": True},
        logs="mock",
        error=error,
    )


def _mock_runner(decision: str, error: str = None) -> MagicMock:
    runner = MagicMock()
    runner.execute_method.return_value = _exec_result(decision, error)
    return runner


def _mock_validator(status: ValidationReportStatus) -> MagicMock:
    validator = MagicMock()
    report = ValidationReport(
        method_id="M_GEN",
        method_version=1,
        status=status,
        failure_reason=None if status == ValidationReportStatus.PASSED else "Mock failure",
    )
    validator.validate.return_value = report
    return validator


def _profile(country: str = "India", doc_type: str = "CDC") -> dict:
    return {
        "issuing_country": country,
        "document_type": doc_type,
        "document_numbers": ["ABC123"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidationEngine(unittest.TestCase):

    # 1. Active method in registry -------------------------------------------

    def test_active_method_executed_directly(self):
        """When an active method exists it is executed and never triggers generation."""
        db_path = "test_engine_1.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            registry.register_method(_active_method())

            runner = _mock_runner("VERIFIED")
            engine = ValidationEngine(registry=registry, runner=runner)
            engine.validator = _mock_validator(ValidationReportStatus.PASSED)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            self.assertEqual(decision.document_result, DocumentResult.VALID)
            self.assertEqual(decision.evidence_quality, EvidenceQuality.HIGH)
            self.assertEqual(decision.method_id, "M_ACTIVE")
            # validator.validate should NOT have been called
            engine.validator.validate.assert_not_called()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 2. No active method + DB source ----------------------------------------

    @patch("engine.validation_engine.remember_source")
    @patch("engine.validation_engine.lookup_source")
    @patch("engine.validation_engine.generate_candidate_method")
    def test_db_source_triggers_generation_and_execution(self, mock_gen, mock_lookup, mock_remember):
        db_path = "test_engine_2.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")

            mock_lookup.return_value = [{"country": "India", "url": "https://mock.com"}]
            candidate = _active_method("M_GEN")
            candidate.status = MethodStatus.TESTING
            mock_gen.return_value = candidate

            engine = ValidationEngine(registry=registry, runner=runner)
            engine.validator = _mock_validator(ValidationReportStatus.PASSED)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            mock_lookup.assert_called_once()
            mock_gen.assert_called_once()
            engine.validator.validate.assert_called_once()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 3. No active method + no DB source + discovery -------------------------

    @patch("engine.validation_engine.remember_source")
    @patch("engine.validation_engine.lookup_source")
    @patch("engine.validation_engine.generate_candidate_method")
    def test_discovery_triggered_when_no_db_source(self, mock_gen, mock_lookup, mock_remember):
        db_path = "test_engine_3.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")

            mock_lookup.return_value = []          # no DB source
            candidate = _active_method("M_DISC")
            candidate.status = MethodStatus.TESTING
            mock_gen.return_value = candidate

            engine = ValidationEngine(
                registry=registry, runner=runner, enable_discovery=True
            )
            engine.validator = _mock_validator(ValidationReportStatus.PASSED)

            # Patch the discovery agent so it doesn't do real web searches
            mock_discovery_result = {
                "result": {"source": {"url": "https://discovered.example.com"}},
            }
            with patch("engine.validation_engine.run_discovery", return_value=mock_discovery_result):
                decision = engine.validate(_profile("NewCountry", "CoC"))

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            mock_gen.assert_called_once()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 3b. DB-source generation refuses → fall through to discovery ----------

    @patch("engine.validation_engine.remember_source")
    @patch("engine.validation_engine.lookup_source")
    @patch("engine.validation_engine.generate_candidate_method")
    def test_generation_failure_falls_through_to_discovery(self, mock_gen, mock_lookup, mock_remember):
        """A generation refusal off a DB source retries via discovery.

        Live case: the Indian SID's only DB source was esamudra (tagged
        IN_CDC/IN_INDOS); the evidence gate refused an SID method there, and
        only discovery — seeded from the document's printed domain — could
        find the SID verifier. The old code returned the refusal immediately.
        """
        db_path = "test_engine_3b.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")

            mock_lookup.return_value = [
                {"url": "https://unrelated.example.com", "country": "India"}
            ]
            candidate = _active_method("M_DISC_SID")
            candidate.status = MethodStatus.TESTING
            # First generation (from the DB source) refuses; the second
            # (from the discovered source) succeeds.
            mock_gen.side_effect = [
                Exception("Source page has no workflow option or API-path evidence"),
                candidate,
            ]

            engine = ValidationEngine(
                registry=registry, runner=runner, enable_discovery=True
            )
            engine.validator = _mock_validator(ValidationReportStatus.PASSED)

            mock_discovery_result = {
                "result": {"url": "https://sid-portal.example.com/verify"},
            }
            with patch(
                "engine.validation_engine.run_discovery", return_value=mock_discovery_result
            ) as mock_disc:
                decision = engine.validate(_profile("India", "SID"))

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            self.assertEqual(mock_gen.call_count, 2)
            mock_disc.assert_called_once()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 3c. DB-source generation refuses + discovery finds nothing -------------

    @patch("engine.validation_engine.remember_source")
    @patch("engine.validation_engine.lookup_source")
    @patch("engine.validation_engine.generate_candidate_method")
    def test_generation_failure_preserved_when_discovery_finds_nothing(self, mock_gen, mock_lookup, mock_remember):
        db_path = "test_engine_3c.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")

            mock_lookup.return_value = [
                {"url": "https://unrelated.example.com", "country": "India"}
            ]
            mock_gen.side_effect = Exception(
                "Source page has no workflow option or API-path evidence"
            )

            engine = ValidationEngine(
                registry=registry, runner=runner, enable_discovery=True
            )
            engine.validator = _mock_validator(ValidationReportStatus.PASSED)

            with patch(
                "engine.validation_engine.run_discovery",
                return_value={"result": {}},
            ):
                decision = engine.validate(_profile("India", "SID"))

            # The specific generation refusal is the more informative cause —
            # it must not be replaced by a generic "no source" unavailable.
            self.assertEqual(decision.decision_status, DecisionStatus.TECHNICAL_FAILURE)
            self.assertIn("no workflow option", decision.failure_reason)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 4. No source anywhere --------------------------------------------------

    @patch("engine.validation_engine.lookup_source")
    def test_no_source_returns_validation_unavailable(self, mock_lookup):
        db_path = "test_engine_4.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")
            mock_lookup.return_value = []

            engine = ValidationEngine(
                registry=registry, runner=runner, enable_discovery=False
            )

            decision = engine.validate(_profile("UnknownLand", "XDoc"))

            self.assertEqual(decision.decision_status, DecisionStatus.VALIDATION_UNAVAILABLE)
            self.assertEqual(decision.document_result, DocumentResult.UNKNOWN)
            self.assertEqual(decision.evidence_quality, EvidenceQuality.NONE)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 5. Active method → TECHNICAL_FAILURE -----------------------------------

    def test_technical_failure_does_not_mark_document_invalid(self):
        db_path = "test_engine_5.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            registry.register_method(_active_method())

            runner = _mock_runner("TECHNICAL_FAILURE", error="Container crash")
            engine = ValidationEngine(registry=registry, runner=runner)

            decision = engine.validate(_profile())

            # A technical failure must NOT produce DocumentResult.INVALID
            self.assertNotEqual(decision.document_result, DocumentResult.INVALID)
            self.assertEqual(decision.decision_status, DecisionStatus.TECHNICAL_FAILURE)
            self.assertEqual(decision.document_result, DocumentResult.UNKNOWN)
            self.assertEqual(decision.evidence_quality, EvidenceQuality.NONE)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_empty_comparison_response_has_unknown_document_result(self):
        db_path = "test_engine_empty_comparison.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            method = _active_method("M_EMPTY_COMPARISON")
            method.expected_responses = {
                "comparison_mode": "field_match",
                "method_schema": CURRENT_METHOD_SCHEMA,
                "document_type_key": "IN_CDC",
            }
            method.required_inputs = ["document_number"]
            registry.register_method(method)

            runner = MagicMock()
            runner.execute_method.return_value = ExecutionResult(
                decision_status=ExecutionDecisionStatus.UNCERTAIN,
                evidence={"http_status": 200},
                raw_response="",
            )
            engine = ValidationEngine(
                registry=registry,
                runner=runner,
                credential_provider=lambda: {"document_number": "ABC123"},
            )

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.TECHNICAL_FAILURE)
            self.assertEqual(decision.document_result, DocumentResult.UNKNOWN)
            self.assertEqual(decision.evidence_quality, EvidenceQuality.NONE)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 6. Generated method fails validation -----------------------------------

    @patch("engine.validation_engine.lookup_source")
    @patch("engine.validation_engine.generate_candidate_method")
    def test_failed_validation_returns_unavailable(self, mock_gen, mock_lookup):
        db_path = "test_engine_6.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            runner = _mock_runner("VERIFIED")

            mock_lookup.return_value = [{"country": "India", "url": "https://mock.com"}]
            candidate = _active_method("M_FAIL")
            candidate.status = MethodStatus.TESTING
            mock_gen.return_value = candidate

            engine = ValidationEngine(registry=registry, runner=runner)
            engine.validator = _mock_validator(ValidationReportStatus.FAILED)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VALIDATION_UNAVAILABLE)
            self.assertIn("failed validation", decision.failure_reason)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 7. Active method + REJECTED -------------------------------------------

    def test_active_method_rejected_result(self):
        db_path = "test_engine_7.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            registry.register_method(_active_method())

            runner = _mock_runner("REJECTED")
            engine = ValidationEngine(registry=registry, runner=runner)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.REJECTED)
            self.assertEqual(decision.document_result, DocumentResult.INVALID)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 8. VERIFIED without confirmed success markers → capped at LOW ----------

    def test_verified_without_success_markers_capped_to_low(self):
        """A method with no confirmed success_keywords must never yield a
        HIGH/MEDIUM-quality VERIFIED decision (REJECTED-only policy)."""
        db_path = "test_engine_8.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            method = _active_method("M_REJ_ONLY")
            method.expected_responses = {
                "success_keywords": [],
                "method_schema": CURRENT_METHOD_SCHEMA,
                "document_type_key": "IN_CDC",
            }
            method.limitations = [
                "REJECTED-only until a human-confirmed probe supplies the success marker."
            ]
            registry.register_method(method)

            runner = _mock_runner("VERIFIED")
            engine = ValidationEngine(registry=registry, runner=runner)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            self.assertEqual(decision.evidence_quality, EvidenceQuality.LOW)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # 9. Real lookup values come from the credential provider ----------------

    def test_inputs_sourced_from_credential_provider_not_redacted_profile(self):
        """document_number must come from the raw extraction (provider), never
        from the redacted profile whose value is a [DOCUMENT_NUMBER] token."""
        db_path = "test_engine_9.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            method = _active_method("M_CREDS")
            method.required_inputs = ["document_number"]
            registry.register_method(method)

            runner = _mock_runner("VERIFIED")
            engine = ValidationEngine(
                registry=registry,
                runner=runner,
                credential_provider=lambda: {
                    "document_number": "09NL5250",
                    "date_of_birth": "07/09/1992",
                },
            )

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VERIFIED)
            sent_inputs = runner.execute_method.call_args[0][0].inputs
            self.assertEqual(sent_inputs["document_number"], "09NL5250")
            self.assertEqual(sent_inputs["date_of_birth"], "07/09/1992")
            # The redaction token must never be submitted
            self.assertNotIn("[DOCUMENT_NUMBER]", json.dumps(sent_inputs))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_missing_credentials_refuse_execution(self):
        """Without a credential provider, a method requiring document_number
        must be refused (VALIDATION_UNAVAILABLE) — never executed with the
        redacted profile's token as the lookup value."""
        db_path = "test_engine_10.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            method = _active_method("M_NOCRED")
            method.required_inputs = ["document_number", "date_of_birth"]
            registry.register_method(method)

            runner = _mock_runner("VERIFIED")
            engine = ValidationEngine(registry=registry, runner=runner)

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VALIDATION_UNAVAILABLE)
            self.assertIn("Refusing to submit redaction tokens", decision.failure_reason)
            # The runner must never have been called
            runner.execute_method.assert_not_called()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_redaction_token_as_credential_is_rejected(self):
        """A provider that (buggy) returns tokens must not have them submitted."""
        db_path = "test_engine_11.db"
        try:
            registry = MethodRegistry(db_path=db_path)
            method = _active_method("M_TOKEN")
            method.required_inputs = ["document_number"]
            registry.register_method(method)

            runner = _mock_runner("VERIFIED")
            engine = ValidationEngine(
                registry=registry,
                runner=runner,
                credential_provider=lambda: {"document_number": "[DOCUMENT_NUMBER]"},
            )

            decision = engine.validate(_profile())

            self.assertEqual(decision.decision_status, DecisionStatus.VALIDATION_UNAVAILABLE)
            runner.execute_method.assert_not_called()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
