from enum import Enum
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class DocumentResult(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    NOT_FOUND = "NOT_FOUND"
    EXPIRED = "EXPIRED"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class EvidenceQuality(str, Enum):
    HIGH = "HIGH"       # Direct API response with authoritative confirmation
    MEDIUM = "MEDIUM"   # Web form / keyword match from official source
    LOW = "LOW"         # Indirect / uncertain source
    NONE = "NONE"       # No evidence collected


class DecisionStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


class ValidationDecision(BaseModel):
    """The final, authoritative output of the validation engine."""
    document_result: DocumentResult
    evidence_quality: EvidenceQuality
    decision_status: DecisionStatus
    evidence: Dict[str, Any] = Field(default_factory=dict)
    method_id: Optional[str] = None
    method_type: Optional[str] = None
    source_url: Optional[str] = None
    raw_response: Optional[str] = None
    failure_reason: Optional[str] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
