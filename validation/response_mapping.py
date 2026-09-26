"""LLM-driven response field discovery for per-method comparison mappings.

When a live registry response comes back, this module asks the local LLM to
semantically match response fields to the document's raw profile fields.
The resulting mapping is stored on the method so future comparisons use the
method's OWN discovered fields — not a hardcoded global assumption.

Image fields (base64-decodable BMP/PNG/JPEG) are flagged separately and
compared via the existing shared vision_call path (same as CAPTCHA solving).
"""

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from utils.llm_client import generate_json, vision_call

logger = logging.getLogger(__name__)

# Magic-byte prefixes for common image formats (after base64-decode)
_IMAGE_MAGIC = {
    b"\x89PNG":  "png",
    b"\xff\xd8":  "jpeg",
    b"BM":       "bmp",
    b"GIF8":     "gif",
}

# Minimum base64 length to bother checking (tiny strings aren't images)
_MIN_B64_IMAGE_LEN = 50


def _is_base64_image(value: str) -> Optional[str]:
    """Check if a string value looks like base64-encoded image data.

    Returns the detected image format name (e.g. 'bmp', 'png') or None.
    """
    if not isinstance(value, str) or len(value) < _MIN_B64_IMAGE_LEN:
        return None
    # Quick heuristic: base64 is mostly [A-Za-z0-9+/=]
    if not re.match(r'^[A-Za-z0-9+/=\s]{200,}$', value[:500]):
        return None
    try:
        raw = base64.b64decode(value[:64], validate=True)
        for magic, fmt in _IMAGE_MAGIC.items():
            if raw.startswith(magic):
                return fmt
        return None
    except Exception:
        return None


def _flatten_response(response_data: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten a nested dict/list into a single-level dict of field paths → values."""
    result: Dict[str, Any] = {}
    if isinstance(response_data, dict):
        for key, value in response_data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                result.update(_flatten_response(value, full_key))
            else:
                result[full_key] = value
    elif isinstance(response_data, list):
        for i, item in enumerate(response_data):
            full_key = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                result.update(_flatten_response(item, full_key))
            else:
                result[full_key] = item
    return result


def _classify_response_fields(
    flat_fields: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Separate text fields from image fields.

    Returns (text_fields, image_fields) where values are stringified for text
    and the detected format string for images.
    """
    text_fields: Dict[str, str] = {}
    image_fields: Dict[str, str] = {}
    for key, value in flat_fields.items():
        str_val = str(value) if value is not None else ""
        img_fmt = _is_base64_image(str_val)
        if img_fmt:
            image_fields[key] = img_fmt
        else:
            text_fields[key] = str_val
    return text_fields, image_fields


def discover_field_mapping(
    response_data: Any,
    raw_profile: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Ask the local LLM which response fields correspond to which profile fields.

    Args:
        response_data: The parsed response (dict/list from JSON, or raw text).
        raw_profile: The document's raw lookup credentials + any additional
                     profile fields available (expiry_date, identifying marks, etc.).

    Returns:
        A mapping dict with:
          - "text_mappings": {response_field: profile_field, ...}
          - "image_fields": {response_field: image_format, ...}
          - "unmapped_response_fields": [field_names that had no match]
        Or None if the LLM call failed.
    """
    while isinstance(response_data, str):
        try:
            parsed = json.loads(response_data)
            if parsed == response_data:
                break
            response_data = parsed
        except (json.JSONDecodeError, TypeError):
            try:
                clean_str = response_data.replace('\\"', '"').replace('\\\\', '\\')
                parsed = json.loads(clean_str)
                if parsed == response_data:
                    break
                response_data = parsed
            except (json.JSONDecodeError, TypeError):
                break

    if isinstance(response_data, dict):
        flat = _flatten_response(response_data)
    else:
        flat = {"raw_response": str(response_data)}

    text_fields, image_fields = _classify_response_fields(flat)

    if not text_fields and not image_fields:
        logger.warning("discover_field_mapping: no fields found in response")
        return None

    # Build profile field list with sample values (redacted if needed)
    profile_info = {}
    for key, value in raw_profile.items():
        if value and isinstance(value, str) and not value.startswith("["):
            profile_info[key] = value

    if not profile_info:
        logger.warning("discover_field_mapping: no usable profile fields")
        return None

    system_prompt = (
        "You are a semantic field matcher for a document verification system.\n\n"
        "You are given:\n"
        "1. RESPONSE FIELDS: field names and their values from a government registry's API response.\n"
        "2. PROFILE FIELDS: field names and values extracted from the document being verified.\n\n"
        "Your task: identify which response fields semantically correspond to which profile fields.\n"
        "For example, a response field named 'dateOfExpiry' with value '19-04-2031' corresponds to "
        "a profile field 'expiry_date' with value '19/04/2031'.\n\n"
        "Rules:\n"
        "- Match fields by SEMANTIC MEANING, not exact name match.\n"
        "- A response field representing a date matches a profile field representing the same date.\n"
        "- A response field representing a name matches a profile field representing a name.\n"
        "- A response field about identifying marks/characteristics matches a profile field about the same.\n"
        "- If a response field has NO clear correspondence to ANY profile field, do NOT force-map it.\n"
        "- Only include mappings you are confident about.\n\n"
        "Return ONLY a JSON object with this exact structure:\n"
        "{\n"
        '  "mappings": [\n'
        '    {"response_field": "<name>", "profile_field": "<name>", "confidence": "HIGH"|"MEDIUM"},\n'
        "    ...\n"
        "  ]\n"
        "}\n\n"
        "Do not include explanations outside the JSON."
    )

    user_prompt = json.dumps({
        "response_fields": {k: v[:200] for k, v in text_fields.items()},
        "profile_fields": profile_info,
    }, indent=2)

    logger.info("discover_field_mapping: asking LLM to map %d response fields to %d profile fields",
                len(text_fields), len(profile_info))

    llm_result = generate_json(system_prompt, user_prompt)

    if not llm_result or "mappings" not in llm_result:
        logger.warning("discover_field_mapping: LLM returned no usable mappings")
        return None

    # Build the discovered mapping
    text_mappings: Dict[str, str] = {}
    mapped_response_fields = set()
    for entry in llm_result.get("mappings", []):
        resp_field = entry.get("response_field", "")
        prof_field = entry.get("profile_field", "")
        confidence = entry.get("confidence", "MEDIUM")
        if resp_field and prof_field and confidence in ("HIGH", "MEDIUM"):
            text_mappings[resp_field] = prof_field
            mapped_response_fields.add(resp_field)

    unmapped = [f for f in text_fields if f not in mapped_response_fields]

    result = {
        "text_mappings": text_mappings,
        "image_fields": image_fields,
        "unmapped_response_fields": unmapped,
    }

    logger.info(
        "discover_field_mapping result: %d text mappings, %d image fields, %d unmapped",
        len(text_mappings), len(image_fields), len(unmapped),
    )
    return result


def compare_image_field(
    image_b64: str,
    image_format: str,
    profile_context: Dict[str, str],
    field_name: str = "",
) -> Dict[str, Any]:
    """Compare an image field from the response against the document profile.

    Uses the EXISTING shared vision_call path (same as CAPTCHA solving) — no
    duplicate vision implementation. Asks the model a general question about
    what the image shows and whether it plausibly corresponds to the profile.

    Returns a dict with 'score' (0-100), 'description', and 'plausible' (bool).
    """
    # Build a context-aware prompt — let the model read the image and decide
    context_parts = []
    for key, value in profile_context.items():
        if value and isinstance(value, str) and not value.startswith("["):
            context_parts.append(f"  {key}: {value}")
    context_str = "\n".join(context_parts) if context_parts else "No additional context available."

    prompt = (
        f"This image was returned by a government maritime registry as part of "
        f"a seafarer document verification response"
        f"{f' (field name: {field_name})' if field_name else ''}.\n\n"
        f"Document profile context:\n{context_str}\n\n"
        f"Describe briefly what this image shows (signature, photo, stamp, etc.) "
        f"and state whether it appears to be a genuine verification artifact "
        f"(not blank, not corrupted, contains meaningful content).\n"
        f"Reply in this exact format:\n"
        f"TYPE: <what the image shows>\n"
        f"GENUINE: YES or NO\n"
        f"BRIEF: <one-line description>"
    )

    # Use the existing vision_call — same endpoint, same toggle, same path
    # Allow more tokens for this descriptive task than CAPTCHA solving
    reply = vision_call(prompt, image_b64, timeout_s=30)

    if not reply:
        return {"score": 0, "description": "vision_call failed", "plausible": False}

    reply_lower = reply.lower()
    genuine = "genuine: yes" in reply_lower or "yes" in reply_lower.split("genuine:")[-1][:20] if "genuine:" in reply_lower else False

    return {
        "score": 75.0 if genuine else 25.0,
        "description": reply.strip()[:300],
        "plausible": genuine,
    }
