"""Structural coverage: does a method actually consume its required inputs?

Declarative methods (HTTP/WEB_FORM/...) must reference each required input as a
``{{field}}`` placeholder somewhere in ``execution_steps``. SCRIPT methods carry
code instead of steps and must read each declared input via
``get_input("field")``.

Shared by the validator's hard pre-check and the generator's SCRIPT fallback so
the two never drift. Hard-wired invariant, not an LLM heuristic: live case
(2026-09-29) had a document-number param pinned to the literal "Indos" with no
``{{document_number}}`` anywhere, so the known-fake probe passed byte-identically
for any input and genuine documents were confidently REJECTED.
"""

import json
import re
from typing import List

from registry.models import MethodType, ValidationMethod


def missing_required_inputs(method: ValidationMethod) -> List[str]:
    """Required inputs the execution body never actually consumes."""
    required = method.required_inputs or []
    if method.method_type == MethodType.SCRIPT:
        body = method.script_source or ""
        if not body and method.execution_steps:
            body = str((method.execution_steps[0] or {}).get("code", ""))
        return [
            f for f in required
            if not re.search(
                r"get_input\(\s*['\"]" + re.escape(f) + r"['\"]", body
            )
        ]

    steps_body = re.sub(
        r"\s+", " ", json.dumps(method.execution_steps or [], default=str)
    )
    return [f for f in required if "{{" + f + "}}" not in steps_body]
