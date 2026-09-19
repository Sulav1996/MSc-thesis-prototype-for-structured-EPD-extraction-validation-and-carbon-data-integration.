
import re
from datetime import datetime, timezone

from dictionary import (
    detect_standard_profile,
    match_indicator,
    missing_value_status,
    normalize_module,
    normalize_text,
)


NUMBER_RE = re.compile(
    r"^[+\-−]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:\s*[Ee][+\-]?\d+)?$"
)


def parse_number(raw):
    """Parse EPD scientific notation and decimal comma conservatively."""
    if raw is None:
        return None

    text = str(raw).strip().replace("−", "-").replace("‐", "-")
    text = re.sub(r"\s+(?=[Ee][+\-]?\d+$)", "", text)

    if not NUMBER_RE.match(text):
        return None

    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _clean_cell(cell):
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\xa0", " ")).strip()


def _clean_lines(text):
    return [
        re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()
        for line in text.splitlines()
        if line.strip()
    ]


def _label_pattern(label):
    words = [re.escape(part) for part in label.split()]
    return r"^\s*" + r"\s+".join(words) + r"\s*[:\-–—]?\s*(.*)$"


def _find_labeled_value(raw_document, labels, max_pages=None):
    """
    Find a value either on the same line as a label or on the next non-empty line.
    Long labels are tried first to prevent 'Owner' matching before
    'Owner of the Declaration'.
    """
    labels = sorted(labels, key=len, reverse=True)
    pages = raw_document["pages"]

    if max_pages is not None:
        pages = pages[:max_pages]

    for page in pages:
        lines = _clean_lines(page["text"])

        for index, line in enumerate(lines):
            for label in labels:
                match = re.match(
                    _label_pattern(label),
                    line,
                    flags=re.IGNORECASE,
                )

                if not match:
                    continue

                remainder = match.group(1).strip()

                if remainder:
                    return (
                        remainder,
                        page["page_number"],
                        line,
                    )

                if index + 1 < len(lines):
                    return (
                        lines[index + 1],
                        page["page_number"],
                        f"{line} | {lines[index + 1]}",
                    )

    return None, None, None


def _add_metadata(metadata, provenance, key, value, page, source_text,
                  method, confidence):
    if value in (None, ""):
        return

    metadata[key] = value
    provenance[key] = {
        "source_page": page,
        "source_text": source_text,
        "extraction_method": method,
        "confidence": confidence,
    }


def _extract_cover_product_name(raw_document, manufacturer=None):
    """
    Conservative cover-page product-name fallback for EPD layouts
    that place the title after the validity date.
    """
    if not raw_document["pages"]:
        return None, None

    page = raw_document["pages"][0]
    lines = _clean_lines(page["text"])

    start_index = None

    for index, line in enumerate(lines):
        if normalize_text(line) in {
            "valid to",
            "valid until",
            "validity",
            "expiry date",
        }:
            # Skip the value line after the label.
            start_index = index + 2
            break

    if start_index is None:
        return None, None

    title_lines = []

    for line in lines[start_index:]:
        n = normalize_text(line)

        if "http://" in n or "https://" in n or "www." in n:
            break

        if manufacturer and n == normalize_text(manufacturer):
            break

        if n in {
            "environmental product declaration",
            "publisher",
            "programme holder",
            "program holder",
            "owner of the declaration",
        }:
            continue

        title_lines.append(line)

        if len(title_lines) >= 3:
            break

    if not title_lines:
        return None, None

    title = " ".join(title_lines).strip()

    return title, " | ".join(title_lines)


def _extract_section_text(page_text, start_heading, end_headings):
    """
    Return text between a heading and the first following end heading.
    """
    lines = _clean_lines(page_text)
    start_norm = normalize_text(start_heading)
    end_norms = {normalize_text(item) for item in end_headings}

    start = None

    for index, line in enumerate(lines):
        if normalize_text(line) == start_norm:
            start = index + 1
            break

    if start is None:
        return None

    selected = []

    for line in lines[start:]:
        if normalize_text(line) in end_norms:
            break
        selected.append(line)

    text = " ".join(selected).strip()
    return text or None


def _extract_declared_unit_from_tables(raw_document):
    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = table.get("cells", [])

            for row in rows:
                clean = [_clean_cell(cell) for cell in row]

                if not clean:
                    continue

                if normalize_text(clean[0]) == "declared unit":
                    value = clean[1] if len(clean) > 1 else ""
                    unit = clean[2] if len(clean) > 2 else ""

                    if value and unit:
                        return (
                            f"{value} {unit}",
                            page["page_number"],
                            " | ".join(cell for cell in clean if cell),
                        )

    return None, None, None


def _extract_product_composition(raw_document):
    """
    Capture composition tables containing percentage rows.
    """
    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = table.get("cells", [])

            if len(rows) < 3:
                continue

            header = [_clean_cell(cell) for cell in rows[0]]

            if (
                len(header) >= 3
                and normalize_text(header[0]) == "name"
                and normalize_text(header[1]) == "value"
                and normalize_text(header[2]) == "unit"
            ):
                parsed = []

                for row in rows[1:]:
                    clean = [_clean_cell(cell) for cell in row]

                    if len(clean) < 2 or not clean[0]:
                        continue

                    value = parse_number(clean[1])
                    unit = clean[2] if len(clean) > 2 else ""

                    if unit == "%" or "%" in unit:
                        parsed.append(
                            {
                                "component": clean[0],
                                "value": value if value is not None else clean[1],
                                "unit": unit or "%",
                            }
                        )

                if len(parsed) >= 3:
                    return parsed, page["page_number"]

    return None, None


def _extract_technical_specifications(raw_document):
    """
    Capture the first technical-data Name/Value/Unit table before the LCA section.
    """
    for page in raw_document["pages"]:
        if page["page_number"] > 4:
            break

        for table in page.get("tables", []):
            rows = table.get("cells", [])

            if len(rows) < 3:
                continue

            header = [_clean_cell(cell) for cell in rows[0]]

            if (
                len(header) >= 3
                and normalize_text(header[0]) == "name"
                and normalize_text(header[1]) == "value"
                and normalize_text(header[2]) == "unit"
            ):
                # Skip composition and declared-unit tables.
                first_names = {
                    normalize_text(_clean_cell(row[0]))
                    for row in rows[1:4]
                    if row and row[0]
                }

                if "declared unit" in first_names:
                    continue

                if any(
                    "glazing unit" in name
                    or "wooden frame" in name
                    or "polyurethane frame" in name
                    for name in first_names
                ):
                    continue

                result = {}

                for row in rows[1:]:
                    clean = [_clean_cell(cell) for cell in row]

                    if not clean or not clean[0]:
                        continue

                    result[clean[0]] = {
                        "value": clean[1] if len(clean) > 1 else None,
                        "unit": clean[2] if len(clean) > 2 else None,
                    }

                if result:
                    return result, page["page_number"]

    return None, None


def _extract_packaging_materials(raw_document):
    """
    Capture A5 packaging scenario rows as packaging material quantities.
    """
    packaging_terms = (
        "packaging for waste treatment",
        "eps packaging",
        "film packaging",
    )

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = table.get("cells", [])

            if len(rows) < 2:
                continue

            parsed = []

            for row in rows[1:]:
                clean = [_clean_cell(cell) for cell in row]

                if len(clean) < 2 or not clean[0]:
                    continue

                name_norm = normalize_text(clean[0])

                if any(term in name_norm for term in packaging_terms):
                    parsed.append(
                        {
                            "material": clean[0],
                            "value": parse_number(clean[1]),
                            "unit": clean[2] if len(clean) > 2 else None,
                        }
                    )

            if parsed:
                return parsed, page["page_number"]

    return None, None


def extract_metadata(raw_document):
    """
    Extract high-value EPD metadata using conservative, field-specific rules.
    Generic short aliases such as 'Product', 'Owner' and 'Classification'
    are deliberately avoided because they caused false matches in references
    and narrative text.
    """
    metadata = {}
    provenance = {}

    # Identification
    value, page, source = _find_labeled_value(
        raw_document,
        ["Declaration number", "EPD registration number", "EPD number", "EPD ID"],
        max_pages=3,
    )
    _add_metadata(
        metadata, provenance, "registration_number",
        value, page, source, "field_specific_label", 0.99
    )

    value, page, source = _find_labeled_value(
        raw_document,
        ["Programme holder", "Programme operator", "Program operator", "Publisher"],
        max_pages=3,
    )
    _add_metadata(
        metadata, provenance, "programme_operator",
        value, page, source, "field_specific_label", 0.97
    )

    value, page, source = _find_labeled_value(
        raw_document,
        ["Owner of the Declaration", "Owner of the declaration", "Declaration owner", "Manufacturer"],
        max_pages=3,
    )
    _add_metadata(
        metadata, provenance, "manufacturer",
        value, page, source, "field_specific_label", 0.97
    )

    value, page, source = _find_labeled_value(
        raw_document,
        ["Issue date", "Date of issue", "Publication date"],
        max_pages=3,
    )
    _add_metadata(
        metadata, provenance, "publication_date",
        value, page, source, "field_specific_label", 0.99
    )

    value, page, source = _find_labeled_value(
        raw_document,
        ["Valid to", "Valid until", "Expiry date", "Expiration date"],
        max_pages=3,
    )
    _add_metadata(
        metadata, provenance, "valid_until",
        value, page, source, "field_specific_label", 0.99
    )

    # Product name from cover page after dates.
    product_name, source = _extract_cover_product_name(
        raw_document,
        manufacturer=metadata.get("manufacturer"),
    )
    _add_metadata(
        metadata, provenance, "product_name",
        product_name, 1 if product_name else None, source,
        "cover_title", 0.95
    )

    # Standard/profile
    profile = detect_standard_profile(raw_document["text"])
    metadata["standard_profile"] = profile

    if profile.startswith("EN15804_A2"):
        standard = "EN 15804+A2"
    elif profile == "EN15804_A1_CML":
        standard = "EN 15804+A1"
    else:
        standard = None

    if standard:
        _add_metadata(
            metadata, provenance, "standard",
            standard, None, standard,
            "standard_profile_detection", 0.98
        )

    # PCR / product category
    full_text = raw_document["text"].replace("\xa0", " ")

    pcr_match = re.search(
        r"This declaration is based on the product category rules:\s*"
        r"([^\n]+)",
        full_text,
        flags=re.IGNORECASE,
    )
    if pcr_match:
        pcr = re.sub(r"\s+", " ", pcr_match.group(1)).strip()
        _add_metadata(
            metadata, provenance, "pcr",
            pcr, 2, pcr_match.group(0)[:250],
            "field_specific_regex", 0.96
        )

        category = pcr.split(",")[0].strip()
        _add_metadata(
            metadata, provenance, "product_category",
            category, 2, pcr,
            "pcr_category", 0.90
        )

    # Verifier is commonly written as a name followed by "(Independent verifier)".
    for page_data in raw_document["pages"][:3]:
        lines = _clean_lines(page_data["text"])

        for index, line in enumerate(lines):
            if "independent verifier" in normalize_text(line):
                if line.startswith("(") and index > 0:
                    verifier = lines[index - 1].rstrip(",")
                    _add_metadata(
                        metadata, provenance, "verifier",
                        verifier, page_data["page_number"],
                        f"{verifier} | {line}",
                        "field_specific_context", 0.98
                    )
                    break

        if "verifier" in metadata:
            break

    # Verification type
    verification_text = normalize_text(full_text)
    if "independent verification" in verification_text:
        if re.search(r"\bx\s+externally\b", verification_text):
            verification_type = "External third-party verification"
        elif re.search(r"\binternally\s+x\b", verification_text):
            verification_type = "Internal verification"
        else:
            verification_type = None

        if verification_type:
            _add_metadata(
                metadata, provenance, "verification_type",
                verification_type, 2, verification_type,
                "verification_checkbox_text", 0.90
            )

    # EPD type / system boundary
    boundary_match = re.search(
        r'type\s+of\s+the\s+EPD\s+is\s+["“]?([^"\n”]+)["”]?',
        full_text,
        flags=re.IGNORECASE,
    )
    if boundary_match:
        boundary = re.sub(r"\s+", " ", boundary_match.group(1)).strip(" .")
        _add_metadata(
            metadata, provenance, "epd_type",
            boundary, 4, boundary_match.group(0)[:250],
            "field_specific_regex", 0.97
        )
        _add_metadata(
            metadata, provenance, "system_boundary",
            boundary, 4, boundary_match.group(0)[:250],
            "field_specific_regex", 0.97
        )

    # Declared unit from structured table first.
    value, page, source = _extract_declared_unit_from_tables(raw_document)

    if value is None:
        unit_match = re.search(
            r"declared\s+unit\s+is\s+"
            r"(\d+(?:[.,]\d+)?)\s*(kg|t|m2|m²|m3|m³|m|pcs?|pieces?)\b",
            full_text,
            flags=re.IGNORECASE,
        )
        if unit_match:
            value = f"{unit_match.group(1)} {unit_match.group(2)}"
            page = None
            source = unit_match.group(0)

    _add_metadata(
        metadata, provenance, "declared_unit_raw",
        value, page, source, "declared_unit_table_or_regex", 0.99
    )

    # Reference service life
    no_rsl = re.search(
        r"No\s+reference\s+service\s+life\s*\(RSL\)\s+is\s+defined[^.]*\.",
        full_text,
        flags=re.IGNORECASE,
    )

    if no_rsl:
        metadata["reference_service_life_years"] = None
        metadata["reference_service_life_status"] = "not_defined"
        provenance["reference_service_life_years"] = {
            "source_page": 4,
            "source_text": re.sub(r"\s+", " ", no_rsl.group(0)).strip(),
            "extraction_method": "explicit_no_rsl_statement",
            "confidence": 0.99,
        }
    else:
        rsl_match = re.search(
            r"(?:reference\s+service\s+life|\bRSL\b)"
            r".{0,80}?(\d+(?:[.,]\d+)?)\s*(?:years?|yrs?|a)\b",
            full_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if rsl_match:
            metadata["reference_service_life_years"] = float(
                rsl_match.group(1).replace(",", ".")
            )
            provenance["reference_service_life_years"] = {
                "source_page": None,
                "source_text": re.sub(r"\s+", " ", rsl_match.group(0))[:250],
                "extraction_method": "field_specific_regex",
                "confidence": 0.92,
            }

    # Geography
    geo_match = re.search(
        r"Geographic\s+Representativeness.*?"
        r"(?:lifespan|life\s*span)\s*:\s*([A-Za-z][A-Za-z ,;/\-]+)",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if geo_match:
        geography = geo_match.group(1).split("\n")[0].strip(" .")
        _add_metadata(
            metadata, provenance, "geography",
            geography, 4, geo_match.group(0)[-250:],
            "field_specific_regex", 0.92
        )

    # Production sites from scope.
    site_match = re.search(
        r"production\s+take\s+place\s+in\s+([^.\n]+)",
        full_text,
        flags=re.IGNORECASE,
    )
    if site_match:
        sites = re.sub(r"\s+", " ", site_match.group(1)).strip(" .")
        _add_metadata(
            metadata, provenance, "production_site",
            sites, 2, site_match.group(0),
            "field_specific_regex", 0.92
        )

    # Product description
    description = _extract_section_text(
        raw_document["pages"][2]["text"] if len(raw_document["pages"]) >= 3 else "",
        "Product description/Product definition",
        ["Application", "Technical Data"],
    )
    if description:
        _add_metadata(
            metadata, provenance, "product_description",
            description, 3, description[:500],
            "section_text", 0.88
        )

    # Composition / technical data
    composition, composition_page = _extract_product_composition(raw_document)
    if composition:
        _add_metadata(
            metadata, provenance, "product_composition",
            composition, composition_page,
            f"{len(composition)} composition rows",
            "structured_table", 0.98
        )

    technical, technical_page = _extract_technical_specifications(raw_document)
    if technical:
        _add_metadata(
            metadata, provenance, "technical_specifications",
            technical, technical_page,
            f"{len(technical)} technical-property rows",
            "structured_table", 0.98
        )

    packaging, packaging_page = _extract_packaging_materials(raw_document)
    if packaging:
        _add_metadata(
            metadata, provenance, "packaging_materials",
            packaging, packaging_page,
            f"{len(packaging)} packaging scenario rows",
            "structured_table", 0.95
        )

    # Manufacturing process
    if len(raw_document["pages"]) >= 4:
        page4_text = raw_document["pages"][3]["text"].replace("\xa0", " ")
        process_match = re.search(
            r"The\s+polyurethane\s+components\s+are\s+produced\s+internally.*?"
            r"where\s+they\s+are\s+assembled\s+into\s+the\s+final\s+window\s+product\.",
            page4_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if process_match:
            process = re.sub(r"\s+", " ", process_match.group(0)).strip()
            _add_metadata(
                metadata, provenance, "manufacturing_process",
                process, 4, process[:500],
                "section_regex", 0.90
            )

    # Data quality
    quality_match = re.search(
        r"Data\s+quality\s+and\s+a\s+sensitivity\s+analysis.*?"
        r"limited\s+influence\s+on\s+the\s+results\.",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if quality_match:
        quality = re.sub(r"\s+", " ", quality_match.group(0)).strip()
        _add_metadata(
            metadata, provenance, "data_quality",
            quality, 4, quality,
            "section_regex", 0.90
        )

    # Comparability statement
    if len(raw_document["pages"]) >= 4:
        comparability = _extract_section_text(
            raw_document["pages"][3]["text"],
            "Comparability",
            ["LCA: Scenarios and additional technical information"],
        )
        if comparability:
            _add_metadata(
                metadata, provenance, "comparability_statement",
                comparability, 4, comparability[:500],
                "section_text", 0.90
            )

    # LCA software and background database
    software_match = re.search(
        r"The\s+LCA\s+modelling\s+software\s+is\s+([^.\n]+)",
        full_text,
        flags=re.IGNORECASE,
    )
    if software_match:
        software = re.sub(r"\s+", " ", software_match.group(1)).strip()
        _add_metadata(
            metadata, provenance, "lca_software",
            software, 8, software_match.group(0),
            "field_specific_regex", 0.94
        )

    db_match = re.search(
        r"Managed\s+LCA\s+Content\s*\(v?([0-9.]+)\)\s+and\s+"
        r"Ecoinvent\s*\(v?([0-9.]+)\)",
        full_text,
        flags=re.IGNORECASE,
    )
    if db_match:
        databases = (
            f"Managed LCA Content v{db_match.group(1)}; "
            f"Ecoinvent v{db_match.group(2)}"
        )
        _add_metadata(
            metadata, provenance, "background_database",
            databases, 4, db_match.group(0),
            "field_specific_regex", 0.97
        )

    # Programme instructions (GPI)
    gpi_match = re.search(
        r"General\s+Instructions\s+for\s+the\s+EPD\s+programme.*?"
        r"Version\s+([0-9.]+)",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if gpi_match:
        gpi = f"IBU General Instructions for the EPD programme, Version {gpi_match.group(1)}"
        _add_metadata(
            metadata, provenance, "gpi",
            gpi, 7, re.sub(r"\s+", " ", gpi_match.group(0))[:300],
            "reference_section_regex", 0.92
        )

    # Language: only infer if the declaration ID has an explicit language suffix.
    registration = metadata.get("registration_number", "")
    suffix_match = re.search(r"-([A-Z]{2})$", registration)

    if suffix_match:
        language_map = {
            "EN": "English",
            "DE": "German",
            "DA": "Danish",
            "FR": "French",
        }
        code = suffix_match.group(1)
        language = language_map.get(code)

        if language:
            _add_metadata(
                metadata, provenance, "language",
                language, 1, f"EPD ID suffix: -{code}",
                "registration_suffix_inference", 0.75
            )

    return metadata, provenance


def _find_parameter_header(rows):
    """
    Find the actual 'Parameter / Unit / module...' result header anywhere
    in a detected table. This fixes tables where a system-boundary matrix
    appears above the environmental result table in the same PyMuPDF table.
    """
    for row_index, row in enumerate(rows):
        clean = [_clean_cell(cell) for cell in row]
        normalized = [normalize_text(cell) for cell in clean]

        parameter_cols = [
            index
            for index, cell in enumerate(normalized)
            if cell in {"parameter", "indicator"}
        ]

        if not parameter_cols:
            continue

        module_columns = {}

        for column_index, cell in enumerate(clean):
            module = normalize_module(cell)

            if module:
                module_columns[column_index] = module

        if len(module_columns) < 2:
            continue

        unit_col = None

        for column_index, cell in enumerate(normalized):
            if cell == "unit":
                unit_col = column_index
                break

        return {
            "row_index": row_index,
            "parameter_col": parameter_cols[0],
            "unit_col": unit_col,
            "module_columns": module_columns,
            "header_row": clean,
        }

    return None


def _normalize_pdf_unit(raw_unit):
    """
    Fix common PDF text-layer artifacts without changing the preserved raw unit.
    Example from the VELUX EPD:
      'kg CO eq\\n2' -> 'kg CO2 eq'
    """
    if raw_unit is None:
        return None

    unit = _clean_cell(raw_unit)

    unit = re.sub(
        r"\bCO\s+eq\s+2\b",
        "CO2 eq",
        unit,
        flags=re.IGNORECASE,
    )

    unit = re.sub(
        r"\bCO\s*2\s*eq\b",
        "CO2 eq",
        unit,
        flags=re.IGNORECASE,
    )

    return unit


def extract_module_declarations(raw_document):
    """
    Extract X / MND / MNR declarations from the system-boundary matrix.
    """
    declarations = {}

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = table.get("cells", [])

            for row_index in range(len(rows) - 1):
                module_row = [_clean_cell(cell) for cell in rows[row_index]]
                status_row = [_clean_cell(cell) for cell in rows[row_index + 1]]

                module_columns = {}

                for column_index, cell in enumerate(module_row):
                    module = normalize_module(cell)

                    if module and module in {
                        "A1", "A2", "A3", "A4", "A5",
                        "B1", "B2", "B3", "B4", "B5", "B6", "B7",
                        "C1", "C2", "C3", "C4", "D",
                    }:
                        module_columns[column_index] = module

                if len(module_columns) < 5:
                    continue

                valid_status_count = 0

                for column_index, module in module_columns.items():
                    if column_index >= len(status_row):
                        continue

                    status = normalize_text(status_row[column_index]).upper()

                    if status in {"X", "MND", "MNR"}:
                        declarations[module] = {
                            "status": status,
                            "source_page": page["page_number"],
                            "source_table": table["table_number"],
                        }
                        valid_status_count += 1

                if valid_status_count >= 5:
                    return declarations

    return declarations


def extract_result_tables(raw_document, standard_profile):
    """
    Extract module-wise environmental results from PDF tables.

    Key correction:
    the module header is located anywhere inside each detected table,
    not just in the first four rows. Some EPDs place the system-boundary
    matrix and the environmental impact table inside one large PyMuPDF table.
    """
    results = {}

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = table.get("cells", [])

            if not rows:
                continue

            header = _find_parameter_header(rows)

            if not header:
                continue

            start_row = header["row_index"] + 1
            parameter_col = header["parameter_col"]
            unit_col = header["unit_col"]
            module_columns = header["module_columns"]
            header_row = header["header_row"]

            for row in rows[start_row:]:
                clean_row = [_clean_cell(cell) for cell in row]

                if not any(clean_row):
                    continue

                if parameter_col >= len(clean_row):
                    continue

                raw_label = clean_row[parameter_col]

                if not raw_label:
                    continue

                # Stop treating subsequent repeated header rows as data.
                if normalize_text(raw_label) in {"parameter", "indicator"}:
                    continue

                raw_unit = None

                if unit_col is not None and unit_col < len(clean_row):
                    raw_unit = _normalize_pdf_unit(clean_row[unit_col])

                indicator_match = match_indicator(
                    raw_label,
                    standard_profile,
                    raw_unit,
                )

                if not indicator_match:
                    continue

                code = indicator_match["code"]

                result = results.setdefault(
                    code,
                    {
                        "method_profile": standard_profile,
                        "canonical_unit": indicator_match.get("canonical_unit"),
                        "modules": {},
                    },
                )

                for column_index, module in module_columns.items():
                    if column_index >= len(clean_row):
                        continue

                    raw_value = clean_row[column_index]

                    if raw_value == "":
                        continue

                    status = missing_value_status(raw_value)
                    numeric = parse_number(raw_value)

                    if status is None and numeric is None:
                        continue

                    result["modules"][module] = {
                        "value": numeric,
                        "status": status or "declared",
                        "provenance": "reported",
                        "raw_value": raw_value,
                        "raw_unit": raw_unit,
                        "raw_indicator_label": raw_label,
                        "raw_module_header": (
                            header_row[column_index]
                            if column_index < len(header_row)
                            else module
                        ),
                        "source_page": page["page_number"],
                        "source_table": table["table_number"],
                        "confidence": indicator_match.get("confidence", 0.90),
                        "extraction_method": "pdf_table",
                    }

    return results


def determine_a1_a3_mode(results):
    indicator = results.get("GWP-total") or results.get("GWP")

    if not indicator:
        return "none"

    modules = indicator.get("modules", {})
    split = [module in modules for module in ("A1", "A2", "A3")]
    aggregate = "A1-A3" in modules

    if aggregate and all(split):
        return "split_plus_reported_total"

    if aggregate and not any(split):
        return "aggregate_only"

    if not aggregate and all(split):
        return "split_only"

    if aggregate or any(split):
        return "partial_split"

    return "none"


def extract_epd(pdf_bytes, filename):
    from extraction.pdf_reader import read_pdf

    raw_document = read_pdf(pdf_bytes)

    metadata, metadata_provenance = extract_metadata(raw_document)
    profile = metadata.get("standard_profile", "unresolved")

    results = extract_result_tables(raw_document, profile)
    module_declarations = extract_module_declarations(raw_document)

    return {
        "schema_version": "1.1",
        "document": {
            "source_file": filename,
            "source_database_or_programme": metadata.get("programme_operator"),
            "language": metadata.get("language"),
            "standard_profile": profile,
            "extraction_method": "pdf_text+pdf_table",
            "page_count": raw_document["page_count"],
            "pages_without_text": raw_document["pages_without_text"],
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        },
        "metadata": metadata,
        "metadata_provenance": metadata_provenance,
        "physical_properties": {},
        "results": results,
        "module_declarations": module_declarations,
        "a1_a3_reporting_mode": determine_a1_a3_mode(results),
        "scenarios": {
            "A4": {},
            "A5": {},
            "B": {},
            "C1": {},
            "C2": {},
            "C3": {},
            "C4": {},
            "D": {},
            "EOL_common": {},
        },
        "qa": {
            "warnings": [],
            "errors": [],
            "manual_review_required": True,
        },
        "review": {
            "status": "extracted_not_approved",
            "human_verified": False,
        },
    }
