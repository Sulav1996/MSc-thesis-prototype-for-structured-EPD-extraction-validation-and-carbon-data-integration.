"""
Transfer model: the versioned JSON document that moves an EPD between
extraction → review → storage → retrieval → export (and later LCAbyg / graph).

* ``schema_version`` "2.0" (records saved by the first prototype are "1.1" and
  are migrated automatically, nothing is deleted).
* ``epd_uid`` is a stable key: the registration number when available,
  otherwise the ILCD UUID, otherwise the file hash.
* ``flatten_results`` gives the long format (one row per indicator × module ×
  scenario) that the index database and CSV exports use.
* Bundles (.zip with manifest.json, records/*.json, files/*) move a whole
  database between a laptop and the Render server.
"""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone

SCHEMA_VERSION = "2.0"
TRANSFER_FORMAT = "epd-prototype-transfer"
TRANSIENT_PREFIX = "_"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return re.sub(r"-{2,}", "-", value).strip("-.")[:80]


def make_epd_uid(record: dict) -> str:
    metadata = record.get("metadata", {})
    registration = metadata.get("registration_number")
    if registration and re.search(r"\d", registration):
        return _slug(registration.upper())
    if metadata.get("epd_uuid"):
        return "ilcd-" + _slug(metadata["epd_uuid"].lower())
    sha = record.get("document", {}).get("sha256")
    if sha:
        return "sha-" + sha[:20]
    payload = json.dumps(strip_transient(record), sort_keys=True, default=str).encode()
    return "rec-" + hashlib.sha256(payload).hexdigest()[:20]


def content_hash(record: dict) -> str:
    """Hash of the scientific content (ignores timestamps/storage info) to detect real changes."""
    clean = strip_transient(record)
    for key in ("storage", "review"):
        clean.pop(key, None)
    clean.get("document", {}).pop("extracted_at", None)
    clean.get("step1", {}).pop("evaluated_at", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# (de)serialisation
# ---------------------------------------------------------------------------

def strip_transient(record: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in record.items() if not key.startswith(TRANSIENT_PREFIX)}


def to_json_bytes(record: dict, compress: bool = True) -> bytes:
    data = json.dumps(strip_transient(record), ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return gzip.compress(data, compresslevel=6) if compress else data


def from_json_bytes(data: bytes) -> dict:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8-sig"))


# ---------------------------------------------------------------------------
# migration 1.1 → 2.0
# ---------------------------------------------------------------------------

def migrate_record(record: dict, full_text: str = "") -> dict:
    """Bring older records to schema 2.0 without discarding any stored value."""
    record = copy.deepcopy(record)
    version = str(record.get("schema_version", "1.0"))
    if version == SCHEMA_VERSION and record.get("step1") and record.get("classification"):
        return record

    from extraction.categorizer import categorize_epd
    from extraction.common import resolve_validity_dates
    from extraction.metadata import parse_declared_unit
    from extraction.step1_dgnb import evaluate_step1
    from extraction.tables import determine_a1_a3_mode

    record.setdefault("document", {})
    record.setdefault("metadata", {})
    record.setdefault("physical_properties", {})
    record.setdefault("results", {})
    record.setdefault("module_declarations", {})
    metadata = record["metadata"]
    document = record["document"]

    document.setdefault("source_format", "pdf" if str(document.get("source_file", "")).lower().endswith(".pdf")
                        else "unknown")
    document.setdefault("migrated_from_schema", version)
    pub, val = resolve_validity_dates(metadata.get("publication_date"), metadata.get("valid_until"))
    if pub:
        metadata.setdefault("publication_date_iso", pub["iso"])
    if val:
        metadata.setdefault("valid_until_iso", val["iso"])
    declared = parse_declared_unit(metadata.get("declared_unit_raw"))
    if declared:
        metadata.setdefault("declared_quantity", declared.get("quantity"))
        metadata.setdefault("declared_unit", declared.get("unit"))
        metadata.setdefault("declared_unit_dimension", declared.get("dimension"))
    if metadata.get("verification_type") and "iso14025_mentioned" not in metadata:
        metadata["iso14025_mentioned"] = True if "external" in metadata["verification_type"].lower() else None

    # v1.1 kept the conversion factor under metadata or physical_properties.
    physical = record["physical_properties"]
    if metadata.get("conversion_factor_to_1kg") and "conversion_factor_to_1kg" not in physical:
        physical["conversion_factor_to_1kg"] = metadata["conversion_factor_to_1kg"]

    record["a1_a3_reporting_mode"] = determine_a1_a3_mode(record["results"])
    if not record.get("classification"):
        record["classification"] = categorize_epd(record, full_text)
    record["step1"] = evaluate_step1(record, full_text)
    record["schema_version"] = SCHEMA_VERSION
    return record


# ---------------------------------------------------------------------------
# long format + summaries
# ---------------------------------------------------------------------------

RESULT_COLUMNS = [
    "epd_uid", "indicator", "module", "scenario", "value", "status", "unit", "raw_value", "raw_unit",
    "source_page", "source_table", "confidence", "extraction_method", "provenance", "human_edited",
]


def flatten_results(record: dict) -> list[dict]:
    uid = record.get("epd_uid")
    rows = []
    for code, data in record.get("results", {}).items():
        unit = data.get("canonical_unit")
        seen = set()
        for module, value in data.get("modules", {}).items():
            scenario = value.get("scenario") or ""
            seen.add((module, scenario))
            rows.append(_row(uid, code, module, scenario, unit, value))
        for module, scenarios in data.get("scenario_modules", {}).items():
            for scenario, value in scenarios.items():
                if (module, scenario) not in seen:
                    rows.append(_row(uid, code, module, scenario, unit, value))
    return rows


def _row(uid, code, module, scenario, unit, value) -> dict:
    return {
        "epd_uid": uid, "indicator": code, "module": module, "scenario": scenario or "",
        "value": value.get("value"), "status": value.get("status"), "unit": unit,
        "raw_value": value.get("raw_value"), "raw_unit": value.get("raw_unit"),
        "source_page": value.get("source_page"), "source_table": value.get("source_table"),
        "confidence": value.get("confidence"), "extraction_method": value.get("extraction_method"),
        "provenance": value.get("provenance"), "human_edited": bool(value.get("human_edited")),
    }


def summary_row(record: dict) -> dict:
    """The columns stored in the index table for search/filter/sort."""
    metadata = record.get("metadata", {})
    document = record.get("document", {})
    classification = record.get("classification", {})
    step1 = record.get("step1", {})
    physical = record.get("physical_properties", {})
    summary = step1.get("summary", {})
    params = step1.get("parameters", {})
    gwp_code = step1.get("gwp_indicator")
    stage = next((m for m in step1.get("modules", []) if m["module"] == "A1-A3"), {})
    d_module = next((m for m in step1.get("modules", []) if m["module"] == "D"), {})
    searchable = " ".join(str(metadata.get(key) or "") for key in (
        "registration_number", "product_name", "manufacturer", "programme_operator", "pcr",
        "product_description")) + " " + " ".join(str(classification.get(k) or "") for k in (
            "category", "category_label", "building_element"))
    return {
        "epd_uid": record.get("epd_uid"),
        "registration_number": metadata.get("registration_number"),
        "product_name": metadata.get("product_name"),
        "manufacturer": metadata.get("manufacturer"),
        "programme_operator": metadata.get("programme_operator"),
        "eco_platform_member": 1 if summary.get("eco_platform_member") else 0,
        "category": classification.get("category"),
        "category_label": classification.get("category_label"),
        "building_element": classification.get("building_element"),
        "category_confidence": classification.get("confidence"),
        "standard": metadata.get("standard"),
        "standard_profile": metadata.get("standard_profile"),
        "declared_unit": metadata.get("declared_unit_raw"),
        "declared_quantity": metadata.get("declared_quantity"),
        "declared_unit_code": metadata.get("declared_unit"),
        "mass_per_declared_unit_kg": physical.get("mass_per_declared_unit_kg"),
        "conversion_factor_to_1kg": physical.get("conversion_factor_to_1kg"),
        "rsl_years": metadata.get("reference_service_life_years"),
        "publication_date": metadata.get("publication_date_iso"),
        "valid_until": metadata.get("valid_until_iso"),
        "geography": metadata.get("geography"),
        "gwp_indicator": gwp_code,
        "gwp_a1_a3": stage.get("gwp_value"),
        "gwp_a1_a3_provenance": stage.get("value_provenance"),
        "gwp_d": d_module.get("gwp_value"),
        "verification": (params.get("third_party_verification") or {}).get("status"),
        "step1_pass": summary.get("pass"),
        "step1_warning": summary.get("warning"),
        "step1_fail": summary.get("fail"),
        "step1_missing": summary.get("missing"),
        "dgnb_mandatory_met": 1 if summary.get("dgnb_mandatory_met") else 0,
        "source_format": document.get("source_format"),
        "source_file": document.get("source_file"),
        "source_sha256": document.get("sha256"),
        "review_status": record.get("review", {}).get("status"),
        "search_text": searchable.lower()[:4000],
    }


# ---------------------------------------------------------------------------
# exports / bundles
# ---------------------------------------------------------------------------

def results_csv(records: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RESULT_COLUMNS)
    writer.writeheader()
    for record in records:
        for row in flatten_results(record):
            writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def summary_csv(records: list[dict]) -> bytes:
    rows = [summary_row(record) for record in records]
    if not rows:
        return b""
    fields = [key for key in rows[0] if key != "search_text"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def jsonl(records: list[dict]) -> bytes:
    return b"\n".join(json.dumps(strip_transient(r), ensure_ascii=False, default=str).encode() for r in records)


def build_bundle(records: list[dict], files: dict[str, bytes] | None = None, dictionary_version: str | None = None) -> bytes:
    """ZIP: manifest.json + records/<uid>.json + blobs/<key> (source PDFs/XML) + results_long.csv."""
    buffer = io.BytesIO()
    manifest = {
        "format": TRANSFER_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "dictionary_version": dictionary_version,
        "records": [],
        "files": sorted((files or {}).keys()),
    }
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            name = f"records/{record['epd_uid']}.json"
            archive.writestr(name, json.dumps(strip_transient(record), ensure_ascii=False, indent=1, default=str))
            manifest["records"].append({"epd_uid": record["epd_uid"], "path": name,
                                        "version": record.get("storage", {}).get("version")})
        for key, data in (files or {}).items():
            archive.writestr(f"blobs/{key}", data)
        archive.writestr("manifest.json", json.dumps(manifest, indent=1))
        archive.writestr("results_long.csv", results_csv(records))
    return buffer.getvalue()


def read_bundle(data: bytes) -> tuple[dict, list[dict], dict[str, bytes]]:
    archive = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("format") != TRANSFER_FORMAT:
        raise ValueError("Not an EPD prototype transfer bundle.")
    records = [json.loads(archive.read(item["path"])) for item in manifest.get("records", [])]
    files = {}
    for key in manifest.get("files", []):
        info = archive.getinfo(f"blobs/{key}")
        if info.file_size > 400 * 1024 * 1024:
            continue
        files[key] = archive.read(info)
    return manifest, records, files
