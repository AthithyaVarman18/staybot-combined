"""
Performance log: one record per customer message.

Each record says how the reply was produced (AI or instant), how long it
took, which model answered, tokens used, and any models skipped for quota.
Records are kept in memory (last 2,000) and, when the Supabase `ai_calls`
table exists (supabase_ai_calls.sql), saved there too so they survive
restarts. Saving happens in a background thread so it never slows a reply.
"""

import statistics
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from src.services import db


MEMORY_SIZE = 2000

_records = deque(maxlen=MEMORY_SIZE)
_lock = threading.Lock()

# After a failed save (e.g. table not created yet) stop trying for a while.
_db_paused_until = 0.0
DB_PAUSE_SECONDS = 600


def record(
    channel: str,
    outcome: str,
    total_ms: int,
    perf: dict = None,
    quick_kind: str = None,
    session_id: str = None,
    error: str = None,
):
    """outcome: 'ai', 'quick_reply' or 'error'."""

    perf = perf or {}

    row = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "outcome": outcome,
        "quick_kind": quick_kind,
        "total_ms": int(total_ms),
        "ai_ms": perf.get("ai_ms"),
        "model": perf.get("model"),
        "prompt_tokens": perf.get("prompt_tokens"),
        "completion_tokens": perf.get("completion_tokens"),
        "total_tokens": perf.get("total_tokens"),
        "prompt_chars": perf.get("prompt_chars"),
        "attempts": len(perf.get("attempts") or []),
        "skipped_models": perf.get("skipped_models") or [],
        "session_id": session_id,
        "error": (error or "")[:300] or None,
    }

    with _lock:
        _records.append(row)

    if db.ENABLED:
        threading.Thread(target=_save, args=(row,), daemon=True).start()

    return row


def _save(row: dict):

    global _db_paused_until

    if time.time() < _db_paused_until:
        return

    try:
        db.create_ai_call(row)
    except Exception as e:
        _db_paused_until = time.time() + DB_PAUSE_SECONDS
        print(f"Performance log not saved to Supabase (run supabase_ai_calls.sql?): {type(e).__name__}")


def _rows_since(hours: float) -> tuple[list[dict], str]:

    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    if db.ENABLED and time.time() >= _db_paused_until:
        try:
            return db.list_ai_calls(since.isoformat(timespec="seconds")), "supabase"
        except Exception:
            pass

    with _lock:
        rows = [r for r in _records if r["created_at"] >= since.isoformat(timespec="seconds")]

    return rows, "memory (since the server started)"


def percentile(values: list, pct: float):
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, round(pct / 100 * (len(values) - 1))))
    return values[index]


def summary(hours: float = 24) -> dict:

    rows, source = _rows_since(hours)

    ai = [r for r in rows if r["outcome"] == "ai"]
    quick = [r for r in rows if r["outcome"] == "quick_reply"]
    errors = [r for r in rows if r["outcome"] == "error"]

    ai_times = [r["total_ms"] for r in ai if r.get("total_ms") is not None]
    tokens = [r["total_tokens"] for r in ai if r.get("total_tokens")]
    prompt_tokens = [r["prompt_tokens"] for r in ai if r.get("prompt_tokens")]
    completion_tokens = [r["completion_tokens"] for r in ai if r.get("completion_tokens")]

    models = {}
    for r in ai:
        models[r.get("model") or "unknown"] = models.get(r.get("model") or "unknown", 0) + 1

    channels = {}
    for r in rows:
        channels[r.get("channel") or "unknown"] = channels.get(r.get("channel") or "unknown", 0) + 1

    return {
        "hours": hours,
        "source": source,
        "messages": len(rows),
        "ai_replies": len(ai),
        "quick_replies": len(quick),
        "errors": len(errors),
        "quick_reply_share": round(100 * len(quick) / len(rows)) if rows else 0,
        "error_share": round(100 * len(errors) / len(rows)) if rows else 0,
        "ai_time_ms": {
            "median": int(statistics.median(ai_times)) if ai_times else None,
            "p90": percentile(ai_times, 90),
            "slowest": max(ai_times) if ai_times else None,
        },
        "quick_time_ms_median": int(statistics.median([r["total_ms"] for r in quick])) if quick else None,
        "tokens": {
            "avg_total": round(statistics.mean(tokens)) if tokens else None,
            "avg_prompt": round(statistics.mean(prompt_tokens)) if prompt_tokens else None,
            "avg_reply": round(statistics.mean(completion_tokens)) if completion_tokens else None,
            "total": sum(tokens),
        },
        "models": models,
        "channels": channels,
        "quota_skips": sum(1 for r in rows if r.get("skipped_models")),
        "recent": sorted(rows, key=lambda r: r["created_at"], reverse=True)[:25],
    }
