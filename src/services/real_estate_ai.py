import os
import re
import json
import threading
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.prompts.system_prompt import SYSTEM_PROMPT
from src.services import cache


load_dotenv()


# Both providers speak the OpenAI chat-completions format, so only the
# URL, key and model names differ. Pick one with AI_PROVIDER in .env.
PROVIDERS = {
    "gemini": {
        "name": "Gemini",
        "key_env": "GEMINI_API_KEY",
        "models_env": "GEMINI_MODELS",
        "url_env": "GEMINI_URL",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models": [
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "key_env": "OPENROUTER_API_KEY",
        "models_env": "OPENROUTER_MODEL",
        "url_env": "OPENROUTER_URL",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        # The thinkingmachines/inkling free models were removed: OpenRouter
        # returns 403 for them outside its listed agent apps.
        "models": [
            "nex-agi/nex-n2.5-mini:free",
            "nex-agi/nex-n2.5-pro:free",
        ],
    },
}

AI_PROVIDER = os.getenv("AI_PROVIDER", "openrouter").strip().lower()

if AI_PROVIDER not in PROVIDERS:
    raise RuntimeError(
        f"AI_PROVIDER must be one of {list(PROVIDERS)}, got {AI_PROVIDER!r}"
    )

PROVIDER = PROVIDERS[AI_PROVIDER]

API_KEY = os.getenv(PROVIDER["key_env"])

API_URL = os.getenv(PROVIDER["url_env"]) or PROVIDER["url"]

# Models are tried in this order. Models listed in .env
# (comma-separated) go first, then the defaults.
MODELS = list(dict.fromkeys(
    [m.strip() for m in os.getenv(PROVIDER["models_env"], "").split(",") if m.strip()]
    + PROVIDER["models"]
))


class DailyLimitError(RuntimeError):
    """The account's free-model daily quota is used up.
    It applies to every free model, so trying the next one is pointless."""

class NetworkError(RuntimeError):
    """The provider could not be reached at all. Every model goes through
    the same connection, so trying the next model is pointless."""

TIMEOUT = int(
    os.getenv("AI_TIMEOUT") or os.getenv("OPENROUTER_TIMEOUT") or "120"
)

# Gemini's "thinking" models (2.5/3.x flash and flash-lite) spend part of
# their output budget on an internal reasoning pass before the visible
# JSON answer. With no max_tokens set, a long conversation/system prompt
# can leave nothing for the actual answer - the API still returns 200 with
# an empty message.content, which then fails JSON parsing with a cryptic
# "Expecting value: line 1 column 1 (char 0)". A generous explicit budget
# avoids that; override with AI_MAX_TOKENS if replies still get cut off.
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS") or "4096")

NETWORK_RETRIES = 2

# Local time for resolving "this Saturday", "tomorrow" and viewing slots.
# Viewing times ("Saturday at 10am", "tomorrow") are read in this timezone.
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE") or "America/New_York")


def clean_json_response(text: str) -> str:
    """
    Remove markdown code fences if the model returns:

    ```json
    {...}
    ```
    """

    text = text.strip()

    if text.startswith("```json"):
        text = text[7:]

    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    return text.strip()


def parse_model_json(text) -> dict:
    """
    Parse the model's JSON, repairing the common mistakes small models
    make: text around the object, trailing commas before } or ], and
    // comments. Raises json.JSONDecodeError if it still isn't valid.
    """

    cleaned = clean_json_response(text or "")

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as first_error:

        start, end = cleaned.find("{"), cleaned.rfind("}")

        if start == -1 or end <= start:
            raise first_error

        repaired = cleaned[start:end + 1]
        repaired = re.sub(r"^\s*//.*$", "", repaired, flags=re.MULTILINE)
        # "// note" after a value at the end of a line (not "http://" inside a string)
        repaired = re.sub(r'([,{\[]|"|\d|true|false|null)\s*//[^\n"]*$', r"\1", repaired, flags=re.MULTILINE)
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)

        try:
            result = json.loads(repaired)
        except json.JSONDecodeError:
            raise first_error

        print("Repaired invalid JSON from the model")

    if not isinstance(result, dict):
        raise json.JSONDecodeError("Expected a JSON object", cleaned, 0)

    return result


def call_model(model: str, messages: list, usage_out: dict = None) -> str:

    if not API_KEY:
        raise RuntimeError(
            f"{PROVIDER['key_env']} is not configured in .env."
        )

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    if AI_PROVIDER == "openrouter":
        headers["HTTP-Referer"] = os.getenv(
            "OPENROUTER_SITE_URL",
            "http://localhost:8000"
        )
        headers["X-Title"] = os.getenv(
            "OPENROUTER_APP_NAME",
            "Property Management AI"
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_object"
        }
    }

    # Retry dropped connections (Wi-Fi blips, SSL EOF), and a same-provider
    # 5xx ("high demand", momentary outage) - these usually clear within a
    # few seconds, and without this the 3 fallback models here are all the
    # same provider, so a provider-wide blip fails all of them instantly
    # with no delay in between. Other HTTP errors (429, 4xx) are not
    # retried here; the model fallback loop / quota cooldown handles those.
    for attempt in range(NETWORK_RETRIES + 1):
        try:
            response = requests.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=TIMEOUT
            )
            if response.status_code in (500, 502, 503, 504) and attempt < NETWORK_RETRIES:
                wait = 2 ** attempt
                print(f"{PROVIDER['name']} server error {response.status_code} (likely transient), retrying in {wait}s...")
                time.sleep(wait)
                continue
            break
        except requests.exceptions.ProxyError as e:

            # A laptop's VPN client, or a leftover system-proxy setting,
            # often points HTTP(S)_PROXY at a proxy that then can't reach
            # (or refuses) this specific host, even though the machine has
            # normal internet access. One direct attempt, bypassing that
            # proxy, fixes it in that case without needing the user to
            # hunt down and unset the proxy env var themselves.
            try:
                response = requests.post(
                    API_URL,
                    headers=headers,
                    json=payload,
                    timeout=TIMEOUT,
                    proxies={"http": None, "https": None},
                )
                print("Proxy blocked the request; direct connection worked instead.")
                break
            except requests.exceptions.RequestException:
                pass

            if attempt == NETWORK_RETRIES:
                raise NetworkError(
                    f"Could not reach {PROVIDER['name']} after "
                    f"{NETWORK_RETRIES + 1} tries - a proxy is blocking or "
                    f"can't reach {API_URL.split('/')[2]} ({type(e).__name__}). "
                    "This is usually a school/office network, VPN, or firewall "
                    "blocking AI API traffic rather than a problem with this "
                    "app. Try a different network (e.g. a phone hotspot) to "
                    "confirm, ask your network admin to allow that host, or "
                    "set a different AI_PROVIDER in .env if the other one "
                    "isn't blocked."
                ) from e
            wait = 2 ** attempt
            print(f"Proxy error, retrying in {wait}s...")
            time.sleep(wait)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            if attempt == NETWORK_RETRIES:
                raise NetworkError(
                    f"Could not reach {PROVIDER['name']} after "
                    f"{NETWORK_RETRIES + 1} tries. Check your internet "
                    f"connection and try again. ({type(e).__name__})"
                ) from e
            wait = 2 ** attempt
            print(f"Network error ({type(e).__name__}), retrying in {wait}s...")
            time.sleep(wait)

    if response.status_code == 429 and "free-models-per-day" in response.text:
        raise DailyLimitError(
            "OpenRouter free daily limit reached (50 requests/day on a "
            "free account). It resets at midnight UTC, or add $10 of "
            "credits at https://openrouter.ai/settings/credits to get "
            "1000 free requests/day."
        )

    if response.status_code == 429:
        raise RateLimitError(
            f"RATE_LIMIT: {response.text}",
            cooldown_seconds=quota_cooldown_seconds(response.text)
        )

    if response.status_code in [500, 502, 503, 504]:
        raise RuntimeError(
            f"SERVER_ERROR {response.status_code}: "
            f"{response.text}"
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"{PROVIDER['name'].upper()}_ERROR {response.status_code}: "
            f"{response.text}"
        )

    data = response.json()

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Invalid {PROVIDER['name']} response: {data}"
        )

    if not content:
        # A 200 with empty content, usually the model spending its whole
        # token budget on internal reasoning (see MAX_TOKENS above) or a
        # safety block - either way there's nothing to parse as JSON, so
        # say so plainly instead of letting json.loads("") raise a
        # confusing "Expecting value: line 1 column 1" further down.
        raise RuntimeError(
            f"{PROVIDER['name']} returned an empty response "
            f"(finish_reason={choice.get('finish_reason')!r})"
        )

    if usage_out is not None:
        usage = data.get("usage") or {}
        usage_out["prompt_tokens"] = usage.get("prompt_tokens")
        usage_out["completion_tokens"] = usage.get("completion_tokens")
        usage_out["total_tokens"] = usage.get("total_tokens")

    return content


# ---------------------------------------------------------------------
# Quota memory: skip models that just said "limit reached"
# ---------------------------------------------------------------------

class RateLimitError(RuntimeError):

    def __init__(self, message, cooldown_seconds=60):
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


_model_cooldowns = {}        # model -> unix time it may be tried again
_cooldown_lock = threading.Lock()

DAILY_QUOTA_COOLDOWN = 3600  # re-check a daily quota once an hour


def quota_cooldown_seconds(error_text: str) -> int:
    """How long to leave a model alone after a 429. Daily quotas get an
    hour; per-minute ones use the provider's retry delay (or 60s)."""

    text = str(error_text or "")

    if re.search(r"PerDay|per.day|daily", text, re.IGNORECASE):
        return DAILY_QUOTA_COOLDOWN

    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)

    return max(5, min(int(float(match.group(1))) + 1, 300)) if match else 60


def cool_down(model: str, seconds: int):
    # Always kept in memory too, so a Redis outage never loses the cooldown.
    if cache.available():
        cache.set(f"model_cooldown:{model}", "1", ex_seconds=seconds)
    with _cooldown_lock:
        _model_cooldowns[model] = time.time() + seconds
    print(f"Skipping {model} for {seconds}s (quota reached)")


def _cooldown_remaining(model: str) -> int:
    """Seconds left before `model` may be tried again, 0 if it's ready.
    Redis-backed when configured (shared across instances/restarts);
    falls back to the old in-memory dict otherwise."""

    shared = cache.ttl(f"model_cooldown:{model}") if cache.available() else 0

    with _cooldown_lock:
        remaining = _model_cooldowns.get(model, 0) - time.time()
    local = int(remaining) if remaining > 0 else 0
    return max(shared, local)


def models_in_order() -> tuple[list[str], list[str]]:
    """(models to try now, models skipped because they're cooling down).
    If every model is cooling down, try the one that recovers first."""

    remaining = {m: _cooldown_remaining(m) for m in MODELS}

    ready = [m for m in MODELS if remaining[m] <= 0]
    cooling = [m for m in MODELS if remaining[m] > 0]

    if not ready and cooling:
        soonest = min(cooling, key=lambda m: remaining[m])
        return [soonest], [m for m in cooling if m != soonest]

    return ready, cooling


def cooldown_status() -> dict:
    return {m: r for m in MODELS if (r := _cooldown_remaining(m)) > 0}


# Action signals: (flag, points, label, patterns, intents).
# A signal counts if the LLM set the flag, the intent matches, or
# a pattern appears in the LLM's free-text signals. Patterns match
# word starts, so "request to schedule a viewing" and
# "viewing_request" both match, but "sea view" and "review" do not.
ACTION_SIGNALS = [
    (
        "wants_viewing", 20, "Wants to visit / view",
        [r"\bviewing", r"\bto view\b", r"\bvisit", r"\btour\b",
         r"\bsee (it|the (property|place|flat|house|apartment))"],
        ["schedule_viewing"],
    ),
    (
        "wants_to_apply", 20, "Wants to apply / take it",
        [r"\bappl(y|ies|ied|ication)", r"\bready to (rent|buy|move)",
         r"\btoken (amount|advance)", r"\btake (it|the (property|place|flat|house))"],
        ["application"],
    ),
    (
        "wants_to_negotiate", 15, "Wants to negotiate",
        [r"\bnegotia", r"\breduc", r"\bdiscount", r"\bbest price"],
        ["negotiate"],
    ),
    (
        "asked_about_documents", 10, "Asked about documents",
        [r"\bdocument", r"\bpaperwork", r"\bkyc\b", r"\bid proof"],
        [],
    ),
    (
        "asked_about_deposit_or_lease", 10, "Asked about deposit / lease",
        [r"\bdeposit", r"\blease\b", r"\brental agreement", r"\badvance\b"],
        [],
    ),
    (
        "asked_for_contact_or_next_steps", 10, "Asked for contact / next steps",
        [r"\bcontact", r"\bnext step", r"\b(call|phone)\b",
         r"\bowner'?s? (number|details)", r"\bmeet the owner"],
        [],
    ),
]

# Most action points a lead can get, so one chatty message
# can't jump straight to 100.
ACTION_POINTS_CAP = 35

NEGATIVE_SIGNALS = [
    (
        "not_interested", -40, "Not interested",
        [r"\bnot interested", r"\bno thanks", r"\bfound another",
         r"\bno longer looking"],
    ),
    (
        "just_browsing", -25, "Just browsing",
        [r"\bjust (browsing|looking)", r"\bbrowsing", r"\bmaybe later",
         r"\bnot sure yet"],
    ),
    (
        "unrealistic_requirements", -15, "Unrealistic requirements",
        [r"\bunrealistic"],
    ),
]

TENANT_FIELDS = [
    ("location", 10, "Location given"),
    ("property_type", 5, "Property type given"),
    ("bedrooms", 10, "Bedrooms given"),
    ("budget", 10, "Budget given"),
    ("rent_or_buy", 5, "Rent or buy given"),
    ("move_in_date", 10, "Move-in date given"),
    ("pets", 5, "Pet needs given"),
    ("parking", 5, "Parking needs given"),
    ("amenities", 5, "Amenities given"),
]

OWNER_FIELDS = [
    ("property_type", 10, "Property type given"),
    ("location", 10, "Location given"),
    ("bedrooms", 10, "Bedrooms given"),
    ("price", 15, "Rent or sale price given"),
    ("available_from", 10, "Availability given"),
    ("deposit", 5, "Deposit given"),
    ("pets_allowed", 5, "Pet policy given"),
    ("parking", 5, "Parking given"),
    ("furnished", 5, "Furnishing given"),
    ("amenities", 5, "Amenities given"),
]

OWNER_INTENTS = ["list_property", "update_property", "advertise_property"]


def normalize(text) -> str:
    return (
        str(text)
        .lower()
        .replace("_", " ")
        .replace("-", " ")
    )


def has_value(value) -> bool:
    """
    True for real answers, including False ("no pets") and 0.
    False for null, empty strings, empty lists and "unknown".
    """

    if value is None:
        return False

    if isinstance(value, bool):
        return True

    if isinstance(value, (list, dict, str)):
        if isinstance(value, str) and normalize(value).strip() in [
            "", "null", "none", "unknown", "not specified", "n/a"
        ]:
            return False
        return len(value) > 0

    return True


def calculate_interest_score(result: dict) -> dict:
    """
    Deterministic qualification scoring.

    The LLM identifies signals, but this function calculates
    the final score so that the score is more predictable.
    """

    qualification = result.get("qualification") or {}
    flags = qualification.get("signal_flags") or {}
    intent = normalize(result.get("intent") or "").replace(" ", "_")
    role = normalize(result.get("role") or "")

    signal_text = " | ".join(
        normalize(signal)
        for signal in (qualification.get("signals") or [])
    )

    breakdown = []

    def add(points: int, reason: str):
        breakdown.append({
            "reason": reason,
            "points": points
        })

    # ---------------------------------------------------------
    # Details provided
    # ---------------------------------------------------------

    is_owner = role == "owner" or intent in OWNER_INTENTS

    if is_owner:

        details = dict(result.get("property_details") or {})
        details["price"] = details.get("rent") or details.get("sale_price")

        for field, points, reason in OWNER_FIELDS:
            if has_value(details.get(field)):
                add(points, reason)

        if intent in OWNER_INTENTS:
            add(15, "Wants to list / advertise")

    else:

        requirements = result.get("requirements") or {}

        for field, points, reason in TENANT_FIELDS:
            if has_value(requirements.get(field)):
                add(points, reason)

    # ---------------------------------------------------------
    # Strong action signals
    # ---------------------------------------------------------

    action_points = 0

    for flag, points, reason, patterns, intents in ACTION_SIGNALS:

        matched = (
            flags.get(flag) is True
            or intent in intents
            or any(re.search(p, signal_text) for p in patterns)
        )

        if not matched:
            continue

        points = min(points, ACTION_POINTS_CAP - action_points)

        if points <= 0:
            continue

        action_points += points
        add(points, reason)

    # ---------------------------------------------------------
    # Negative signals
    # ---------------------------------------------------------

    for flag, points, reason, patterns in NEGATIVE_SIGNALS:

        matched = (
            flags.get(flag) is True
            or any(re.search(p, signal_text) for p in patterns)
        )

        if matched:
            add(points, reason)

    score = sum(item["points"] for item in breakdown)
    score = max(0, min(score, 100))

    if score >= 85:
        status = "very_hot"
    elif score >= 70:
        status = "hot"
    elif score >= 40:
        status = "warm"
    else:
        status = "cold"

    return {
        "intent_score": score,
        "lead_status": status,
        "score_breakdown": breakdown
    }


def analyze_message(
    message: str,
    conversation_history=None,
    property_context=None,
    shown_listings=None,
    channel_note=None,
    correction_note=None,
    investor_stage_note=None,
    account_note=None,
):

    if not message or not message.strip():
        raise ValueError(
            "Message cannot be empty."
        )

    if conversation_history is None:
        conversation_history = []

    if property_context is None:
        property_context = {}

    # ---------------------------------------------------------
    # Build context
    # ---------------------------------------------------------

    if property_context:

        context_message = f"""
PROPERTY CONTEXT
================

The following property information is available to the AI.

{json.dumps(property_context, indent=2, default=str)}

IMPORTANT:
- Use this information when answering property questions.
- Never invent property information.
- If the requested information is missing, say that it needs to be checked.
"""

    else:

        context_message = """
PROPERTY CONTEXT
================

No specific property is selected (general enquiry).

IMPORTANT:
- The application searches the listings database using the
  requirements you extract, and appends the matching listings
  to the end of your "response" automatically.
- Do NOT list, name, describe or invent any property yourself.
- Do NOT say that you have no listings, and do NOT say that
  information needs to be checked.
- Keep "response" short: acknowledge what they want and, if useful,
  ask ONE question that narrows the search (for example budget,
  area or bedrooms if missing).
- If the user asks about a listing that was already shown, answer
  from LISTINGS ALREADY SHOWN below ("the first one" = position 1).
  If a fact is missing there, say that it needs to be checked.
"""

        if shown_listings:

            context_message += f"""
LISTINGS ALREADY SHOWN
======================

{json.dumps(shown_listings, indent=2, default=str)}
"""

    # One system message: Gemini's OpenAI-compatible endpoint keeps only
    # the last system message, which dropped the instructions and schema.
    now = datetime.now(APP_TIMEZONE)

    today_message = (
        f"TODAY: {now:%A, %d %B %Y}, {now:%H:%M} "
        f"({APP_TIMEZONE.key}). Use this to work out dates like "
        "\"this Saturday\" or \"tomorrow\"."
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + "\n\n" + today_message + "\n\n" + context_message
            + (f"\n\nCHANNEL\n=======\n\n{channel_note}" if channel_note else "")
            + (f"\n\nACCOUNT\n=======\n\n{account_note}" if account_note else "")
            + (f"\n\nINVESTOR STAGE\n==============\n\n{investor_stage_note}" if investor_stage_note else "")
            + (f"\n\n{correction_note}" if correction_note else "")
        }
    ]

    # ---------------------------------------------------------
    # Conversation history
    # ---------------------------------------------------------

    for item in conversation_history:

        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")

        if role not in ["user", "assistant"]:
            continue

        if not content:
            continue

        messages.append({
            "role": role,
            "content": str(content)
        })

    # ---------------------------------------------------------
    # Current message
    # ---------------------------------------------------------

    messages.append({
        "role": "user",
        "content": message.strip()
    })

    last_error = None

    # ---------------------------------------------------------
    # Model fallback (skipping models that are out of quota)
    # ---------------------------------------------------------

    to_try, skipped = models_in_order()

    perf = {
        "provider": PROVIDER["name"],
        "skipped_models": skipped,
        "attempts": [],
        "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
    }

    for model in to_try:

        print(
            f"Trying {PROVIDER['name']} model: {model}"
        )

        started = time.time()
        usage = {}

        def attempt(outcome):
            perf["attempts"].append({
                "model": model,
                "outcome": outcome,
                "ms": int((time.time() - started) * 1000),
            })

        try:

            raw_response = call_model(
                model=model,
                messages=messages,
                usage_out=usage
            )

            result = parse_model_json(raw_response)

            # -------------------------------------------------
            # Ensure required sections exist
            # -------------------------------------------------

            if "requirements" not in result:
                result["requirements"] = {}

            if "qualification" not in result:
                result["qualification"] = {}

            if "property_details" not in result:
                result["property_details"] = {}

            # -------------------------------------------------
            # Calculate deterministic lead score
            # -------------------------------------------------

            score = calculate_interest_score(
                result
            )

            result["qualification"].update(score)

            # -------------------------------------------------
            # Model information
            # -------------------------------------------------

            attempt("ok")

            result["_model_used"] = model
            result["_perf"] = {
                **perf,
                "model": model,
                "ai_ms": sum(a["ms"] for a in perf["attempts"]),
                **usage,
            }

            print(
                f"Successfully classified using: {model}"
            )

            return result

        except (DailyLimitError, NetworkError):

            attempt("unavailable")
            raise

        except RateLimitError as e:

            attempt("quota")
            cool_down(model, e.cooldown_seconds)
            last_error = str(e)
            continue

        except json.JSONDecodeError as e:

            attempt("invalid_json")

            last_error = (
                f"Invalid JSON from {model}: {e}"
            )

            print(last_error)

            continue

        except Exception as e:

            attempt("error")

            last_error = str(e)

            print(
                f"Model failed: {model}"
            )

            print(
                f"Reason: {last_error}"
            )

            continue

    error = RuntimeError(
        f"All {PROVIDER['name']} models failed. "
        f"Last error: {last_error}"
    )
    error.perf = perf
    raise error
