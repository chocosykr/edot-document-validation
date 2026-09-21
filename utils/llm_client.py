import os
import json
import re
import urllib.request
import urllib.error
import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load env variables (assumes .env is in project root)
load_dotenv()

LLM_URL = os.getenv("LLM_URL", "https://ai.edot-solutions.com/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "AI_Local")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract the first valid JSON object from text that may contain
    trailing commentary or markdown fences.
    """
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the outermost { ... } using brace counting
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None

    return None


def generate_json(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """
    Calls LLMs to generate a JSON response.
    Tries Google AI Studio (native), then Groq, then the local fallback.
    Parses the response and returns a dictionary, or None on failure.
    """
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    USE_LOCAL_LLM_ONLY = os.getenv("USE_LOCAL_LLM_ONLY", "false").lower() == "true"

    if USE_LOCAL_LLM_ONLY:
        logger.info("USE_LOCAL_LLM_ONLY is set to true. Bypassing external APIs.")
        GOOGLE_API_KEY = ""
        GROQ_API_KEY = ""

    # Try Google Gemini (Native API)
    if GOOGLE_API_KEY:
        logger.info("Trying LLM provider: Google AI Studio (gemini-3.8-flash)")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GOOGLE_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": system_prompt + "\n\n" + user_prompt}]
                }
            ],
            "generationConfig": {"temperature": 0.1}
        }
        
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                content = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                result = _extract_json(content)
                if result is not None:
                    return result
                logger.error(f"Could not extract JSON from Google response: {content[:200]}")
        except urllib.error.HTTPError as e:
            logger.warning(f"Google HTTP Error: {e.code} {e.reason}\n{e.read().decode('utf-8')}")
        except Exception as e:
            logger.warning(f"Google Unexpected error: {e}")
            
        logger.info("Falling back to next provider after Google failure...")

    # Set up OpenAI-compatible providers
    providers = []
    if GROQ_API_KEY:
        providers.append({
            "name": "Groq",
            "url": "https://api.groq.com/openai/v1/chat/completions",
            "model": "openai/gpt-oss-20b",
            "api_key": GROQ_API_KEY
        })
    if LLM_API_KEY:
        providers.append({
            "name": "Local Model",
            "url": LLM_URL,
            "model": LLM_MODEL,
            "api_key": LLM_API_KEY
        })

    for provider in providers:
        logger.info(f"Trying LLM provider: {provider['name']} ({provider['model']})")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider['api_key']}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        payload = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
        }

        req = urllib.request.Request(
            provider["url"],
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                content = response_data["choices"][0]["message"]["content"].strip()

                result = _extract_json(content)
                if result is None:
                    logger.error(f"Could not extract JSON from {provider['name']} response: {content[:200]}")
                    continue

                return result

        except urllib.error.HTTPError as e:
            logger.warning(f"{provider['name']} HTTP Error: {e.code} {e.reason}\n{e.read().decode('utf-8')}")
        except urllib.error.URLError as e:
            logger.warning(f"{provider['name']} URL Error: {e.reason}")
        except Exception as e:
            logger.warning(f"{provider['name']} Unexpected error: {e}")

        logger.info(f"Falling back to next provider after {provider['name']} failure...")

    logger.error("All LLM providers failed.")
    return None

