"""
Vision classifier for tenant maintenance reports.

Given a tenant's written description and an optional photo, calls an
OpenRouter multimodal model and returns a normalized classification:
issue type, urgency, whether the photo actually matches the text, and a
recommended next action.

This always talks to OpenRouter directly (not through AI_PROVIDER /
real_estate_ai.py), because the free vision-capable models used here
are OpenRouter-specific and independent from whichever provider the
main chat AI is configured to use. It shares OPENROUTER_API_KEY with
the rest of the app. Without an OpenRouter key it falls back to Gemini
(GEMINI_API_KEY), whose OpenAI-compatible endpoint also takes photos.
"""

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)

OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")

OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "Staybot")


def _timeout() -> int:
    raw = os.getenv("MAINTENANCE_AI_TIMEOUT") or os.getenv("OPENROUTER_TIMEOUT") or "120"
    try:
        value = int(raw)
        return value if value > 0 else 120
    except (TypeError, ValueError):
        return 120


TIMEOUT = _timeout()

ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}

# Free multimodal models, tried in order. openrouter/free is OpenRouter's
# own router - it picks a free model that supports whatever the request
# needs (here, image input) - so it's the most reliable first attempt.
DEFAULT_MODELS = [
    "openrouter/free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "dots-studio/dots-3-note-preview:free",
]


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_URL = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

GEMINI_DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]


def _env_list(name: str) -> list:
    return [m.strip() for m in os.getenv(name, "").split(",") if m.strip()]


def use_gemini() -> bool:
    """No OpenRouter key but a Gemini one: classify with Gemini instead."""
    return not OPENROUTER_API_KEY and bool(GEMINI_API_KEY)


def get_models() -> list:
    configured = _env_list("MAINTENANCE_MODELS")
    if configured:
        return configured
    if use_gemini():
        return _env_list("GEMINI_MODELS") or GEMINI_DEFAULT_MODELS
    return DEFAULT_MODELS


SYSTEM_PROMPT = """
You are a property maintenance classification AI.

You analyze:
1. The tenant's written maintenance description.
2. An optional uploaded photo.

Your job is NOT simply to repeat what the tenant wrote.

When a photo is provided, you MUST independently inspect it and compare
what is visually observable with what the tenant wrote.

============================================================
CRITICAL PHOTO/TEXT CONSISTENCY RULE
============================================================

The tenant's text can be wrong, incomplete, misleading, or ambiguous.
NEVER assume the photo matches the text.

Example - text says "gas is leaking" but the photo clearly shows water
leaking from a ceiling: you must NOT conclude "gas leak". Recognize that
the visual evidence indicates water intrusion and that the text and
photo disagree.

When text and photo conflict:
- Give greater weight to clearly observable visual evidence.
- Do not invent visual evidence, and do not claim the photo proves
  something that cannot actually be seen.
- Set photoTextMatch to false and explain the mismatch in the summary.
- Use needs_review when the disagreement could create a safety risk or
  the actual problem cannot be confidently identified.

============================================================
SAFETY RULE
============================================================

Gas leaks, electrical hazards, fire, flooding, structural damage, and
other potentially dangerous conditions should be treated conservatively.
Do not claim a photo definitively proves a gas leak unless there is
actual visual evidence for it. If the tenant reports a possible gas leak
but the photo does not visually support that, distinguish the reported
claim from what the photo actually shows and still recommend appropriate
emergency precautions.

============================================================
PROPERTY RELEVANCE
============================================================

PROPERTY_RELATED examples: leaking pipe, leaking ceiling, water damage,
broken toilet or sink, electrical outlet problem, broken AC, heating
problem, damaged door or window, appliance problem, plumbing problem,
pest problem, mold, roof leak, flooding inside the property, a gas or
utility concern involving the property, heating/cooling issue.

NOT_PROPERTY_RELATED examples: a water bottle leaking, someone's phone
being broken, random conversation, an unrelated personal problem, a
joke, sports, food, school work, general non-property questions.

============================================================
CLASSIFICATION
============================================================

Return JSON with these fields:

isPropertyIssue: true / false
issueType: one of plumbing, electrical, hvac, appliance, structural,
  water_damage, gas, pest, security, door_window, heating, cooling,
  other, or null
urgency: urgent / normal / low / null
status: classified / rejected / needs_review
photoTextMatch: true / false / null (null = no photo, or not enough
  visual evidence either way)
confidence: number from 0.0 to 1.0
summary: short explanation
recommendedAction: practical next action for the tenant or the team

Do not blindly trust the tenant's text. Do not blindly trust a photo
either - use observable evidence, and if it conflicts, say so explicitly.

Return ONLY valid JSON. Do not use markdown. Do not add commentary
outside the JSON.
"""


def extract_json(text: str) -> dict:
    if not text:
        raise ValueError("Empty response from the maintenance classifier.")

    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start >= 0:
        decoder = json.JSONDecoder()
        try:
            result, _ = decoder.raw_decode(text[start:])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not parse the classifier's response as JSON.")


ALLOWED_ISSUE_TYPES = {
    "plumbing", "electrical", "hvac", "appliance", "structural",
    "water_damage", "gas", "pest", "security", "door_window",
    "heating", "cooling", "other",
}

ALLOWED_URGENCY = {"urgent", "normal", "low"}

ALLOWED_STATUS = {"classified", "rejected", "needs_review"}


def normalize_result(result: dict) -> dict:
    is_property = result.get("isPropertyIssue")
    if not isinstance(is_property, bool):
        is_property = None

    issue_type = result.get("issueType")
    if issue_type:
        issue_type = str(issue_type).strip().lower()
        if issue_type not in ALLOWED_ISSUE_TYPES:
            issue_type = "other"
    else:
        issue_type = None

    urgency = result.get("urgency")
    if urgency:
        urgency = str(urgency).strip().lower()
        if urgency not in ALLOWED_URGENCY:
            urgency = None
    else:
        urgency = None

    status = result.get("status")
    status = str(status).strip().lower() if status else None
    if status not in ALLOWED_STATUS:
        if is_property is True:
            status = "classified"
        elif is_property is False:
            status = "rejected"
        else:
            status = "needs_review"

    # Accept the field under either name - imageTextMatch is what the
    # original classifier prompt used, photoTextMatch is this one's.
    photo_text_match = result.get("photoTextMatch", result.get("imageTextMatch"))
    if photo_text_match not in (True, False, None):
        photo_text_match = None

    confidence = result.get("confidence")
    try:
        if isinstance(confidence, bool):
            raise TypeError
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None

    summary = result.get("summary")
    summary = str(summary) if summary is not None else None

    recommended_action = result.get("recommendedAction")
    recommended_action = str(recommended_action) if recommended_action is not None else None

    return {
        "isPropertyIssue": is_property,
        "issueType": issue_type,
        "urgency": urgency,
        "status": status,
        "photoTextMatch": photo_text_match,
        "confidence": confidence,
        "summary": summary,
        "recommendedAction": recommended_action,
    }


def make_image_data_url(image_bytes: bytes, content_type: Optional[str]) -> str:
    if not content_type or content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        if content_type:
            logger.warning("Unsupported image content type %r, defaulting to image/jpeg", content_type)
        content_type = "image/jpeg"

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{content_type};base64,{encoded}"


def classify_maintenance_photo(
    message: str = "",
    image_bytes: Optional[bytes] = None,
    image_content_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Call OpenRouter's (or, without an OpenRouter key, Gemini's) vision
    models and return a normalized result dict."""

    gemini = use_gemini()
    if not OPENROUTER_API_KEY and not gemini:
        raise RuntimeError("Set OPENROUTER_API_KEY or GEMINI_API_KEY in .env to classify maintenance reports.")

    message = (message or "").strip()
    has_image = bool(image_bytes)

    user_content = [{
        "type": "text",
        "text": f"TENANT MAINTENANCE DESCRIPTION:\n\n{message if message else '[No text provided]'}\n",
    }]

    if image_bytes:
        image_url = make_image_data_url(image_bytes, image_content_type)

        user_content.append({
            "type": "text",
            "text": (
                "IMPORTANT: A photo has been attached. Before deciding the final "
                "classification: 1) inspect the photo independently, 2) describe "
                "what is visibly observable internally, 3) compare the visual "
                "evidence against the tenant's text, 4) if they conflict, set "
                "photoTextMatch to false, 5) do not allow the tenant's wording to "
                "override clear visual evidence, 6) do not invent details that "
                "cannot be seen."
            ),
        })
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})

    payload = {
        "model": None,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 1200,
        # Several free models think silently before answering; a small
        # max_tokens budget can otherwise be entirely consumed by hidden
        # reasoning, leaving nothing for the JSON answer.
        "reasoning": {"enabled": False},
    }

    url = OPENROUTER_URL
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME

    if gemini:
        # Gemini rejects OpenRouter's "reasoning" field; give its thinking
        # room within max_tokens instead.
        url = GEMINI_URL
        headers = {"Authorization": f"Bearer {GEMINI_API_KEY}", "Content-Type": "application/json"}
        payload.pop("reasoning", None)
        payload["max_tokens"] = 4000

    all_errors = []

    for model in get_models():
        payload["model"] = model

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)

            if response.status_code in (429, 502, 503, 504):
                all_errors.append(f"{model}: HTTP {response.status_code}")
                logger.warning("Temporary failure from %s: %s", model, response.status_code)
                time.sleep(1)
                continue

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = response.text
                all_errors.append(f"{model}: HTTP {response.status_code}: {error_data}")
                logger.warning("Model failed: %s", all_errors[-1])
                continue

            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                raise ValueError("OpenRouter returned no choices.")

            content = choices[0].get("message", {}).get("content")
            if isinstance(content, list):
                content = "".join(item.get("text", "") for item in content if isinstance(item, dict))

            if not content:
                raise ValueError("OpenRouter returned empty content.")

            result = normalize_result(extract_json(content))

            # A clear photo/text mismatch should never pass through as a
            # normal, high-confidence classification.
            if has_image and result.get("photoTextMatch") is False:
                result["status"] = "needs_review"
                confidence = result.get("confidence")
                result["confidence"] = 0.5 if confidence is None else min(confidence, 0.65)

            return result

        except Exception as exc:
            all_errors.append(f"{model}: {exc}")
            logger.warning("Error using %s: %s", model, exc)
            time.sleep(0.5)
            continue

    raise RuntimeError("All maintenance classifier models failed:\n" + "\n".join(all_errors))
