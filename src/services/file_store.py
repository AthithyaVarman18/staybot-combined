"""
Private file storage for onboarding documents and lease PDFs.

Files live in a PRIVATE Supabase Storage bucket and are only read by the
server with the service_role key; browsers download them through our own
access-checked endpoints, never through a public URL. Objects are written
with x-upsert: false, so an existing file is never silently overwritten.
"""

import hashlib
import os

import requests

from src.services import db

BUCKET = (os.getenv("ONBOARDING_BUCKET") or "onboarding-private").strip()
_bucket_ready = False


class StorageError(RuntimeError):
    pass


def _url(path):
    return f"{db.REST_URL.removesuffix('/rest/v1')}/storage/v1/{path}"


def _headers(extra=None):
    return {"apikey": db.HEADERS["apikey"], "Authorization": db.HEADERS["Authorization"], **(extra or {})}


def ensure_bucket():
    global _bucket_ready
    if _bucket_ready:
        return
    found = requests.get(_url(f"bucket/{BUCKET}"), headers=_headers(), timeout=15)
    if found.ok:
        if found.json().get("public"):
            raise StorageError(f"Storage bucket {BUCKET} is public. Make it private in Supabase before storing documents.")
    else:
        created = requests.post(_url("bucket"), headers=_headers({"Content-Type": "application/json"}),
                                json={"id": BUCKET, "name": BUCKET, "public": False}, timeout=15)
        if not created.ok and "already exists" not in created.text:
            raise StorageError(f"Could not create private storage bucket: {created.status_code} {created.text[:200]}")
    _bucket_ready = True


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(path: str, data: bytes, content_type: str):
    if not db.ENABLED:
        raise StorageError("Supabase is not configured.")
    ensure_bucket()
    response = requests.post(_url(f"object/{BUCKET}/{path}"),
                             headers=_headers({"Content-Type": content_type, "x-upsert": "false"}), data=data, timeout=60)
    if not response.ok:
        raise StorageError(f"Upload failed: {response.status_code} {response.text[:200]}")


def get(path: str) -> bytes:
    if not db.ENABLED:
        raise StorageError("Supabase is not configured.")
    response = requests.get(_url(f"object/{BUCKET}/{path}"), headers=_headers(), timeout=60)
    if not response.ok:
        raise StorageError(f"Download failed: {response.status_code}")
    return response.content
