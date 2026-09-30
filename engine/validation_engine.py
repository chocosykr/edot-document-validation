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

from db.lookup import lookup_source, remember_source
from discovery.agent import run_discovery
from registry.models import ValidationMethod, MethodStatus, MethodType, CURRENT_METHOD_SCHEMA
from registry.document_types import profile_document_type_key
from registry.repository import MethodRegistry
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

# Default test case used when validating a freshly generated method. Inputs
# are built PER METHOD (see _structural_test_case): the probe must satisfy
# every required input the method declares, or the runner refuses the
# submission before any request is sent and the method can never validate.
#
# NOTE: `date_of_birth` is REQUIRED for document-number methods, not
# optional. Methods generated from an extracted XHR contract map
# dob -> {{date_of_birth}} and list it in required_inputs. If the structural
# test omits DOB, the executor submits an empty DOB string, which can produce
# a third server response that neither confirmed discriminator accounts for —
# the structural test would then fail, or pass for the wrong reason.
def _structural_test_case(method=None) -> TestCase:
    from execution.safety import structural_test_value_for, contact_only_inputs

    inputs = {
        "document_number": "TEST_STRUCTURAL_001",
        "date_of_birth": "01/01/1990",
    }
    if method is not None:
        for name in (method.required_inputs or []):
            if name in inputs or not str(name or "").strip():
                continue
            inputs[name] = structural_test_value_for(name)
    return TestCase(
        name="structural_check",
        inputs=inputs,
        expected_decision="REJECTED",
        is_required=True,
    )


_GENERIC_TEST_CASES = [_structural_test_case()]

# Evidence quality by method type
_QUALITY_MAP: Dict[MethodType, EvidenceQuality] = {
    MethodType.HTTP:     EvidenceQuality.HIGH,
    MethodType.WEB_FORM: EvidenceQuality.MEDIUM,
    MethodType.QR_URL:   EvidenceQuality.MEDIUM,
    MethodType.BROWSER:  EvidenceQuality.MEDIUM,
    MethodType.MANUAL:   EvidenceQuality.LOW,
    # SCRIPT methods carry LLM-authored transport code; the harness no longer
    # reads the site itself, so treat the evidence as corroborable rather than
    # authoritative until the decider policy is extended (Phase 4.5).
    MethodType.SCRIPT:   EvidenceQuality.MEDIUM,
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
    ):
        """
        credential_provider: optional callable returning the REAL lookup
        values (document_number, date_of_birth) sourced from the raw,
        pre-redaction extraction of the document currently being validated.
        The redacted profile carries tokens like [DOCUMENT_NUMBER] which must
        NEVER be submitted to a live endpoint, so executor inputs are built
        exclusively from this provider.

        Values stay in memory only: never logged, never persisted (document
        number and DOB are PII — no exceptions). Entry points without a raw
        profile (MCP server, batch) leave credential_provider unset; the
        engine then refuses to run methods requiring those inputs instead of
        submitting tokens.
        """
        self.registry = registry or MethodRegistry()
        self.runner = runner or DockerMethodRunner()
        # The engine's validator exists to run the generator's structural
        # test (known-fake TEST_STRUCTURAL_001 against the live endpoint), so
        # it is the one caller allowed to submit those marker values. Real
        # executions (_execute_and_decide) go through the strict runner guard.
        self.validator = MethodValidator(
            runner=self.runner,
            registry=self.registry,
            allow_structural_test_values=True,
        )
        self.enable_discovery = enable_discovery
        self.credential_provider = credential_provider

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

        if not db_sources:
            # Source reuse: an authority that verifies one document type
            # (esamudra -> IN_INDOS) often verifies sibling types from the
            # SAME portal — the page's own search-type selector lists them.
            # Hand the same-country source to the generator; the narrow
            # path's doc-type evidence gate (page option / API path) decides
            # compatibility deterministically, and generation fails honestly
            # when the page does not name this document type.
            from db.lookup import lookup_country_sources
            same_country = lookup_country_sources(redacted_profile)
            if same_country:
                logger.info(
                    "No doc-type-tagged source, but %d same-country source(s) "
                    "exist — attempting source reuse via page evidence.",
                    len(same_country),
                )
                db_sources = same_country

        # A failed generation off a DB source must not end the run when
        # discovery can still find a doc-type-specific portal. Live case
        # (2026-09-28): the Indian SID's only DB source was esamudra, tagged
        # IN_CDC/IN_INDOS — the generator's evidence gate correctly refused
        # to mint an SID method there, and only discovery (seeded from the
        # document's printed dgshipping.gov.in domain) could find the SID
        # verifier. Fall-through is deliberately narrow: generation/refusal
        # failures (TECHNICAL_FAILURE) retry via discovery; a candidate that
        # DID generate but failed its structural validation stays visible in
        # the registry as TESTING and does not trigger a second search.
        generation_failure: Optional[ValidationDecision] = None

        if db_sources:
            logger.info(
                "Found %d source(s) in DB. Generating candidate method.",
                len(db_sources),
            )
            source_info = db_sources[0]
            decision = self._generate_validate_and_execute(
                redacted_profile, source_info
            )
            if decision.decision_status != DecisionStatus.TECHNICAL_FAILURE:
                return decision
            generation_failure = decision
            if not self.enable_discovery:
                return decision
            logger.warning(
                "Generation from DB source failed (%s); falling through to discovery.",
                decision.failure_reason,
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
                if generation_failure:
                    return generation_failure
                return self._unavailable(
                    f"Discovery agent failed: {e}"
                )

        if generation_failure:
            # Discovery found nothing; the generation failure is the more
            # informative cause — report it rather than a generic "no source".
            return generation_failure

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
            available_inputs = list(self._build_inputs(redacted_profile).keys())
            candidate = generate_candidate_method(
                redacted_profile,
                source_info,
                available_inputs=available_inputs,
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

        # Validate candidate (bounded test in Docker) — test inputs built
        # for THIS method so every required input is satisfiable.
        report = self.validator.validate(candidate, [_structural_test_case(candidate)])

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
        # Cumulative knowledge: the structural test just proved this source
        # deterministically refuses a known-fake lookup, i.e. the URL drives
        # a real verification endpoint. Remember it for the routing DB so
        # future documents of this type skip discovery entirely.
        try:
            doc_key = (candidate.expected_responses or {}).get("document_type_key")
            if doc_key and source_info.get("url"):
                if remember_source(
                    redacted_profile.get("issuing_country") or "",
                    source_info.get("url"),
                    doc_key,
                ):
                    logger.info(
                        "Remembered source %s for %s/%s",
                        source_info.get("url"),
                        redacted_profile.get("issuing_country"),
                        doc_key,
                    )
        except Exception as e:
            logger.warning("remember_source failed (non-fatal): %s", e)
        return self._execute_and_decide(candidate, redacted_profile)

    def _execute_and_decide(
        self,
        method: ValidationMethod,
        redacted_profile: Dict[str, Any],
    ) -> ValidationDecision:
        """Build inputs, guard required ones, execute, and build the decision."""
        inputs = self._build_inputs(redacted_profile, method=method)

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
            raw_profile = dict(raw_profile or {})

            # Merge non-PII fields from the redacted profile so that field
            # mapping discovery and comparison can match on ALL available
            # document fields (e.g. expiry_date, identifying marks), not just
            # the 3 traditional credential fields.
            for key in ("expiry_date", "issue_date"):
                value = redacted_profile.get(key)
                if value and isinstance(value, str) and not value.startswith("["):
                    raw_profile.setdefault(key, value)
            # identifying_fields may contain non-PII like identifying marks
            id_fields = redacted_profile.get("identifying_fields")
            if isinstance(id_fields, dict):
                for key, value in id_fields.items():
                    if value and isinstance(value, str) and not value.startswith("["):
                        raw_profile.setdefault(key, value)

            # Use the method's own discovered field mapping
            field_mapping = (method.expected_responses or {}).get("field_mapping")

            # If no mapping exists yet and we have a real, non-empty response
            # that is not a technical failure (e.g. an HTTP 400 CAPTCHA error),
            # discover one on the fly using the LLM.
            if (
                not field_mapping 
                and exec_result.raw_response 
                and exec_result.decision_status != ExecutionDecisionStatus.TECHNICAL_FAILURE
            ):
                field_mapping = self._discover_and_store_mapping(
                    method, exec_result.raw_response, raw_profile,
                )

            exec_result = compare_response(exec_result, raw_profile, field_mapping=field_mapping)

        return self._build_decision(exec_result, method)

    def _discover_and_store_mapping(
        self,
        method: ValidationMethod,
        raw_response: str,
        raw_profile: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        """Discover response field mapping using the LLM and store it on the method.

        Called exactly once per method when a real, non-empty response comes back
        and the method has no discovered mapping yet. The mapping is stored in
        expected_responses.field_mapping and persisted to the registry.
        """
        from validation.response_mapping import discover_field_mapping

        logger.info(
            "Discovering response field mapping for method %s", method.method_id
        )

        field_mapping = discover_field_mapping(raw_response, raw_profile)

        if not field_mapping:
            logger.warning(
                "Field mapping discovery returned nothing for method %s",
                method.method_id,
            )
            return None

        text_mappings = field_mapping.get("text_mappings", {})
        image_fields = field_mapping.get("image_fields", {})

        if not text_mappings and not image_fields:
            logger.warning(
                "Discovered mapping has no usable fields for method %s",
                method.method_id,
            )
            return None

        # Store the mapping on the method for future comparisons.
        # Persisted via the granular expected_responses update: re-registering
        # the whole method here would write back this object's stale status
        # (TESTING) over the registry's ACTIVE row and leave the version
        # untouched — a silent demotion of a working method.
        expected = dict(method.expected_responses or {})
        expected["field_mapping"] = field_mapping
        method.expected_responses = expected

        if self.registry:
            try:
                self.registry.update_expected_responses(
                    method.method_id, expected
                )
                logger.info(
                    "Stored discovered field mapping for method %s: "
                    "%d text mappings, %d image fields",
                    method.method_id,
                    len(text_mappings),
                    len(image_fields),
                )
            except Exception as e:
                logger.warning(
                    "Failed to persist field mapping for method %s: %s",
                    method.method_id, e,
                )

        return field_mapping

    def _build_inputs(
        self,
        redacted_profile: Dict[str, Any],
        method: Optional[ValidationMethod] = None,
    ) -> Dict[str, str]:
        """
        Build the inputs the executor scripts need.

        document_number and date_of_birth come EXCLUSIVELY from the
        credential provider (raw, pre-redaction extraction). The redacted
        profile's values are tokens ([DOCUMENT_NUMBER], [DATE_OF_BIRTH]) —
        submitting them to a live endpoint produces a meaningless third
        server response and, worse, normalizes token submission.

        Non-PII lookup keys (e.g. a QR verification_url) still come from the
        redacted profile.

        The provider is called with the method's required_inputs when it
        accepts an argument. A person-folder provider
        (fixtures/person_folder.py) uses that list to resolve missing
        required IDENTITY keys from the subject's other documents — lookup
        context only; it never changes which document is being verified.
        """
        inputs: Dict[str, str] = {}

        credentials: Dict[str, str] = {}
        if self.credential_provider is not None:
            try:
                try:
                    credentials = (
                        self.credential_provider(
                            list((method.required_inputs if method else []) or [])
                        )
                        or {}
                    )
                except TypeError:
                    # Provider predates the required_inputs argument.
                    credentials = self.credential_provider() or {}
            except Exception as e:
                logger.warning("credential_provider failed: %s", e)
                credentials = {}

        if not isinstance(credentials, dict):
            credentials = {}

        # Drop OCR-garbage identity values (e.g. a DOB that cannot be a real
        # calendar date, or a DOB printed AFTER the issue date) BEFORE they
        # can be submitted to a live registry. A garbage credential behaves
        # like a missing one: the engine refuses, or folder mode resolves the
        # field from another document — never a false not-found.
        from execution.safety import credible_credentials
        credentials = credible_credentials(credentials)

        for key, value in credentials.items():
            val_str = str(value).strip()
            if val_str and not val_str.startswith("["):
                inputs[key] = val_str

        # A QR/URL method needs the verification_url from identifying_fields
        identifying = redacted_profile.get("identifying_fields", {})
        if isinstance(identifying, dict):
            url = identifying.get("verification_url") or identifying.get("qr_url")
            if url:
                inputs["verification_url"] = str(url)

        doc_type_key = redacted_profile.get("document_type_key")
        if doc_type_key:
            inputs["document_type_key"] = str(doc_type_key)

        country = redacted_profile.get("issuing_country")
        if country:
            inputs["issuing_country"] = str(country)

        return inputs

    @staticmethod
    def _missing_required_inputs(
        method: ValidationMethod, inputs: Dict[str, str]
    ) -> List[str]:
        """Required inputs the engine could not supply real values for.

        Fields declared contact_only_inputs (notification email/phone etc.,
        no bearing on the identity match) are exempt: the runner synthesizes
        a plausible-format inert value for them at execution time.
        """
        from execution.safety import contact_only_inputs
        contact_fields = contact_only_inputs(method)
        return [
            k for k in (method.required_inputs or [])
            if not inputs.get(k) and k not in contact_fields
        ]

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
