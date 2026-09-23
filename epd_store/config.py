"""
Store settings from environment variables (Render) or ``.streamlit/secrets.toml`` (local).

    EPD_STORAGE_BACKEND   auto (default) | local | gdrive
                          auto = gdrive when Drive credentials exist, otherwise local
    EPD_DATA_DIR          working folder for the SQLite index, local blobs and cache
                          (default: <project>/data/store)
    GDRIVE_CLIENT_ID      OAuth client id  (Google Cloud console → Credentials → Desktop app)
    GDRIVE_CLIENT_SECRET  OAuth client secret
    GDRIVE_REFRESH_TOKEN  from ``python tools/google_drive_auth.py``
    GDRIVE_FOLDER_NAME    Drive folder created by the app (default: EPD_Prototype_Store)
    EPD_SNAPSHOT_MAX_MB   upload the index snapshot after every save while it is smaller
                          than this size (default 50); larger indexes sync on demand
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:  # Streamlit secrets are optional and only exist inside a Streamlit run.
        import streamlit as st  # noqa: WPS433

        if name in st.secrets:
            return str(st.secrets[name]).strip()
        section = st.secrets.get("gdrive", {}) if hasattr(st.secrets, "get") else {}
        short = name.replace("GDRIVE_", "").lower()
        if short in section:
            return str(section[short]).strip()
    except Exception:
        return None
    return None


@dataclass(frozen=True)
class StoreSettings:
    backend: str
    data_dir: Path
    gdrive_client_id: str | None = None
    gdrive_client_secret: str | None = None
    gdrive_refresh_token: str | None = None
    gdrive_folder_name: str = "EPD_Prototype_Store"
    snapshot_max_mb: float = 50.0

    @property
    def index_path(self) -> Path:
        return self.data_dir / "epd_index.sqlite"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "drive_cache"

    @property
    def gdrive_configured(self) -> bool:
        return bool(self.gdrive_client_id and self.gdrive_client_secret and self.gdrive_refresh_token)


def load_settings(**overrides) -> StoreSettings:
    client_id = _secret("GDRIVE_CLIENT_ID")
    client_secret = _secret("GDRIVE_CLIENT_SECRET")
    refresh_token = _secret("GDRIVE_REFRESH_TOKEN")
    backend = (_secret("EPD_STORAGE_BACKEND") or "auto").lower()
    if backend == "auto":
        backend = "gdrive" if (client_id and client_secret and refresh_token) else "local"
    data_dir = Path(_secret("EPD_DATA_DIR") or PROJECT_DIR / "data" / "store")
    settings = {
        "backend": backend,
        "data_dir": data_dir,
        "gdrive_client_id": client_id,
        "gdrive_client_secret": client_secret,
        "gdrive_refresh_token": refresh_token,
        "gdrive_folder_name": _secret("GDRIVE_FOLDER_NAME") or "EPD_Prototype_Store",
        "snapshot_max_mb": float(_secret("EPD_SNAPSHOT_MAX_MB") or 50),
    }
    settings.update(overrides)
    return StoreSettings(**settings)
