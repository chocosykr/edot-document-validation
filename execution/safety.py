"""
Live-submission safety guard
============================

Single source of truth for the rule "refuse to submit redaction tokens or
incomplete data to a live endpoint".

The main engine applies this rule in ``engine/validation_engine.py``
(``_missing_required_inputs`` + refusal). It must apply identically on every
other path that can reach a live registry — the MCP server's
``validate_document`` tool forwards caller-supplied inputs verbatim, and the
agentic fallback loop drives that same tool. An incident (September 2026,
method ``M_MY_DMA_001``) showed the MCP path submitting incomplete/placeholder
values to the live ``dmamyanmar.org`` endpoint, producing a real HTTP 500 on a
foreign government server. Every execution path therefore funnels through
``guard_inputs()`` before any request is sent.
"""

import re
from typing import Any, Dict, List

# A redaction token looks like [PERSON_NAME], [DOCUMENT_NUMBER], [REDACTED]…
_REDACTION_TOKEN_RE = re.compile(r"^\[[A-Z][A-Z0-9 _]*\]$")

# Placeholder/obviously-fake values that must never reach a live registry.
# These cover the generic template placeholders the executor scripts support,
# the legacy validator's structural-test inputs, and a few universal fakes.
PLACEHOLDER_VALUES = frozenset({
    "test", "test_value", "placeholder", "redacted", "unknown", "n/a",
    "na", "none", "null", "example", "sample", "dummy", "xxx", "xxxx",
    "xxx123", "123456", "12345678", "1234567890", "0123456789",
    "test_structural_001",
})

# Structural-test identifiers the legacy validator seeds on purpose. They are
# placeholders like any other fake value (``is_placeholder_value`` returns
# True for them), BUT the generator's structural test is *expected* to submit
# them to live endpoints (a "known-fake must be rejected" probe), so execution
# paths running that test explicitly opt out via
# ``allow_structural_test_values=True``. No other path may.
STRUCTURAL_TEST_VALUES = frozenset({
    "test_structural_001",
})

# Structural-probe field values BY FIELD ROLE: the structural test must be
# able to satisfy every required input of a generated method (a method for a
# portal that wants CDC number + serial + passport + reply email cannot be
# probed with a document number alone — the runner refuses it before any
# request is sent, and the method can never be validated). Each field gets a
# deterministic known-fake value by matching its NAME against these patterns;
# the values stay obviously-fake (ZZ/9 digits, .invalid mail) so a structural
# probe can never be mistaken for a real lookup.
_STRUCTURAL_FIELD_PATTERNS: list[tuple[str, str]] = [
    ("passport", "ZZ0000000"),
    ("cdc", "ZZ9999999"),
    ("serial", "ZZ8888888"),
    ("indos", "ZZ7777777"),
    ("email", "structural.probe@dvs.invalid"),
    ("phone", "+950000000000"),
    ("date_of_birth", "01/01/1990"),
]
_STRUCTURAL_DEFAULT = "ZZ000111"


def structural_test_value_for(field_name: str) -> str:
    """The known-fake value the structural probe submits for this field."""
    n = str(field_name or "").lower()
    for marker, value in _STRUCTURAL_FIELD_PATTERNS:
        if marker in n:
            return value
    return _STRUCTURAL_DEFAULT

# Real-world note: shape heuristics ("does this look like a plausible
# credential number?") were considered and deliberately left out — a real
# document number could legitimately look unusual, and a false refusal here
# merely fails closed while a false acceptance submits junk to a live
# government system. Pattern blocklists + redaction tokens + repeated-char
# detection catch the placeholder shapes that actually occur in this
# codebase's prompts and test fixtures.


class UnsafeInputError(ValueError):
    """Raised when executor inputs would submit placeholder or incomplete data to a live endpoint."""


# ---------------------------------------------------------------------------
# Contact-only fields
# ---------------------------------------------------------------------------
# Some registries carry required form fields that have NO bearing on the
# identity match: a notification email the result is sent to, a callback
# phone number, a requester reference. Such a field is declared in the
# method's expected_responses as
#
#     "contact_only_inputs": ["reply_email", ...]
#
# and is then treated as SYNTHESIZABLE: it is not an unsatisfiable required
# input, and a plausible-format value is generated for it at execution time.
# This is a general schema flag any registry's method can use — it is not a
# special case for one site. Identity fields (document numbers, names, birth
# dates, anything checked against a record) must NEVER be marked contact_only;
# a fake identity value could produce a false REJECTED or a false VERIFIED.

# RFC 2606 reserves .invalid (and .test / .example) — mail to these hosts can
# never be delivered anywhere, so a synthesized address is inert by
# construction, not just by politeness.
_SYNTH_CONTACT_HOST = "notifications.dvs.invalid"

_CONTACT_HINTS = {
    "email": ("email", "e_mail", "mail"),
    "phone": ("phone", "mobile", "tel", "contact_number"),
}


def contact_only_inputs(method) -> set:
    """The method's declared contact-only (synthesizable) input names."""
    declared = (getattr(method, "expected_responses", None) or {}).get(
        "contact_only_inputs"
    )
    if isinstance(declared, list):
        return {str(x) for x in declared}
    return set()


def synthesize_contact_value(field_name: str) -> str:
    """A plausible-format, deliberately inert value for a contact-only field.

    Email addresses use the reserved .invalid TLD (RFC 2606): syntactically
    valid for any form validator, deliverable nowhere.
    """
    n = str(field_name or "").lower()
    if any(h in n for h in _CONTACT_HINTS["email"]):
        local = re.sub(r"[^a-z0-9]+", "", n)[:12] or "notifications"
        return f"{local}@{_SYNTH_CONTACT_HOST}"
    if any(h in n for h in _CONTACT_HINTS["phone"]):
        return "+950000000000"
    return f"dvs-automated-{re.sub(r'[^a-z0-9]+', '-', n) or 'notification'}"


def fill_contact_only_inputs(inputs: Dict[str, Any], method) -> Dict[str, Any]:
    """Return inputs with synthesized values for MISSING contact-only fields.

    Called by the Docker runner just before the guard so that a method which
    legitimately needs a notification email (or similar) executes without
    every caller having to invent one. Explicitly supplied values are kept
    untouched; identity fields are never touched.
    """
    contact = contact_only_inputs(method)
    if not contact:
        return inputs
    filled = dict(inputs or {})
    for name in contact:
        if not str(filled.get(name, "") or "").strip():
            filled[name] = synthesize_contact_value(name)
    return filled


def is_placeholder_value(value: str) -> bool:
    """Return True if the value looks like a redaction token or placeholder."""
    v = str(value or "").strip()
    if not v:
        return True
    if _REDACTION_TOKEN_RE.match(v):
        return True
    lowered = v.lower()
    if lowered in PLACEHOLDER_VALUES:
        return True
    # A value made of the same character repeated 4+ times ("xxxx", "1111")
    if len(set(lowered)) == 1 and len(lowered) >= 4:
        return True
    return False


def guard_inputs(
    inputs: Dict[str, Any],
    required_inputs: List[str],
    *,
    allow_structural_test_values: bool = False,
    contact_only_inputs: set | None = None,
) -> None:
    """Refuse unsafe executor inputs before any live submission.

    Two layers of protection, mirroring the engine's own rule:

    1. A method's ``required_inputs`` must all be present with non-empty
       values (the engine's "refusing to submit redaction tokens" check).
       Fields declared ``contact_only_inputs`` in the method's
       expected_responses are exempt from this check — they are synthesized
       at execution time (see ``synthesize_contact_value``) — but a value
       supplied for them must still look plausible, not like a token or a
       placeholder.
    2. No input value may be a redaction token or recognizable placeholder.
       This is what the engine's presence-check alone cannot catch: a caller
       can satisfy "required_inputs present" with ``"email": "[EMAIL]"`` or
       ``"passport_number": "placeholder"`` — exactly the shape of the
       September 2026 MCP incident.

    Raises UnsafeInputError (a ValueError) describing every problem found.

    ``allow_structural_test_values=True`` is for the generator's structural
    test, whose known-fake probes (e.g. TEST_STRUCTURAL_001) are intentionally
    submitted to live endpoints and must not be blocked.
    """
    problems: List[str] = []
    contact_fields = contact_only_inputs or set()

    missing = [
        k for k in (required_inputs or [])
        if not str(inputs.get(k, "") or "").strip()
        and k not in contact_fields
    ]
    if missing:
        problems.append(
            "Missing required input(s): %s — refusing to submit redaction "
            "tokens or incomplete data to a live endpoint." % missing
        )

    for key, value in (inputs or {}).items():
        v = str(value or "").strip()
        if not v:
            continue
        if _REDACTION_TOKEN_RE.match(v):
            problems.append(
                f"Input {key!r} is a redaction token ({v!r}); tokens must "
                "never be submitted to a live endpoint."
            )
            continue
        if allow_structural_test_values and (
            v.upper() in {s.upper() for s in STRUCTURAL_TEST_VALUES}
            or any(
                v == structural_test_value_for(k)
                for k in (inputs or {})
            )
        ):
            # Explicitly opted-in structural probe — allowed despite being a
            # known-fake value.
            continue
        if is_placeholder_value(v):
            problems.append(
                f"Input {key!r} looks like a placeholder value ({v!r}); "
                "refusing to submit it to a live endpoint."
            )

    if problems:
        raise UnsafeInputError(" ".join(problems))
