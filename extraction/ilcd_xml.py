"""
ILCD+EPD (v1.1 – v1.3) importer for machine-readable EPDs.

Accepts a single process data set (``.xml``) or an ILCD archive (``.zip`` with
``processes/``, ``flows/`` … folders, as exported by ÖKOBAUDAT, ECO Portal,
EPD Danmark or InData nodes). Values are identified by the indicator UUIDs of
the ILCD+EPD master data (``ilcd_indicator_registry`` in the dictionary), so
no text matching is needed and confidence is 1.0.

The output has the same record structure as the PDF extractor.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

from dictionary import compliance_system, indicator_by_uuid, indicator_info
from extraction.common import evidence, parse_date

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

FLOW_PROPERTY_UNITS = {
    "93a60a56-a3c8-11da-a746-0800200b9a66": ("mass", "kg"),
    "93a60a56-a3c8-19da-a746-0800200c9a66": ("area", "m2"),
    "93a60a56-a3c8-22da-a746-0800200c9a66": ("volume", "m3"),
    "838aaa23-0117-11db-92e3-0800200c9a66": ("length", "m"),
    "01846770-4cfe-4a25-8ad9-919d8d378345": ("items", "piece"),
    "93a60a56-a3c8-14da-a746-0800200c9a66": ("energy", "MJ"),
    "93a60a56-a3c8-11da-a746-0800200c9a66": ("energy", "MJ"),
}
BIOGENIC_PRODUCT = "62e503ce-544a-4599-b2ad-bcea15a7bf20"
BIOGENIC_PACKAGING = "262a541b-209e-44cc-a426-33bce30de7b1"
MAX_XML_BYTES = 50 * 1024 * 1024
MAX_PROCESSES_PER_ARCHIVE = 500


# ---------------------------------------------------------------------------
# namespace-agnostic element helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element, name):
    if element is None:
        return []
    return [child for child in element if _local(child.tag) == name]


def _path(element, path: str):
    """First element for a '/'-separated local-name path (namespaces ignored)."""
    current = [element] if element is not None else []
    for part in path.split("/"):
        nxt = []
        for item in current:
            nxt.extend(_children(item, part))
        current = nxt
        if not current:
            return None
    return current[0]


def _all(element, path: str) -> list:
    current = [element] if element is not None else []
    for part in path.split("/"):
        nxt = []
        for item in current:
            nxt.extend(_children(item, part))
        current = nxt
    return current


def _descendants(element, name) -> list:
    return [el for el in element.iter() if _local(el.tag) == name] if element is not None else []


def _attr(element, name):
    if element is None:
        return None
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _text(element) -> str | None:
    if element is None:
        return None
    text = "".join(element.itertext()).strip()
    return re.sub(r"\s+", " ", text) or None


def _multilang(elements, preferred=("en", "da", "de")) -> tuple[str | None, str | None]:
    """(text, language) preferring English, then Danish, then German."""
    items = [(el.get(XML_LANG) or "", _text(el)) for el in elements if _text(el)]
    if not items:
        return None, None
    for lang in preferred:
        for code, text in items:
            if code == lang:
                return text, code
    return items[0][1], items[0][0] or None


def _short_description(reference) -> str | None:
    return _multilang(_children(reference, "shortDescription"))[0] if reference is not None else None


# ---------------------------------------------------------------------------
# flow data set (reference product flow)
# ---------------------------------------------------------------------------

def parse_flow_dataset(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    info = {"uuid": _text(_descendants(root, "UUID")[0]) if _descendants(root, "UUID") else None,
            "properties": {}, "material_properties": {}}
    reference_index = _text(_path(root, "flowInformation/quantitativeReference/referenceToReferenceFlowProperty"))
    for flow_property in _all(root, "flowProperties/flowProperty"):
        ref = _path(flow_property, "referenceToFlowPropertyDataSet")
        uuid = (_attr(ref, "refObjectId") or "").lower()
        try:
            value = float(_text(_path(flow_property, "meanValue")) or "nan")
        except ValueError:
            continue
        info["properties"][uuid] = {"value": value, "name": _short_description(ref),
                                    "internal_id": _attr(flow_property, "dataSetInternalID")}
        if _attr(flow_property, "dataSetInternalID") == reference_index:
            info["reference_property"] = uuid

    # MatML material properties (grammage, gross density, conversion factor to 1 kg, …)
    details = {_attr(item, "id"): _text(_path(item, "Name")) for item in _descendants(root, "PropertyDetails")}
    for data in _descendants(root, "PropertyData"):
        name = details.get(_attr(data, "property"))
        try:
            value = float(_text(_path(data, "Data")) or "nan")
        except ValueError:
            continue
        if name:
            info["material_properties"][name.strip().lower()] = value
    return info


# ---------------------------------------------------------------------------
# process data set
# ---------------------------------------------------------------------------

def _amounts(container) -> list[dict]:
    out = []
    for amount in _descendants(container, "amount"):
        raw = _text(amount)
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            value = None
        out.append({"module": _attr(amount, "module"), "scenario": _attr(amount, "scenario"),
                    "raw": raw, "value": value})
    return out


def _add_result(results, info, code, unit, amount, path, confidence=1.0):
    if not amount.get("module"):
        return
    entry = results.setdefault(code, {
        "method_profile": info.get("profile"),
        "canonical_unit": info.get("unit") or unit,
        "modules": {},
        "ilcd_uuid": info.get("uuid"),
    })
    record = {
        "value": amount["value"],
        "status": "declared" if amount["value"] is not None else "not_declared",
        "provenance": "reported",
        "raw_value": amount["raw"],
        "raw_unit": unit,
        "raw_indicator_label": info.get("name_en"),
        "raw_module_header": amount["module"],
        "source_page": None,
        "source_table": path,
        "confidence": confidence,
        "extraction_method": "ilcd_xml",
    }
    module = amount["module"].strip().replace("–", "-")
    scenario = amount.get("scenario")
    if scenario:
        record["scenario"] = scenario
        entry.setdefault("scenario_modules", {}).setdefault(module, {})[scenario] = record
        if module not in entry["modules"]:
            entry["modules"][module] = {**record, "note": f"scenario '{scenario}'"}
    else:
        entry["modules"][module] = record


def parse_process_dataset(xml_bytes: bytes, filename: str, flows: dict | None = None) -> dict:
    root = ET.fromstring(xml_bytes)
    if _local(root.tag) != "processDataSet":
        raise ValueError("Not an ILCD process data set (root element is %s)" % _local(root.tag))

    pi = _path(root, "processInformation")
    dsi = _path(pi, "dataSetInformation")
    mv = _path(root, "modellingAndValidation")
    ai = _path(root, "administrativeInformation")
    metadata: dict = {}
    provenance: dict = {}

    def put(key, value, xpath, confidence=1.0):
        if value in (None, "", [], {}):
            return
        metadata[key] = value
        provenance[key] = evidence(None, xpath, "ilcd_xml", confidence, ilcd_path=xpath)

    put("epd_uuid", _text(_path(dsi, "UUID")), "processDataSet/processInformation/dataSetInformation/UUID")
    name, lang = _multilang(_all(dsi, "name/baseName"))
    put("product_name", name, "processDataSet/processInformation/dataSetInformation/name/baseName")
    classes = []
    for classification in _all(dsi, "classificationInformation/classification"):
        levels = sorted(_children(classification, "class"), key=lambda c: int(_attr(c, "level") or 0))
        path = " / ".join(filter(None, (_text(c) for c in levels)))
        if path:
            system = _attr(classification, "name") or _attr(classification, "classes") or "classification"
            classes.append(f"{system}: {path}")
            ids = [ _attr(c, "classId") for c in levels if _attr(c, "classId")]
            if ids:
                metadata.setdefault("classification_codes", []).append({"system": system, "class_ids": ids,
                                                                        "path": path})
    put("ilcd_classification", classes,
        "processDataSet/processInformation/dataSetInformation/classificationInformation/classification/class")
    put("general_comment", _multilang(_children(dsi, "generalComment"))[0],
        "processDataSet/processInformation/dataSetInformation/generalComment")

    other = _path(dsi, "other")
    rsl = _path(other, "referenceServiceLife")
    years = _attr(rsl, "years") if rsl is not None else None
    if years is None:
        years = _text(_path(other, "serviceLife/years"))
    if years:
        try:
            put("reference_service_life_years", float(years),
                "processDataSet/processInformation/dataSetInformation/other/epd24:referenceServiceLife/@epd24:years")
            metadata["reference_service_life_status"] = "declared"
        except ValueError:
            pass
    else:
        metadata["reference_service_life_status"] = "not_found"
        metadata["reference_service_life_years"] = None

    scenarios = {}
    for scenario in _descendants(other, "scenario"):
        scenarios[_attr(scenario, "name") or f"scenario_{len(scenarios) + 1}"] = {
            "group": _attr(scenario, "group"),
            "default": _attr(scenario, "default") == "true",
            "description": _multilang(_children(scenario, "description"))[0],
        }
    eol = {}
    for data in _descendants(other, "eolScenarioData"):
        item = {}
        for part in ("collection", "recovery", "disposal"):
            element = _path(data, part)
            if element is not None:
                item.update({f"{part}_{_local(k)}": v for k, v in element.attrib.items()})
        eol[_attr(data, "scenario") or f"eol_{len(eol) + 1}"] = item

    # reference flow / declared unit
    reference_id = _text(_path(pi, "quantitativeReference/referenceToReferenceFlow"))
    functional = _multilang(_all(pi, "quantitativeReference/functionalUnitOrOther"))[0]
    reference_exchange = None
    for exchange in _all(root, "exchanges/exchange"):
        if _attr(exchange, "dataSetInternalID") == reference_id:
            reference_exchange = exchange
            break

    physical: dict = {}
    declared_quantity = None
    declared_unit = None
    dimension = None
    if reference_exchange is not None:
        flow_ref = _path(reference_exchange, "referenceToFlowDataSet")
        flow_uuid = (_attr(flow_ref, "refObjectId") or "").lower()
        put("reference_flow", _short_description(flow_ref), "processDataSet/exchanges/exchange/referenceToFlowDataSet")
        try:
            declared_quantity = float(_text(_path(reference_exchange, "meanAmount")) or "nan")
        except ValueError:
            declared_quantity = None
        flow = (flows or {}).get(flow_uuid)
        if flow:
            ref_prop = flow.get("reference_property")
            dimension, declared_unit = FLOW_PROPERTY_UNITS.get(ref_prop, (None, None))
            props = flow["properties"]
            ref_value = props.get(ref_prop, {}).get("value") or 1.0
            mass_prop = props.get("93a60a56-a3c8-11da-a746-0800200b9a66")
            if mass_prop and declared_quantity:
                physical["mass_per_declared_unit_kg"] = round(declared_quantity * mass_prop["value"] / ref_value, 6)
                physical["conversion_factor_to_1kg"] = round(declared_quantity /
                                                             physical["mass_per_declared_unit_kg"], 8)
                physical["mass_formula"] = "ILCD flow properties (mass / reference flow property)"
            for uuid, key in ((BIOGENIC_PRODUCT, "biogenic_carbon_product"),
                              (BIOGENIC_PACKAGING, "biogenic_carbon_packaging")):
                if uuid in props and declared_quantity:
                    physical[key + "_kgC"] = round(declared_quantity * props[uuid]["value"] / ref_value, 6)
            material = flow.get("material_properties", {})
            mapping = {"grammage": "grammage_kg_m2", "gross density": "gross_density_kg_m3",
                       "bulk density": "bulk_density_kg_m3", "layer thickness": "layer_thickness_m",
                       "linear density": "linear_density_kg_m", "weight per piece": "weight_per_piece_kg",
                       "conversion factor to 1 kg": "conversion_factor_to_1kg"}
            for name_key, target in mapping.items():
                if name_key in material:
                    physical.setdefault(target, material[name_key])
    if declared_quantity is not None and declared_unit:
        put("declared_unit_raw", f"{declared_quantity:g} {declared_unit}",
            "processDataSet/processInformation/quantitativeReference/referenceToReferenceFlow")
    elif functional:
        put("declared_unit_raw", functional, "processDataSet/processInformation/quantitativeReference/functionalUnitOrOther",
            0.8)
    if declared_quantity is not None:
        metadata["declared_quantity"] = declared_quantity
    if declared_unit:
        metadata["declared_unit"] = declared_unit
        metadata["declared_unit_dimension"] = dimension

    # time
    put("reference_year", _text(_path(pi, "time/referenceYear")), "processDataSet/processInformation/time/referenceYear")
    publication = _text(next(iter(_descendants(_path(pi, "time"), "publicationDateOfEPD")), None))
    expiration = _text(next(iter(_descendants(_path(pi, "time"), "expirationDateOfEPD")), None))
    valid_year = _text(_path(pi, "time/dataSetValidUntil"))
    put("publication_date", publication, "processDataSet/processInformation/time/other/epd2:publicationDateOfEPD")
    put("valid_until", expiration or valid_year, "processDataSet/processInformation/time/other/epd2:expirationDateOfEPD"
        if expiration else "processDataSet/processInformation/time/dataSetValidUntil")
    if publication and parse_date(publication):
        metadata["publication_date_iso"] = parse_date(publication)["iso"]
    if metadata.get("valid_until") and parse_date(metadata["valid_until"]):
        parsed = parse_date(metadata["valid_until"])
        if parsed.get("precision") == "year":
            parsed["iso"] = f"{metadata['valid_until'][:4]}-12-31"
        metadata["valid_until_iso"] = parsed["iso"]

    # geography / technology
    location = _path(pi, "geography/locationOfOperationSupplyOrProduction")
    put("geography", _attr(location, "location"),
        "processDataSet/processInformation/geography/locationOfOperationSupplyOrProduction/@location")
    put("geography_description", _multilang(_children(location, "descriptionOfRestrictions"))[0],
        "processDataSet/processInformation/geography/locationOfOperationSupplyOrProduction/descriptionOfRestrictions")
    put("product_description", _multilang(_all(pi, "technology/technologicalApplicability"))[0],
        "processDataSet/processInformation/technology/technologicalApplicability")
    put("manufacturing_process", _multilang(_all(pi, "technology/technologyDescriptionAndIncludedProcesses"))[0],
        "processDataSet/processInformation/technology/technologyDescriptionAndIncludedProcesses")

    # modelling and validation
    lci = _path(mv, "LCIMethodAndAllocation")
    put("ilcd_type_of_dataset", _text(_path(lci, "typeOfDataSet")),
        "processDataSet/modellingAndValidation/LCIMethodAndAllocation/typeOfDataSet")
    put("pcr", _short_description(_path(lci, "referenceToLCAMethodDetails")),
        "processDataSet/modellingAndValidation/LCIMethodAndAllocation/referenceToLCAMethodDetails")
    subtype = _text(next(iter(_descendants(lci, "subType")), None))
    put("ilcd_subtype", subtype, "processDataSet/modellingAndValidation/LCIMethodAndAllocation/other/epd:subType")

    dstr = _path(mv, "dataSourcesTreatmentAndRepresentativeness")
    sources = [_short_description(ref) for ref in _children(dstr, "referenceToDataSource")]
    put("background_database", "; ".join(filter(None, sources)),
        "processDataSet/modellingAndValidation/dataSourcesTreatmentAndRepresentativeness/referenceToDataSource")
    put("use_advice", _multilang(_children(dstr, "useAdviceForDataSet"))[0],
        "processDataSet/modellingAndValidation/dataSourcesTreatmentAndRepresentativeness/useAdviceForDataSet")
    manufacturers = [_short_description(_path(m, "contact")) for m in _descendants(dstr, "manufacturer")]
    sites = []
    for site in _descendants(dstr, "site"):
        parts = [_text(_path(site, "name")), _text(_path(site, "streetAddress")), _text(_path(site, "geoCode"))]
        sites.append(", ".join(filter(None, parts)))
    if sites:
        put("production_site", "; ".join(sites),
            "processDataSet/modellingAndValidation/dataSourcesTreatmentAndRepresentativeness/other/epd24:manufacturers/"
            "epd24:manufacturer/epd24:sites/epd24:site")

    review = _path(mv, "validation/review")
    review_type = _attr(review, "type")
    reviewer = _short_description(_path(review, "referenceToNameOfReviewerAndInstitution"))
    put("verifier", reviewer, "processDataSet/modellingAndValidation/validation/review/referenceToNameOfReviewerAndInstitution")
    put("ilcd_review_type", review_type, "processDataSet/modellingAndValidation/validation/review/@type")
    if review_type:
        external = review_type in ("Independent external review", "Accredited third party review",
                                   "Independent review panel")
        metadata["verification_type"] = "External third-party verification" if external else "Internal verification"
        provenance["verification_type"] = evidence(None, review_type, "ilcd_xml", 1.0,
                                                   ilcd_path="processDataSet/modellingAndValidation/validation/review/@type")

    standard_profile = "unresolved"
    compliance_names = []
    for ref in _descendants(_path(mv, "complianceDeclarations"), "referenceToComplianceSystem"):
        system = compliance_system(_attr(ref, "refObjectId"))
        label = _short_description(ref)
        compliance_names.append(system["name"] if system else label)
        if system and system.get("profile"):
            standard_profile = system["profile"]
        elif label and "15804" in label and standard_profile == "unresolved":
            standard_profile = "EN15804_A2_UNKNOWN_EF" if "A2" in label.upper() else "EN15804_A1_CML"
        if (system and system.get("standard") == "ISO 14025") or (label and "14025" in label):
            metadata["iso14025_mentioned"] = True
    put("compliance_statement", "; ".join(filter(None, compliance_names)),
        "processDataSet/modellingAndValidation/complianceDeclarations/compliance/referenceToComplianceSystem")

    # administrative information
    po = _path(ai, "publicationAndOwnership")
    put("registration_number", _text(_path(po, "registrationNumber")),
        "processDataSet/administrativeInformation/publicationAndOwnership/registrationNumber")
    put("dataset_version", _text(_path(po, "dataSetVersion")),
        "processDataSet/administrativeInformation/publicationAndOwnership/dataSetVersion")
    put("permanent_uri", _text(_path(po, "permanentDataSetURI")),
        "processDataSet/administrativeInformation/publicationAndOwnership/permanentDataSetURI")
    publisher = _short_description(next(iter(_descendants(po, "referenceToPublisher")), None))
    authority = _short_description(_path(po, "referenceToRegistrationAuthority"))
    put("programme_operator", publisher or authority,
        "processDataSet/administrativeInformation/publicationAndOwnership/other/referenceToPublisher"
        if publisher else "processDataSet/administrativeInformation/publicationAndOwnership/referenceToRegistrationAuthority")
    owner = _short_description(_path(po, "referenceToOwnershipOfDataSet"))
    commissioner = _short_description(_path(ai, "commissionerAndGoal/referenceToCommissioner"))
    put("manufacturer", next((m for m in manufacturers if m), None) or owner or commissioner,
        "processDataSet/modellingAndValidation/dataSourcesTreatmentAndRepresentativeness/other/epd24:manufacturers"
        if any(manufacturers) else "processDataSet/administrativeInformation/publicationAndOwnership/"
                                   "referenceToOwnershipOfDataSet")
    put("language", {"en": "English", "de": "German", "da": "Danish"}.get(lang, lang),
        "processDataSet/processInformation/dataSetInformation/name/baseName/@xml:lang")

    # results
    results: dict = {}
    unknown = []
    for lcia in _all(root, "LCIAResults/LCIAResult"):
        ref = _path(lcia, "referenceToLCIAMethodDataSet")
        uuid = (_attr(ref, "refObjectId") or "").lower()
        info = indicator_by_uuid(uuid)
        unit = _short_description(next(iter(_descendants(lcia, "referenceToUnitGroupDataSet")), None))
        if not info:
            unknown.append({"uuid": uuid, "name": _short_description(ref)})
            continue
        details = indicator_info(info["code"])
        payload = {**info, "uuid": uuid, "unit": details.get("unit") or info.get("unit"),
                   "profile": (info.get("profiles") or [None])[0]}
        for amount in _amounts(lcia):
            _add_result(results, payload, info["code"], unit, amount, "LCIAResults/LCIAResult")
        if standard_profile == "unresolved":
            profiles = info.get("profiles") or []
            if any(p.startswith("EN15804_A2") for p in profiles):
                standard_profile = "EN15804_A2_EF3.1" if profiles == ["EN15804_A2_EF3.1"] else "EN15804_A2_UNKNOWN_EF"
            elif "EN15804_A1" in profiles:
                standard_profile = "EN15804_A1_CML"
    for exchange in _all(root, "exchanges/exchange"):
        ref = _path(exchange, "referenceToFlowDataSet")
        uuid = (_attr(ref, "refObjectId") or "").lower()
        amounts = _amounts(exchange)
        if not amounts:
            continue
        info = indicator_by_uuid(uuid)
        unit = _short_description(next(iter(_descendants(exchange, "referenceToUnitGroupDataSet")), None))
        if not info:
            unknown.append({"uuid": uuid, "name": _short_description(ref)})
            continue
        details = indicator_info(info["code"])
        payload = {**info, "uuid": uuid, "unit": details.get("unit") or info.get("unit"), "profile": "shared"}
        for amount in amounts:
            _add_result(results, payload, info["code"], unit, amount, "exchanges/exchange")

    # When several scenarios exist for a module, the ILCD default scenario fills the module slot.
    defaults = {name for name, item in scenarios.items() if item.get("default")}
    for data in results.values():
        for module, variants in data.get("scenario_modules", {}).items():
            chosen = next((name for name in variants if name in defaults), None)
            if chosen:
                data["modules"][module] = {**variants[chosen], "note": f"default scenario '{chosen}'"}

    metadata["standard_profile"] = standard_profile
    metadata["standard"] = ("EN 15804+A2" if standard_profile.startswith("EN15804_A2")
                            else "EN 15804+A1" if standard_profile == "EN15804_A1_CML" else None)
    declarations = {}
    for data in results.values():
        for module in data.get("modules", {}):
            declarations.setdefault(module, {"status": "X", "source_page": None, "source_table": None,
                                             "extraction_method": "ilcd_xml (module present)"})

    text_parts = [metadata.get(k) for k in ("product_name", "product_description", "general_comment",
                                            "manufacturing_process", "use_advice", "geography_description")]
    text_parts += [s.get("description") for s in scenarios.values()]
    return {
        "metadata": metadata,
        "metadata_provenance": provenance,
        "physical_properties": physical,
        "results": results,
        "module_declarations": declarations,
        "scenarios": {"ilcd_scenarios": scenarios, "EOL_common": eol} if (scenarios or eol) else {},
        "unknown_indicators": unknown,
        "document_text": "\n".join(filter(None, text_parts)),
        "source_path": filename,
    }


def parse_ilcd_bytes(data: bytes, filename: str) -> list[dict]:
    """Parse a process XML or an ILCD ZIP archive; returns one parsed dict per process data set."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        archive = zipfile.ZipFile(io.BytesIO(data))
        flows = {}
        processes = []
        for info in archive.infolist():
            name = info.filename
            lower = name.lower()
            # Guard against oversized members (zip bombs); ILCD data sets are small XML files.
            if not lower.endswith(".xml") or info.file_size > MAX_XML_BYTES:
                continue
            if "/flows/" in lower or lower.startswith("flows/"):
                try:
                    flow = parse_flow_dataset(archive.read(name))
                    if flow.get("uuid"):
                        flows[flow["uuid"].lower()] = flow
                except ET.ParseError:
                    continue
            elif "/processes/" in lower or lower.startswith("processes/"):
                processes.append(name)
        return [parse_process_dataset(archive.read(name), name, flows)
                for name in processes[:MAX_PROCESSES_PER_ARCHIVE]]
    return [parse_process_dataset(data, filename, {})]
