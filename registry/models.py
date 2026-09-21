from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field

class MethodStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    TESTING = "TESTING"
    INACTIVE = "INACTIVE"

class MethodType(str, Enum):
    HTTP = "HTTP"
    WEB_FORM = "WEB_FORM"
    BROWSER = "BROWSER"
    QR_URL = "QR_URL"
    MANUAL = "MANUAL"


CURRENT_METHOD_SCHEMA = "field-comparison-v3"

class ValidationMethod(BaseModel):
    method_id: str
    document_type: Optional[str] = None
    country: Optional[str] = None
    issuer: Optional[str] = None
    method_type: MethodType
    version: int = 1
    source_url: str
    required_inputs: List[str] = Field(default_factory=list)
    execution_steps: List[Dict[str, Any]] = Field(default_factory=list)
    expected_responses: Dict[str, Any] = Field(default_factory=dict)
    limitations: List[str] = Field(default_factory=list)
    status: MethodStatus = MethodStatus.TESTING
