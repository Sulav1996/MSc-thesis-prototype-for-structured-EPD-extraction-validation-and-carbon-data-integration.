"""
Storage and transfer layer of the EPD prototype.

    from epd_store import get_store
    store = get_store()                   # settings from env vars / Streamlit secrets
    store.save_record(record, pdf_bytes, "file.pdf", full_text)
    rows, total = store.search(text="window", category="skylight")
    record = store.get_record(rows[0]["epd_uid"])
"""

from __future__ import annotations

import threading

from epd_store.config import StoreSettings, load_settings
from epd_store.store import EPDStore

_STORE: EPDStore | None = None
_LOCK = threading.Lock()


def get_store(settings: StoreSettings | None = None) -> EPDStore:
    """Process-wide store instance (Streamlit reruns share it)."""
    global _STORE
    with _LOCK:
        if _STORE is None or settings is not None:
            _STORE = EPDStore(settings or load_settings())
        return _STORE


__all__ = ["EPDStore", "StoreSettings", "get_store", "load_settings"]
