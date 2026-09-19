def validate_epd(record):
    errors = []
    warnings = []

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
    if metadata.get("standard_profile") == "unresolved":
        warnings.append("EN 15804 / impact-method profile could not be resolved.")

    gwp = results.get("GWP-total") or results.get("GWP")
    if not gwp:
        errors.append("No GWP result row was extracted from a detected table.")
    else:
        modules = gwp.get("modules", {})
        if not any(module in modules for module in ("A1-A3", "A1", "A2", "A3")):
            errors.append("No product-stage GWP (A1-A3 or A1/A2/A3) was extracted.")

        if "D" in modules:
            # Presence is fine; this message reminds the downstream logic.
            pass

    if record.get("a1_a3_reporting_mode") == "partial_split":
        warnings.append(
            "A1-A3 reporting is partial. Do not derive missing A1/A2/A3 values."
        )

    if record.get("document", {}).get("pages_without_text"):
        warnings.append(
            "One or more PDF pages had no text layer. Scanned content may require OCR later."
        )

    record.setdefault("qa", {})
    record["qa"]["errors"] = errors
    record["qa"]["warnings"] = warnings
    record["qa"]["manual_review_required"] = bool(errors or warnings) or True

    return record
