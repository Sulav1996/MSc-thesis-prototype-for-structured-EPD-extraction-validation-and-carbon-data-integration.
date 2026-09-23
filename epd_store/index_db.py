"""
SQLite index of stored EPDs (Python standard library only).

Tables
------
epd                 one row per EPD (latest version): search columns + compressed record JSON
epd_result          long format: epd × indicator × module × scenario (fast comparisons)
epd_module          declared modules (X / MND / MNR / ND) with the DGNB scope
epd_step1           Step 1 parameter statuses (filter "DGNB-ready" EPDs)
epd_version         every saved version with the blob key of its JSON document
blob                source files and text blobs (content-addressed)
meta                key/value (schema version, last sync, migrations)

The index is *derived* data: it can always be rebuilt from the versioned
record documents in the blob store (local folder or Google Drive).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path

from epd_store.transfer import flatten_results, summary_row, utc_now

INDEX_SCHEMA_VERSION = 1

SUMMARY_COLUMNS = [
    "registration_number", "product_name", "manufacturer", "programme_operator", "eco_platform_member",
    "category", "category_label", "building_element", "category_confidence", "standard", "standard_profile",
    "declared_unit", "declared_quantity", "declared_unit_code", "mass_per_declared_unit_kg",
    "conversion_factor_to_1kg", "rsl_years", "publication_date", "valid_until", "geography", "gwp_indicator",
    "gwp_a1_a3", "gwp_a1_a3_provenance", "gwp_d", "verification", "step1_pass", "step1_warning", "step1_fail",
    "step1_missing", "dgnb_mandatory_met", "source_format", "source_file", "source_sha256", "review_status",
    "search_text",
]

DDL = f"""
CREATE TABLE IF NOT EXISTS epd (
    epd_uid TEXT PRIMARY KEY,
    {", ".join(f"{column} {'REAL' if column in ('category_confidence', 'declared_quantity', 'mass_per_declared_unit_kg', 'conversion_factor_to_1kg', 'rsl_years', 'gwp_a1_a3', 'gwp_d') else 'INTEGER' if column in ('eco_platform_member', 'step1_pass', 'step1_warning', 'step1_fail', 'step1_missing', 'dgnb_mandatory_met') else 'TEXT'}" for column in SUMMARY_COLUMNS)},
    record_version INTEGER NOT NULL,
    content_hash TEXT,
    record_blob_key TEXT,
    source_blob_key TEXT,
    text_blob_key TEXT,
    record_json_z BLOB NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_epd_category ON epd(category);
CREATE INDEX IF NOT EXISTS ix_epd_manufacturer ON epd(manufacturer);
CREATE INDEX IF NOT EXISTS ix_epd_registration ON epd(registration_number);
CREATE INDEX IF NOT EXISTS ix_epd_valid_until ON epd(valid_until);
CREATE INDEX IF NOT EXISTS ix_epd_sha ON epd(source_sha256);

CREATE TABLE IF NOT EXISTS epd_result (
    epd_uid TEXT NOT NULL REFERENCES epd(epd_uid) ON DELETE CASCADE,
    indicator TEXT NOT NULL,
    module TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    value REAL,
    status TEXT,
    unit TEXT,
    raw_value TEXT,
    raw_unit TEXT,
    source_page INTEGER,
    source_table TEXT,
    confidence REAL,
    extraction_method TEXT,
    provenance TEXT,
    human_edited INTEGER,
    PRIMARY KEY (epd_uid, indicator, module, scenario)
);
CREATE INDEX IF NOT EXISTS ix_result_indicator_module ON epd_result(indicator, module);

CREATE TABLE IF NOT EXISTS epd_module (
    epd_uid TEXT NOT NULL REFERENCES epd(epd_uid) ON DELETE CASCADE,
    module TEXT NOT NULL,
    declared TEXT,
    dgnb_scope TEXT,
    gwp_value REAL,
    value_provenance TEXT,
    PRIMARY KEY (epd_uid, module)
);

CREATE TABLE IF NOT EXISTS epd_step1 (
    epd_uid TEXT NOT NULL REFERENCES epd(epd_uid) ON DELETE CASCADE,
    parameter TEXT NOT NULL,
    status TEXT,
    value_json TEXT,
    message TEXT,
    PRIMARY KEY (epd_uid, parameter)
);

CREATE TABLE IF NOT EXISTS epd_version (
    epd_uid TEXT NOT NULL,
    version INTEGER NOT NULL,
    saved_at TEXT NOT NULL,
    content_hash TEXT,
    record_blob_key TEXT,
    review_status TEXT,
    note TEXT,
    PRIMARY KEY (epd_uid, version)
);

CREATE TABLE IF NOT EXISTS blob (
    key TEXT PRIMARY KEY,
    sha256 TEXT,
    size INTEGER,
    content_type TEXT,
    original_name TEXT,
    backend TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SORTABLE = {"updated_at", "product_name", "manufacturer", "category", "gwp_a1_a3", "valid_until",
            "registration_number", "publication_date"}


class IndexDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(DDL)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('index_schema_version', ?)",
                         (str(INDEX_SCHEMA_VERSION),))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = DELETE")  # single-file database: easy to snapshot/sync
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ writes
    def upsert(self, record: dict, *, version: int, content_hash: str, record_blob_key: str | None,
               source_blob_key: str | None, text_blob_key: str | None, note: str | None = None) -> None:
        uid = record["epd_uid"]
        summary = summary_row(record)
        now = utc_now()
        payload = zlib.compress(json.dumps(record, ensure_ascii=False, default=str).encode(), 6)
        with self._lock, self.connect() as conn:
            existing = conn.execute("SELECT created_at FROM epd WHERE epd_uid = ?", (uid,)).fetchone()
            created = existing["created_at"] if existing else now
            for table in ("epd_result", "epd_module", "epd_step1"):
                conn.execute(f"DELETE FROM {table} WHERE epd_uid = ?", (uid,))
            columns = ["epd_uid"] + SUMMARY_COLUMNS + [
                "record_version", "content_hash", "record_blob_key", "source_blob_key", "text_blob_key",
                "record_json_z", "archived", "created_at", "updated_at"]
            values = [uid] + [summary.get(column) for column in SUMMARY_COLUMNS] + [
                version, content_hash, record_blob_key, source_blob_key, text_blob_key, payload, 0, created, now]
            conn.execute(f"INSERT OR REPLACE INTO epd ({', '.join(columns)}) VALUES "
                         f"({', '.join('?' for _ in columns)})", values)

            rows = flatten_results(record)
            conn.executemany(
                "INSERT OR REPLACE INTO epd_result (epd_uid, indicator, module, scenario, value, status, unit, "
                "raw_value, raw_unit, source_page, source_table, confidence, extraction_method, provenance, "
                "human_edited) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(uid, r["indicator"], r["module"], r["scenario"], r["value"], r["status"], r["unit"],
                  None if r["raw_value"] is None else str(r["raw_value"]), r["raw_unit"],
                  r["source_page"] if isinstance(r["source_page"], int) else None,
                  None if r["source_table"] is None else str(r["source_table"]), r["confidence"],
                  r["extraction_method"], r["provenance"], int(r["human_edited"])) for r in rows])

            conn.executemany(
                "INSERT INTO epd_module (epd_uid, module, declared, dgnb_scope, gwp_value, value_provenance) "
                "VALUES (?,?,?,?,?,?)",
                [(uid, m["module"], m.get("declared"), m.get("dgnb_scope"), m.get("gwp_value"),
                  m.get("value_provenance")) for m in record.get("step1", {}).get("modules", [])])

            conn.executemany(
                "INSERT INTO epd_step1 (epd_uid, parameter, status, value_json, message) VALUES (?,?,?,?,?)",
                [(uid, pid, p.get("status"), json.dumps(p.get("value"), default=str)[:4000], p.get("message"))
                 for pid, p in record.get("step1", {}).get("parameters", {}).items()])

            conn.execute("INSERT OR REPLACE INTO epd_version (epd_uid, version, saved_at, content_hash, "
                         "record_blob_key, review_status, note) VALUES (?,?,?,?,?,?,?)",
                         (uid, version, now, content_hash, record_blob_key,
                          record.get("review", {}).get("status"), note))

    def register_blob(self, key: str, sha256: str | None, size: int, content_type: str, original_name: str | None,
                      backend: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO blob (key, sha256, size, content_type, original_name, backend, "
                         "created_at) VALUES (?,?,?,?,?,?,?)",
                         (key, sha256, size, content_type, original_name, backend, utc_now()))

    def set_archived(self, uid: str, archived: bool = True) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE epd SET archived = ?, updated_at = ? WHERE epd_uid = ?",
                         (1 if archived else 0, utc_now(), uid))

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ------------------------------------------------------------------ reads
    def get_meta(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def current(self, uid: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT epd_uid, record_version, content_hash, source_sha256, record_blob_key, "
                               "source_blob_key, text_blob_key, archived FROM epd WHERE epd_uid = ?",
                               (uid,)).fetchone()
        return dict(row) if row else None

    def find_by_sha(self, sha256: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT epd_uid FROM epd WHERE source_sha256 = ?", (sha256,)).fetchall()
        return [row["epd_uid"] for row in rows]

    def get_record(self, uid: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT record_json_z FROM epd WHERE epd_uid = ?", (uid,)).fetchone()
        return json.loads(zlib.decompress(row["record_json_z"])) if row else None

    def versions(self, uid: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM epd_version WHERE epd_uid = ? ORDER BY version DESC", (uid,)).fetchall()
        return [dict(row) for row in rows]

    def search(self, text: str = "", category: str | None = None, manufacturer: str | None = None,
               standard: str | None = None, only_valid: bool = False, only_dgnb: bool = False,
               include_archived: bool = False, sort: str = "updated_at", descending: bool = True,
               limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, params = [], []
        if not include_archived:
            where.append("archived = 0")
        for token in (text or "").lower().split():
            where.append("search_text LIKE ?")
            params.append(f"%{token}%")
        if category:
            where.append("category = ?")
            params.append(category)
        if manufacturer:
            where.append("manufacturer = ?")
            params.append(manufacturer)
        if standard:
            where.append("standard = ?")
            params.append(standard)
        if only_valid:
            where.append("(valid_until IS NULL OR valid_until >= date('now'))")
        if only_dgnb:
            where.append("dgnb_mandatory_met = 1")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sort = sort if sort in SORTABLE else "updated_at"
        order = f"ORDER BY {sort} {'DESC' if descending else 'ASC'} NULLS LAST" if sqlite3.sqlite_version_info >= (3, 30) \
            else f"ORDER BY {sort} {'DESC' if descending else 'ASC'}"
        columns = ", ".join(["epd_uid"] + [c for c in SUMMARY_COLUMNS if c != "search_text"] +
                            ["record_version", "archived", "created_at", "updated_at"])
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM epd {clause}", params).fetchone()[0]
            rows = conn.execute(f"SELECT {columns} FROM epd {clause} {order} LIMIT ? OFFSET ?",
                                params + [int(limit), int(offset)]).fetchall()
        return [dict(row) for row in rows], total

    def results(self, uids: list[str], indicators: list[str] | None = None,
                modules: list[str] | None = None) -> list[dict]:
        if not uids:
            return []
        where = [f"epd_uid IN ({','.join('?' for _ in uids)})"]
        params: list = list(uids)
        if indicators:
            where.append(f"indicator IN ({','.join('?' for _ in indicators)})")
            params += indicators
        if modules:
            where.append(f"module IN ({','.join('?' for _ in modules)})")
            params += modules
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM epd_result WHERE {' AND '.join(where)} "
                                "ORDER BY epd_uid, indicator, module, scenario", params).fetchall()
        return [dict(row) for row in rows]

    def facets(self) -> dict:
        with self.connect() as conn:
            categories = conn.execute("SELECT category, category_label, COUNT(*) AS n FROM epd WHERE archived = 0 "
                                      "GROUP BY category, category_label ORDER BY n DESC").fetchall()
            manufacturers = conn.execute("SELECT manufacturer, COUNT(*) AS n FROM epd WHERE archived = 0 AND "
                                         "manufacturer IS NOT NULL GROUP BY manufacturer ORDER BY manufacturer"
                                         ).fetchall()
            standards = conn.execute("SELECT standard, COUNT(*) AS n FROM epd WHERE archived = 0 "
                                     "GROUP BY standard").fetchall()
        return {"categories": [dict(r) for r in categories], "manufacturers": [dict(r) for r in manufacturers],
                "standards": [dict(r) for r in standards]}

    def stats(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(archived = 0) AS active, "
                "SUM(archived = 0 AND (valid_until IS NULL OR valid_until >= date('now'))) AS valid, "
                "SUM(archived = 0 AND dgnb_mandatory_met = 1) AS dgnb, "
                "SUM(archived = 0 AND eco_platform_member = 1) AS eco FROM epd").fetchone()
            results = conn.execute("SELECT COUNT(*) FROM epd_result").fetchone()[0]
            blobs = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blob").fetchone()
        stats = {key: (row[key] or 0) for key in row.keys()}
        stats.update({"result_rows": results, "blobs": blobs[0], "blob_bytes": blobs[1],
                      "index_bytes": self.path.stat().st_size if self.path.exists() else 0})
        return stats

    def all_uids(self, include_archived: bool = True) -> list[str]:
        with self.connect() as conn:
            query = "SELECT epd_uid FROM epd" + ("" if include_archived else " WHERE archived = 0")
            return [row[0] for row in conn.execute(query).fetchall()]

    def backup_to(self, target: Path) -> Path:
        """Consistent copy of the database (SQLite online backup API)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            source = sqlite3.connect(str(self.path))
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        return target
