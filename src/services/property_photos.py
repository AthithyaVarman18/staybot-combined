"""
Photos an owner attaches to their listing draft through the chat.

Unlike src/services/file_store.py (private lease/onboarding documents),
listing photos need to be shown to tenants once the draft is approved,
so they live in a PUBLIC Supabase Storage bucket. Each photo is saved
under a path keyed by the conversation/session, so photos attached
before the draft even exists (e.g. the very first message) land in the
same place as ones attached after src/services/owner_listings.py has
created the property row.
"""

import os
import uuid

import requests

from src.services import db


BUCKET = (os.getenv("PROPERTY_PHOTOS_BUCKET") or "property-photos").strip()

_bucket_ready = False

EXTENSION_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class StorageError(RuntimeError):
    pass


def _url(path: str) -> str:
    return f"{db.REST_URL.removesuffix('/rest/v1')}/storage/v1/{path}"


def _headers(extra: dict = None) -> dict:
    return {"apikey": db.HEADERS["apikey"], "Authorization": db.HEADERS["Authorization"], **(extra or {})}


def ensure_bucket():
    """Create the bucket as PUBLIC if it doesn't exist yet. Unlike
    file_store.ensure_bucket(), this one is meant to be public - listing
    photos are shown on the public listings page."""

    global _bucket_ready

    if _bucket_ready:
        return

    found = requests.get(_url(f"bucket/{BUCKET}"), headers=_headers(), timeout=15)

    if not found.ok:
        created = requests.post(
            _url("bucket"),
            headers=_headers({"Content-Type": "application/json"}),
            json={"id": BUCKET, "name": BUCKET, "public": True},
            timeout=15,
        )
        if not created.ok and "already exists" not in created.text:
            raise StorageError(f"Could not create the {BUCKET} storage bucket: {created.status_code} {created.text[:200]}")

    _bucket_ready = True


def public_url(path: str) -> str:
    return _url(f"object/public/{BUCKET}/{path}")


def save(owner_key: str, image_bytes: bytes, content_type: str) -> str:
    """Upload one photo under a folder named for this owner's
    conversation/session and return its public URL. owner_key should be
    the conversation_id or session_id - anything stable for the whole
    draft, so photos from different messages land together."""

    if not db.ENABLED:
        raise StorageError("Supabase is not configured.")

    ext = EXTENSION_BY_TYPE.get(content_type, "jpg")
    ensure_bucket()

    path = f"{owner_key or 'unknown'}/{uuid.uuid4().hex[:12]}.{ext}"

    response = requests.post(
        _url(f"object/{BUCKET}/{path}"),
        headers=_headers({"Content-Type": content_type or "image/jpeg", "x-upsert": "false"}),
        data=image_bytes,
        timeout=60,
    )

    if not response.ok:
        raise StorageError(f"Photo upload failed: {response.status_code} {response.text[:200]}")

    return public_url(path)
