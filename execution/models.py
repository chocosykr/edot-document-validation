from enum import Enum
from typing import Dict, Any, Optional
from pydantic import BaseModel
from registry.models import ValidationMethod

class ExecutionDecisionStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"

class ExecutionRequest(BaseModel):
    method: ValidationMethod
    inputs: Dict[str, str]

class ExecutionResult(BaseModel):
    decision_status: ExecutionDecisionStatus
    evidence: Dict[str, Any]
    raw_response: Optional[str] = None
    logs: str = ""
    error: Optional[str] = None
