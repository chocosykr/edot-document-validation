from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class AttemptOutcome(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


class ValidationReportStatus(str, Enum):
    PASSED = "PASSED"       # All required criteria met — method can be activated
    FAILED = "FAILED"       # Max attempts exhausted without passing
    ERROR = "ERROR"         # Could not run at all (infra failure)


class TestCase(BaseModel):
    """A single test input + expected outcome for a validation method."""
    name: str
    inputs: Dict[str, str]
    expected_decision: str          # e.g. "VERIFIED", "REJECTED"
    is_required: bool = True        # Required test cases must pass for the report to pass


class ValidationAttempt(BaseModel):
    """Record of a single execution attempt during validation."""
    attempt_number: int
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    test_case_name: str
    inputs: Dict[str, str]
    expected_decision: str
    actual_decision: str
    outcome: AttemptOutcome
    logs: str = ""
    raw_response: str = ""   # executor's response body (bounded) — separate from Docker infra logs
    error: Optional[str] = None
    improvement_applied: Optional[str] = None


class ValidationReport(BaseModel):
    """Full report of a method validation run."""
    method_id: str
    method_version: int
    status: ValidationReportStatus
    attempts: List[ValidationAttempt] = Field(default_factory=list)
    failure_reason: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
