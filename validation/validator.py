"""
Rule-based validation of an extracted EPD record before it can be approved.

Errors block approval; warnings are shown to the reviewer. Human approval is
always required, even when no rule fires.
"""

from __future__ import annotations

from dictionary import unit_key

PRODUCT_STAGE = ("A1", "A2", "A3")


def _value(results, code, module):
    record = results.get(code, {}).get("modules", {}).get(module)
    return record.get("value") if record else None


def _check_a1_a3_sum(results, warnings):
    for code, data in results.items():
        total = _value(results, code, "A1-A3")
        parts = [_value(results, code, module) for module in PRODUCT_STAGE]
        if total is None or any(part is None for part in parts):
            continue
        calculated = sum(parts)
        tolerance = max(abs(total) * 0.05, 1e-9)
        if abs(calculated - total) > tolerance:
            warnings.append(f"{code}: A1+A2+A3 = {calculated:.4g} differs from the reported A1-A3 = {total:.4g} "
                            "by more than 5 % (the reported total is kept).")


def _check_gwp_components(results, warnings):
    if "GWP-total" not in results:
        return
    for module in results["GWP-total"].get("modules", {}):
        total = _value(results, "GWP-total", module)
        parts = [_value(results, code, module) for code in ("GWP-fossil", "GWP-biogenic", "GWP-luluc")]
        if total is None or any(part is None for part in parts):
            continue
        calculated = sum(parts)
        if abs(calculated - total) > max(abs(total) * 0.05, 0.05):
            warnings.append(f"GWP-total {module}: fossil + biogenic + luluc = {calculated:.4g} but "
                            f"GWP-total = {total:.4g} — check the row alignment.")


def _check_units(results, warnings):
    for code, data in results.items():
        canonical = unit_key(data.get("canonical_unit"))
        raws = {record.get("raw_unit") for record in data.get("modules", {}).values() if record.get("raw_unit")}
        for raw in raws:
            raw_key = unit_key(raw)
            if canonical and raw_key and raw_key != canonical:
                warnings.append(f"{code}: reported unit '{raw}' is not the expected '{data.get('canonical_unit')}'.")


def validate_epd(record: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    metadata = record.get("metadata", {})
    results = record.get("results", {})

    if not metadata.get("product_name"):
        warnings.append("Product name was not extracted.")
    if not metadata.get("manufacturer"):
        warnings.append("Manufacturer was not extracted.")
    if not metadata.get("registration_number"):
        warnings.append("EPD registration number was not extracted.")
    if not metadata.get("declared_unit_raw"):
        errors.append("Declared unit is missing.")
    if metadata.get("standard_profile") in (None, "unresolved"):
        warnings.append("EN 15804 / impact-method profile could not be resolved.")
    if metadata.get("date_format_assumption"):
        warnings.append(f"Dates were read as {metadata['date_format_assumption']} — confirm day/month order.")

    gwp = results.get("GWP-total") or results.get("GWP")
    if not results:
        errors.append("No environmental result table was extracted.")
    elif not gwp:
        errors.append("No GWP result row was extracted.")
    else:
        modules = gwp.get("modules", {})
        if not any(module in modules for module in ("A1-A3",) + PRODUCT_STAGE):
            errors.append("No product-stage GWP (A1-A3 or A1/A2/A3) was extracted.")

    if record.get("a1_a3_reporting_mode") == "partial_split":
        warnings.append("A1-A3 reporting is partial. Missing A1/A2/A3 values are never derived.")

    _check_a1_a3_sum(results, warnings)
    _check_gwp_components(results, warnings)
    _check_units(results, warnings)

    low_confidence = sum(1 for data in results.values() for rec in data.get("modules", {}).values()
                         if (rec.get("confidence") or 1) < 0.85 and not rec.get("human_verified"))
    if low_confidence:
        warnings.append(f"{low_confidence} values have extraction confidence below 0.85 — compare with the source.")

    if record.get("document", {}).get("pages_without_text"):
        warnings.append("One or more PDF pages had no text layer. Scanned content needs manual entry (no OCR).")

    step1 = record.get("step1", {})
    for parameter in step1.get("parameters", {}).values():
        if parameter["status"] == "fail":
            warnings.append(f"DGNB requirement not met · {parameter.get('parameter') or parameter['id']}: "
                            f"{parameter['message']}")
        elif parameter["status"] == "missing":
            warnings.append(f"Step 1 · {parameter.get('parameter') or parameter['id']}: {parameter['message']}")
    if step1.get("validity", {}).get("valid_today") is False:
        warnings.append(f"The EPD expired on {step1['validity'].get('valid_until')}.")

    classification = record.get("classification", {})
    if classification and not classification.get("user_override") and (classification.get("confidence") or 0) < 0.5:
        warnings.append("Category confidence is low — confirm the building material / component category.")

    record.setdefault("qa", {})
    record["qa"]["errors"] = errors
    record["qa"]["warnings"] = warnings
    record["qa"]["manual_review_required"] = True
    return record
