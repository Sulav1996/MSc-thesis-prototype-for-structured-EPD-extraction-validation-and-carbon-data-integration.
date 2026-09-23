"""
Identity and Step 1 metadata extraction from PDF text.

The extractor only looks for the fields that Step 1 needs (standard,
verification, programme operator, RSL, conversion factor, …) and the identity
fields needed to store, fetch and categorise an EPD (EPD ID, product name,
manufacturer, dates, declared unit, PCR, description). Labels come from the
dictionary (``step3_dictionary[*].pdf_aliases``) so new wording is added to
the dictionary, not to the code.
"""

from __future__ import annotations

import re

from dictionary import (
    detect_standard_profile,
    load_dictionary,
    metadata_aliases,
    normalize_text,
    step3_field,
    unit_quantity,
)
from extraction.common import (
    clean_cell,
    clean_lines,
    column_text_below,
    detect_language,
    evidence,
    find_page,
    page_segments,
    parse_number,
    resolve_validity_dates,
    segment_below,
    sentence_around,
)

BOILERPLATE_TITLES = {
    "environmental product declaration", "environmental product declarations", "epd",
    "miljøvaredeklaration", "umwelt-produktdeklaration", "as per iso 14025 and en 15804+a2",
    "in accordance with iso 14025 and en 15804+a2", "general information", "product",
    "verified environmental product declaration", "type iii environmental product declaration",
}

REGISTRATION_PATTERNS = [
    r"\bEPD-[A-Z0-9]{2,6}-\d{8}-[A-Z0-9]{2,8}(?:-[A-Z]{2})?\b",
    r"\bMD-\d{5}(?:-[A-Z]{2})?\b",
    r"\bS-P-\d{4,6}\b",
    r"\bEPD-IES-\d{5,8}(?::\d+)?\b",
    r"\bNEPD-\d{2,6}-\d{2,6}(?:-[A-Z]{2})?\b",
    r"\bHUB-\d{3,6}\b",
    r"\bEPD-Kiwa-[A-Z]{2,4}-\d{4,8}(?:-[A-Z0-9]+)*\b",
    r"\bRTS[_-]\d{2,5}[_-]\d{2,4}\b",
    r"\bITB-EPD\s?\d{2,5}\b",
    r"\bBREG\s?EN\s?EPD\s?(?:No\.?:?\s?)?\d{6}(?:\s?(?:BRE|EN)\s?\d+)?\b",
]


def _aliases(key: str) -> list[str]:
    field = step3_field(key) or {}
    aliases = list(field.get("pdf_aliases", []))
    # Only long v3 aliases are safe as line prefixes ("Owner", "No." caused false matches).
    for alias in metadata_aliases(key):
        if len(alias) >= 8 and alias not in aliases:
            aliases.append(alias)
    return aliases


def _label_regex(label: str) -> re.Pattern:
    words = [re.escape(part) for part in label.split()]
    return re.compile(r"^\s*" + r"\s+".join(words) + r"\s*(?:\([^)]{1,20}\))?\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)


def find_labeled_value(raw_document: dict, labels: list[str], max_pages: int = 3, validator=None):
    """
    Find a value next to a label, in this order:
      1. detected table rows (label cell → next non-empty cell),
      2. column-aware text segments from positioned words (value to the right on the
         same segment, or the segment directly below in the same column),
      3. plain text lines (same line, then next line).
    Longer labels are tried first ('Owner of the declaration' before 'Owner').

    Returns (value, page_number, source_text) or (None, None, None).
    """
    labels = sorted({label for label in labels if label}, key=len, reverse=True)
    label_norms = [normalize_text(label).rstrip(":") for label in labels]
    compiled = [_label_regex(label) for label in labels]
    pages = raw_document["pages"][:max_pages] if max_pages else raw_document["pages"]

    def ok(value):
        value = (value or "").strip()
        return (bool(value) and not value.startswith(("/", "&", "|")) and not _looks_like_heading(value)
                and (validator is None or validator(value)))

    for page in pages:
        for table in page.get("tables", []):
            for row in table.get("cells", []):
                cells = [clean_cell(cell) for cell in row]
                non_empty = [cell for cell in cells if cell]
                if len(non_empty) < 2:
                    continue
                if normalize_text(non_empty[0]).rstrip(":") in label_norms:
                    value = " ".join(non_empty[1:3]).strip()
                    if ok(value):
                        return value, page["page_number"], " | ".join(non_empty)

        segments = page_segments(page) if page.get("words") else []
        for segment in segments:
            for regex in compiled:
                match = regex.match(segment["text"])
                if not match:
                    continue
                remainder = match.group(1).strip()
                if remainder and ok(remainder):
                    return remainder, page["page_number"], segment["text"]
                if not remainder:
                    below = segment_below(segments, segment)
                    if below and ok(below["text"]):
                        return below["text"], page["page_number"], f"{segment['text']} | {below['text']}"
                break

        lines = clean_lines(page["text"])
        for index, line in enumerate(lines):
            for regex in compiled:
                match = regex.match(line)
                if not match:
                    continue
                remainder = match.group(1).strip()
                if remainder and ok(remainder):
                    return remainder, page["page_number"], line
                if not remainder:
                    for step in (1, 2):
                        if index + step < len(lines) and ok(lines[index + step]):
                            return lines[index + step], page["page_number"], f"{line} | {lines[index + step]}"
                break
    return None, None, None


HEADING_WORDS = {
    "scope", "scope:", "verification", "general information", "product", "issue date", "valid to",
    "declaration number", "programme holder", "owner of the declaration", "publisher", "declared unit",
    "declared product / declared unit", "application", "technical data", "comparability",
}


def _looks_like_heading(value: str) -> bool:
    return normalize_text(value).rstrip(":") in {h.rstrip(":") for h in HEADING_WORDS}


def _valid_registration(value: str) -> bool:
    value = value.strip()
    return 3 <= len(value) <= 60 and bool(re.search(r"\d", value)) and len(value.split()) <= 4


def _valid_date(value: str) -> bool:
    from extraction.common import parse_date
    return parse_date(value) is not None


def _valid_name(value: str) -> bool:
    value = value.strip()
    if not (2 <= len(value) <= 160):
        return False
    low = normalize_text(value)
    if re.fullmatch(r"[\d\s./\-:]+", value):
        return False
    return not low.startswith(("www.", "http")) and low not in {"yes", "no", "x"}


def _add(metadata, provenance, key, value, page, source, method, confidence):
    if value in (None, "", [], {}):
        return
    metadata[key] = value
    provenance[key] = evidence(page, source, method, confidence)


# ---------------------------------------------------------------------------
# individual fields
# ---------------------------------------------------------------------------

def _registration_number(raw_document):
    value, page, source = find_labeled_value(raw_document, _aliases("registration_number"), 3, _valid_registration)
    if value:
        token = re.split(r"\s{2,}|\s\|\s", value)[0].strip()
        return token, page, source, "field_label", 0.97
    head_text = "\n".join(p["text"] for p in raw_document["pages"][:3])
    for pattern in REGISTRATION_PATTERNS:
        match = re.search(pattern, head_text)
        if match:
            return match.group(0), find_page(raw_document, match.group(0)), match.group(0), "registration_pattern", 0.9
    return None, None, None, None, None


def _cover_title_ibu(raw_document, manufacturer=None):
    """IBU-style cover: title follows the 'Valid to' date."""
    lines = clean_lines(raw_document["pages"][0]["text"]) if raw_document["pages"] else []
    start = None
    for index, line in enumerate(lines):
        norm = normalize_text(line)
        if norm in {"valid to", "valid until", "validity", "expiry date"}:
            start = index + 2
            break
        match = re.match(r"^(valid to|valid until)\s+\S+", norm)
        if match:
            start = index + 1
            break
    if start is None:
        return None, None
    title = []
    for line in lines[start:]:
        norm = normalize_text(line)
        if "http" in norm or "www." in norm or (manufacturer and norm == normalize_text(manufacturer)):
            break
        if norm in BOILERPLATE_TITLES or norm in {"publisher", "programme holder", "owner of the declaration"}:
            continue
        title.append(line)
        if len(title) >= 3:
            break
    return (" ".join(title).strip(), " | ".join(title)) if title else (None, None)


def _cover_title_by_font(raw_document, exclude=()):
    """Largest-font lines on page 1 (generic cover layouts)."""
    lines = raw_document.get("title_lines") or []
    if not lines:
        return None, None
    excluded = {normalize_text(item) for item in exclude if item}
    candidates = []
    for item in lines:
        norm = normalize_text(item["text"])
        if len(norm) < 4 or norm in BOILERPLATE_TITLES or norm in excluded:
            continue
        if any(word in norm for word in ("iso 14025", "en 15804", "declaration number", "registration")):
            continue
        if re.fullmatch(r"[\d\s./\-:]+", norm):
            continue
        candidates.append(item)
    if not candidates:
        return None, None
    top_size = max(item["size"] for item in candidates)
    title = [item["text"] for item in candidates if item["size"] >= top_size - 0.5][:3]
    return " ".join(title).strip(), " | ".join(title)


def _declared_unit(raw_document):
    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            for row in table.get("cells", []):
                cells = [clean_cell(cell) for cell in row]
                non_empty = [cell for cell in cells if cell]
                if len(non_empty) >= 2 and normalize_text(non_empty[0]) in {"declared unit", "functional unit"}:
                    value = " ".join(non_empty[1:3])
                    if re.search(r"\d", value):
                        return value, page["page_number"], " | ".join(non_empty), "declared_unit_table", 0.99
    full = raw_document["text"].replace("\xa0", " ")
    pattern = re.compile(
        r"(?:declared|functional)\s+unit\s*(?:is|:|=)?\s*(?:defined\s+as\s+)?(?:the\s+)?"
        r"(\d+(?:[.,]\d+)?)\s*(kg|kilograms?|tonnes?|t|m2|m²|m\^2|square\s+met(?:re|er)s?|m3|m³|cubic\s+met(?:re|er)s?"
        r"|running\s+met(?:re|er)s?|linear\s+met(?:re|er)s?|lm|m|pcs?\.?|pieces?|units?|items?|stk)\b",
        re.IGNORECASE,
    )
    match = pattern.search(full)
    if match:
        value = f"{match.group(1)} {match.group(2)}"
        return value, find_page(raw_document, match.group(0)[:40]), match.group(0), "declared_unit_regex", 0.95
    value, page, source = find_labeled_value(raw_document, _aliases("declared_unit_raw"), 6,
                                             lambda v: bool(re.search(r"\d", v)) and len(v) < 160)
    if value:
        return value, page, source, "field_label", 0.85
    return None, None, None, None, None


def parse_declared_unit(raw: str | None) -> dict:
    """'1 m2' -> {'quantity': 1.0, 'unit': 'm2', 'dimension': 'area'}."""
    if not raw:
        return {}
    text = normalize_text(raw).replace("m^2", "m2").replace("m^3", "m3")
    text = re.sub(r"square\s+met(?:re|er)s?", "m2", text)
    text = re.sub(r"cubic\s+met(?:re|er)s?", "m3", text)
    text = re.sub(r"(?:running|linear)\s+met(?:re|er)s?", "m", text)
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|kilograms?|tonnes?|t|m2|m3|lm|m|pcs?\.?|pieces?|units?|items?|stk)\b", text)
    if not match:
        return {}
    quantity = parse_number(match.group(1))
    unit = match.group(2).rstrip(".")
    unit = {"kilogram": "kg", "kilograms": "kg", "tonne": "t", "tonnes": "t", "lm": "m", "pc": "piece",
            "pcs": "piece", "pieces": "piece", "unit": "piece", "units": "piece", "item": "piece",
            "items": "piece", "stk": "piece"}.get(unit, unit)
    quantity_info = unit_quantity("Item(s)" if unit == "piece" else unit)
    dimension = quantity_info[0] if quantity_info else None
    return {"quantity": quantity, "unit": unit, "dimension": dimension,
            "factor_to_reference": quantity_info[1] if quantity_info else None}


def _rsl(raw_document, provenance):
    full = raw_document["text"].replace("\xa0", " ")
    no_rsl = re.search(
        r"(?:no\s+reference\s+service\s+life\s*(?:\(RSL\))?\s+is\s+(?:defined|declared|given|stated)"
        r"|reference\s+service\s+life\s*(?:\(RSL\))?\s*(?:is\s+)?(?:not\s+(?:defined|declared|relevant|applicable)"
        r"|[:\-]\s*(?:n/?a|nd|not\s+declared|not\s+relevant)))[^.\n]*\.?",
        full, re.IGNORECASE)
    if no_rsl:
        text = re.sub(r"\s+", " ", no_rsl.group(0)).strip()
        return None, "not_defined", evidence(find_page(raw_document, text[:40]), text,
                                             "explicit_no_rsl_statement", 0.97)

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            for row in table.get("cells", []):
                cells = [clean_cell(cell) for cell in row if clean_cell(cell)]
                if len(cells) >= 2 and re.match(r"^(reference service life|rsl|service life)\b",
                                                normalize_text(cells[0])):
                    number = parse_number(re.sub(r"[^\d.,]", "", cells[1]) or None)
                    unit = normalize_text(" ".join(cells[1:3]))
                    if number and 1 <= number <= 200 and re.search(r"\b(a|years?|yrs?|år|jahre)\b", unit):
                        return number, "declared", evidence(page["page_number"], " | ".join(cells),
                                                            "rsl_table", 0.95)

    match = re.search(
        r"(?:reference\s+service\s+life|\bRSL\b|referencelevetid|referenz-nutzungsdauer)"
        r"[^.\n]{0,80}?(\d{1,3}(?:[.,]\d+)?)\s*(?:years?|yrs?|a|år|jahre)\b",
        full, re.IGNORECASE | re.DOTALL)
    if match:
        years = float(match.group(1).replace(",", "."))
        if 1 <= years <= 200:
            text = re.sub(r"\s+", " ", match.group(0))[:250]
            return years, "declared", evidence(find_page(raw_document, text[:30]), text, "rsl_regex", 0.9)
    return None, "not_found", None


def _pcr(raw_document):
    labels = ["This declaration is based on the product category rules", "Product category rules (PCR)",
              "Product Category Rules", "Core PCR", "Sub-PCR", "Complementary PCR", "PCR", "Produktkategoriregler"]
    value, page, source = find_labeled_value(
        raw_document, labels, 4,
        lambda v: 4 <= len(v) <= 200 and not normalize_text(v).startswith(("the ", "and ", "(")))
    if value:
        return re.split(r"\s{3,}", value)[0].strip(" .:"), page, source
    full = raw_document["text"].replace("\xa0", " ")
    for pattern in (r"product category rules\s*\(PCR\)\s*[:\-]?\s*([^\n]{4,200})",
                    r"\bPCR\s*[:\-]\s*([^\n]{4,200})"):
        match = re.search(pattern, full, re.IGNORECASE)
        if match:
            value = re.split(r"\s{3,}", re.sub(r"[ \t]+", " ", match.group(1)))[0].strip(" .:")
            if len(value) >= 4 and not _looks_like_heading(value):
                return value, find_page(raw_document, match.group(0)[:40]), match.group(0)[:250]
    return None, None, None


def _verifier(raw_document):
    role = re.compile(r"^\(?\s*(independent|external|third[- ]party)\s+verifier\s*\)?$", re.IGNORECASE)
    for page in raw_document["pages"][:4]:
        segments = page_segments(page) if page.get("words") else []
        for segment in segments:
            if role.match(segment["text"].strip()):
                above = [s for s in segments if s["y1"] <= segment["y0"] + 1 and
                         (min(s["x1"], segment["x1"]) - max(s["x0"], segment["x0"]) > 0 or
                          abs(s["x0"] - segment["x0"]) < 20)]
                if above:
                    name = max(above, key=lambda s: s["y1"])["text"].rstrip(",").strip()
                    if _valid_name(name) and not name.startswith("("):
                        return name, page["page_number"], f"{name} | {segment['text']}", "verifier_context", 0.95
        lines = clean_lines(page["text"])
        for index, line in enumerate(lines):
            if role.match(line.strip()) and index > 0:
                name = lines[index - 1].rstrip(",").strip()
                if _valid_name(name) and not name.startswith("("):
                    return name, page["page_number"], f"{name} | {line}", "verifier_context", 0.95
            match = re.search(r"^([^()]{3,80}?),?\s*\((?:independent|external|third[- ]party) verifier\)", line,
                              re.IGNORECASE)
            if match:
                return match.group(1).strip(), page["page_number"], line, "verifier_context", 0.9
    value, page, source = find_labeled_value(raw_document, _aliases("verifier"), 6,
                                             lambda v: _valid_name(v) and not v.startswith("("))
    if value:
        return value, page, source, "field_label", 0.85
    return None, None, None, None, None


_CHECKED = {"☒", "☑", "■", "✓", "✔", "⊠", "[x]", "(x)", "x", "X", "⌧", "▣"}
_UNCHECKED = {"☐", "□", "[ ]", "( )", "❑", "▢"}


def detect_verification(raw_document) -> dict:
    """
    Decide internal vs external (third-party) verification according to ISO 14025.
    Uses checkbox positions when words are available, then wording.
    """
    full = raw_document["text"]
    norm_full = normalize_text(full)
    iso14025 = "14025" in norm_full
    result = {"value": None, "iso14025_mentioned": iso14025, "evidence": None, "confidence": 0.0}

    # 1. checkbox glyphs next to the options (positions)
    for page in raw_document["pages"][:6]:
        words = page.get("words") or []
        if not words:
            continue
        for index, word in enumerate(words):
            token = normalize_text(word["text"]).strip(".,:;")
            if token not in {"externally", "external", "internally", "internal"}:
                continue
            same_line = [w for w in words if abs((w["y0"] + w["y1"]) / 2 - (word["y0"] + word["y1"]) / 2) < 4]
            marks = [w for w in same_line if w["text"].strip() in _CHECKED | _UNCHECKED]
            if not marks:
                continue
            nearest = min(marks, key=lambda m: min(abs(m["x1"] - word["x0"]), abs(m["x0"] - word["x1"])))
            distance = min(abs(nearest["x1"] - word["x0"]), abs(nearest["x0"] - word["x1"]))
            if distance > 60:
                continue
            # If the same mark is closer to the other option, it belongs to that one.
            others = [w for w in same_line if normalize_text(w["text"]).strip(".,:;") in
                      {"externally", "external", "internally", "internal"} and w is not word]
            if others:
                other = others[0]
                other_distance = min(abs(nearest["x1"] - other["x0"]), abs(nearest["x0"] - other["x1"]))
                if other_distance < distance:
                    continue
            checked = nearest["text"].strip() in _CHECKED
            if checked:
                external = token.startswith("extern")
                result.update({
                    "value": "External third-party verification" if external else "Internal verification",
                    "evidence": evidence(page["page_number"],
                                         " ".join(w["text"] for w in sorted(same_line, key=lambda w: w["x0"])),
                                         "checkbox_position", 0.93),
                    "confidence": 0.93,
                })
                return result

    # 2. IBU template text: "internally  X  externally"
    match = re.search(r"internally\s+(x|☒|☑)\s+externally", full, re.IGNORECASE)
    if match:
        result.update({"value": "External third-party verification",
                       "evidence": evidence(find_page(raw_document, "internally"), match.group(0),
                                            "ibu_checkbox_text", 0.85), "confidence": 0.85})
        return result
    match = re.search(r"(☒|☑|\[x\]|■)\s*(external|third[- ]party|epd verification)", full, re.IGNORECASE)
    if match:
        result.update({"value": "External third-party verification",
                       "evidence": evidence(find_page(raw_document, match.group(0)), match.group(0),
                                            "checkbox_text", 0.9), "confidence": 0.9})
        return result
    match = re.search(r"(☒|☑|\[x\]|■)\s*(internal)", full, re.IGNORECASE)
    if match:
        result.update({"value": "Internal verification",
                       "evidence": evidence(find_page(raw_document, match.group(0)), match.group(0),
                                            "checkbox_text", 0.9), "confidence": 0.9})
        return result

    # 3. wording
    wording = re.search(
        r"(independent\s+(?:external\s+)?verifier|third[- ]party\s+verifi\w+|externally\s+verified|"
        r"external\s+verification|verified\s+by\s+an?\s+independent\s+third\s+party|independent\s+third[- ]party)",
        full, re.IGNORECASE)
    if wording:
        start, end = wording.span()
        result.update({"value": "External third-party verification",
                       "evidence": evidence(find_page(raw_document, wording.group(0)),
                                            sentence_around(full, start, end), "verification_wording", 0.75),
                       "confidence": 0.75})
    return result


def _geography(raw_document):
    full = raw_document["text"].replace("\xa0", " ")
    match = re.search(r"Geographic(?:al)?\s+Representativeness.*?(?:lifespan|life\s*span)\s*:\s*([A-Za-z][A-Za-z ,;/\-]+)",
                      full, re.IGNORECASE | re.DOTALL)
    if match:
        value = match.group(1).split("\n")[0].strip(" .")
        return value, find_page(raw_document, "representativeness"), match.group(0)[-250:], "geography_regex", 0.9
    value, page, source = find_labeled_value(raw_document, _aliases("geography"), 8,
                                             lambda v: 2 <= len(v) <= 120 and not re.search(r"\d{3,}", v))
    if value:
        return value, page, source, "field_label", 0.8
    return None, None, None, None, None


def _product_description(raw_document):
    headings = {"product description", "product definition", "product description/product definition",
                "description of the product", "product description and application", "produktbeskrivelse"}
    stop = {"application", "technical data", "base materials", "base materials/ancillary materials",
            "manufacture", "production", "lca: calculation rules", "declared unit", "packaging",
            "placing on the market/application rules", "delivery status", "environment and health during use"}

    def is_heading(text):
        norm = normalize_text(text).rstrip(":")
        return norm in headings or norm.startswith("product description")

    def is_stop(text):
        return normalize_text(text).rstrip(":") in stop

    for page in raw_document["pages"][:6]:
        segments = page_segments(page) if page.get("words") else []
        for segment in segments:
            if is_heading(segment["text"]):
                text = column_text_below(segments, segment, stop=is_stop)
                if len(text) > 30:
                    return text, page["page_number"], text[:400]
        lines = clean_lines(page["text"])
        for index, line in enumerate(lines):
            if is_heading(line):
                collected = []
                for nxt in lines[index + 1:]:
                    if is_stop(nxt):
                        break
                    collected.append(nxt)
                    if sum(len(item) for item in collected) > 1500:
                        break
                text = " ".join(collected).strip()
                if len(text) > 30:
                    return text, page["page_number"], text[:400]
    return None, None, None


# ---------------------------------------------------------------------------
# physical properties → conversion factor (ILCD MaterialProperties definition)
# ---------------------------------------------------------------------------

_PROPERTY_LABELS = [
    ("conversion_factor_to_1kg", r"conversion\s+factor\s+(?:to\s+)?1\s*kg|umrechnungsfaktor\s+zu\s+1\s*kg"),
    ("mass_per_declared_unit_kg", r"(?:mass|weight)\s+(?:per|of\s+(?:the\s+)?)\s*(?:declared|functional)\s+unit"
                                  r"|conversion\s+factor\s+to\s+mass|mass\s+reference|weight\s+per\s+unit"),
    ("weight_per_piece_kg", r"weight\s+per\s+piece|mass\s+per\s+piece"),
    ("grammage_kg_m2", r"grammage|area\s+weight|weight\s+per\s+m2|flächengewicht|mass\s+per\s+m2"),
    ("gross_density_kg_m3", r"gross\s+density|rohdichte"),
    ("bulk_density_kg_m3", r"bulk\s+density|schüttdichte"),
    ("density_kg_m3", r"(?<!gross\s)(?<!bulk\s)\bdensity\b|densitet|dichte"),
    ("linear_density_kg_m", r"linear\s+density|weight\s+per\s+(?:running\s+)?met(?:re|er)|mass\s+per\s+met(?:re|er)"),
]


def extract_physical_properties(raw_document, declared: dict) -> tuple[dict, dict]:
    properties, provenance = {}, {}
    candidates = []

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            for row in table.get("cells", []):
                cells = [clean_cell(cell) for cell in row if clean_cell(cell)]
                if len(cells) >= 2:
                    candidates.append((page["page_number"], cells[0], " ".join(cells[1:3]), " | ".join(cells)))
        for line in clean_lines(page["text"]):
            match = re.match(r"^(.{3,60}?)[\s:]+(-?\d[\d.,]*(?:\s*[Ee][+\-]?\d+)?)\s*(\S{0,12})\s*$", line)
            if match:
                candidates.append((page["page_number"], match.group(1), f"{match.group(2)} {match.group(3)}", line))

    for key, pattern in _PROPERTY_LABELS:
        if key in properties:
            continue
        regex = re.compile(pattern, re.IGNORECASE)
        for page_number, label, value_text, source in candidates:
            if not regex.search(label) or len(label) > 70:
                continue
            number = parse_number(re.split(r"\s+", value_text.strip())[0])
            if number is None or number <= 0:
                continue
            properties[key] = number
            provenance[key] = evidence(page_number, source, "property_label", 0.9)
            break

    quantity = declared.get("quantity")
    dimension = declared.get("dimension")
    unit = declared.get("unit")
    mass = None
    formula = None
    if properties.get("mass_per_declared_unit_kg"):
        mass = properties["mass_per_declared_unit_kg"]
        formula = "mass per declared unit reported in the EPD"
    elif properties.get("conversion_factor_to_1kg") and quantity:
        mass = quantity / properties["conversion_factor_to_1kg"]
        formula = "m = M / f (ILCD+EPD 'conversion factor to 1 kg')"
    elif dimension == "mass" and quantity:
        mass = quantity * (declared.get("factor_to_reference") or 1.0)
        formula = f"declared unit is a mass ({quantity:g} {unit})"
    elif dimension == "area" and quantity and properties.get("grammage_kg_m2"):
        mass = properties["grammage_kg_m2"] * quantity
        formula = f"m = grammage × {quantity:g} m2"
    elif dimension == "volume" and quantity and (properties.get("gross_density_kg_m3") or properties.get("density_kg_m3")
                                                  or properties.get("bulk_density_kg_m3")):
        density = properties.get("gross_density_kg_m3") or properties.get("density_kg_m3") or properties.get("bulk_density_kg_m3")
        mass = density * quantity
        formula = f"m = density × {quantity:g} m3"
    elif dimension == "length" and quantity and properties.get("linear_density_kg_m"):
        mass = properties["linear_density_kg_m"] * quantity
        formula = f"m = linear density × {quantity:g} m"
    elif dimension == "items" and quantity and properties.get("weight_per_piece_kg"):
        mass = properties["weight_per_piece_kg"] * quantity
        formula = f"m = weight per piece × {quantity:g}"

    if mass and mass > 0:
        properties["mass_per_declared_unit_kg"] = round(mass, 6)
        if "conversion_factor_to_1kg" not in properties and quantity:
            properties["conversion_factor_to_1kg"] = round(quantity / mass, 8)
            provenance["conversion_factor_to_1kg"] = evidence(
                provenance.get("grammage_kg_m2", provenance.get("mass_per_declared_unit_kg", {})).get("source_page"),
                formula, "derived_ilcd_definition", 0.9, formula="f = M / m (ILCD+EPD MaterialProperties)")
        properties["mass_formula"] = formula
    return properties, provenance


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def extract_metadata(raw_document: dict, filename: str | None = None) -> tuple[dict, dict, dict]:
    """Return (metadata, provenance, physical_properties)."""
    metadata: dict = {}
    provenance: dict = {}
    full_text = raw_document["text"]

    value, page, source, method, confidence = _registration_number(raw_document)
    _add(metadata, provenance, "registration_number", value, page, source, method, confidence)

    value, page, source = find_labeled_value(raw_document, _aliases("programme_operator"), 3, _valid_name)
    _add(metadata, provenance, "programme_operator", value, page, source, "field_label", 0.9)

    value, page, source = find_labeled_value(raw_document, _aliases("manufacturer"), 3, _valid_name)
    _add(metadata, provenance, "manufacturer", value, page, source, "field_label", 0.9)

    publication, pub_page, pub_source = find_labeled_value(raw_document, _aliases("publication_date"), 3, _valid_date)
    valid, val_page, val_source = find_labeled_value(raw_document, _aliases("valid_until"), 3, _valid_date)
    pub_parsed, val_parsed = resolve_validity_dates(publication, valid)
    _add(metadata, provenance, "publication_date", publication, pub_page, pub_source, "field_label", 0.97)
    _add(metadata, provenance, "valid_until", valid, val_page, val_source, "field_label", 0.97)
    if pub_parsed:
        metadata["publication_date_iso"] = pub_parsed["iso"]
    if val_parsed:
        metadata["valid_until_iso"] = val_parsed["iso"]
    if (pub_parsed and pub_parsed.get("ambiguous")) or (val_parsed and val_parsed.get("ambiguous")):
        metadata["date_format_assumption"] = (pub_parsed or val_parsed).get("format")

    # Product name: explicit label → IBU cover → largest font on page 1.
    value, page, source = find_labeled_value(raw_document, ["Product name", "Name of product", "Produktnavn",
                                                            "Produktname"], 2, _valid_name)
    method = "field_label"
    if not value:
        value, source = _cover_title_ibu(raw_document, metadata.get("manufacturer"))
        page, method = (1 if value else None), "cover_title"
    if not value:
        value, source = _cover_title_by_font(raw_document, exclude=[metadata.get("manufacturer"),
                                                                    metadata.get("programme_operator")])
        page, method = (1 if value else None), "cover_largest_font"
    _add(metadata, provenance, "product_name", value, page, source, method,
         0.93 if method != "cover_largest_font" else 0.75)

    profile = detect_standard_profile(full_text)
    metadata["standard_profile"] = profile
    standard = {"EN15804_A1_CML": "EN 15804+A1"}.get(profile, "EN 15804+A2" if profile.startswith("EN15804_A2") else None)
    if standard:
        match = re.search(r"[^\n]{0,80}15804[^\n]{0,60}", full_text)
        snippet = match.group(0).strip() if match else standard
        _add(metadata, provenance, "standard", standard, find_page(raw_document, "15804"), snippet,
             "standard_profile_detection", 0.97)

    value, page, source = _pcr(raw_document)
    _add(metadata, provenance, "pcr", value, page, source, "pcr_regex", 0.9)

    value, page, source, method, confidence = _verifier(raw_document)
    _add(metadata, provenance, "verifier", value, page, source, method, confidence)

    verification = detect_verification(raw_document)
    if verification["value"]:
        metadata["verification_type"] = verification["value"]
        provenance["verification_type"] = verification["evidence"]
    metadata["iso14025_mentioned"] = verification["iso14025_mentioned"]

    value, page, source, method, confidence = _declared_unit(raw_document)
    _add(metadata, provenance, "declared_unit_raw", value, page, source, method, confidence)
    declared = parse_declared_unit(value)
    if declared:
        metadata["declared_quantity"] = declared.get("quantity")
        metadata["declared_unit"] = declared.get("unit")
        metadata["declared_unit_dimension"] = declared.get("dimension")

    years, status, rsl_evidence = _rsl(raw_document, provenance)
    metadata["reference_service_life_years"] = years
    metadata["reference_service_life_status"] = status
    if rsl_evidence:
        provenance["reference_service_life_years"] = rsl_evidence

    value, page, source, method, confidence = _geography(raw_document)
    _add(metadata, provenance, "geography", value, page, source, method, confidence)

    value, page, source = _product_description(raw_document)
    _add(metadata, provenance, "product_description", value, page, source, "section_text", 0.85)

    language, lang_conf = detect_language(full_text)
    registration = metadata.get("registration_number") or ""
    suffix = re.search(r"-(EN|DA|DK|DE|NO|SV|FR|NL)$", registration)
    if suffix:
        language = {"EN": "English", "DA": "Danish", "DK": "Danish", "DE": "German", "NO": "Norwegian",
                    "SV": "Swedish", "FR": "French", "NL": "Dutch"}[suffix.group(1)]
        lang_conf = 0.9
    _add(metadata, provenance, "language", language, None, "stop-word frequency / ID suffix",
         "language_detection", lang_conf)

    physical, physical_provenance = extract_physical_properties(raw_document, declared)
    provenance.update({f"physical_properties.{key}": value for key, value in physical_provenance.items()})
    return metadata, provenance, physical


def extract_extended_metadata(raw_document: dict) -> tuple[dict, dict]:
    """
    Optional Step 3 fields (composition, technical data, packaging, software/database).
    Only used when EPD_EXTRACTION_PROFILE=extended; Step 1 does not need them.
    """
    metadata, provenance = {}, {}
    full = raw_document["text"].replace("\xa0", " ")

    for page in raw_document["pages"]:
        for table in page.get("tables", []):
            rows = [[clean_cell(cell) for cell in row] for row in table.get("cells", [])]
            parsed = []
            for row in rows[1:]:
                cells = [cell for cell in row if cell]
                if len(cells) >= 2 and ("%" in " ".join(cells[1:3])):
                    number = parse_number(cells[1].replace("%", "").strip())
                    parsed.append({"component": cells[0], "value": number if number is not None else cells[1],
                                   "unit": "%"})
            if len(parsed) >= 3 and "product_composition" not in metadata:
                metadata["product_composition"] = parsed
                provenance["product_composition"] = evidence(page["page_number"], f"{len(parsed)} rows",
                                                             "structured_table", 0.9)

    match = re.search(r"(?:LCA\s+(?:modelling\s+)?software|software)\s*(?:is|:|used)?\s*([^.\n]{3,80})", full,
                      re.IGNORECASE)
    if match:
        metadata["lca_software"] = match.group(1).strip()
        provenance["lca_software"] = evidence(find_page(raw_document, match.group(0)[:30]), match.group(0),
                                              "field_regex", 0.7)
    databases = sorted({m.group(0) for m in re.finditer(
        r"(ecoinvent\s*(?:v|version)?\s*\d(?:\.\d+)*|Managed LCA Content\s*\(?v?\d{4}(?:\.\d)?\)?|"
        r"GaBi\s*(?:database)?\s*(?:SP\s*\d+|20\d\d)?|Sphera MLC[^,.\n]{0,20}|ÖKOBAUDAT\s*\d{4}(?:-I+)?)",
        full, re.IGNORECASE)})
    if databases:
        metadata["background_database"] = "; ".join(databases)
        provenance["background_database"] = evidence(None, ", ".join(databases), "database_names", 0.8)
    return metadata, provenance
