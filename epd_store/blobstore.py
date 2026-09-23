"""
Blob stores for source files, extracted text, versioned record documents and
database snapshots.

Keys are POSIX-style paths, e.g.::

    files/pdf/ab/ab12…ef.pdf          (content-addressed by SHA-256 → no duplicates)
    text/ab/ab12…ef.txt.gz
    records/EPD-VEL-20250344-CBI1-EN/v0001.json.gz
    db/epd_index.sqlite

``LocalBlobStore`` writes below ``data/store/blobs``. ``GoogleDriveBlobStore``
writes into a folder in your Google Drive and keeps a local read cache, so a
file is downloaded at most once per server instance.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from epd_store.gdrive import DriveClient

KIND_BY_PREFIX = {"files": "file", "text": "text", "records": "record", "db": "snapshot"}


def _kind(key: str) -> str:
    return KIND_BY_PREFIX.get(key.split("/", 1)[0], "other")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class LocalBlobStore:
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"Invalid blob key: {key}")
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            overwrite: bool = False, meta: dict | None = None) -> dict:
        path = self._path(key)
        if overwrite or not path.exists():
            _atomic_write(path, data)
        return {"backend": self.name, "key": key, "size": len(data), "content_type": content_type}

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str = "", modified_after: str | None = None) -> list[dict]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        out = []
        for path in base.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp-"):
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                if modified_after and modified <= modified_after:
                    continue
                out.append({"key": path.relative_to(self.root).as_posix(), "size": path.stat().st_size,
                            "modified": modified})
        return sorted(out, key=lambda item: item["key"])

    def describe(self) -> str:
        return f"Local folder: {self.root}"


class GoogleDriveBlobStore:
    name = "gdrive"

    def __init__(self, client: DriveClient, cache_dir: Path):
        self.client = client
        self.cache = LocalBlobStore(cache_dir)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            overwrite: bool = False, meta: dict | None = None) -> dict:
        folder = (key.split("/", 1)[0],)
        properties = {"epd_kind": _kind(key), **(meta or {})}
        info = self.client.upload(key, data, content_type, folder, properties, overwrite=overwrite)
        self.cache.put(key, data, content_type, overwrite=True)
        return {"backend": self.name, "key": key, "size": len(data), "content_type": content_type,
                "drive_file_id": info.get("id")}

    def get(self, key: str) -> bytes:
        # Content-addressed and versioned keys never change, so the cache is safe for them.
        mutable = key.startswith("db/")
        if not mutable and self.cache.exists(key):
            return self.cache.get(key)
        info = self.client.find_by_key(key)
        if not info:
            raise FileNotFoundError(key)
        data = self.client.download(info["id"])
        self.cache.put(key, data, overwrite=True)
        return data

    def exists(self, key: str) -> bool:
        return self.cache.exists(key) or self.client.find_by_key(key) is not None

    def list(self, prefix: str = "", modified_after: str | None = None) -> list[dict]:
        kind = _kind(prefix) if prefix else None
        files = self.client.list_by_kind(kind, modified_after) if kind else []
        out = []
        for item in files:
            key = (item.get("appProperties") or {}).get("epd_key")
            if key and key.startswith(prefix):
                out.append({"key": key, "size": int(item.get("size") or 0), "modified": item.get("modifiedTime"),
                            "drive_file_id": item.get("id")})
        return sorted(out, key=lambda item: item["key"])

    def describe(self) -> str:
        return f"Google Drive folder '{self.client.root_folder_name}' (local cache: {self.cache.root})"
