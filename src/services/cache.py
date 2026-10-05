"""
Shared Redis cache - dedup, quota cooldowns, and (optionally) the
properties cache, all moved out of plain-Python dicts so they survive a
restart and stay correct across more than one server instance.

Same pattern as Supabase elsewhere in this project: if REDIS_URL isn't
set (or the redis package isn't installed, or Redis can't be reached),
everything here quietly no-ops (or falls back to "treat as new") rather
than crashing, so local dev without Redis still works. Callers keep their
in-memory fallback for that case.
"""

import os

try:
    import redis
except ImportError:  # redis not installed - run without it
    redis = None

REDIS_URL = (os.getenv("REDIS_URL") or "").strip().strip('"').strip("'")

_client = None
if REDIS_URL and redis is not None:
    try:
        _client = redis.from_url(REDIS_URL, decode_responses=True,
                                 socket_connect_timeout=3, socket_timeout=3)
    except Exception as exc:
        print(f"REDIS_URL is set but invalid ({exc}); running without Redis.")
        _client = None
elif REDIS_URL:
    print("REDIS_URL is set but the 'redis' package isn't installed (pip install -r requirement.txt); running without Redis.")


def available() -> bool:
    return _client is not None


def _warn(op: str, exc: Exception):
    print(f"Redis {op} failed (non-fatal, using the in-memory fallback): {exc}")


def get(key: str):
    if not _client:
        return None
    try:
        return _client.get(key)
    except Exception as exc:
        _warn("get", exc)
        return None


def set(key: str, value: str, ex_seconds: int | None = None) -> bool:
    """True if Redis stored it; False without Redis or on an error."""
    if not _client:
        return False
    try:
        _client.set(key, value, ex=ex_seconds)
        return True
    except Exception as exc:
        _warn("set", exc)
        return False


def set_nx(key: str, value: str, ex_seconds: int):
    """Atomic 'set if not exists'. True if THIS call set it (first time
    seen); False if the key was already there. None when Redis isn't
    configured or can't be reached - the caller then uses its own
    in-memory check."""

    if not _client:
        return None
    try:
        return bool(_client.set(key, value, nx=True, ex=ex_seconds))
    except Exception as exc:
        _warn("set_nx", exc)
        return None


def exists(key: str) -> bool:
    if not _client:
        return False
    try:
        return bool(_client.exists(key))
    except Exception as exc:
        _warn("exists", exc)
        return False


def ttl(key: str) -> int:
    """Seconds left before the key expires, 0 if it's missing/expired
    (or Redis can't be reached)."""

    if not _client:
        return 0
    try:
        result = _client.ttl(key)
    except Exception as exc:
        _warn("ttl", exc)
        return 0
    return result if result and result > 0 else 0


def delete(key: str):
    if not _client:
        return
    try:
        _client.delete(key)
    except Exception as exc:
        _warn("delete", exc)
