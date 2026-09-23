"""
Backward-compatible storage helpers (first prototype API).

New code should use ``epd_store.get_store()`` directly. These wrappers keep
older scripts working:

    load_epds()                 → list of all stored records (latest versions)
    save_epd(record)            → store an approved record
    save_uploaded_pdf(b, name)  → store a source file, returns its blob key
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from epd_store import get_store

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "epds.json"  # legacy file, migrated once into the store


def load_epds() -> list[dict]:
    store = get_store()
    return store.records(store.index.all_uids(include_archived=False))


def save_epd(record: dict, source_bytes: bytes | None = None, source_name: str | None = None) -> dict:
    return get_store().save_record(record, source_bytes, source_name)


def save_uploaded_pdf(pdf_bytes: bytes, original_name: str) -> str:
    store = get_store()
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    key = f"files/pdf/{sha[:2]}/{sha}.pdf"
    store.blobs.put(key, pdf_bytes, "application/pdf")
    store.index.register_blob(key, sha, len(pdf_bytes), "application/pdf", original_name, store.blobs.name)
    return key
