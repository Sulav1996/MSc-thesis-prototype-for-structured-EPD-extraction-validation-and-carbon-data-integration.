"""
Environmental-result tables and the system-boundary (module declaration) matrix.

Two independent strategies are used so that extraction does not depend on one
PDF library's table detector:

1. ``extract_results_from_tables`` – works on tables detected by PyMuPDF /
   pdfplumber. The module header row is searched anywhere in a table and may
   repeat (several result blocks merged into one detected table).
2. ``extract_results_from_words`` – rebuilds rows from positioned words for
   borderless tables. Values are assigned to the nearest module column.

Both return the same structure::

    {indicator_code: {"method_profile", "canonical_unit",
                      "modules": {module: value_record},
                      "scenario_modules": {module: {scenario: value_record}}}}
"""

from __future__ import annotations

import re
from dictionary import (
    STANDARD_MODULES,
    match_indicator,
    missing_value_status,
    normalize_text,
    parse_module_header,
    unit_key,
)
from extraction.common import clean_cell, group_lines, parse_number

LABEL_HEADERS = {
    "parameter", "parameters", "indicator", "indicators", "impact category", "impact categories",
    "environmental indicator", "environmental indicators", "core environmental impact indicators",
    "core indicators", "indicator unit", "abbreviation", "abbr.", "abbr", "category", "results",
    "impact indicator", "resource use", "waste categories", "output flows", "indikator", "parameter unit",
}
UNIT_HEADERS = {"unit", "units", "unit.", "enhed", "einheit", "[unit]", "unité"}

UNIT_TAIL_RE = re.compile(
    r"(?:\[|\()?\s*("
    r"kg\s*co\s*2?\s*(?:-?\s*eq\.?v?\.?|e)(?:\s*2)?|kg\s*cfc\s*-?\s*11\s*(?:-?eq\.?v?\.?|e)?|kg\s*r11\s*-?eq\.?v?\.?"
    r"|mol\s*h\+?\s*-?eq\.?v?\.?|kg\s*p\s*-?(?:eq\.?v?\.?|e)|kg\s*po4\S*\s*-?eq\.?v?\.?|kg\s*n\s*-?(?:eq\.?v?\.?|e)"
    r"|mol\s*n\s*-?(?:eq\.?v?\.?|e)|kg\s*nmvoc\s*-?(?:eq\.?v?\.?|e)?|kg\s*so\s*2?\s*-?(?:eq\.?v?\.?|e)"
    r"|kg\s*(?:c2h4|ethene)\s*-?(?:eq\.?v?\.?|e)|kg\s*sb\s*-?(?:eq\.?v?\.?|e)"
    r"|m3\s*world\s*eq\.?v?\.?(?:\s*deprived)?|m³\s*world\s*eq\.?(?:\s*deprived)?|m3\s*depr\.?"
    r"|kbq\s*u-?235\s*-?(?:eq\.?v?\.?|e)|disease\s*incidence|ctue|ctuh|mj(?:,?\s*net calorific value)?"
    r"|m3|m³|kg|dimensionless|-"
    r")\s*(?:\]|\))?\s*$",
    re.IGNORECASE,
)

STATUS_TOKENS = {"mnd", "nd", "nr", "mnr", "n/a", "na", "n.a.", "inb", "-", "–", "x"}


def normalize_pdf_unit(raw_unit):
    """
    Fix PDF text-layer artefacts without changing the preserved raw unit.
    'kg CO eq 2' / 'kg CO eq\\n2' -> 'kg CO2 eq'.
    """
    if raw_unit is None:
        return None
    unit = clean_cell(raw_unit)
    unit = re.sub(r"\bCO\s+eq\.?\s+2\b", "CO2 eq", unit, flags=re.IGNORECASE)
    unit = re.sub(r"\bCO\s*2\s*eq\b", "CO2 eq", unit, flags=re.IGNORECASE)
    unit = re.sub(r"\bSO\s+eq\.?\s+2\b", "SO2 eq", unit, flags=re.IGNORECASE)
    unit = re.sub(r"\bm\s*3\b", "m3", unit)
    return unit or None


def split_label_unit(label: str) -> tuple[str, str | None]:
    """'GWP-total [kg CO2 eq]' -> ('GWP-total', 'kg CO2 eq')."""
    text = clean_cell(label)
    match = UNIT_TAIL_RE.search(text)
    if match and match.start() > 1:
        head = text[:match.start()].strip(" -–:[(")
        if head:
            return head, match.group(1).strip()
    return text, None


def _value_record(raw_value, raw_unit, raw_label, header, page, table, confidence, method, scenario=None):
    status = missing_value_status(raw_value)
    numeric = parse_number(raw_value)
    if status is None and numeric is None:
        return None
    record = {
        "value": numeric,
        "status": status or "declared",
        "provenance": "reported",
        "raw_value": raw_value,
        "raw_unit": raw_unit,
        "raw_indicator_label": raw_label,
        "raw_module_header": header,
        "source_page": page,
        "source_table": table,
        "confidence": confidence,
        "extraction_method": method,
    }
    if scenario:
        record["scenario"] = scenario
    return record


def _resolve_indicator(raw_label, raw_unit, profile):
    match = match_indicator(raw_label, profile, raw_unit)
    if not match:
        return None
    # Context rule from the v3 dictionary: "water use" in plain m3 is net fresh water (FW),
    # water deprivation (WDP) is reported in m3 world eq. deprived.
    if match["code"] == "WDP" and unit_key(raw_unit) == "m3":
        return {"code": "FW", "profile": "shared", "canonical_unit": "m3", "confidence": 0.85,
                "context_rule": "WDP label with plain m3 unit reinterpreted as FW"}
    return match


def _store(results, indicator, profile, module, scenario, record):
    entry = results.setdefault(indicator["code"], {
        "method_profile": profile,
        "canonical_unit": indicator.get("canonical_unit"),
        "modules": {},
    })
    if scenario:
        entry.setdefault("scenario_modules", {}).setdefault(module, {})
        entry["scenario_modules"][module].setdefault(scenario, record)
        # The first scenario also fills the plain module slot, clearly marked.
        if module not in entry["modules"]:
            entry["modules"][module] = {**record, "note": f"first declared scenario ({scenario})"}
        return
    existing = entry["modules"].get(module)
    # Keep the first high-quality value; a later duplicate only fills gaps.
    if existing is None or (existing.get("value") is None and record.get("value") is not None):
        entry["modules"][module] = record


# ---------------------------------------------------------------------------
# strategy 1: detected tables
# ---------------------------------------------------------------------------

def _header_info(clean_row):
    modules = {}
    for column, cell in enumerate(clean_row):
        module, scenario = parse_module_header(cell)
        if module:
            modules[column] = (module, scenario, cell)
    if len(modules) < 2:
        return None
    normalized = [normalize_text(cell) for cell in clean_row]
    unit_col = next((i for i, cell in enumerate(normalized) if cell in UNIT_HEADERS), None)
    label_col = next((i for i, cell in enumerate(normalized) if cell in LABEL_HEADERS), None)
    return {"modules": modules, "unit_col": unit_col, "label_col": label_col,
            "first_module_col": min(modules)}


def _row_label_and_unit(clean_row, header):
    first = header["first_module_col"]
    unit_col = header["unit_col"]
    raw_unit = None
    if unit_col is not None and unit_col < len(clean_row) and unit_col < first:
        raw_unit = normalize_pdf_unit(clean_row[unit_col])

    if header["label_col"] is not None and header["label_col"] < len(clean_row) and clean_row[header["label_col"]]:
        label = clean_row[header["label_col"]]
    else:
        parts = [cell for index, cell in enumerate(clean_row[:first]) if cell and index != unit_col]
        label = " ".join(parts)

    if not raw_unit:
        label, raw_unit = split_label_unit(label)
        raw_unit = normalize_pdf_unit(raw_unit)
    return label.strip(), raw_unit


def _row_values(clean_row, header):
    """Aligned values; if alignment fails, fall back to left-to-right order."""
    aligned = {}
    for column, (module, scenario, cell) in header["modules"].items():
        if column < len(clean_row) and clean_row[column] != "":
            aligned[column] = clean_row[column]

    tokens = [cell for cell in clean_row[header["first_module_col"]:] if cell != ""]
    value_like = [t for t in tokens if parse_number(t) is not None or normalize_text(t) in STATUS_TOKENS]
    if len(value_like) > len(aligned) and len(value_like) == len(header["modules"]):
        ordered_columns = sorted(header["modules"])
        return {column: value for column, value in zip(ordered_columns, value_like)}, "order"
    return aligned, "aligned"


def extract_results_from_tables(raw_document: dict, standard_profile: str) -> tuple[dict, set]:
    results: dict = {}
    pages_with_results: set = set()

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            header = None
            for row in table.get("cells", []):
                clean_row = [clean_cell(cell) for cell in row]
                if not any(clean_row):
                    continue
                new_header = _header_info(clean_row)
                if new_header:
                    header = new_header
                    continue
                if header is None:
                    continue

                label, raw_unit = _row_label_and_unit(clean_row, header)
                if not label or normalize_text(label) in LABEL_HEADERS:
                    continue
                indicator = _resolve_indicator(label, raw_unit, standard_profile)
                if not indicator:
                    continue

                values, mode = _row_values(clean_row, header)
                confidence = indicator.get("confidence", 0.9) * (1.0 if mode == "aligned" else 0.95)
                for column, raw_value in values.items():
                    module, scenario, header_cell = header["modules"][column]
                    record = _value_record(raw_value, raw_unit, label, header_cell, page["page_number"],
                                           table.get("table_number"), round(confidence, 3),
                                           "pdf_table" if mode == "aligned" else "pdf_table_order", scenario)
                    if record:
                        _store(results, indicator, standard_profile, module, scenario, record)
                        pages_with_results.add(page["page_number"])
    return results, pages_with_results


# ---------------------------------------------------------------------------
# strategy 2: positioned words (borderless tables)
# ---------------------------------------------------------------------------

def _group_lines(words: list[dict]) -> list[list[dict]]:
    return group_lines(words)


def _module_tokens(line: list[dict]) -> list[dict]:
    """Module headers on a line; joins split tokens such as 'A1-' 'A3' or 'A1' '-' 'A3'."""
    out, index = [], 0
    while index < len(line):
        matched = None
        for span in (3, 2, 1):
            if index + span > len(line):
                continue
            group = line[index:index + span]
            if span > 1 and any(group[k + 1]["x0"] - group[k]["x1"] > 4.0 for k in range(span - 1)):
                continue
            text = "".join(word["text"] for word in group)
            module, scenario = parse_module_header(text)
            if module:
                matched = (span, module, scenario, text, group[0]["x0"], group[-1]["x1"])
                break
        if matched:
            span, module, scenario, text, x0, x1 = matched
            out.append({"module": module, "scenario": scenario, "text": text, "x0": x0, "x1": x1,
                        "xc": (x0 + x1) / 2})
            index += span
        else:
            index += 1
    return out


def _value_tokens(tokens: list[dict]) -> list[dict]:
    """Numbers/status tokens; joins '2.88' 'E+00' and '-' '1.3E+00'."""
    out, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        text = token["text"]
        if index + 1 < len(tokens):
            nxt = tokens[index + 1]
            close = nxt["x0"] - token["x1"] < 3.5
            if close and re.fullmatch(r"[Ee][+\-−]?\d+", nxt["text"]) and parse_number(text) is not None:
                text, token = text + nxt["text"], {**token, "x1": nxt["x1"], "text": text + nxt["text"]}
                index += 1
            elif close and text in {"-", "−", "–"} and parse_number(nxt["text"]) is not None:
                text, token = "-" + nxt["text"], {**token, "x1": nxt["x1"], "text": "-" + nxt["text"]}
                index += 1
        if parse_number(text) is not None or normalize_text(text) in STATUS_TOKENS - {"x", "-", "–"}:
            out.append({**token, "text": text, "xc": (token["x0"] + token["x1"]) / 2})
        index += 1
    return out


def extract_results_from_words(raw_document: dict, standard_profile: str, skip_pages: set | None = None) -> dict:
    results: dict = {}
    skip_pages = skip_pages or set()

    for page in raw_document["pages"]:
        if page["page_number"] in skip_pages or not page.get("words"):
            continue
        lines = _group_lines(page["words"])
        header = None
        pending_label: list[str] = []
        for line in lines:
            modules = _module_tokens(line)
            if len(modules) >= 3:
                centers = [m["xc"] for m in modules]
                gaps = [b - a for a, b in zip(centers, centers[1:])] or [40.0]
                header = {"modules": modules, "first_x0": min(m["x0"] for m in modules),
                          "half_gap": max(6.0, min(gaps) / 2.0)}
                pending_label = []
                continue
            if header is None:
                continue

            boundary = header["first_x0"] - 4.0
            label_words = [w for w in line if w["x1"] <= boundary]
            value_words = [w for w in line if w["x1"] > boundary]
            values = _value_tokens(value_words)
            label_text = " ".join(w["text"] for w in label_words).strip()

            if not values:
                # A label that wraps over two lines without values.
                if label_text and len(label_text) < 80:
                    pending_label = (pending_label + [label_text])[-2:]
                else:
                    pending_label = []
                continue

            full_label = " ".join(pending_label + [label_text]).strip() if pending_label else label_text
            pending_label = []
            candidates = [label_text, full_label] if full_label != label_text else [label_text]
            indicator, raw_label, raw_unit = None, None, None
            for candidate in candidates:
                label, unit = split_label_unit(candidate)
                unit = normalize_pdf_unit(unit)
                indicator = _resolve_indicator(label, unit, standard_profile)
                if indicator:
                    raw_label, raw_unit = label, unit
                    break
            if not indicator:
                continue

            for value in values:
                nearest = min(header["modules"], key=lambda m: abs(m["xc"] - value["xc"]))
                if abs(nearest["xc"] - value["xc"]) > header["half_gap"] * 1.6:
                    continue
                record = _value_record(value["text"], raw_unit, raw_label, nearest["text"], page["page_number"],
                                       None, round(indicator.get("confidence", 0.9) * 0.9, 3),
                                       "pdf_words", nearest["scenario"])
                if record:
                    _store(results, indicator, standard_profile, nearest["module"], nearest["scenario"], record)
    return results


def merge_results(primary: dict, secondary: dict) -> dict:
    """Add indicators/modules from ``secondary`` that ``primary`` does not have."""
    merged = {code: {**data, "modules": dict(data.get("modules", {}))} for code, data in primary.items()}
    for code, data in secondary.items():
        target = merged.setdefault(code, {**data, "modules": {}})
        for module, record in data.get("modules", {}).items():
            target["modules"].setdefault(module, record)
        for module, scenarios in data.get("scenario_modules", {}).items():
            target.setdefault("scenario_modules", {}).setdefault(module, {}).update(
                {k: v for k, v in scenarios.items()
                 if k not in target.get("scenario_modules", {}).get(module, {})})
    return merged


# ---------------------------------------------------------------------------
# system-boundary matrix (X / MND / MNR / ND)
# ---------------------------------------------------------------------------

_DECLARATION_MARKS = {
    "x": "X", "✓": "X", "✔": "X", "☒": "X", "☑": "X", "■": "X", "yes": "X", "included": "X",
    "mnd": "MND", "nd": "ND", "mnr": "MNR", "nr": "NR", "mna": "MND", "inb": "ND", "-": "ND",
}


def _declaration_status(cell):
    return _DECLARATION_MARKS.get(normalize_text(cell))


def extract_module_declarations(raw_document: dict) -> dict:
    """Extract the X / MND / MNR / ND system-boundary row (tables first, then words)."""
    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = [[clean_cell(cell) for cell in row] for row in table.get("cells", [])]
            for index in range(len(rows) - 1):
                columns = {}
                for column, cell in enumerate(rows[index]):
                    module, scenario = parse_module_header(cell)
                    if module in STANDARD_MODULES and not scenario:
                        columns[column] = module
                if len(columns) < 5:
                    continue
                for offset in (1, 2):
                    if index + offset >= len(rows):
                        break
                    status_row = rows[index + offset]
                    declarations = {}
                    for column, module in columns.items():
                        if column < len(status_row):
                            status = _declaration_status(status_row[column])
                            if status:
                                declarations[module] = {"status": status, "source_page": page["page_number"],
                                                        "source_table": table.get("table_number"),
                                                        "extraction_method": "pdf_table"}
                    if len(declarations) >= 5:
                        return declarations

    for page in raw_document["pages"]:
        lines = _group_lines(page.get("words", []))
        for index, line in enumerate(lines[:-1]):
            modules = [m for m in _module_tokens(line) if m["module"] in STANDARD_MODULES and not m["scenario"]]
            if len(modules) < 8:
                continue
            for offset in (1, 2):
                if index + offset >= len(lines):
                    break
                marks = [w for w in lines[index + offset] if _declaration_status(w["text"])]
                if len(marks) < 5:
                    continue
                declarations = {}
                gap = min((b["xc"] - a["xc"] for a, b in zip(modules, modules[1:])), default=30.0)
                for mark in marks:
                    xc = (mark["x0"] + mark["x1"]) / 2
                    nearest = min(modules, key=lambda m: abs(m["xc"] - xc))
                    if abs(nearest["xc"] - xc) <= max(8.0, gap * 0.6):
                        declarations[nearest["module"]] = {"status": _declaration_status(mark["text"]),
                                                           "source_page": page["page_number"],
                                                           "source_table": None,
                                                           "extraction_method": "pdf_words"}
                if len(declarations) >= 5:
                    return declarations
    return {}


def determine_a1_a3_mode(results: dict) -> str:
    indicator = results.get("GWP-total") or results.get("GWP")
    if not indicator:
        return "none"
    modules = indicator.get("modules", {})
    split = [module in modules and modules[module].get("value") is not None for module in ("A1", "A2", "A3")]
    aggregate = "A1-A3" in modules and modules["A1-A3"].get("value") is not None
    if aggregate and all(split):
        return "split_plus_reported_total"
    if aggregate and not any(split):
        return "aggregate_only"
    if not aggregate and all(split):
        return "split_only"
    if aggregate or any(split):
        return "partial_split"
    return "none"
