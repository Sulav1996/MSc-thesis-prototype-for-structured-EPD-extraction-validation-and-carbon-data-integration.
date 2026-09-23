"""
Extraction orchestrator: uploaded file → normalised EPD record (transfer model v2).

    PDF  → pdf_reader → metadata + result tables (+ word fallback) ─┐
    XML/ZIP (ILCD+EPD) → ilcd_xml ─────────────────────────────────┤
                                                                   ▼
                       Step 1 (DGNB checklist) + Step 2 (category) + QA

Nothing is written to storage here; the record is a draft until a person
approves it in the review screen.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from dictionary import load_dictionary, step1_profile
from extraction.categorizer import categorize_epd
from extraction.common import parse_number  # noqa: F401  (re-exported for older imports)
from extraction.metadata import extract_extended_metadata, extract_metadata
from extraction.step1_dgnb import evaluate_step1
from extraction.tables import (
    determine_a1_a3_mode,
    extract_module_declarations,
    extract_results_from_tables,
    extract_results_from_words,
    merge_results,
)

SCHEMA_VERSION = "2.0"
EXTRACTOR_VERSION = "2.0.0"


def extraction_profile() -> str:
    """'step1' (default): only Step 1 indicators are kept. 'extended': keep everything found."""
    return os.environ.get("EPD_EXTRACTION_PROFILE", "step1").strip().lower()


def step1_indicator_codes() -> set[str]:
    codes = set()
    for parameter in step1_profile().get("parameters", []):
        for indicator in parameter.get("indicators", []):
            for family_codes in indicator.get("codes", {}).values():
                codes.update(family_codes)
    return codes


def detect_format(file_bytes: bytes, filename: str) -> str:
    name = (filename or "").lower()
    head = file_bytes[:512].lstrip()
    if head.startswith(b"%PDF") or name.endswith(".pdf"):
        return "pdf"
    if head.startswith(b"PK") or name.endswith(".zip"):
        return "ilcd_zip"
    if head.startswith(b"<?xml") or head.startswith(b"<") or name.endswith(".xml"):
        return "ilcd_xml"
    return "unknown"


def _base_record(filename, file_bytes, source_format) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "epd_uid": None,
        "document": {
            "source_file": filename,
            "source_format": source_format,
            "sha256": hashlib.sha256(file_bytes).hexdigest(),
            "file_size": len(file_bytes),
            "extracted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "extractor_version": EXTRACTOR_VERSION,
            "dictionary_version": load_dictionary().get("version"),
            "extraction_profile": extraction_profile(),
        },
        "metadata": {},
        "metadata_provenance": {},
        "physical_properties": {},
        "results": {},
        "module_declarations": {},
        "a1_a3_reporting_mode": "none",
        "scenarios": {},
        "classification": {},
        "step1": {},
        "qa": {"warnings": [], "errors": [], "manual_review_required": True},
        "review": {"status": "extracted_not_approved", "human_verified": False},
    }


def _finish(record: dict, full_text: str) -> dict:
    if extraction_profile() != "extended":
        keep = step1_indicator_codes()
        dropped = sorted(code for code in record["results"] if code not in keep)
        record["results"] = {code: data for code, data in record["results"].items() if code in keep}
        if dropped:
            record["document"]["indicators_not_kept"] = dropped
    record["a1_a3_reporting_mode"] = determine_a1_a3_mode(record["results"])
    record["classification"] = categorize_epd(record, full_text)
    record["step1"] = evaluate_step1(record, full_text)
    record["_full_text"] = full_text  # transient: saved as a text blob, not inside the record
    return record


def _extract_pdf(file_bytes: bytes, filename: str) -> dict:
    from extraction.pdf_reader import read_pdf

    raw = read_pdf(file_bytes)
    record = _base_record(filename, file_bytes, "pdf")
    metadata, provenance, physical = extract_metadata(raw, filename)
    if extraction_profile() == "extended":
        extra, extra_prov = extract_extended_metadata(raw)
        metadata.update({k: v for k, v in extra.items() if k not in metadata})
        provenance.update(extra_prov)

    profile = metadata.get("standard_profile", "unresolved")
    table_results, pages_with_results = extract_results_from_tables(raw, profile)
    word_results = extract_results_from_words(raw, profile, skip_pages=pages_with_results)
    results = merge_results(table_results, word_results)

    record["document"].update({
        "source_database_or_programme": metadata.get("programme_operator"),
        "language": metadata.get("language"),
        "standard_profile": profile,
        "extraction_method": "pdf_text+pdf_table" + ("+pdf_words" if word_results else ""),
        "pdf_backend": raw.get("backend"),
        "page_count": raw["page_count"],
        "pages_without_text": raw["pages_without_text"],
    })
    record.update({
        "metadata": metadata,
        "metadata_provenance": provenance,
        "physical_properties": physical,
        "results": results,
        "module_declarations": extract_module_declarations(raw),
    })
    return _finish(record, raw["text"])


def _extract_ilcd(file_bytes: bytes, filename: str, source_format: str) -> list[dict]:
    from extraction.ilcd_xml import parse_ilcd_bytes

    records = []
    for parsed in parse_ilcd_bytes(file_bytes, filename):
        record = _base_record(filename, file_bytes, source_format)
        metadata = parsed["metadata"]
        record["document"].update({
            "source_path_in_archive": parsed.get("source_path") if source_format == "ilcd_zip" else None,
            "source_database_or_programme": metadata.get("programme_operator"),
            "language": metadata.get("language"),
            "standard_profile": metadata.get("standard_profile"),
            "extraction_method": "ilcd_xml",
            "unknown_indicator_uuids": parsed.get("unknown_indicators") or None,
        })
        record.update({
            "metadata": metadata,
            "metadata_provenance": parsed["metadata_provenance"],
            "physical_properties": parsed["physical_properties"],
            "results": parsed["results"],
            "module_declarations": parsed["module_declarations"],
            "scenarios": parsed.get("scenarios", {}),
        })
        records.append(_finish(record, parsed.get("document_text", "")))
    return records


def extract_epd_records(file_bytes: bytes, filename: str) -> list[dict]:
    """All EPD records in an uploaded file (an ILCD ZIP can hold several)."""
    source_format = detect_format(file_bytes, filename)
    if source_format == "pdf":
        return [_extract_pdf(file_bytes, filename)]
    if source_format in ("ilcd_xml", "ilcd_zip"):
        return _extract_ilcd(file_bytes, filename, source_format)
    raise ValueError(f"Unsupported file type for {filename!r}. Upload a PDF, an ILCD+EPD XML or an ILCD ZIP.")


def extract_epd(file_bytes: bytes, filename: str) -> dict:
    """Backward-compatible entry point: the first (usually only) record of a file."""
    records = extract_epd_records(file_bytes, filename)
    if not records:
        raise ValueError("No EPD process data set found in the file.")
    return records[0]


def refresh_derived_blocks(record: dict, full_text: str = "") -> dict:
    """Re-run Step 1, A1-A3 mode and (unless overridden) Step 2 after human edits."""
    record["a1_a3_reporting_mode"] = determine_a1_a3_mode(record.get("results", {}))
    classification = record.get("classification") or {}
    if not classification.get("user_override"):
        record["classification"] = categorize_epd(record, full_text)
    record["step1"] = evaluate_step1(record, full_text)
    return record
