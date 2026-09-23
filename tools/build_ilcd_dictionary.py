#!/usr/bin/env python3
"""
Build the ILCD-based EPD dictionary (version 4) from the local "ILCD pdf" folder.

What it does
------------
1. Parses the machine-readable ILCD / ILCD+EPD reference material:
   * ``ILCD-EPD-Data-Format-release-v1.3/doc/asciidoc/ilcd-epd-v1.3.adoc``
     (field table: XPath, requirement, datatype, ILCD + InData definitions,
     EN 15804+A2 clause, ECO Platform conformity, ISO 22057 GUID),
   * ``doc/identifiers/*.csv`` (indicator / flow UUIDs for EN 15804+A1,
     +A2 EF3.0, +A2 EF3.1, country-specific indicators, compliance systems,
     background databases, flow properties and unit groups),
   * ``EPD_Developer_Docs/EPD_reference_data/ILCD/unitgroups/*.xml``
     (ILCD unit groups with conversion factors),
   * ``schemas/*.xsd`` (ILCD and EPD-extension enumerations),
   * ``sample_data/OEKOBAU.DAT_Categories.xml`` (classification tree),
   * ``doc/MaterialProperties.md`` (material property identifiers).
2. Combines them with the curated specifications in ``dictionary_specs.py``
   (Step 1 DGNB profile, Step 3 column list, ECO Platform operators,
   Step 2 material categories, PDF label aliases).
3. Writes
   * ``data/ilcd_reference.json`` – compact ILCD reference extract,
   * ``data/epd_dictionary.json`` – the existing dictionary with all original
     keys left untouched plus new v4 keys.

Only the Python standard library is used, so the script runs on any
Python >= 3.9 without installing packages.

Usage (from the project folder)::

    python tools/build_ilcd_dictionary.py
    python tools/build_ilcd_dictionary.py --ilcd-dir "ILCD pdf" --check
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dictionary_specs as specs  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
DICTIONARY_VERSION = "4.0.0-ilcd"

RELEASE_DIR_NAME = "ILCD-EPD-Data-Format-release-v1.3"
DEVDOCS_DIR_NAME = "EPD_Developer_Docs"

ADOC_COLUMNS = [
    "order", "id_previous", "id_new", "format_version", "name_de", "name_en", "element",
    "required", "occurrence", "datatype", "ilcd_definition_en", "definition_de",
    "indata_definition_en", "further_explanations_en", "indata_cp2020", "deviation",
    "extension", "indata_cpen2020", "edoc_id", "example", "en15804_a2_chapter",
    "en15804_a2_required", "eco_platform_conformity", "iso22057_guid", "iso22057_required",
    "iso21930_mapping", "iso21930_required", "indent", "path",
]

REQUIREMENT_LABELS = {"m": "mandatory", "r": "recommended", "o": "optional"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    if text is None:
        return ""
    text = str(text).replace(" ", " ").strip()
    return "" if text.lower() == "nan" else re.sub(r"[ \t]+", " ", text)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_from_name(name: str) -> str | None:
    """'Global Warming Potential - total (GWP-total)' -> 'GWP-total'."""
    match = re.search(r"\(([^()]+)\)\s*$", name.strip())
    if not match:
        return None
    code = match.group(1).strip()
    # Country-specific names such as "(GWP-IOBC/GHG, EF 3.0)".
    code = code.split(",")[0].strip()
    return code


# ---------------------------------------------------------------------------
# ILCD parsers
# ---------------------------------------------------------------------------

def parse_adoc_field_table(path: Path) -> list[dict]:
    """Parse the 29-column EPD data set table of ilcd-epd-v1.3.adoc."""
    lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()

    start = None
    for index, line in enumerate(lines):
        if line.startswith('| [role="title"]##Path##'):
            start = index + 1
            break
    if start is None:
        raise ValueError(f"Could not find the field table header in {path}")

    cells: list[str] = []
    current: str | None = None
    for line in lines[start:]:
        if line.startswith("|==="):
            break
        if line.startswith("| "):
            if current is not None:
                cells.append(current)
            current = line[2:]
        elif current is not None:
            current += "\n" + line
    if current is not None:
        cells.append(current)

    def clean_cell(cell: str) -> str:
        cell = cell.strip()
        cell = re.sub(r'^\[role="[^"]*"\]', "", cell).strip()
        if cell.startswith("##") and cell.endswith("##"):
            cell = cell[2:-2]
        cell = cell.replace("{nbsp}", " ").replace(" +\n", "\n")
        return _clean(cell)

    cells = [clean_cell(cell) for cell in cells]
    if len(cells) % len(ADOC_COLUMNS):
        raise ValueError(f"Unexpected adoc table shape: {len(cells)} cells")

    rows = []
    for offset in range(0, len(cells), len(ADOC_COLUMNS)):
        row = dict(zip(ADOC_COLUMNS, cells[offset:offset + len(ADOC_COLUMNS)]))
        row["element"] = row["element"].strip()
        rows.append(row)
    return rows


def compact_adoc_row(row: dict) -> dict:
    compact = {
        "order": int(row["order"]) if row["order"].isdigit() else row["order"],
        "field_name_en": row["name_en"],
        "element": row["element"],
        "path": row["path"],
        "requirement": REQUIREMENT_LABELS.get(row["required"], row["required"] or None),
        "occurrence": row["occurrence"] or None,
        "datatype": row["datatype"] or None,
        "ilcd_definition_en": row["ilcd_definition_en"] or None,
        "indata_definition_en": row["indata_definition_en"] or None,
        "indata_cp2020": REQUIREMENT_LABELS.get(row["indata_cp2020"], row["indata_cp2020"] or None),
        "en15804_a2_chapter": row["en15804_a2_chapter"] or None,
        "en15804_a2_required": row["en15804_a2_required"] or None,
        "eco_platform_conformity": row["eco_platform_conformity"] or None,
        "iso22057_guid": row["iso22057_guid"] or None,
        "format_version": row["format_version"] or None,
    }
    return {key: value for key, value in compact.items() if value not in (None, "")}


def read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {(key or "").strip(): _clean(value) for key, value in row.items()}
            for row in reader
        ]


def parse_indicator_csv(path: Path, profile: str) -> list[dict]:
    out = []
    for row in read_csv_rows(path):
        uuid = row.get("UUID", "")
        name = row.get("Name (en)", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", uuid or ""):
            continue
        code = _code_from_name(name)
        out.append({
            "uuid": uuid,
            "version": row.get("Version") or None,
            "code": code,
            "name_en": name,
            "unit_en": row.get("Unit (en)") or None,
            "unit_group_uuid": row.get("UnitGroup UUID") or None,
            "name_de": row.get("Name (de)") or row.get("de") or None,
            "profile": profile,
            "kind": "flow" if code in INVENTORY_CODES else "lcia",
        })
    return out


INVENTORY_CODES = {
    "PERE", "PERM", "PERT", "PENRE", "PENRM", "PENRT", "SM", "RSF", "NRSF", "FW",
    "HWD", "NHWD", "RWD", "CRU", "MFR", "MER", "EEE", "EET",
}


def parse_unit_groups(folder: Path) -> dict:
    ns = {"u": "http://lca.jrc.it/ILCD/UnitGroup", "common": "http://lca.jrc.it/ILCD/Common"}
    groups = {}
    for file in sorted(folder.glob("*.xml")):
        root = ET.parse(file).getroot()
        uuid = root.findtext(".//common:UUID", default="", namespaces=ns).strip()
        names = {
            el.get("{http://www.w3.org/XML/1998/namespace}lang", "en"): (el.text or "").strip()
            for el in root.findall(".//u:dataSetInformation/common:name", ns)
        }
        ref_index = root.findtext(".//u:quantitativeReference/u:referenceToReferenceUnit",
                                  default="0", namespaces=ns).strip()
        units = {}
        reference_unit = None
        for unit in root.findall(".//u:units/u:unit", ns):
            unit_name = (unit.findtext("u:name", default="", namespaces=ns) or "").strip()
            try:
                factor = float(unit.findtext("u:meanValue", default="nan", namespaces=ns))
            except ValueError:
                continue
            if unit_name:
                units[unit_name] = factor
            if unit.get("dataSetInternalID") == ref_index:
                reference_unit = unit_name
        groups[uuid] = {
            "name_en": names.get("en") or next(iter(names.values()), ""),
            "name_de": names.get("de"),
            "reference_unit": reference_unit,
            "units_to_reference": units,
        }
    return groups


def parse_enumerations(xsd_files: list[Path]) -> dict:
    xs = "{http://www.w3.org/2001/XMLSchema}"
    enums = {}
    for file in xsd_files:
        root = ET.parse(file).getroot()
        for simple in root.iter(xs + "simpleType"):
            values = [item.get("value") for item in simple.iter(xs + "enumeration")]
            if values and simple.get("name"):
                enums[simple.get("name")] = values
    return enums


def parse_categories(path: Path) -> dict:
    root = ET.parse(path).getroot()
    systems = {}

    def walk(element, parent_path):
        items = []
        for child in element:
            tag = child.tag.split("}")[-1]
            if tag == "category":
                item = {"id": child.get("id"), "name": child.get("name")}
                children = walk(child, parent_path + [child.get("name")])
                if children:
                    item["children"] = children
                items.append(item)
        return items

    for categories in root:
        if categories.tag.split("}")[-1] != "categories":
            continue
        data_type = categories.get("dataType", "Process")
        systems.setdefault(data_type, walk(categories, []))
    return {"name": root.get("name"), "by_data_type": systems}


def flatten_categories(tree: list[dict], out: dict | None = None, path=None) -> dict:
    out = {} if out is None else out
    path = path or []
    for item in tree:
        full = path + [item["name"]]
        out[item["id"]] = {"name_de": item["name"], "path_de": " / ".join(full)}
        flatten_categories(item.get("children", []), out, full)
    return out


def parse_material_properties(path: Path) -> list[dict]:
    props = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].startswith("**") or set(cells[0]) <= {"-", " "}:
            continue
        name, identifier, unit = cells[0], cells[1], cells[2]
        if not identifier or identifier.startswith("---"):
            continue
        props.append({
            "property": name,
            "identifier": identifier,
            "unit": unit or "-",
            "comment": re.sub(r"<br>", " ", cells[3]).strip() if len(cells) > 3 else "",
        })
    return props


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def build_indicator_registry(indicator_lists: list[list[dict]], existing: dict) -> tuple[dict, list[str]]:
    """UUID -> indicator info, and code -> UUIDs per profile, validated against v3."""
    by_uuid: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    warnings: list[str] = []

    for items in indicator_lists:
        for item in items:
            code = item["code"]
            entry = by_uuid.setdefault(item["uuid"], {
                "code": code,
                "kind": item["kind"],
                "name_en": item["name_en"],
                "unit": item["unit_en"],
                "unit_group_uuid": item["unit_group_uuid"],
                "profiles": [],
            })
            if item["profile"] not in entry["profiles"]:
                entry["profiles"].append(item["profile"])
            by_code.setdefault(code, {})[item["profile"]] = item["uuid"]

    # Compare with UUIDs stored in the v3 dictionary (kept unchanged, reported only).
    for profile, indicators in existing.get("impact_indicators", {}).items():
        for code, details in indicators.items():
            stored = dict(details.get("uuid_by_method", {}))
            if details.get("uuid"):
                stored["single"] = details["uuid"]
            for method, uuid in stored.items():
                if uuid and uuid not in by_uuid:
                    warnings.append(
                        f"v3 dictionary {profile}/{code} UUID {uuid} ({method}) is not in the "
                        "ILCD+EPD v1.3 identifier lists; the v4 registry is authoritative."
                    )
    return {"by_uuid": by_uuid, "by_code": by_code}, warnings


def build_extra_aliases(indicator_lists: list[list[dict]]) -> dict:
    """Official ILCD+EPD English names become additional PDF-label aliases."""
    aliases: dict[str, list[str]] = {}
    for items in indicator_lists:
        for item in items:
            code = item["code"]
            name = item["name_en"]
            bare = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()
            for alias in (name, bare):
                if alias and alias not in aliases.setdefault(code, []):
                    aliases[code].append(alias)
    return aliases


def build_step3(adoc_by_path: dict, registry: dict, errors: list[str]) -> dict:
    sections = []
    for section in specs.STEP3_SECTIONS:
        fields = []
        for spec in section["fields"]:
            field = {key: value for key, value in spec.items() if key not in ("ilcd_paths",)}
            paths = list(spec.get("ilcd_paths", []))

            if spec.get("type") == "indicator":
                code = spec["indicator_code"]
                kind = "inventory" if code in INVENTORY_CODES else "lcia"
                paths = paths or list(specs.INDICATOR_RESULT_PATHS[kind])
                field["ilcd_indicator_uuids"] = registry["by_code"].get(code, {})
                uuid = next(iter(field["ilcd_indicator_uuids"].values()), None)
                if uuid:
                    info = registry["by_uuid"][uuid]
                    field["canonical_unit"] = info["unit"]
                    field["unit_group_uuid"] = info["unit_group_uuid"]
                    field["ilcd_name_en"] = info["name_en"]
                else:
                    errors.append(f"Step 3 indicator {code} has no ILCD+EPD UUID.")
            elif spec.get("type") == "module":
                paths = paths or [f"{specs.LCIA_AMOUNT}/@epd:module", f"{specs.EXCH_AMOUNT}/@epd:module"]
                field["ilcd_module_value"] = spec["module"]

            rows = []
            for path in paths:
                if path.startswith("flowDataSet/"):
                    rows.append({"path": path, "data_set": "flow data set (reference product flow)"})
                    continue
                row = adoc_by_path.get(path)
                if row is None:
                    errors.append(f"Step 3 field {spec['key']}: path not in ILCD+EPD v1.3 table: {path}")
                    continue
                rows.append(compact_adoc_row(row))
            field["ilcd"] = {"paths": paths, "fields": rows}
            fields.append(field)
        sections.append({"id": section["id"], "label": section["label"], "fields": fields})

    return {
        "description": "Dictionary of all Step 3 columns (Claude instruction 1) mapped to ILCD+EPD v1.3.",
        "extract_scope_legend": {
            "step1_parameter": "Extracted from uploaded EPDs (Step 1 DGNB parameter).",
            "step1_identity": "Extracted because storage, retrieval or categorisation needs it.",
            "provenance": "Produced automatically for every stored value.",
            "dictionary_only": "Defined in the dictionary; extraction planned for a later step.",
        },
        "sections": sections,
    }


def build_unit_conversion(unit_groups: dict) -> dict:
    wanted = {
        "93a60a57-a4c8-11da-a746-0800200c9a66": "mass",
        "93a60a57-a3c8-18da-a746-0800200c9a66": "area",
        "93a60a57-a3c8-12da-a746-0800200c9a66": "volume",
        "838aaa22-0117-11db-92e3-0800200c9a66": "length",
        "93a60a57-a3c8-11da-a746-0800200c9a66": "energy",
        "5beb6eed-33a9-47b8-9ede-1dfe8f679159": "items",
        "93a60a57-a3c8-16da-a746-0800200c9a66": "radioactivity",
    }
    out = {}
    for uuid, quantity in wanted.items():
        group = unit_groups.get(uuid)
        if not group:
            continue
        out[quantity] = {
            "unit_group_uuid": uuid,
            "unit_group_name": group["name_en"],
            "reference_unit": group["reference_unit"],
            "factors_to_reference": group["units_to_reference"],
        }
    # Common PDF spellings that ILCD writes differently.
    out["pdf_unit_aliases"] = {
        "m²": "m2", "m^2": "m2", "sqm": "m2", "m³": "m3", "m^3": "m3", "cbm": "m3",
        "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t", "kilogram": "kg", "kilograms": "kg",
        "piece": "Item(s)", "pieces": "Item(s)", "pcs": "Item(s)", "pc": "Item(s)", "stk": "Item(s)",
        "unit": "Item(s)", "item": "Item(s)", "items": "Item(s)", "metre": "m", "meter": "m",
        "running metre": "m", "lm": "m", "kwh": "kWh",
    }
    return out


def build_category_taxonomy(oekobaudat: dict) -> dict:
    flat = flatten_categories(oekobaudat["by_data_type"].get("Process", []))
    categories = []
    for spec in specs.MATERIAL_CATEGORIES:
        item = copy.deepcopy(spec)
        item["oekobaudat_categories"] = [
            {"id": cid, **flat.get(cid, {"name_de": None, "path_de": None})}
            for cid in spec.get("oekobaudat", [])
        ]
        item.pop("oekobaudat", None)
        item["cpa_hint"] = item.pop("cpa", [])
        categories.append(item)
    return {
        "description": "Step 2 building material / component categories with keyword evidence rules.",
        "field_weights": specs.CATEGORY_FIELD_WEIGHTS,
        "keyword_weights": {"strong": 3.0, "weak": 1.0, "pattern": 2.5, "pcr_hint": 2.0},
        "fallback": {"id": "other", "label": "Other / unclassified", "building_element": "Unknown"},
        "categories": categories,
    }


def build_reference_basis(ilcd_dir: Path, files: dict[str, Path]) -> dict:
    basis = {}
    for label, path in files.items():
        if path.exists():
            basis[label] = {
                "file": str(path.relative_to(ilcd_dir.parent)) if ilcd_dir.parent in path.parents else str(path),
                "sha256": _sha256(path),
            }
    pdfs = sorted(p.name for p in ilcd_dir.glob("*.pdf"))
    basis["ilcd_pdf_documents"] = pdfs
    return basis


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build(ilcd_dir: Path, dictionary_path: Path, reference_path: Path, check_only: bool = False) -> int:
    release = ilcd_dir / RELEASE_DIR_NAME
    devdocs = ilcd_dir / DEVDOCS_DIR_NAME
    identifiers = release / "doc" / "identifiers"

    files = {
        "field_table_adoc": release / "doc" / "asciidoc" / "ilcd-epd-v1.3.adoc",
        "indicators_en15804_a1": identifiers / "EN15804+A1_indicators.csv",
        "indicators_en15804_a2_ef30": identifiers / "EN15804+A2_EF3.0_indicators.csv",
        "indicators_en15804_a2_ef31": identifiers / "EN15804+A2_EF3.1_indicators.csv",
        "indicators_country_specific": identifiers / "Country-specific_indicators.csv",
        "common_references": identifiers / "Common_references.csv",
        "flow_properties_unit_groups": identifiers / "Flow_properties_and_unit_groups.csv",
        "background_db_ecoinvent": identifiers / "BackgroundDB_SourceDatasets_ecoinvent.csv",
        "background_db_gabi": identifiers / "BackgroundDB_SourceDatasets_GaBi.csv",
        "enumerations_ilcd": release / "schemas" / "ILCD_Common_EnumerationValues.xsd",
        "enumerations_epd_2013": release / "schemas" / "EPD_Extensions_2013.xsd",
        "enumerations_epd_2024": release / "schemas" / "EPD_Extensions_2024.xsd",
        "oekobaudat_categories": release / "sample_data" / "OEKOBAU.DAT_Categories.xml",
        "material_properties": release / "doc" / "MaterialProperties.md",
    }
    missing = [str(path) for path in files.values() if not path.exists()]
    unit_group_dir = devdocs / "EPD_reference_data" / "ILCD" / "unitgroups"
    if not unit_group_dir.exists():
        missing.append(str(unit_group_dir))
    if missing:
        print("Missing ILCD reference files:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    existing = json.loads(dictionary_path.read_text(encoding="utf-8"))
    # Rebuild from the pristine v3 content if the file was already upgraded.
    base = existing.get("_v3_original_keys")
    if base:
        existing_v3 = {key: existing[key] for key in base if key in existing}
    else:
        existing_v3 = existing

    errors: list[str] = []

    adoc_rows = parse_adoc_field_table(files["field_table_adoc"])
    adoc_by_path = {row["path"]: row for row in adoc_rows if row["path"]}

    indicator_lists = [
        parse_indicator_csv(files["indicators_en15804_a1"], "EN15804_A1"),
        parse_indicator_csv(files["indicators_en15804_a2_ef30"], "EN15804_A2_EF3.0"),
        parse_indicator_csv(files["indicators_en15804_a2_ef31"], "EN15804_A2_EF3.1"),
    ]
    country_specific = [
        {"uuid": row["UUID"], "name_en": row["Name (en)"], "unit": row["Unit (en)"],
         "unit_group_uuid": row["UnitGroup UUID"], "countries": row["Country/-ies"]}
        for row in read_csv_rows(files["indicators_country_specific"])
        if re.fullmatch(r"[0-9a-f-]{36}", row.get("UUID", ""))
    ]
    registry, registry_warnings = build_indicator_registry(indicator_lists, existing_v3)

    common_refs = [
        {"name": row["Name"], "uuid": row["UUID"], "dataset_type": row["Dataset type"]}
        for row in read_csv_rows(files["common_references"]) if row.get("UUID")
    ]
    compliance_systems = {}
    for ref in common_refs:
        name = ref["name"]
        if "15804+A1" in name:
            compliance_systems[ref["uuid"]] = {"standard": "EN 15804+A1", "profile": "EN15804_A1_CML", "name": name}
        elif "15804+A2" in name and "3.1" in name:
            compliance_systems[ref["uuid"]] = {"standard": "EN 15804+A2", "profile": "EN15804_A2_EF3.1", "name": name}
        elif "15804+A2" in name:
            compliance_systems[ref["uuid"]] = {"standard": "EN 15804+A2", "profile": "EN15804_A2_EF3.0", "name": name}
        elif name.startswith("ISO"):
            compliance_systems[ref["uuid"]] = {"standard": name, "profile": None, "name": name}

    background_dbs = []
    for row in read_csv_rows(files["background_db_ecoinvent"]):
        if row.get("UUID"):
            background_dbs.append({"family": "ecoinvent", "version": row.get("ecoinvent Database Version"),
                                   "name": row.get("Name"), "uuid": row["UUID"]})
    for row in read_csv_rows(files["background_db_gabi"]):
        if row.get("UUID"):
            background_dbs.append({"family": "GaBi / Sphera MLC", "version": row.get("GaBi Database Version"),
                                   "name": row.get("Name"), "uuid": row["UUID"]})

    flow_properties = [
        {"name": row.get("Flow property"), "uuid": row.get("Flow property UUID"),
         "reference_unit": row.get("Reference unit") or None,
         "unit_group": row.get("Reference unit group") or None,
         "unit_group_uuid": row.get("Reference unit group UUID") or None}
        for row in read_csv_rows(files["flow_properties_unit_groups"]) if row.get("Flow property UUID")
    ]

    unit_groups = parse_unit_groups(unit_group_dir)
    enumerations = parse_enumerations([
        files["enumerations_ilcd"], files["enumerations_epd_2013"], files["enumerations_epd_2024"],
    ])
    oekobaudat = parse_categories(files["oekobaudat_categories"])
    material_properties = parse_material_properties(files["material_properties"])

    step3 = build_step3(adoc_by_path, registry, errors)

    # ------------------------------------------------------------------ outputs
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    reference_basis = build_reference_basis(ilcd_dir, files)

    reference = {
        "name": "ILCD+EPD v1.3 reference extract",
        "generated_at": generated_at,
        "generated_by": "tools/build_ilcd_dictionary.py",
        "sources": reference_basis,
        "epd_dataset_fields": [compact_adoc_row(row) for row in adoc_rows],
        "indicators": {
            "EN15804_A1": indicator_lists[0],
            "EN15804_A2_EF3.0": indicator_lists[1],
            "EN15804_A2_EF3.1": indicator_lists[2],
            "country_specific": country_specific,
        },
        "common_references": common_refs,
        "background_databases": background_dbs,
        "flow_properties": flow_properties,
        "unit_groups": unit_groups,
        "enumerations": enumerations,
        "material_properties": material_properties,
        "oekobaudat_categories": oekobaudat,
    }

    wanted_enums = [
        "TypeOfReviewValues", "SubTypeValues", "TypeOfQuantitativeReferenceValues",
        "LicenseTypeValues", "ManufacturerVariabilityValues", "ProductVariabilityValues",
        "VariationRange", "UseConditionFactors", "DataSourceTypeValues",
        "TypeOfProcessValues", "WorkflowAndPublicationStatusValues",
    ]

    v3_keys = list(existing_v3.keys())
    dictionary = copy.deepcopy(existing_v3)
    dictionary["previous_version"] = existing_v3.get("version")
    dictionary["version"] = DICTIONARY_VERSION
    dictionary["_v3_original_keys"] = v3_keys
    dictionary["v4_generated_at"] = generated_at
    dictionary["v4_note"] = (
        "All v3 keys are kept unchanged (Step 1 instruction: keep the existing dictionary). "
        "Keys added in v4 are listed in v4_added_keys and were generated from the ILCD folder "
        "by tools/build_ilcd_dictionary.py."
    )
    added = {
        "ilcd_reference_file": "ilcd_reference.json",
        "ilcd_reference_basis": reference_basis,
        "step3_dictionary": step3,
        "step1_dgnb_profile": specs.STEP1_PROFILE,
        "dgnb_module_scope_labels": specs.DGNB_SCOPE_LABELS,
        "eco_platform_programme_operators": specs.ECO_PLATFORM_OPERATORS,
        "material_categories": build_category_taxonomy(oekobaudat),
        "ilcd_indicator_registry": registry,
        "ilcd_indicator_aliases": build_extra_aliases(indicator_lists),
        "ilcd_country_specific_indicators": country_specific,
        "ilcd_compliance_systems": compliance_systems,
        "ilcd_background_databases": background_dbs,
        "ilcd_flow_properties": flow_properties,
        "ilcd_material_properties": material_properties,
        "ilcd_unit_conversion": build_unit_conversion(unit_groups),
        "ilcd_enumerations": {name: enumerations[name] for name in wanted_enums if name in enumerations},
        "ilcd_xml_namespaces": {
            "process": "http://lca.jrc.it/ILCD/Process",
            "flow": "http://lca.jrc.it/ILCD/Flow",
            "common": "http://lca.jrc.it/ILCD/Common",
            "epd": "http://www.iai.kit.edu/EPD/2013",
            "epd2": "http://www.indata.network/EPD/2019",
            "epd23": "http://www.indata.network/EPD/2023",
            "epd24": "http://www.indata.network/EPD/2024",
            "matml": "http://www.matml.org/",
        },
        "build_warnings": registry_warnings,
    }
    dictionary.update(added)
    dictionary["v4_added_keys"] = list(added.keys())

    if errors:
        print("Dictionary build failed:", file=sys.stderr)
        for error in errors:
            print("  - " + error, file=sys.stderr)
        return 1

    n_fields = sum(len(section["fields"]) for section in step3["sections"])
    print(f"ILCD field table rows: {len(adoc_rows)}")
    print(f"Indicator UUIDs: {len(registry['by_uuid'])} | unit groups: {len(unit_groups)} | "
          f"enumerations: {len(enumerations)}")
    print(f"Step 3 dictionary: {len(step3['sections'])} sections, {n_fields} columns")
    for warning in registry_warnings:
        print("warning: " + warning)

    if check_only:
        print("--check: nothing written.")
        return 0

    reference_path.write_text(json.dumps(reference, indent=1, ensure_ascii=False), encoding="utf-8")
    dictionary_path.write_text(json.dumps(dictionary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {reference_path} and {dictionary_path} (version {DICTIONARY_VERSION}).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ilcd-dir", default=str(PROJECT_DIR / "ILCD pdf"))
    parser.add_argument("--dictionary", default=str(PROJECT_DIR / "data" / "epd_dictionary.json"))
    parser.add_argument("--reference-out", default=str(PROJECT_DIR / "data" / "ilcd_reference.json"))
    parser.add_argument("--check", action="store_true", help="Parse and validate only; do not write files.")
    args = parser.parse_args(argv)
    return build(Path(args.ilcd_dir), Path(args.dictionary), Path(args.reference_out), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
