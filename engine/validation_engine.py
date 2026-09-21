"""
Production Validation Engine
=============================
Orchestrates the full validation pipeline:

  redacted_profile
        │
        ▼
  1. Registry lookup    → ACTIVE method exists?
        │                       │
        │ Yes                   │ No
        ▼                       ▼
  2. Execute method      3. DB source lookup
        │                       │
        ▼                       │ Source exists?
  4. Build decision       ┌─────┘
                          │ Yes          │ No
                          ▼              ▼
                   Generate method   Discovery agent
                          │              │
                          └──────┬───────┘
                                 ▼
                          Validate method (Stage 5)
                                 │
                          Passed? Execute → Decision
                                 │
                          Failed? → VALIDATION_UNAVAILABLE

Handles:
- TECHNICAL_FAILURE without marking the document invalid
- Graceful VALIDATION_UNAVAILABLE when no method can be built
- Evidence quality assignment per method type
"""

import logging
from typing import Optional, Dict, Any, List, Callable

from db.lookup import lookup_source
from discovery.agent import run_discovery
from registry.models import ValidationMethod, MethodStatus, MethodType, CURRENT_METHOD_SCHEMA
from registry.document_types import profile_document_type_key
from registry.repository import MethodRegistry
from registry.seed_store import SeedStore
from generation.generator import generate_candidate_method
from execution.docker_runner import DockerMethodRunner
from execution.models import ExecutionRequest, ExecutionDecisionStatus
from validation.validator import MethodValidator
from validation.field_comparison import compare_response
from validation.models import TestCase, ValidationReportStatus
from engine.models import (
    ValidationDecision,
    DocumentResult,
    EvidenceQuality,
    DecisionStatus,
)

logger = logging.getLogger(__name__)

# Default test cases used when validating a freshly generated method.
# These are intentionally generic; real test cases should be seeded by
# the generator from source documentation.
#
# NOTE: `date_of_birth` is REQUIRED here, not optional. Methods generated
# from an extracted XHR contract map dob -> {{date_of_birth}} and list it in
# required_inputs. If the structural test omits DOB, the executor submits an
# empty DOB string, which can produce a third server response that neither
# confirmed discriminator accounts for — the structural test would then fail,
# or pass for the wrong reason.
_GENERIC_TEST_CASES = [
    TestCase(
        name="structural_check",
        inputs={
            "document_number": "TEST_STRUCTURAL_001",
            "date_of_birth": "01/01/1990",
        },
        expected_decision="REJECTED",
        is_required=True,
    ),
]

# Evidence quality by method type
_QUALITY_MAP: Dict[MethodType, EvidenceQuality] = {
    MethodType.HTTP:     EvidenceQuality.HIGH,
    MethodType.WEB_FORM: EvidenceQuality.MEDIUM,
    MethodType.QR_URL:   EvidenceQuality.MEDIUM,
    MethodType.BROWSER:  EvidenceQuality.MEDIUM,
    MethodType.MANUAL:   EvidenceQuality.LOW,
}


class ValidationEngine:
    """
    Main entry point for document validation.
    """

    def __init__(
        self,
        registry: Optional[MethodRegistry] = None,
        runner: Optional[DockerMethodRunner] = None,
        enable_discovery: bool = True,
        credential_provider: Optional[Callable[[], Optional[Dict[str, str]]]] = None,
        seed_store: Optional["SeedStore"] = None,
    ):
        """
        credential_provider: optional callable returning the REAL lookup
        values (document_number, date_of_birth) sourced from the raw,
        pre-redaction extraction of the document currently being validated.
        The redacted profile carries tokens like [DOCUMENT_NUMBER] which must
        NEVER be submitted to a live endpoint, so executor inputs are built
        exclusively from this provider.

        seed_store: optional SeedStore holding the encrypted seed credential
        (one real, confirmed-valid record per registry/document-type,
        onboarded once by a consenting person). The seed — NOT the current
        document's values — is the real-input source for the discriminator
        discovery probe; a seed valid for one registry proves nothing about
        any other registry.

        Values from both sources stay in memory only: never logged, never
        persisted (document number and DOB are PII — no exceptions). Entry
        points without a raw profile (MCP server, batch) leave
        credential_provider unset; the engine then refuses to run methods
        requiring those inputs instead of submitting tokens.
        """
        self.registry = registry or MethodRegistry()
        self.runner = runner or DockerMethodRunner()
        self.validator = MethodValidator(runner=self.runner, registry=self.registry)
        self.enable_discovery = enable_discovery
        self.credential_provider = credential_provider
        self.seed_store = seed_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def validate(self, redacted_profile: Dict[str, Any]) -> ValidationDecision:
        """
        Run the full validation pipeline for a document.

        Args:
            redacted_profile: Output of ocr/extractor.py — PII-free document facts.

        Returns:
            ValidationDecision with document_result, evidence_quality, decision_status,
            and supporting evidence.
        """
        country = redacted_profile.get("issuing_country") or ""
        doc_type = redacted_profile.get("document_type") or ""

        logger.info(
            "Starting validation — country=%r doc_type=%r", country, doc_type
        )

        # ----------------------------------------------------------
        # Step 1: Check registry for an ACTIVE method
        # ----------------------------------------------------------
        active_methods = self._find_active_methods(country, doc_type, redacted_profile)

        if active_methods:
            logger.info(
                "Found %d active method(s) in registry. Executing best match.",
                len(active_methods),
            )
            return self._execute_best_method(active_methods, redacted_profile)

        # ----------------------------------------------------------
        # Step 2: No active method — check confirmed source DB
        # ----------------------------------------------------------
        logger.info("No active method found. Checking confirmed source database.")
        db_sources = lookup_source(redacted_profile)

        if db_sources:
            logger.info(
                "Found %d source(s) in DB. Generating candidate method.",
                len(db_sources),
            )
            source_info = db_sources[0]
            return self._generate_validate_and_execute(
                redacted_profile, source_info
            )

        # ----------------------------------------------------------
        # Step 3: No DB source — try discovery (if enabled)
        # ----------------------------------------------------------
        if self.enable_discovery:
            logger.info("No DB source found. Running discovery agent.")
            try:
                discovery_result = run_discovery(redacted_profile)
                best_source = discovery_result.get("result", {})
                if best_source:
                    logger.info("Discovery found a source. Generating candidate method.")
                    return self._generate_validate_and_execute(
                        redacted_profile, best_source
                    )
            except Exception as e:
                logger.error("Discovery agent failed: %s", e)
                return self._unavailable(
                    f"Discovery agent failed: {e}"
                )

        # ----------------------------------------------------------
        # Step 4: Nothing found — cannot validate
        # ----------------------------------------------------------
        logger.warning("No validation source found for this document.")
        return self._unavailable(
            "No confirmed source or active method available for "
            f"country={country!r}, document_type={doc_type!r}."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_active_methods(
        self, country: str, doc_type: str, redacted_profile: Optional[Dict[str, Any]] = None
    ) -> List[ValidationMethod]:
        try:
            methods = self.registry.find_methods(country=country, document_type=doc_type)
            required_document_key = profile_document_type_key(redacted_profile or {"document_type": doc_type})
            return [
                m for m in methods
                if m.status == MethodStatus.ACTIVE
                and (m.expected_responses or {}).get("method_schema") == CURRENT_METHOD_SCHEMA
                and (m.expected_responses or {}).get("document_type_key") == required_document_key
            ]
        except Exception as e:
            logger.error("Registry lookup failed: %s", e)
            return []

    def _execute_best_method(
        self,
        methods: List[ValidationMethod],
        redacted_profile: Dict[str, Any],
    ) -> ValidationDecision:
        method = methods[0]
        return self._execute_and_decide(method, redacted_profile)

    def _generate_validate_and_execute(
        self,
        redacted_profile: Dict[str, Any],
        source_info: Dict[str, Any],
    ) -> ValidationDecision:
        # Generate candidate
        try:
            candidate = generate_candidate_method(
                redacted_profile,
                source_info,
                seed_provider=self._seed_provider(redacted_profile),
            )
        except Exception as e:
            logger.error("Method generation failed: %s", e)
            return self._failure(f"Method generation failed: {e}")

        # Register candidate (status=TESTING)
        try:
            self.registry.register_method(candidate)
        except Exception as e:
            logger.error("Failed to register candidate: %s", e)
            return self._failure(f"Registry error: {e}")

        # Validate candidate (bounded test in Docker)
        report = self.validator.validate(candidate, _GENERIC_TEST_CASES)

        if report.status != ValidationReportStatus.PASSED:
            logger.warning(
                "Candidate method %s failed validation. Returning VALIDATION_UNAVAILABLE.",
                candidate.method_id,
            )
            return self._unavailable(
                f"Generated method {candidate.method_id} failed validation: "
                f"{report.failure_reason}",
                method=candidate
            )

        # Execute the now-active method
        logger.info(
            "Candidate %s passed validation. Executing.", candidate.method_id
        )
        return self._execute_and_decide(candidate, redacted_profile)

    def _seed_provider(
        self, redacted_profile: Dict[str, Any]
    ) -> Optional[Callable[[], Optional[Dict[str, str]]]]:
        """
        Build the real-input source for the discriminator-discovery probe.

        Policy (anti-circularity — the document under validation must never
        calibrate the discriminator that judges it):
          - Seed store wired (production wiring): the ONLY real-input source
            is the STORED SEED for (issuing_country, document_type). If no
            seed is onboarded for the scope — or the store is unreadable —
            return None so the probe runs one-sided (REJECTED-only; the
            method stays capped at evidence_quality LOW). The current
            document's raw extraction is NEVER substituted: it may be
            forged, and probing with it would cache a discriminator learned
            from untrusted data, silently.
          - No seed store at all (legacy wiring/tests): backward-compatible
            fallback to the current document's raw extraction via
            credential_provider. The values still pass the same non-token
            guard as executor inputs.

        The returned callable decrypts lazily so the seed plaintext exists in
        memory only while the probe runs; no seed value is ever logged.
        """
        if self.seed_store is not None:
            country = redacted_profile.get("issuing_country") or ""
            doc_type = redacted_profile.get("document_type") or ""
            try:
                if self.seed_store.has_seed(country, doc_type):
                    def seed_from_store():
                        values = self.seed_store.get_seed(country, doc_type) or {}
                        if isinstance(values, dict):
                            workflow = values.get("workflow_params") or {}
                            if workflow:
                                return {**values, "workflow_params": dict(workflow)}
                        return values
                    return seed_from_store
            except Exception as e:
                logger.warning("Seed store lookup failed: %s", e)
            # Seed store in force but no usable seed for this scope: probe
            # without real inputs (REJECTED-only) — never substitute the
            # document under validation.
            return None

        if self.credential_provider is None:
            return None

        def seed_from_raw_extraction():
            values = self.credential_provider() or {}
            if not isinstance(values, dict):
                return None
            clean = {
                k: v.strip()
                for k, v in values.items()
                if isinstance(v, str) and v.strip() and not v.strip().startswith("[")
            }
            return clean or None

        return seed_from_raw_extraction

    def _execute_and_decide(
        self,
        method: ValidationMethod,
        redacted_profile: Dict[str, Any],
    ) -> ValidationDecision:
        """Build inputs, guard required ones, execute, and build the decision."""
        inputs = self._build_inputs(redacted_profile)

        missing = self._missing_required_inputs(method, inputs)
        if missing:
            # A redaction token must never be submitted to a live endpoint.
            # Real values come only from the raw extraction via the
            # credential provider; without them, refuse rather than guess.
            reason = (
                f"Method {method.method_id} requires real value(s) {missing} "
                "which were not available from the raw document extraction. "
                "Refusing to submit redaction tokens to a live endpoint."
            )
            logger.warning(reason)
            return self._unavailable(reason, method=method)

        req = ExecutionRequest(method=method, inputs=inputs)
        exec_result = self.runner.execute_method(req)

        if (method.expected_responses or {}).get("comparison_mode") == "field_match":
            raw_profile = self.credential_provider() if self.credential_provider else {}
            exec_result = compare_response(exec_result, raw_profile or {})

        return self._build_decision(exec_result, method)

    def _build_inputs(self, redacted_profile: Dict[str, Any]) -> Dict[str, str]:
        """
        Build the inputs the executor scripts need.

        document_number and date_of_birth come EXCLUSIVELY from the
        credential provider (raw, pre-redaction extraction). The redacted
        profile's values are tokens ([DOCUMENT_NUMBER], [DATE_OF_BIRTH]) —
        submitting them to a live endpoint produces a meaningless third
        server response and, worse, normalizes token submission.

        Non-PII lookup keys (e.g. a QR verification_url) still come from the
        redacted profile.
        """
        inputs: Dict[str, str] = {}

        credentials: Dict[str, str] = {}
        if self.credential_provider is not None:
            try:
                credentials = self.credential_provider() or {}
            except Exception as e:
                logger.warning("credential_provider failed: %s", e)
                credentials = {}

        if not isinstance(credentials, dict):
            credentials = {}

        doc_number = str(credentials.get("document_number") or "").strip()
        if doc_number and not doc_number.startswith("["):
            inputs["document_number"] = doc_number

        dob = str(credentials.get("date_of_birth") or "").strip()
        if dob and not dob.startswith("["):
            inputs["date_of_birth"] = dob

        # A QR/URL method needs the verification_url from identifying_fields
        identifying = redacted_profile.get("identifying_fields", {})
        if isinstance(identifying, dict):
            url = identifying.get("verification_url") or identifying.get("qr_url")
            if url:
                inputs["verification_url"] = str(url)

        return inputs

    @staticmethod
    def _missing_required_inputs(
        method: ValidationMethod, inputs: Dict[str, str]
    ) -> List[str]:
        """Required inputs the engine could not supply real values for."""
        return [k for k in (method.required_inputs or []) if not inputs.get(k)]

    def _build_decision(
        self, exec_result, method: ValidationMethod
    ) -> ValidationDecision:
        status = exec_result.decision_status

        # Map execution status → document result
        doc_result_map = {
            ExecutionDecisionStatus.VERIFIED:               DocumentResult.VALID,
            ExecutionDecisionStatus.REJECTED:               DocumentResult.INVALID,
            ExecutionDecisionStatus.UNCERTAIN:              DocumentResult.UNKNOWN,
            ExecutionDecisionStatus.VALIDATION_UNAVAILABLE: DocumentResult.UNKNOWN,
            ExecutionDecisionStatus.TECHNICAL_FAILURE:      DocumentResult.UNKNOWN,
        }

        decision_status_map = {
            ExecutionDecisionStatus.VERIFIED:               DecisionStatus.VERIFIED,
            ExecutionDecisionStatus.REJECTED:               DecisionStatus.REJECTED,
            ExecutionDecisionStatus.UNCERTAIN:              DecisionStatus.UNCERTAIN,
            ExecutionDecisionStatus.VALIDATION_UNAVAILABLE: DecisionStatus.VALIDATION_UNAVAILABLE,
            ExecutionDecisionStatus.TECHNICAL_FAILURE:      DecisionStatus.TECHNICAL_FAILURE,
        }

        quality = (
            _QUALITY_MAP.get(method.method_type, EvidenceQuality.LOW)
            if status not in (
                ExecutionDecisionStatus.TECHNICAL_FAILURE,
                ExecutionDecisionStatus.VALIDATION_UNAVAILABLE,
            )
            else EvidenceQuality.NONE
        )

        success_keywords = (method.expected_responses or {}).get("success_keywords") or []
        if (
            (method.expected_responses or {}).get("comparison_mode") != "field_match"
            and status == ExecutionDecisionStatus.VERIFIED
            and not success_keywords
            and quality != EvidenceQuality.NONE
        ):
            quality = EvidenceQuality.LOW

        return ValidationDecision(
            document_result=doc_result_map[status],
            evidence_quality=quality,
            decision_status=decision_status_map[status],
            evidence=exec_result.evidence,
            method_id=method.method_id,
            method_type=method.method_type.value,
            source_url=method.source_url,
            raw_response=exec_result.raw_response,
            failure_reason=exec_result.error,
        )

    @staticmethod
    def _unavailable(reason: str, method: Optional[ValidationMethod] = None) -> ValidationDecision:
        return ValidationDecision(
            document_result=DocumentResult.UNKNOWN,
            evidence_quality=EvidenceQuality.NONE,
            decision_status=DecisionStatus.VALIDATION_UNAVAILABLE,
            failure_reason=reason,
            method_id=method.method_id if method else None,
            method_type=method.method_type.value if method else None,
            source_url=method.source_url if method else None,
        )

    @staticmethod
    def _failure(reason: str, method: Optional[ValidationMethod] = None) -> ValidationDecision:
        return ValidationDecision(
            document_result=DocumentResult.UNKNOWN,
            evidence_quality=EvidenceQuality.NONE,
            decision_status=DecisionStatus.TECHNICAL_FAILURE,
            failure_reason=reason,
            method_id=method.method_id if method else None,
            method_type=method.method_type.value if method else None,
            source_url=method.source_url if method else None,
        )
