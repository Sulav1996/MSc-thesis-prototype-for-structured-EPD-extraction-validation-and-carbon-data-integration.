"""
Dictionary access layer.

``data/epd_dictionary.json`` (v4, built by ``tools/build_ilcd_dictionary.py``)
is the single normalisation layer of the prototype. This module loads it once
and exposes small, cached lookup helpers used by the extractors, the
categoriser, the validator and the Streamlit UI.

The public functions of the earlier version (load_dictionary, normalize_text,
normalize_unit, metadata_aliases, detect_standard_profile, module_alias_map,
normalize_module, match_indicator, missing_value_status) keep their names and
behaviour so older code keeps working.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "data" / "epd_dictionary.json"

STANDARD_MODULES = (
    "A1", "A2", "A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C1", "C2", "C3", "C4", "D",
)
AGGREGATE_MODULES = ("A1-A3", "A1-A4", "A1-A5", "B1-B7", "C1-C4")


@lru_cache(maxsize=1)
def load_dictionary() -> dict:
    with DICTIONARY_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


# ---------------------------------------------------------------------------
# text / unit normalisation
# ---------------------------------------------------------------------------

_REPLACEMENTS = {
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    "₂": "2", "₃": "3", "₄": "4", "²": "2", "³": "3", "⁺": "+", "⁻": "-",
    "­": "", " ": " ", " ": " ", " ": " ",
    "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'",
}
_REPLACE_RE = re.compile("|".join(map(re.escape, _REPLACEMENTS)))


def normalize_text(value) -> str:
    """Normalise text for matching only. Raw source text is preserved elsewhere."""
    if value is None:
        return ""
    value = _REPLACE_RE.sub(lambda m: _REPLACEMENTS[m.group(0)], str(value))
    return re.sub(r"\s+", " ", value.lower()).strip()


@lru_cache(maxsize=1)
def _unit_alias_map() -> dict:
    mapping = {}
    for canonical, aliases in load_dictionary().get("unit_normalization", {}).items():
        for alias in [canonical] + list(aliases):
            mapping[_unit_key_text(alias)] = canonical
    return mapping


def _unit_key_text(unit) -> str:
    text = normalize_text(unit)
    text = text.replace("eqv.", "eq").replace("eqv", "eq").replace("-eq", " eq").replace("eq.", "eq")
    text = text.replace("co2e", "co2 eq").replace("cfc11", "cfc 11").replace("cfc-11", "cfc 11")
    return re.sub(r"\s+", " ", text).strip(" .")


def unit_key(raw_unit) -> str | None:
    """Return the unit_normalization key (e.g. 'kg_CO2_eq') for any known spelling."""
    if not raw_unit:
        return None
    return _unit_alias_map().get(_unit_key_text(raw_unit))


def normalize_unit(raw_unit):
    """Return the canonical unit key when an alias is known, otherwise the stripped raw unit."""
    if raw_unit is None:
        return None
    return unit_key(raw_unit) or str(raw_unit).strip()


def metadata_aliases(field_key: str) -> list[str]:
    """PDF label aliases for a metadata field (v3 metadata_fields + v4 Step 3 aliases)."""
    dictionary = load_dictionary()
    aliases = list(dictionary.get("metadata_fields", {}).get(field_key, {}).get("aliases_en", []))
    step3 = step3_field(field_key)
    if step3:
        aliases += [alias for alias in step3.get("pdf_aliases", []) if alias not in aliases]
    return aliases


# ---------------------------------------------------------------------------
# standard profile
# ---------------------------------------------------------------------------

def detect_standard_profile(text) -> str:
    """
    Detect the EN 15804 profile conservatively.
    +A2 can be detected without knowing EF3.0 vs EF3.1.
    """
    dictionary = load_dictionary()
    normalized = normalize_text(text)
    rules = dictionary["parser_rules"]["standard_detection"]

    has_a2 = any(normalize_text(term) in normalized for term in rules.get("EN15804_A2_UNKNOWN_EF", []))
    has_a2 = has_a2 or bool(re.search(r"15804\s*:?\s*(2012\s*)?\+\s*a2", normalized))
    has_a1 = bool(re.search(r"15804\s*:?\s*(2012\s*)?\+\s*a1\b", normalized))

    if has_a2:
        if re.search(r"\bef\s*3\.1\b|\bef3\.1\b|environmental footprint 3\.1", normalized):
            return "EN15804_A2_EF3.1"
        if re.search(r"\bef\s*3\.0\b|\bef3\.0\b|environmental footprint 3\.0", normalized):
            return "EN15804_A2_EF3.0"
        return "EN15804_A2_UNKNOWN_EF"

    if has_a1 or "cml" in normalized:
        return "EN15804_A1_CML"

    # Indicator/unit hints.
    if "gwp-total" in normalized or "gwp-fossil" in normalized or "gwp fossil" in normalized:
        return "EN15804_A2_UNKNOWN_EF"
    if "mol h+ eq" in normalized or "kg nmvoc eq" in normalized:
        return "EN15804_A2_UNKNOWN_EF"
    return "unresolved"


def profile_family(profile: str | None) -> str | None:
    """'EN15804_A2_EF3.0' -> 'EN15804_A2'."""
    if not profile:
        return None
    if profile.startswith("EN15804_A2"):
        return "EN15804_A2"
    if profile.startswith("EN15804_A1"):
        return "EN15804_A1"
    return None


# ---------------------------------------------------------------------------
# life-cycle modules
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def module_alias_map() -> dict:
    dictionary = load_dictionary()
    mapping = {}
    for group in ("life_cycle_modules", "aggregate_modules"):
        for module, details in dictionary.get(group, {}).items():
            for alias in details.get("aliases", []) + [module]:
                mapping[normalize_text(alias)] = module
    return mapping


@lru_cache(maxsize=1)
def _compact_module_map() -> dict:
    return {key.replace(" ", ""): value for key, value in module_alias_map().items()}


_MODULE_SCENARIO_RE = re.compile(
    r"^(?P<module>a1-a3|a1-a5|a1-a4|b1-b7|c1-c4|a[1-5]|b[1-7]|c[1-4]|d)"
    r"\s*(?:[/(\-_:]\s*|\s+)(?:scenario|scen\.?|sc\.?|s)?\s*(?P<scenario>[0-9a-z]{1,3})\)?$"
)


def normalize_module(raw_header):
    """Return the EN 15804 module code for a table header cell, or None."""
    normalized = normalize_text(raw_header).replace(" ", "")
    if not normalized:
        return None
    return _compact_module_map().get(normalized)


def parse_module_header(raw_header) -> tuple[str | None, str | None]:
    """
    Parse a module header that may carry a scenario suffix.

    'A1-A3' -> ('A1-A3', None); 'C3/1' -> ('C3', '1'); 'C4 (S2)' -> ('C4', '2');
    'D - scenario 1' -> ('D', '1').
    """
    module = normalize_module(raw_header)
    if module:
        return module, None
    text = normalize_text(raw_header)
    text = re.sub(r"\s*[-–]\s*a", "-a", text)
    match = _MODULE_SCENARIO_RE.match(text)
    if not match:
        return None, None
    module = normalize_module(match.group("module"))
    return (module, match.group("scenario").upper()) if module else (None, None)


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------

def _indicator_profiles_for(profile):
    all_profiles = load_dictionary().get("impact_indicators", {})
    if profile == "EN15804_A1_CML":
        return [("EN15804_A1_CML", all_profiles.get("EN15804_A1_CML", {}))]
    if profile in ("EN15804_A2_EF3.0", "EN15804_A2_EF3.1", "EN15804_A2_UNKNOWN_EF"):
        return [(name, values) for name, values in all_profiles.items() if name.startswith("EN15804_A2")]
    return list(all_profiles.items())


def _indicator_label_match(raw_label, candidate) -> float:
    """
    Conservative PDF-label matching.

    1.00 exact, 0.95 exact after removing a footnote marker, 0.92 candidate followed
    by unit/punctuation, 0.88 long canonical phrase contained in the cell, else 0.
    """
    label = normalize_text(raw_label)
    candidate_norm = normalize_text(candidate)
    if not label or not candidate_norm:
        return 0.0
    if label == candidate_norm:
        return 1.0

    label_without_footnote = re.sub(r"\s*\d\)$", "", label)
    label_without_footnote = re.sub(r"[\s¹²³*0-9]+$", "", label_without_footnote).strip()
    if label_without_footnote == candidate_norm:
        return 0.95

    if any(label.startswith(candidate_norm + suffix) for suffix in (" ", "[", "(", ":", ";", ",")):
        return 0.92

    if len(candidate_norm) >= 12 and candidate_norm in label:
        return 0.88
    return 0.0


@lru_cache(maxsize=1)
def _indicator_candidates() -> list[tuple[str, str, str | None, list[str]]]:
    """(code, profile, unit, candidate labels) for every indicator in the dictionary."""
    dictionary = load_dictionary()
    extra = dictionary.get("ilcd_indicator_aliases", {})
    rows = []
    for profile_name, indicators in dictionary.get("impact_indicators", {}).items():
        for code, details in indicators.items():
            labels = [code, details.get("canonical_name", "")] + list(details.get("aliases_en", []))
            labels += [alias for alias in extra.get(code, []) if alias not in labels]
            rows.append((code, profile_name, details.get("unit"), labels))
    for code, details in dictionary.get("shared_resource_waste_output_indicators", {}).items():
        labels = [code, details.get("canonical_name", ""), details.get("short_name", "")]
        labels += list(details.get("aliases_en", []))
        labels += [alias for alias in extra.get(code, []) if alias not in labels]
        rows.append((code, "shared", details.get("unit"), labels))
    return rows


def match_indicator(raw_label, profile="unresolved", raw_unit=None):
    """
    Map a raw PDF indicator label to a canonical indicator code.

    Returns {'code', 'profile', 'canonical_unit', 'confidence'} or None.
    When several indicators match, one whose unit is compatible with the
    raw unit wins (this separates WDP from FW, and AP/EP of +A1 from +A2).
    """
    if not raw_label:
        return None

    allowed_profiles = {name for name, _ in _indicator_profiles_for(profile)} | {"shared"}
    matches = []
    for code, profile_name, unit, labels in _indicator_candidates():
        if profile_name not in allowed_profiles:
            continue
        best = max((_indicator_label_match(raw_label, label) for label in labels if label), default=0.0)
        if best > 0:
            matches.append({
                "code": code,
                "profile": profile_name,
                "canonical_unit": unit,
                "confidence": min(0.98, best),
            })

    if not matches:
        return None

    raw_key = unit_key(raw_unit) if raw_unit else None
    if raw_key:
        compatible = [match for match in matches if unit_key(match["canonical_unit"]) == raw_key]
        if compatible:
            best = max(compatible, key=lambda item: item["confidence"])
            best["unit_verified"] = True
            return best

    return max(matches, key=lambda item: item["confidence"])


@lru_cache(maxsize=1)
def _uuid_index() -> dict:
    return load_dictionary().get("ilcd_indicator_registry", {}).get("by_uuid", {})


def indicator_by_uuid(uuid: str | None) -> dict | None:
    """ILCD+EPD indicator (LCIA method or inventory flow) for a UUID."""
    if not uuid:
        return None
    return _uuid_index().get(uuid.strip().lower())


def indicator_info(code: str) -> dict:
    """Canonical name/unit/category for an indicator code (first profile that has it)."""
    dictionary = load_dictionary()
    for profile in ("EN15804_A2_EF3.1", "EN15804_A2_EF3.0", "EN15804_A1_CML"):
        details = dictionary.get("impact_indicators", {}).get(profile, {}).get(code)
        if details:
            return {"code": code, "profile": profile, **details}
    details = dictionary.get("shared_resource_waste_output_indicators", {}).get(code)
    return {"code": code, "profile": "shared", **details} if details else {"code": code}


def missing_value_status(raw_value):
    groups = load_dictionary()["parser_rules"]["missing_value_tokens"]
    normalized = normalize_text(raw_value)
    for status in ("not_declared", "not_relevant", "not_applicable", "not_available"):
        if any(normalized == normalize_text(token) for token in groups.get(status, [])):
            return status
    if normalized in {"mnr"}:
        return "not_relevant"
    if normalized in {"inb", "ina"}:
        return "not_declared"
    return None


# ---------------------------------------------------------------------------
# v4 helpers: Step 1, Step 2, Step 3, ILCD reference data
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _step3_index() -> dict:
    index = {}
    for section in load_dictionary().get("step3_dictionary", {}).get("sections", []):
        for field in section.get("fields", []):
            index[field["key"]] = {**field, "section_id": section["id"], "section_label": section["label"]}
    return index


def step3_sections() -> list[dict]:
    return load_dictionary().get("step3_dictionary", {}).get("sections", [])


def step3_field(key: str) -> dict | None:
    return _step3_index().get(key)


def step1_profile() -> dict:
    return load_dictionary().get("step1_dgnb_profile", {})


def step1_parameter(parameter_id: str) -> dict:
    for parameter in step1_profile().get("parameters", []):
        if parameter["id"] == parameter_id:
            return parameter
    return {}


def dgnb_module_scope() -> dict:
    return step1_parameter("life_cycle_modules").get("module_scope", {})


def eco_platform_operators() -> dict:
    return load_dictionary().get("eco_platform_programme_operators", {"operators": []})


def material_categories() -> dict:
    return load_dictionary().get("material_categories", {"categories": []})


def compliance_system(uuid: str | None) -> dict | None:
    if not uuid:
        return None
    return load_dictionary().get("ilcd_compliance_systems", {}).get(uuid.strip().lower())


@lru_cache(maxsize=1)
def _unit_factor_index() -> dict:
    conversion = load_dictionary().get("ilcd_unit_conversion", {})
    index = {}
    for quantity, details in conversion.items():
        if quantity == "pdf_unit_aliases" or not isinstance(details, dict):
            continue
        for unit, factor in details.get("factors_to_reference", {}).items():
            index.setdefault(unit.lower(), (quantity, factor, details.get("reference_unit")))
    for alias, unit in conversion.get("pdf_unit_aliases", {}).items():
        target = index.get(unit.lower())
        if target:
            index.setdefault(alias.lower(), target)
    return index


def unit_quantity(unit: str | None):
    """
    ILCD unit-group lookup: 'm²' -> ('area', 1.0, 'm2'); 't' -> ('mass', 1000.0, 'kg').
    Returns None for unknown units.
    """
    if not unit:
        return None
    text = normalize_text(unit).replace("²", "2").replace("³", "3").strip(" .")
    return _unit_factor_index().get(text)
