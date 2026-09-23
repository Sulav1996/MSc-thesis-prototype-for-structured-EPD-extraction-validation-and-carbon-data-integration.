"""
Step 1 — standard EPD information for DGNB / Danish LCA.

``evaluate_step1(record, full_text)`` turns an extracted (or edited) record into
the Step 1 checklist of "Claude instruction 1": core standard, verification,
programme operator, RSL, conversion factor, life-cycle modules, environmental
indicators, resource & waste indicators, Danish data-quality checks and the
Danish CO2 reporting unit.

Every parameter gets a status:
    pass     – requirement met, value found
    warning  – found, but needs attention (e.g. EN 15804+A1, green electricity)
    fail     – requirement not met (e.g. internal verification only)
    missing  – not found in the document (manual review)
    info     – informative / derived value
The function never invents EPD data. Derived numbers (A1-A3 sum from a split,
annualised GWP) are labelled with provenance "calculated" and a formula.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

from dictionary import (
    dgnb_module_scope,
    eco_platform_operators,
    load_dictionary,
    normalize_text,
    profile_family,
    step1_profile,
)
from extraction.common import sentence_around

STATUS_ORDER = {"fail": 0, "missing": 1, "warning": 2, "info": 3, "pass": 4}


def _param(pid, value=None, status="missing", message="", evidence=None, **extra):
    spec = next((p for p in step1_profile().get("parameters", []) if p["id"] == pid), {})
    item = {
        "id": pid,
        "category": spec.get("category"),
        "parameter": spec.get("parameter"),
        "requirement": spec.get("requirement"),
        "rule": spec.get("rule"),
        "value": value,
        "status": status,
        "message": message,
        "evidence": evidence,
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# programme operator (ECO Platform)
# ---------------------------------------------------------------------------

def match_programme_operator(operator_text: str | None, registration_number: str | None,
                             head_text: str = "") -> dict:
    """Match against the ECO Platform programme-operator list in the dictionary."""
    registry = eco_platform_operators()
    best = None

    def alias_hit(alias, text):
        if not text:
            return False
        if len(alias) <= 5:  # short acronyms: case-sensitive, whole word
            return re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text) is not None
        return normalize_text(alias) in normalize_text(text)

    for operator in registry.get("operators", []):
        for source, text, weight in (("programme_operator_field", operator_text, 0.97),
                                     ("document_text", head_text, 0.8)):
            if any(alias_hit(alias, text) for alias in operator.get("aliases", [])):
                if not best or weight > best["confidence"]:
                    best = {"matched": True, "operator_id": operator["id"], "name": operator["name"],
                            "country": operator["country"], "method": source, "confidence": weight}
        for pattern in operator.get("registration_patterns", []):
            if registration_number and re.search(pattern, registration_number.strip(), re.IGNORECASE):
                if not best or 0.9 > best["confidence"]:
                    best = {"matched": True, "operator_id": operator["id"], "name": operator["name"],
                            "country": operator["country"], "method": "registration_number_pattern",
                            "confidence": 0.9}
    if best:
        best["list_checked"] = registry.get("list_checked")
        return best
    return {"matched": False, "list_checked": registry.get("list_checked")}


# ---------------------------------------------------------------------------
# text pattern checks
# ---------------------------------------------------------------------------

def _first_pattern(text: str, patterns: list[str]):
    low = text.lower()
    best = None
    for pattern in patterns:
        index = low.find(pattern.lower())
        if index != -1 and (best is None or index < best[0]):
            best = (index, index + len(pattern), pattern)
    return best


def classify_electricity_mix(full_text: str) -> dict:
    patterns = step1_profile().get("electricity_mix_patterns", {})
    for mix_type in ("residual_mix", "market_based_specific", "location_based_grid"):
        hit = _first_pattern(full_text, patterns.get(mix_type, []))
        if hit:
            # "electricity mix" alone is generic; require the word electricity/strom nearby for grid mixes.
            snippet = sentence_around(full_text, hit[0], hit[1])
            if mix_type == "location_based_grid" and not re.search(r"electric|strom|power|grid|el-?", snippet, re.I):
                continue
            return {"type": mix_type, "matched_term": hit[2], "snippet": snippet}
    return {"type": None}


def classify_data_specificity(full_text: str, metadata: dict) -> dict:
    patterns = step1_profile().get("data_specificity_patterns", {})
    low = full_text.lower()
    hits = {}
    for level, terms in patterns.items():
        for term in terms:
            index = low.find(term)
            if index != -1:
                hits.setdefault(level, (index, index + len(term), term))
    subtype = metadata.get("ilcd_subtype")
    if subtype:
        mapping = {"specific dataset": "product_specific", "average dataset": "manufacturer_group",
                   "representative dataset": "industry_average", "template dataset": "industry_average",
                   "generic dataset": "generic"}
        return {"level": mapping.get(subtype, "unknown"), "ilcd_subtype": subtype,
                "snippet": f"ILCD+EPD epd:subType = {subtype}", "method": "ilcd_xml"}
    for level in ("industry_average", "generic", "manufacturer_group", "product_specific"):
        if level in hits:
            index, end, term = hits[level]
            ilcd = {"product_specific": "specific dataset", "manufacturer_group": "average dataset",
                    "industry_average": "average dataset", "generic": "generic dataset"}[level]
            return {"level": level, "ilcd_subtype": ilcd, "matched_term": term,
                    "snippet": sentence_around(full_text, index, end), "method": "text_pattern"}
    if metadata.get("manufacturer"):
        return {"level": "product_specific", "ilcd_subtype": "specific dataset",
                "snippet": "Single manufacturer named; no average/product-group wording found.",
                "method": "default_single_manufacturer"}
    return {"level": "unknown"}


EOL_TERMS = ["end of life", "end-of-life", "c1-c4", "waste processing", "disposal", "landfill",
             "incineration", "recycling", "waste treatment", "end of the product", "lifespan"]
OTHER_GEOGRAPHIES = ["europe", "european", "eu-27", "eu27", "germany", "german", "global", "sweden", "norway",
                     "finland", "poland", "france", "united kingdom", "netherlands"]


def classify_waste_context(full_text: str, geography: str | None) -> dict:
    """Is the end-of-life scenario Danish? Only sentences that talk about end of life are inspected."""
    danish = step1_profile().get("danish_context_patterns", [])
    low = full_text.lower()
    sentences = []
    for term in EOL_TERMS:
        for match in re.finditer(re.escape(term), low):
            sentences.append(sentence_around(full_text, match.start(), match.end(), limit=400))
    for sentence in sentences:
        padded = f" {sentence.lower()} "
        if any(pattern in padded for pattern in danish):
            return {"context": "danish", "snippet": sentence}
    for sentence in sentences:
        for geo in OTHER_GEOGRAPHIES:
            if re.search(rf"\b{re.escape(geo)}\b", sentence.lower()):
                return {"context": "other", "geography": geo, "snippet": sentence}
    if geography:
        is_dk = "denmark" in geography.lower() or geography.strip().upper() == "DK"
        return {"context": "danish" if is_dk else "other", "geography": geography,
                "snippet": f"Geographical scope: {geography}"}
    return {"context": None}


# ---------------------------------------------------------------------------
# indicator helpers
# ---------------------------------------------------------------------------

def _gwp_code(results: dict, family: str | None) -> str | None:
    if family == "EN15804_A1":
        return "GWP" if "GWP" in results else ("GWP-total" if "GWP-total" in results else None)
    return "GWP-total" if "GWP-total" in results else ("GWP" if "GWP" in results else None)


def module_value(results: dict, code: str | None, module: str):
    if not code:
        return None
    record = results.get(code, {}).get("modules", {}).get(module)
    return record.get("value") if record else None


def product_stage_value(results: dict, code: str | None) -> dict | None:
    """Reported A1-A3, or A1+A2+A3 calculated from a complete split (never invented)."""
    if not code:
        return None
    reported = module_value(results, code, "A1-A3")
    if reported is not None:
        return {"value": reported, "provenance": "reported"}
    parts = [module_value(results, code, module) for module in ("A1", "A2", "A3")]
    if all(part is not None for part in parts):
        return {"value": sum(parts), "provenance": "calculated_from_split",
                "formula": "A1 + A2 + A3 (EPD reports the split only)"}
    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def evaluate_step1(record: dict, full_text: str = "", reference_period: int | None = None) -> dict:
    profile = step1_profile()
    reference_period = reference_period or profile.get("reference_study_period_years", 50)
    metadata = record.get("metadata", {})
    results = record.get("results", {})
    declarations = record.get("module_declarations", {})
    physical = record.get("physical_properties", {})
    standard_profile = metadata.get("standard_profile") or record.get("document", {}).get("standard_profile")
    family = profile_family(standard_profile)
    head_text = full_text[:6000]
    params = {}

    # 1. Core standard
    if family == "EN15804_A2":
        params["core_standard"] = _param("core_standard", "EN 15804+A2", "pass", "Prepared according to EN 15804+A2.",
                                         record.get("metadata_provenance", {}).get("standard"))
    elif family == "EN15804_A1":
        params["core_standard"] = _param("core_standard", "EN 15804+A1", "warning",
                                         "EN 15804+A1 is only accepted during the transition period; "
                                         "results are not comparable with +A2.",
                                         record.get("metadata_provenance", {}).get("standard"))
    else:
        params["core_standard"] = _param("core_standard", None, "missing",
                                         "EN 15804 version not found — check the EPD cover/standard statement.")

    # 2. Verification
    vtype = metadata.get("verification_type")
    verifier = metadata.get("verifier")
    iso = metadata.get("iso14025_mentioned")
    value = {"verification_type": vtype, "verifier": verifier, "iso14025": iso}
    ev = record.get("metadata_provenance", {}).get("verification_type")
    from_ilcd = record.get("document", {}).get("source_format") in ("ilcd_xml", "ilcd_zip")
    if vtype and "external" in vtype.lower():
        if iso:
            msg, status = "Independent external verification according to ISO 14025.", "pass"
        elif from_ilcd:
            msg = (f"ILCD+EPD review type '{metadata.get('ilcd_review_type')}' (external). "
                   "ISO 14025 is implied for ILCD+EPD 'EPD' data sets — confirm in the PDF if needed.")
            status = "pass"
        else:
            msg, status = "External verification found, but ISO 14025 is not named explicitly — confirm.", "warning"
        params["third_party_verification"] = _param("third_party_verification", value, status, msg, ev)
    elif vtype and "internal" in vtype.lower():
        params["third_party_verification"] = _param("third_party_verification", value, "fail",
                                                    "Only internal verification — DGNB requires third-party "
                                                    "verification according to ISO 14025.", ev)
    elif iso or verifier:
        params["third_party_verification"] = _param("third_party_verification", value, "warning",
                                                    "ISO 14025 / verifier found, but internal vs external "
                                                    "verification could not be confirmed.", ev)
    else:
        params["third_party_verification"] = _param("third_party_verification", value, "missing",
                                                    "No verification statement found.")

    # 3. Programme operator
    operator = match_programme_operator(metadata.get("programme_operator"), metadata.get("registration_number"),
                                        head_text)
    op_value = {"programme_operator": metadata.get("programme_operator"), **operator}
    if operator.get("matched"):
        params["programme_operator"] = _param("programme_operator", op_value, "pass",
                                              f"{operator['name']} is an ECO Platform EPD programme "
                                              f"(list checked {operator.get('list_checked')}).",
                                              {"extraction_method": operator["method"],
                                               "confidence": operator["confidence"]})
    elif metadata.get("programme_operator"):
        params["programme_operator"] = _param("programme_operator", op_value, "warning",
                                              "Programme operator not found in the ECO Platform list — "
                                              "check acceptance manually.")
    else:
        params["programme_operator"] = _param("programme_operator", op_value, "missing",
                                              "Programme operator not found.")

    # 4. Reference service life
    rsl = metadata.get("reference_service_life_years")
    rsl_status = metadata.get("reference_service_life_status")
    rsl_ev = record.get("metadata_provenance", {}).get("reference_service_life_years")
    if isinstance(rsl, (int, float)) and rsl > 0:
        params["reference_service_life"] = _param("reference_service_life", {"years": rsl}, "pass",
                                                  f"RSL = {rsl:g} years.", rsl_ev)
    elif rsl_status == "not_defined":
        params["reference_service_life"] = _param("reference_service_life", {"years": None, "status": "not_defined"},
                                                  "warning", "The EPD states that no RSL is defined; use the "
                                                  "national service-life table or a project assumption for B4.",
                                                  rsl_ev)
    else:
        params["reference_service_life"] = _param("reference_service_life", None, "missing", "RSL not found.")

    # 5. Unit conversion factor
    mass = physical.get("mass_per_declared_unit_kg")
    factor = physical.get("conversion_factor_to_1kg")
    cf_value = {"declared_unit": metadata.get("declared_unit_raw"), "mass_per_declared_unit_kg": mass,
                "conversion_factor_to_1kg": factor, "formula": physical.get("mass_formula")}
    if mass or factor:
        params["unit_conversion_factor"] = _param(
            "unit_conversion_factor", cf_value, "pass",
            (f"{mass:g} kg per declared unit; " if mass else "") +
            (f"conversion factor to 1 kg f = {factor:.6g} (ILCD: M / f = m)." if factor else ""),
            record.get("metadata_provenance", {}).get("physical_properties.conversion_factor_to_1kg"))
    elif metadata.get("declared_unit_raw"):
        params["unit_conversion_factor"] = _param("unit_conversion_factor", cf_value, "missing",
                                                  "Declared unit found, but no mass/density/conversion factor.")
    else:
        params["unit_conversion_factor"] = _param("unit_conversion_factor", cf_value, "missing",
                                                  "Declared unit and conversion factor not found.")

    # 6. Life-cycle modules (DGNB scope)
    gwp = _gwp_code(results, family)
    scope = dgnb_module_scope()
    labels = load_dictionary().get("dgnb_module_scope_labels", {})
    modules = []
    for module in ["A1-A3", "A4", "A5", "B1", "B2", "B3", "B4", "B5", "B6", "B7", "C1", "C2", "C3", "C4", "D"]:
        if module == "A1-A3":
            stage = product_stage_value(results, gwp)
            declared = "X" if stage else "; ".join(sorted({declarations.get(m, {}).get("status", "")
                                                          for m in ("A1", "A2", "A3")} - {""})) or None
            gwp_value = stage["value"] if stage else None
            provenance = stage["provenance"] if stage else None
        else:
            declared = declarations.get(module, {}).get("status")
            gwp_value = module_value(results, gwp, module)
            provenance = "reported" if gwp_value is not None else None
            if declared is None and gwp_value is not None:
                declared = "X (derived from results)"
        modules.append({
            "module": module,
            "dgnb_scope": scope.get(module),
            "dgnb_scope_label": labels.get(scope.get(module), scope.get(module)),
            "declared": declared,
            "gwp_indicator": gwp,
            "gwp_value": gwp_value,
            "value_provenance": provenance,
        })
    stage = product_stage_value(results, gwp)
    not_declared = [m["module"] for m in modules
                    if m["dgnb_scope"] in ("included",) and m["gwp_value"] is None]
    if stage:
        msg = "A1-A3 available" + (" (calculated from the reported A1/A2/A3 split)"
                                   if stage["provenance"] != "reported" else " (reported)") + "."
        if not_declared:
            msg += (" DGNB-included modules without EPD values: " + ", ".join(not_declared) +
                    " — use project data (A4/A5), RSL-based replacements (B4) and the energy calculation (B6).")
        params["life_cycle_modules"] = _param("life_cycle_modules", {"available": [m["module"] for m in modules
                                                                                     if m["gwp_value"] is not None],
                                                                       "missing_included": not_declared},
                                              "pass" if not not_declared else "warning", msg)
    else:
        params["life_cycle_modules"] = _param("life_cycle_modules", None, "fail" if results else "missing",
                                              "Mandatory product stage A1-A3 (GWP) not found.")

    # 7-8. Indicator groups
    indicator_rows = []
    for group_id in ("environmental_impact_indicators", "resource_waste_indicators"):
        spec = next((p for p in profile.get("parameters", []) if p["id"] == group_id), {})
        group_status = []
        group_value = {}
        for indicator in spec.get("indicators", []):
            codes = indicator.get("codes", {}).get(family or "EN15804_A2", [])
            present = [code for code in codes if results.get(code, {}).get("modules")]
            primary = indicator.get("primary", {}).get(family or "EN15804_A2")
            for code in codes:
                data = results.get(code, {})
                row = {"group": group_id, "indicator_id": indicator["id"], "label": indicator["label"],
                       "code": code, "requirement": indicator.get("requirement"),
                       "unit": data.get("canonical_unit"), "present": code in present}
                for module in ("A1-A3", "A4", "A5", "B4", "B6", "C3", "C4", "D"):
                    if module == "A1-A3":
                        stage_value = product_stage_value(results, code) if code in present else None
                        row[module] = stage_value["value"] if stage_value else None
                    else:
                        row[module] = module_value(results, code, module)
                indicator_rows.append(row)
            if len(present) == len(codes) and codes:
                status = "pass"
            elif present:
                status = "warning"
            else:
                status = "missing"
            group_status.append(status)
            group_value[indicator["id"]] = {"codes": codes, "present": present, "primary": primary,
                                            "status": status}
        if group_status:
            worst = min(group_status, key=lambda s: STATUS_ORDER[s])
            missing = [k for k, v in group_value.items() if v["status"] != "pass"]
            msg = "All required indicators extracted." if worst == "pass" else \
                "Incomplete: " + ", ".join(missing) + " — check the EPD tables."
            params[group_id] = _param(group_id, group_value, worst, msg)

    # 9. Electricity mix
    mix = classify_electricity_mix(full_text)
    if mix.get("type") == "residual_mix":
        params["electricity_mix"] = _param("electricity_mix", "residual mix", "pass",
                                           "Electricity modelled with a residual mix.",
                                           {"source_text": mix["snippet"], "extraction_method": "text_pattern"})
    elif mix.get("type") == "market_based_specific":
        params["electricity_mix"] = _param("electricity_mix", "supplier-specific / green electricity", "warning",
                                           "Supplier-specific or certified green electricity used — DGNB (DK) "
                                           "expects the residual mix; check whether the EPD is accepted as is.",
                                           {"source_text": mix["snippet"], "extraction_method": "text_pattern"})
    elif mix.get("type") == "location_based_grid":
        params["electricity_mix"] = _param("electricity_mix", "national / grid average mix", "warning",
                                           "Grid (location-based) electricity mix used, not the residual mix.",
                                           {"source_text": mix["snippet"], "extraction_method": "text_pattern"})
    else:
        params["electricity_mix"] = _param("electricity_mix", None, "missing",
                                           "Electricity modelling not described in the extracted text.")

    # 10. Product-specific data
    spec_level = classify_data_specificity(full_text, metadata)
    level = spec_level.get("level")
    labels_level = {"product_specific": "Product-specific (single product, one manufacturer)",
                    "manufacturer_group": "Manufacturer-specific (product group / several sites)",
                    "industry_average": "Industry / sector average",
                    "generic": "Generic data"}
    status = {"product_specific": "pass", "manufacturer_group": "pass", "industry_average": "warning",
              "generic": "warning"}.get(level, "missing")
    message = {
        "product_specific": "Product-specific EPD — highest data priority.",
        "manufacturer_group": "Manufacturer-specific EPD for a product group (representative / worst-case "
                              "product) — accepted as specific data; check that the chosen variant is covered.",
        "industry_average": "Average / sector EPD — lower priority than product-specific data.",
        "generic": "Generic data — lowest priority; replace with a product-specific EPD when possible.",
    }.get(level, "Data specificity could not be determined.")
    params["product_specific_data"] = _param("product_specific_data",
                                             {"level": level, "label": labels_level.get(level),
                                              "ilcd_subtype": spec_level.get("ilcd_subtype")},
                                             status, message,
                                             {"source_text": spec_level.get("snippet"),
                                              "extraction_method": spec_level.get("method")})

    # 11. Waste treatment context
    waste = classify_waste_context(full_text, metadata.get("geography"))
    if waste.get("context") == "danish":
        params["waste_treatment_context"] = _param("waste_treatment_context", "Danish", "pass",
                                                   "End-of-life scenario refers to Denmark.",
                                                   {"source_text": waste.get("snippet")})
    elif waste.get("context") == "other":
        params["waste_treatment_context"] = _param("waste_treatment_context", waste.get("geography"), "warning",
                                                   "End-of-life scenario is not Danish "
                                                   f"({waste.get('geography')}); adapt C3/C4/D to Danish waste "
                                                   "treatment for the project LCA.",
                                                   {"source_text": waste.get("snippet")})
    else:
        params["waste_treatment_context"] = _param("waste_treatment_context", None, "missing",
                                                   "End-of-life geography not stated.")

    # 12. Danish reporting unit (kg CO2e / m2 / year)
    params["co2_reporting_unit"] = _param("co2_reporting_unit", danish_reporting_helper(record, gwp,
                                                                                         reference_period),
                                          "info", f"Building results are reported in "
                                                  f"{profile.get('reporting_unit')} over {reference_period} years. "
                                                  "Values below are per declared unit; divide by the heated floor "
                                                  "area after multiplying by the project quantity.")

    counts = {status: sum(1 for p in params.values() if p["status"] == status)
              for status in ("pass", "warning", "fail", "missing", "info")}
    mandatory_ok = all(params[p]["status"] in ("pass", "warning")
                       for p in ("core_standard", "third_party_verification", "life_cycle_modules")
                       if p in params)
    validity = _validity(metadata)
    return {
        "profile": profile.get("id"),
        "evaluated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "standard_family": family,
        "gwp_indicator": gwp,
        "parameters": params,
        "modules": modules,
        "indicators": indicator_rows,
        "validity": validity,
        "summary": {**counts, "dgnb_mandatory_met": mandatory_ok,
                    "eco_platform_member": bool(operator.get("matched")),
                    "valid_today": validity.get("valid_today")},
    }


def _validity(metadata: dict) -> dict:
    iso = metadata.get("valid_until_iso")
    if not iso:
        return {"valid_until": None, "valid_today": None}
    try:
        valid_until = date.fromisoformat(iso)
    except ValueError:
        return {"valid_until": iso, "valid_today": None}
    return {"valid_until": iso, "valid_today": valid_until >= date.today(),
            "days_remaining": (valid_until - date.today()).days}


def danish_reporting_helper(record: dict, gwp_code: str | None, reference_period: int = 50) -> dict:
    """
    Annualised GWP per declared unit for the modules in the Danish/DGNB scope.

    Indicative only: building LCAs multiply by project quantities and divide by the
    heated floor area. B4 uses N = ceil(period / RSL) - 1 replacements of
    (A1-A3 + C3 + C4) when an RSL is declared; D is always reported separately.
    """
    results = record.get("results", {})
    metadata = record.get("metadata", {})
    stage = product_stage_value(results, gwp_code)
    parts, missing = {}, []
    if stage:
        parts["A1-A3"] = stage["value"]
    else:
        missing.append("A1-A3")
    for module in ("A4", "A5", "C3", "C4"):
        value = module_value(results, gwp_code, module)
        if value is None:
            missing.append(module)
        else:
            parts[module] = value

    rsl = metadata.get("reference_service_life_years")
    b4 = None
    replacements = None
    if isinstance(rsl, (int, float)) and rsl > 0 and "A1-A3" in parts:
        replacements = max(0, math.ceil(reference_period / rsl) - 1)
        b4 = replacements * (parts.get("A1-A3", 0) + parts.get("C3", 0) + parts.get("C4", 0))
        parts["B4 (indicative)"] = b4
    total = sum(parts.values()) if parts else None
    d_value = module_value(results, gwp_code, "D")
    declared = metadata.get("declared_unit_raw")
    return {
        "reporting_unit": "kg CO2e/m2/year",
        "reference_study_period_years": reference_period,
        "gwp_indicator": gwp_code,
        "declared_unit": declared,
        "per_declared_unit_total": total,
        "per_declared_unit_per_year": (total / reference_period) if total is not None else None,
        "module_values": parts,
        "modules_without_epd_value": missing,
        "b4_replacements": replacements,
        "b6": "From the building energy calculation (BE18/BR), not from the product EPD.",
        "module_d_per_declared_unit": d_value,
        "module_d_note": "Module D is reported separately and never subtracted.",
        "formula": "Σ(A1-A3, A4, A5, B4, C3, C4) / reference study period — per declared unit",
        "derived": True,
    }
