"""
EPDStore — the single entry point for saving and fetching EPD data.

    save_record()  approved record + source file + extracted text
                   → blob store (source file, text, versioned JSON document)
                   → SQLite index (search columns, long-format results, Step 1)
                   → Drive snapshot of the index (gdrive backend)
    get_record()   latest (or a specific) version of a record
    search()       paginated, filterable list for the database screen
    results()      long-format values for comparisons / exports

Backends
    local   data/store/{epd_index.sqlite, blobs/}  — laptop use
    gdrive  your Google Drive folder + local cache — Render, where the local
            disk is wiped on every deploy/restart (free plan). On start-up the
            index snapshot is downloaded and any record documents newer than the
            snapshot are re-indexed, so nothing approved is lost.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import mimetypes
import tempfile
import threading
from pathlib import Path

from epd_store.blobstore import GoogleDriveBlobStore, LocalBlobStore
from epd_store.config import PROJECT_DIR, StoreSettings, load_settings
from epd_store.gdrive import DriveClient, DriveError
from epd_store.index_db import IndexDB
from epd_store.transfer import (
    SCHEMA_VERSION,
    build_bundle,
    content_hash,
    from_json_bytes,
    make_epd_uid,
    migrate_record,
    read_bundle,
    strip_transient,
    to_json_bytes,
    utc_now,
)

log = logging.getLogger(__name__)

SNAPSHOT_KEY = "db/epd_index.sqlite"
LEGACY_JSON = PROJECT_DIR / "data" / "epds.json"

EXTENSIONS = {"pdf": ".pdf", "ilcd_xml": ".xml", "ilcd_zip": ".zip"}
MIME = {".pdf": "application/pdf", ".xml": "application/xml", ".zip": "application/zip"}


class EPDStore:
    def __init__(self, settings: StoreSettings | None = None):
        self.settings = settings or load_settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.drive: DriveClient | None = None
        self.startup_messages: list[str] = []

        if self.settings.backend == "gdrive":
            if not self.settings.gdrive_configured:
                raise DriveError("EPD_STORAGE_BACKEND=gdrive but GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / "
                                 "GDRIVE_REFRESH_TOKEN are not set.")
            self.drive = DriveClient(self.settings.gdrive_client_id, self.settings.gdrive_client_secret,
                                     self.settings.gdrive_refresh_token, self.settings.gdrive_folder_name)
            self.blobs = GoogleDriveBlobStore(self.drive, self.settings.cache_dir)
            self._restore_snapshot()
        else:
            self.blobs = LocalBlobStore(self.settings.blob_dir)

        self.index = IndexDB(self.settings.index_path)
        if self.drive:
            self._reconcile_from_blobs()

    # ------------------------------------------------------------------ startup / sync
    def _restore_snapshot(self) -> None:
        """Download the index snapshot from Drive when this instance has no local index."""
        if self.settings.index_path.exists():
            return
        try:
            data = self.blobs.get(SNAPSHOT_KEY)
        except FileNotFoundError:
            self.startup_messages.append("No index snapshot in Google Drive yet — starting a new index.")
            return
        self.settings.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.index_path.write_bytes(data)
        self.startup_messages.append(f"Index snapshot restored from Google Drive ({len(data) / 1e6:.1f} MB).")

    def _reconcile_from_blobs(self) -> int:
        """Index record documents that are newer than the local index (e.g. saved just before a restart)."""
        since = self.index.get_meta("last_reconciled_at")
        if since:  # safety margin for clock differences between this server and Google Drive
            from datetime import datetime, timedelta

            since = (datetime.fromisoformat(since) - timedelta(hours=1)).isoformat()
        try:
            documents = self.blobs.list("records/", modified_after=since)
        except Exception as error:  # network problems must not stop the app
            self.startup_messages.append(f"Could not list record documents: {error}")
            return 0
        latest: dict[str, dict] = {}
        for item in documents:
            parts = item["key"].split("/")
            if len(parts) != 3:
                continue
            uid, name = parts[1], parts[2]
            if uid not in latest or name > latest[uid]["key"].split("/")[2]:
                latest[uid] = item
        added = 0
        for uid, item in latest.items():
            version = int(item["key"].split("/")[2][1:5])
            current = self.index.current(uid)
            if current and current["record_version"] >= version:
                continue
            record = from_json_bytes(self.blobs.get(item["key"]))
            storage = record.get("storage", {})
            self.index.upsert(record, version=version, content_hash=content_hash(record),
                              record_blob_key=item["key"], source_blob_key=storage.get("source_blob_key"),
                              text_blob_key=storage.get("text_blob_key"), note="re-indexed from blob store")
            self._register_record_blobs(record)
            added += 1
        self.index.set_meta("last_reconciled_at", utc_now())
        if added:
            self.startup_messages.append(f"Re-indexed {added} record(s) from the blob store.")
            self.sync_snapshot(force=True)
        return added

    def sync_snapshot(self, force: bool = False) -> str:
        """Upload a consistent copy of the index to Drive (gdrive backend only)."""
        if not self.drive:
            return "Local backend — the index file is already on disk."
        size_mb = self.settings.index_path.stat().st_size / 1e6
        if not force and size_mb > self.settings.snapshot_max_mb:
            return (f"Index is {size_mb:.0f} MB (> {self.settings.snapshot_max_mb:.0f} MB); "
                    "use 'Sync now' to upload the snapshot.")
        with self._lock, tempfile.TemporaryDirectory() as tmp:
            copy = self.index.backup_to(Path(tmp) / "snapshot.sqlite")
            self.blobs.put(SNAPSHOT_KEY, copy.read_bytes(), "application/vnd.sqlite3", overwrite=True)
        self.index.set_meta("last_snapshot_at", utc_now())
        return f"Index snapshot uploaded ({size_mb:.1f} MB)."

    def _register_record_blobs(self, record: dict) -> None:
        storage = record.get("storage", {})
        for key in (storage.get("source_blob_key"), storage.get("text_blob_key")):
            if key:
                self.index.register_blob(key, record.get("document", {}).get("sha256") if key.startswith("files/")
                                         else None, 0, MIME.get(Path(key).suffix, "application/octet-stream"),
                                         storage.get("source_file_name") if key.startswith("files/") else None,
                                         self.blobs.name)

    # ------------------------------------------------------------------ save
    def save_record(self, record: dict, source_bytes: bytes | None = None, source_name: str | None = None,
                    full_text: str | None = None, note: str | None = None) -> dict:
        """
        Store an approved record. Returns {'epd_uid', 'version', 'status'} where status is
        'created', 'updated' or 'unchanged' (identical content is not stored twice).
        """
        full_text = full_text if full_text is not None else record.get("_full_text")
        record = strip_transient(record)
        record["schema_version"] = SCHEMA_VERSION
        record["epd_uid"] = record.get("epd_uid") or make_epd_uid(record)
        uid = record["epd_uid"]
        document = record.setdefault("document", {})

        with self._lock:
            storage = dict(record.get("storage", {}))
            if source_bytes:
                sha = hashlib.sha256(source_bytes).hexdigest()
                document.setdefault("sha256", sha)
                extension = Path(source_name or "").suffix.lower() or EXTENSIONS.get(document.get("source_format"),
                                                                                     ".bin")
                key = f"files/{extension.lstrip('.')}/{sha[:2]}/{sha}{extension}"
                mime = MIME.get(extension) or mimetypes.guess_type(source_name or "")[0] or "application/octet-stream"
                self.blobs.put(key, source_bytes, mime, meta={"sha256": sha, "original_name": (source_name or "")[:90]})
                self.index.register_blob(key, sha, len(source_bytes), mime, source_name, self.blobs.name)
                storage["source_blob_key"] = key
                storage["source_file_name"] = source_name
            if full_text:
                text_bytes = full_text.encode("utf-8")
                sha = hashlib.sha256(text_bytes).hexdigest()
                key = f"text/{sha[:2]}/{sha}.txt.gz"
                self.blobs.put(key, gzip.compress(text_bytes), "application/gzip")
                self.index.register_blob(key, sha, len(text_bytes), "text/plain+gzip", None, self.blobs.name)
                storage["text_blob_key"] = key

            current = self.index.current(uid)
            record["storage"] = storage  # hash is computed without the storage block
            digest = content_hash(record)
            if current and current["content_hash"] == digest:
                return {"epd_uid": uid, "version": current["record_version"], "status": "unchanged"}

            version = (current["record_version"] + 1) if current else 1
            storage.update({"version": version, "saved_at": utc_now(), "backend": self.blobs.name,
                            "previous_version": current["record_version"] if current else None})
            record_key = f"records/{uid}/v{version:04d}.json.gz"
            storage["record_blob_key"] = record_key
            record["storage"] = storage
            self.blobs.put(record_key, to_json_bytes(record), "application/gzip",
                           meta={"epd_uid": uid, "version": version})
            self.index.upsert(record, version=version, content_hash=digest, record_blob_key=record_key,
                              source_blob_key=storage.get("source_blob_key"),
                              text_blob_key=storage.get("text_blob_key"), note=note)
            if self.drive:
                try:
                    self.sync_snapshot()
                except Exception as error:  # the record document is already safe in Drive
                    log.warning("Snapshot upload failed: %s", error)
        return {"epd_uid": uid, "version": version, "status": "updated" if current else "created"}

    # ------------------------------------------------------------------ read
    def get_record(self, uid: str, version: int | None = None) -> dict | None:
        if version is None:
            return self.index.get_record(uid)
        return from_json_bytes(self.blobs.get(f"records/{uid}/v{int(version):04d}.json.gz"))

    def get_source_file(self, uid: str) -> tuple[bytes, str, str] | None:
        current = self.index.current(uid)
        if not current or not current.get("source_blob_key"):
            return None
        key = current["source_blob_key"]
        record = self.index.get_record(uid) or {}
        name = record.get("storage", {}).get("source_file_name") or record.get("document", {}).get("source_file") \
            or Path(key).name
        suffix = Path(key).suffix
        return self.blobs.get(key), name, MIME.get(suffix, "application/octet-stream")

    def get_text(self, uid: str) -> str:
        current = self.index.current(uid)
        if not current or not current.get("text_blob_key"):
            return ""
        try:
            return gzip.decompress(self.blobs.get(current["text_blob_key"])).decode("utf-8")
        except FileNotFoundError:
            return ""

    def search(self, **filters):
        return self.index.search(**filters)

    def results(self, uids, indicators=None, modules=None):
        return self.index.results(list(uids), indicators, modules)

    def records(self, uids) -> list[dict]:
        return [record for record in (self.index.get_record(uid) for uid in uids) if record]

    def versions(self, uid: str) -> list[dict]:
        return self.index.versions(uid)

    def facets(self) -> dict:
        return self.index.facets()

    def archive(self, uid: str, archived: bool = True) -> None:
        """Soft delete: hidden from searches, all versions stay in the blob store."""
        self.index.set_archived(uid, archived)
        if self.drive:
            self.sync_snapshot()

    def status(self) -> dict:
        stats = self.index.stats()
        return {
            "backend": self.blobs.name,
            "location": self.blobs.describe(),
            "index_path": str(self.settings.index_path),
            "last_snapshot_at": self.index.get_meta("last_snapshot_at"),
            "last_reconciled_at": self.index.get_meta("last_reconciled_at"),
            "legacy_migrated_at": self.index.get_meta("legacy_migrated_at"),
            "messages": list(self.startup_messages),
            **stats,
        }

    # ------------------------------------------------------------------ maintenance
    def rebuild_index(self) -> int:
        """Rebuild the index from all versioned record documents in the blob store."""
        self.index.set_meta("last_reconciled_at", None)
        with self._lock:
            documents = self.blobs.list("records/")
            latest: dict[str, str] = {}
            for item in documents:
                parts = item["key"].split("/")
                if len(parts) == 3 and (parts[1] not in latest or parts[2] > latest[parts[1]].split("/")[2]):
                    latest[parts[1]] = item["key"]
            for uid, key in latest.items():
                record = from_json_bytes(self.blobs.get(key))
                storage = record.get("storage", {})
                self.index.upsert(record, version=int(key.split("/")[2][1:5]), content_hash=content_hash(record),
                                  record_blob_key=key, source_blob_key=storage.get("source_blob_key"),
                                  text_blob_key=storage.get("text_blob_key"), note="rebuild_index")
                self._register_record_blobs(record)
            self.index.set_meta("last_reconciled_at", utc_now())
        if self.drive:
            self.sync_snapshot(force=True)
        return len(latest)

    def migrate_legacy_json(self, path: Path = LEGACY_JSON) -> int:
        """
        Import records saved by the first prototype (data/epds.json, schema 1.1) once.
        The JSON file is left untouched.
        """
        if self.index.get_meta("legacy_migrated_at") or not Path(path).exists():
            return 0
        text = Path(path).read_text(encoding="utf-8-sig").strip()
        legacy = json.loads(text) if text else []
        imported = 0
        for old in legacy if isinstance(legacy, list) else []:
            source_bytes, source_name, full_text = None, None, ""
            stored = old.get("document", {}).get("stored_pdf")
            if stored and (PROJECT_DIR / stored).exists():
                source_bytes = (PROJECT_DIR / stored).read_bytes()
                source_name = old.get("document", {}).get("source_file") or Path(stored).name
                try:
                    from extraction.pdf_reader import read_pdf

                    full_text = read_pdf(source_bytes, detect_tables=False)["text"]
                except Exception:
                    full_text = ""
            record = migrate_record(old, full_text)
            if source_bytes:
                record["document"]["sha256"] = hashlib.sha256(source_bytes).hexdigest()
            self.save_record(record, source_bytes, source_name, full_text, note="migrated from data/epds.json")
            imported += 1
        self.index.set_meta("legacy_migrated_at", utc_now())
        return imported

    def export_bundle(self, uids: list[str] | None = None, include_files: bool = True) -> bytes:
        from dictionary import load_dictionary

        uids = uids or self.index.all_uids(include_archived=False)
        records = self.records(uids)
        files = {}
        if include_files:
            for uid in uids:
                current = self.index.current(uid)
                if current and current.get("source_blob_key"):
                    try:
                        files[current["source_blob_key"]] = self.blobs.get(current["source_blob_key"])
                    except FileNotFoundError:
                        continue
        return build_bundle(records, files, load_dictionary().get("version"))

    def import_bundle(self, data: bytes) -> dict:
        manifest, records, files = read_bundle(data)
        summary = {"created": 0, "updated": 0, "unchanged": 0}
        for record in records:
            record = migrate_record(record)
            key = record.get("storage", {}).get("source_blob_key")
            source = files.get(key) if key else None
            name = record.get("storage", {}).get("source_file_name")
            result = self.save_record(record, source, name, None, note="imported bundle")
            summary[result["status"]] += 1
        return summary
