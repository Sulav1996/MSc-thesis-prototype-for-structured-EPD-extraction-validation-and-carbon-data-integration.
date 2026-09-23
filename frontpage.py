"""
EPD Carbon Prototype — Streamlit app.

Upload & Extract   PDF or ILCD+EPD XML/ZIP → Step 1 (DGNB) + Step 2 (category) → review → save
EPD Database       search / filter / fetch / compare / export stored EPDs
Dictionary         ILCD-based Step 3 dictionary, DGNB profile, categories, indicator UUIDs
Storage            backend status, Google Drive sync, bundles (transfer between laptop and Render)
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

st.set_page_config(page_title="EPD Carbon Prototype", page_icon="🏗️", layout="wide")

from dictionary import (  # noqa: E402
    dgnb_module_scope,
    eco_platform_operators,
    load_dictionary,
    material_categories,
    step1_profile,
    step3_sections,
)
from epd_store import get_store, load_settings  # noqa: E402
from epd_store.transfer import jsonl, results_csv, summary_csv  # noqa: E402
from extraction.categorizer import apply_category_override, category_options  # noqa: E402
from extraction.extractor import extract_epd_records, refresh_derived_blocks  # noqa: E402
from extraction.metadata import parse_declared_unit  # noqa: E402
from validation.validator import validate_epd  # noqa: E402

STATUS_ICON = {"pass": "✅ pass", "warning": "⚠️ warning", "fail": "❌ fail", "missing": "❓ missing",
               "info": "ℹ️ info"}
GWP_MODULES = ["A1-A3", "A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3", "B4", "B5", "B6", "B7",
               "C1", "C2", "C3", "C4", "D"]


# ---------------------------------------------------------------------------
# cached resources
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Opening the EPD store…")
def open_store():
    try:
        store = get_store()
        backend_error = None
    except Exception as error:  # e.g. invalid Google Drive credentials
        store = get_store(load_settings(backend="local"))
        backend_error = str(error)
    migrated = store.migrate_legacy_json()
    return store, backend_error, migrated


@st.cache_data(show_spinner=False, max_entries=6)
def extract_cached(file_bytes: bytes, file_name: str):
    return extract_epd_records(file_bytes, file_name)


store, BACKEND_ERROR, MIGRATED = open_store()

st.title("EPD Carbon Prototype")
st.caption("Upload → Extract (Step 1 DGNB parameters) → Categorise (Step 2) → Human review → Store. "
           "Dictionary: ILCD+EPD v1.3 based (Step 3). No external AI API is used.")
if BACKEND_ERROR:
    st.error(f"Cloud storage could not be opened, using the local store instead: {BACKEND_ERROR}")
if MIGRATED:
    st.info(f"{MIGRATED} record(s) from data/epds.json were migrated into the store (schema 2.0).")
if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

upload_tab, database_tab, dictionary_tab, storage_tab = st.tabs(
    ["Upload & Extract", "EPD Database", "Dictionary (ILCD)", "Storage & Transfer"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def fmt(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {fmt(v)}" for k, v in value.items() if v not in (None, "", [], {}))[:300]
    if isinstance(value, list):
        return ", ".join(map(str, value))[:300]
    return str(value)


def step1_table(record: dict) -> pd.DataFrame:
    rows = []
    for parameter in record.get("step1", {}).get("parameters", {}).values():
        evidence = parameter.get("evidence") or {}
        rows.append({
            "Category": parameter.get("category"),
            "Parameter": parameter.get("parameter"),
            "Requirement": parameter.get("requirement"),
            "Status": STATUS_ICON.get(parameter.get("status"), parameter.get("status")),
            "Extracted value": fmt(parameter.get("value")),
            "Assessment": parameter.get("message"),
            "Evidence": fmt(evidence.get("source_text")) if isinstance(evidence, dict) else "—",
            "Page": evidence.get("source_page") if isinstance(evidence, dict) else None,
        })
    return pd.DataFrame(rows)


def module_table(record: dict) -> pd.DataFrame:
    rows = []
    for module in record.get("step1", {}).get("modules", []):
        rows.append({
            "Module": module["module"],
            "DGNB scope": module.get("dgnb_scope_label"),
            "Declared in EPD": module.get("declared") or "—",
            f"{module.get('gwp_indicator') or 'GWP'} (per DU)": module.get("gwp_value"),
            "Value source": module.get("value_provenance") or "—",
        })
    return pd.DataFrame(rows)


def indicator_table(record: dict) -> pd.DataFrame:
    rows = record.get("step1", {}).get("indicators", [])
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    columns = ["label", "code", "unit", "present", "A1-A3", "A4", "A5", "B4", "B6", "C3", "C4", "D"]
    frame = frame[[c for c in columns if c in frame.columns]]
    return frame.rename(columns={"label": "Indicator group", "code": "Indicator", "unit": "Unit",
                                 "present": "Extracted"})


def gwp_indicators(record: dict) -> list[str]:
    results = record.get("results", {})
    if record.get("metadata", {}).get("standard_profile") == "EN15804_A1_CML" or (
            "GWP" in results and "GWP-total" not in results):
        return ["GWP"]
    return ["GWP-total", "GWP-fossil", "GWP-biogenic", "GWP-luluc"]


def gwp_frame(record: dict) -> tuple[pd.DataFrame, list[str]]:
    results = record.get("results", {})
    indicators = gwp_indicators(record)
    declarations = record.get("module_declarations", {})
    scope = dgnb_module_scope()
    labels = load_dictionary().get("dgnb_module_scope_labels", {})
    rows = []
    for module in GWP_MODULES:
        scope_key = scope.get(module) or (scope.get("A1-A3") if module in ("A1", "A2", "A3") else None)
        row = {"Module": module, "Declaration": declarations.get(module, {}).get("status", ""),
               "DGNB scope": labels.get(scope_key, "") if scope_key else ""}
        for indicator in indicators:
            value = results.get(indicator, {}).get("modules", {}).get(module, {}).get("value")
            row[indicator] = float("nan") if value is None else value
        rows.append(row)
    return pd.DataFrame(rows), indicators


def apply_gwp_edits(record: dict, edited: pd.DataFrame, indicators: list[str]) -> None:
    results = record.setdefault("results", {})
    profile = record.get("metadata", {}).get("standard_profile", "unresolved")
    for _, row in edited.iterrows():
        module = str(row["Module"])
        for indicator in indicators:
            raw = row[indicator]
            value = None if pd.isna(raw) else float(raw)
            entry = results.setdefault(indicator, {"method_profile": profile, "canonical_unit": "kg CO2 eq",
                                                   "modules": {}})
            existing = entry["modules"].get(module)
            if existing is None and value is None:
                continue
            if existing is None:
                entry["modules"][module] = {
                    "value": value, "status": "declared", "provenance": "human_entered", "raw_value": None,
                    "raw_unit": None, "raw_indicator_label": None, "raw_module_header": module,
                    "source_page": None, "source_table": None, "confidence": 1.0,
                    "extraction_method": "human_review", "human_verified": True, "human_edited": True,
                }
            elif existing.get("value") != value:
                existing.setdefault("original_extracted_value", existing.get("value"))
                existing.update({"value": value, "human_edited": True, "human_verified": True,
                                 "status": "declared" if value is not None else "not_recorded"})
    for indicator in indicators:
        if indicator in results and not results[indicator].get("modules"):
            results.pop(indicator)


def edit_field(record: dict, key: str, value, where: str = "metadata") -> None:
    """Store a reviewer edit and keep the extracted value for provenance."""
    target = record.setdefault(where, {})
    old = target.get(key)
    if value in ("", None) and old in ("", None):
        return
    if value == old:
        return
    target[key] = value
    provenance = record.setdefault("metadata_provenance", {}).setdefault(
        key if where == "metadata" else f"{where}.{key}", {})
    provenance.setdefault("original_extracted_value", old)
    provenance.update({"extraction_method": "human_review", "confidence": 1.0, "human_edited": True})


def num_text(value) -> str:
    """Editable text for a number without rounding it (so an untouched field never looks edited)."""
    if value is None or value == "":
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def same_number(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)


def parse_float(text):
    try:
        return float(str(text).replace(",", ".").strip()) if str(text).strip() else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Upload & Extract
# ---------------------------------------------------------------------------

def review_identity(record: dict, prefix: str) -> None:
    metadata = record.get("metadata", {})
    provenance = record.get("metadata_provenance", {})

    def help_for(key):
        item = provenance.get(key) or {}
        if not item:
            return "Not extracted — enter it from the EPD if present."
        page = item.get("source_page")
        return (f"{item.get('extraction_method')} · confidence {item.get('confidence')}"
                + (f" · page {page}" if page else "") + (f"\n\n{item.get('source_text')}" if item.get("source_text") else ""))

    st.markdown("#### 1. Identification (needed to store, fetch and categorise)")
    left, right = st.columns(2)
    text_fields = [
        ("registration_number", "EPD ID / registration number"),
        ("product_name", "Product name"),
        ("manufacturer", "Manufacturer / owner of the declaration"),
        ("programme_operator", "Programme operator"),
        ("publication_date", "Publication (issue) date"),
        ("valid_until", "Valid until"),
        ("declared_unit_raw", "Declared unit"),
        ("pcr", "PCR"),
        ("verifier", "Independent verifier"),
        ("geography", "Geographical scope"),
    ]
    for index, (key, label) in enumerate(text_fields):
        column = left if index % 2 == 0 else right
        value = column.text_input(label, value=metadata.get(key) or "", key=f"{prefix}_{key}", help=help_for(key))
        edit_field(record, key, value.strip() or None)

    from extraction.common import resolve_validity_dates

    pub, val = resolve_validity_dates(metadata.get("publication_date"), metadata.get("valid_until"))
    if pub:
        metadata["publication_date_iso"] = pub["iso"]
    if val:
        metadata["valid_until_iso"] = val["iso"]
    declared = parse_declared_unit(metadata.get("declared_unit_raw"))
    if declared:
        metadata.update({"declared_quantity": declared.get("quantity"), "declared_unit": declared.get("unit"),
                         "declared_unit_dimension": declared.get("dimension")})

    st.markdown("#### 2. Step 1 parameters you can correct")
    c1, c2, c3, c4 = st.columns(4)
    standards = ["EN 15804+A2", "EN 15804+A1", "Not found"]
    current_standard = metadata.get("standard") or "Not found"
    chosen = c1.selectbox("Core standard", standards, index=standards.index(current_standard)
                          if current_standard in standards else 2, key=f"{prefix}_standard",
                          help=help_for("standard"))
    if chosen != current_standard:
        edit_field(record, "standard", None if chosen == "Not found" else chosen)
        metadata["standard_profile"] = {"EN 15804+A2": "EN15804_A2_UNKNOWN_EF",
                                        "EN 15804+A1": "EN15804_A1_CML"}.get(chosen, "unresolved")

    verifications = ["External third-party verification", "Internal verification", "Not stated"]
    current_verification = metadata.get("verification_type") or "Not stated"
    chosen = c2.selectbox("Verification (ISO 14025)", verifications,
                          index=verifications.index(current_verification) if current_verification in verifications
                          else 2, key=f"{prefix}_verification", help=help_for("verification_type"))
    edit_field(record, "verification_type", None if chosen == "Not stated" else chosen)
    if chosen.startswith("External"):
        metadata["iso14025_mentioned"] = metadata.get("iso14025_mentioned") or True

    rsl_text = c3.text_input("Reference service life (years)",
                             value=num_text(metadata.get("reference_service_life_years")),
                             key=f"{prefix}_rsl",
                             help=help_for("reference_service_life_years") +
                             ("\n\nThe EPD states that no RSL is defined."
                              if metadata.get("reference_service_life_status") == "not_defined" else ""))
    rsl = parse_float(rsl_text)
    if not same_number(rsl, metadata.get("reference_service_life_years")):
        edit_field(record, "reference_service_life_years", rsl)
        metadata["reference_service_life_status"] = "declared" if rsl else metadata.get(
            "reference_service_life_status", "not_found")

    physical = record.setdefault("physical_properties", {})
    mass_text = c4.text_input("Mass per declared unit (kg)",
                              value=num_text(physical.get("mass_per_declared_unit_kg")),
                              key=f"{prefix}_mass",
                              help=(physical.get("mass_formula") or "Used for the ILCD conversion factor "
                                    "f = M / m (per-kg results = per-DU results × f)."))
    mass = parse_float(mass_text)
    if mass and not same_number(mass, physical.get("mass_per_declared_unit_kg")):
        edit_field(record, "mass_per_declared_unit_kg", mass, where="physical_properties")
        quantity = metadata.get("declared_quantity") or 1.0
        physical["conversion_factor_to_1kg"] = round(quantity / mass, 8)
        physical["mass_formula"] = "entered by reviewer; f = M / m"


def review_category(record: dict, prefix: str) -> None:
    classification = record.get("classification", {})
    st.markdown("#### 3. Step 2 — building material / component category")
    options = category_options()
    ids = [option[0] for option in options]
    labels = dict(options)
    suggested = classification.get("user_override", {}).get("from") if classification.get("user_override") \
        else classification.get("category")
    col1, col2 = st.columns([2, 3])
    choice = col1.selectbox("Category", ids, index=ids.index(suggested) if suggested in ids else len(ids) - 1,
                            format_func=lambda cid: labels.get(cid, cid), key=f"{prefix}_category",
                            help="Automatic suggestion; change it if the EPD belongs to another category.")
    col2.markdown(
        f"**Suggested:** {labels.get(suggested, suggested)} · confidence **{classification.get('confidence', 0):.0%}**"
        f" · building element: {classification.get('building_element') or '—'}  \n"
        f"Alternatives: {', '.join(a['label'] for a in classification.get('alternatives', [])) or '—'}  \n"
        f"ÖKOBAUDAT: {', '.join(c['id'] + ' ' + (c.get('name_de') or '') for c in classification.get('oekobaudat_categories', [])) or '—'}"
        f" · CPA hint: {', '.join(classification.get('cpa_hint', [])) or '—'}")
    if classification.get("evidence"):
        with st.expander("Why this category? (keyword evidence)"):
            st.dataframe(pd.DataFrame(classification["evidence"]), hide_index=True, width="stretch")
    if choice != suggested:
        base = dict(classification)
        base.pop("user_override", None)
        base["category"] = suggested
        record["classification"] = apply_category_override(base, choice)


def show_step1(record: dict) -> None:
    step1 = record.get("step1", {})
    summary = step1.get("summary", {})
    st.markdown("#### 4. Step 1 — DGNB / Danish EPD requirements")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Pass", summary.get("pass", 0))
    m2.metric("Warnings", summary.get("warning", 0))
    m3.metric("Fail", summary.get("fail", 0))
    m4.metric("Missing", summary.get("missing", 0))
    m5.metric("Mandatory met", "Yes" if summary.get("dgnb_mandatory_met") else "No")
    st.dataframe(step1_table(record), hide_index=True, width="stretch",
                 column_config={"Assessment": st.column_config.TextColumn(width="large"),
                                "Evidence": st.column_config.TextColumn(width="medium")})

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Life-cycle modules (DGNB scope)**")
        st.dataframe(module_table(record), hide_index=True, width="stretch")
    with right:
        helper = step1.get("parameters", {}).get("co2_reporting_unit", {}).get("value") or {}
        st.markdown("**Danish reporting unit (kg CO₂e/m²/year)**")
        st.metric("GWP in scope per declared unit (50 years)", fmt(helper.get("per_declared_unit_total")))
        st.metric("… per declared unit per year", fmt(helper.get("per_declared_unit_per_year")))
        st.metric("Module D (reported separately)", fmt(helper.get("module_d_per_declared_unit")))
        st.caption(f"Modules without EPD values: {', '.join(helper.get('modules_without_epd_value', [])) or 'none'}. "
                   "Multiply by the project quantity and divide by the heated floor area for the building result. "
                   "B6 comes from the energy calculation.")

    st.markdown("**Environmental, resource and waste indicators (Step 1 set, DGNB modules)**")
    st.dataframe(indicator_table(record), hide_index=True, width="stretch")


def upload_tab_view() -> None:
    st.header("Upload an EPD")
    uploaded = st.file_uploader("PDF, ILCD+EPD XML or ILCD ZIP", type=["pdf", "xml", "zip"],
                                help="Native-text PDFs are read directly (no OCR). ILCD+EPD files are read "
                                     "by indicator UUID.")
    if uploaded is not None:
        st.write(f"**File:** {uploaded.name} · {uploaded.size / 1024:.1f} KB")
        if st.button("Extract EPD", type="primary"):
            data = uploaded.getvalue()
            with st.spinner("Reading the file and extracting Step 1 information…"):
                try:
                    records = extract_cached(data, uploaded.name)
                except Exception as error:
                    st.error(f"Extraction failed: {error}")
                    records = []
            if records:
                duplicates = store.index.find_by_sha(records[0]["document"]["sha256"])
                st.session_state.update({"draft_records": records, "draft_bytes": data,
                                         "draft_name": uploaded.name, "draft_index": 0,
                                         "draft_duplicates": duplicates})

    records = st.session_state.get("draft_records")
    if not records:
        return

    if len(records) > 1:
        st.session_state["draft_index"] = st.selectbox(
            "This ILCD archive contains several EPD data sets — choose one to review",
            range(len(records)), format_func=lambda i: records[i]["metadata"].get("product_name") or f"Data set {i + 1}")
    if st.session_state.get("draft_duplicates"):
        st.warning("This file is already stored as: " + ", ".join(st.session_state["draft_duplicates"]) +
                   ". Saving again creates a new version only if something changed.")

    index = st.session_state.get("draft_index", 0)
    original = records[index]
    prefix = f"d{original['document']['sha256'][:10]}_{index}"
    record = copy.deepcopy(original)
    full_text = record.get("_full_text", "")

    st.divider()
    doc = record.get("document", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source", doc.get("source_format", "").upper())
    c2.metric("Pages", doc.get("page_count") or "—")
    c3.metric("Standard profile", record["metadata"].get("standard_profile", "unresolved"))
    c4.metric("A1-A3 reporting", record.get("a1_a3_reporting_mode", "none"))
    if doc.get("pages_without_text"):
        st.warning("Pages without a text layer (OCR not enabled): " + ", ".join(map(str, doc["pages_without_text"])))

    review_identity(record, prefix)

    st.markdown("#### GWP results per declared unit (edit only if the EPD shows a different value)")
    st.caption("Blank = not declared / not extracted (never zero). A reported A1-A3 total is never split; "
               "module D is always kept separately.")
    frame, indicators = gwp_frame(record)
    config = {"Module": st.column_config.TextColumn("Module"),
              "Declaration": st.column_config.TextColumn("EPD declaration", help="X / MND / MNR / ND"),
              "DGNB scope": st.column_config.TextColumn("DGNB scope")}
    for indicator in indicators:
        config[indicator] = st.column_config.NumberColumn(indicator, format="%.6g", help="kg CO₂ eq. per declared unit")
    edited = st.data_editor(frame, hide_index=True, width="stretch", height=680,
                            disabled=["Module", "Declaration", "DGNB scope"], column_config=config,
                            key=f"{prefix}_gwp")
    apply_gwp_edits(record, edited, indicators)

    record = refresh_derived_blocks(record, full_text)
    review_category(record, prefix)
    show_step1(record)

    record = validate_epd(record)
    st.markdown("#### 5. Validation")
    for error in record["qa"]["errors"]:
        st.error(error)
    for warning in record["qa"]["warnings"]:
        st.warning(warning)
    if not record["qa"]["errors"] and not record["qa"]["warnings"]:
        st.success("No rule-based problems found.")

    with st.expander("Extraction evidence (all values with page, method and confidence)"):
        rows = []
        for code, data in record.get("results", {}).items():
            for module, value in data.get("modules", {}).items():
                rows.append({"Indicator": code, "Module": module, "Value": value.get("value"),
                             "Raw": value.get("raw_value"), "Unit": value.get("raw_unit"),
                             "Page": value.get("source_page"), "Method": value.get("extraction_method"),
                             "Confidence": value.get("confidence"), "Edited": value.get("human_edited", False)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        meta_rows = [{"Field": key, **{k: v for k, v in (item or {}).items() if k != "source_text"},
                      "Source text": (item or {}).get("source_text")}
                     for key, item in record.get("metadata_provenance", {}).items()]
        st.dataframe(pd.DataFrame(meta_rows), hide_index=True, width="stretch")
    with st.expander("Complete record (transfer model JSON)"):
        st.json({k: v for k, v in record.items() if not k.startswith("_")}, expanded=False)

    st.markdown("#### 6. Approve and store")
    approved = st.checkbox("I compared the values with the source EPD.", key=f"{prefix}_approve")
    override = True
    if record["qa"]["errors"]:
        override = st.checkbox("Store anyway (the record keeps its validation errors).", key=f"{prefix}_override")
    if st.button("Approve and save", type="primary", disabled=not (approved and override)):
        record["review"] = {"status": "approved", "human_verified": True, "approved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
        with st.spinner("Saving (source file, text, record document, index)…"):
            result = store.save_record(record, st.session_state["draft_bytes"], st.session_state["draft_name"],
                                       full_text)
        for key in ("draft_records", "draft_bytes", "draft_name", "draft_index", "draft_duplicates"):
            st.session_state.pop(key, None)
        st.session_state["flash"] = (f"Saved {result['epd_uid']} (version {result['version']}, {result['status']}) "
                                     f"in the {store.blobs.name} store.")
        st.rerun()


with upload_tab:
    upload_tab_view()


# ---------------------------------------------------------------------------
# EPD Database
# ---------------------------------------------------------------------------

def database_tab_view() -> None:
    st.header("Stored EPDs")
    stats = store.index.stats()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("EPDs", stats.get("active", 0))
    m2.metric("Valid today", stats.get("valid", 0))
    m3.metric("DGNB mandatory met", stats.get("dgnb", 0))
    m4.metric("ECO Platform programmes", stats.get("eco", 0))

    facets = store.facets()
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    text = f1.text_input("Search", placeholder="EPD ID, product, manufacturer, operator, PCR, category")
    categories = [None] + [c["category"] for c in facets["categories"]]
    category_labels = {c["category"]: f"{c['category_label']} ({c['n']})" for c in facets["categories"]}
    category = f2.selectbox("Category", categories, format_func=lambda c: "All" if c is None else category_labels.get(c, c))
    manufacturers = [None] + [m["manufacturer"] for m in facets["manufacturers"]]
    manufacturer = f3.selectbox("Manufacturer", manufacturers, format_func=lambda m: "All" if m is None else m)
    standard = f4.selectbox("Standard", [None, "EN 15804+A2", "EN 15804+A1"], format_func=lambda s: s or "All")
    g1, g2, g3, g4, g5 = st.columns(5)
    only_valid = g1.toggle("Valid only")
    only_dgnb = g2.toggle("DGNB mandatory met")
    sort = g3.selectbox("Sort by", ["updated_at", "product_name", "manufacturer", "category", "gwp_a1_a3",
                                    "valid_until"])
    page_size = g4.selectbox("Rows", [25, 50, 100, 250], index=1)
    filters = dict(text=text, category=category, manufacturer=manufacturer, standard=standard,
                   only_valid=only_valid, only_dgnb=only_dgnb, sort=sort,
                   descending=sort in ("updated_at", "valid_until"))
    _, total = store.search(limit=1, **filters)
    page = g5.number_input("Page", min_value=1, max_value=total_pages(total, page_size), value=1)
    rows, total = store.search(limit=page_size, offset=(int(page) - 1) * page_size, **filters)
    st.caption(f"{total} EPD(s) match. GWP A1-A3 is per declared unit — compare only EPDs with the same "
               "declared unit and function.")
    if not rows:
        st.info("No EPDs match. Upload and approve an EPD first.")
        return

    table = pd.DataFrame([{
        "EPD ID": r["epd_uid"], "Product": r["product_name"], "Manufacturer": r["manufacturer"],
        "Category": r["category_label"], "Standard": r["standard"], "Declared unit": r["declared_unit"],
        "GWP A1-A3": r["gwp_a1_a3"], "A1-A3 source": r["gwp_a1_a3_provenance"], "GWP D": r["gwp_d"],
        "RSL (a)": r["rsl_years"], "Valid until": r["valid_until"], "ECO": bool(r["eco_platform_member"]),
        "DGNB ok": bool(r["dgnb_mandatory_met"]), "Version": r["record_version"],
    } for r in rows])
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                         key="db_table", column_config={"GWP A1-A3": st.column_config.NumberColumn(format="%.4g"),
                                                        "GWP D": st.column_config.NumberColumn(format="%.4g")})
    selection = event.selection.rows if event is not None else []
    selected = [rows[i]["epd_uid"] for i in selection if i < len(rows)]

    if len(selected) >= 2:
        st.subheader("Compare selected EPDs (GWP per declared unit)")
        results = store.results(selected, indicators=["GWP-total", "GWP"],
                                modules=["A1-A3", "A1", "A2", "A3", "A4", "A5", "B4", "B6", "C3", "C4", "D"])
        frame = pd.DataFrame(results)
        if not frame.empty:
            frame = frame[frame["scenario"] == ""]
            pivot = frame.pivot_table(index=["epd_uid", "indicator"], columns="module", values="value", aggfunc="first")
            units = {r["epd_uid"]: r["declared_unit"] for r in rows}
            pivot.insert(0, "Declared unit", [units.get(uid) for uid, _ in pivot.index])
            st.dataframe(pivot, width="stretch")
            if len({units.get(uid) for uid in selected}) > 1:
                st.warning("The selected EPDs use different declared units — convert before comparing.")

    if len(selected) != 1:
        st.caption("Select one row to see the full record, several rows to compare.")
        return

    uid = selected[0]
    record = store.get_record(uid)
    st.subheader(record["metadata"].get("product_name") or uid)
    st.caption(f"{uid} · version {record.get('storage', {}).get('version')} · "
               f"saved {record.get('storage', {}).get('saved_at')} · {record.get('storage', {}).get('backend')}")
    show_step1(record)
    frame, _ = gwp_frame(record)
    with st.expander("GWP by module"):
        st.dataframe(frame, hide_index=True, width="stretch")
    with st.expander("Versions"):
        st.dataframe(pd.DataFrame(store.versions(uid)), hide_index=True, width="stretch")

    d1, d2, d3, d4 = st.columns(4)
    d1.download_button("Record JSON", json.dumps(record, ensure_ascii=False, indent=1, default=str),
                       file_name=f"{uid}.json", mime="application/json")
    d2.download_button("Results CSV (long)", results_csv([record]), file_name=f"{uid}_results.csv", mime="text/csv")
    source = store.get_source_file(uid)
    if source:
        d3.download_button("Source file", source[0], file_name=source[1], mime=source[2])
    if d4.button("Archive (hide)", help="Soft delete: the EPD disappears from searches; all versions stay stored."):
        store.archive(uid)
        st.session_state["flash"] = f"{uid} archived."
        st.rerun()

    if st.button("Re-run Step 1 / Step 2 with the current dictionary",
                 help="Uses the stored text, keeps a reviewer's category choice and saves a new version only if "
                      "the result changes."):
        full_text = store.get_text(uid)
        updated = refresh_derived_blocks(copy.deepcopy(record), full_text)
        updated = validate_epd(updated)
        result = store.save_record(updated, None, None, None, note="re-evaluated with dictionary "
                                   + str(load_dictionary().get("version")))
        st.session_state["flash"] = f"{uid}: {result['status']} (version {result['version']})."
        st.rerun()


def total_pages(total: int, size: int) -> int:
    return max(1, -(-total // size))


with database_tab:
    database_tab_view()


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------

with dictionary_tab:
    dictionary = load_dictionary()
    st.header("EPD dictionary (ILCD+EPD based)")
    st.write(f"**Version:** {dictionary.get('version')} (previous: {dictionary.get('previous_version')}) · "
             f"generated {dictionary.get('v4_generated_at')} by `tools/build_ilcd_dictionary.py` from the "
             "`ILCD pdf` folder. All v3 keys are kept unchanged.")

    st.subheader("Step 3 — dictionary columns mapped to ILCD+EPD v1.3")
    for section in step3_sections():
        with st.expander(section["label"]):
            rows = []
            for field in section["fields"]:
                ilcd = field.get("ilcd", {}).get("fields", [])
                first = ilcd[0] if ilcd else {}
                rows.append({
                    "Column": field["label"],
                    "What should be included": field.get("description"),
                    "Extracted": field.get("extract_scope"),
                    "ILCD+EPD path(s)": "\n".join(field.get("ilcd", {}).get("paths", [])) or field.get("ilcd_note", ""),
                    "ILCD requirement": first.get("requirement"),
                    "InData CP-2020": first.get("indata_cp2020"),
                    "EN 15804+A2": first.get("en15804_a2_chapter"),
                    "ISO 22057 GUID": first.get("iso22057_guid"),
                    "Indicator UUIDs": fmt(field.get("ilcd_indicator_uuids")) if field.get("ilcd_indicator_uuids") else "",
                    "Unit": field.get("canonical_unit") or field.get("unit"),
                    "PDF aliases": ", ".join(field.get("pdf_aliases", [])),
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.subheader("Step 1 — DGNB profile")
    profile = step1_profile()
    st.dataframe(pd.DataFrame([{"Category": p["category"], "Parameter": p["parameter"],
                                "Requirement": p["requirement"], "Rule": p.get("rule")}
                               for p in profile.get("parameters", [])]), hide_index=True, width="stretch")
    st.dataframe(pd.DataFrame([{"Module": m, "DGNB scope": s} for m, s in dgnb_module_scope().items()]),
                 hide_index=True)

    st.subheader("Step 2 — categories")
    st.dataframe(pd.DataFrame([{
        "ID": c["id"], "Category": c["label"], "Parent": c.get("parent"), "Building element": c["building_element"],
        "ÖKOBAUDAT": ", ".join(o["id"] for o in c.get("oekobaudat_categories", [])),
        "CPA hint": ", ".join(c.get("cpa_hint", [])), "Strong keywords": ", ".join(c.get("strong", [])),
    } for c in material_categories().get("categories", [])]), hide_index=True, width="stretch")

    st.subheader("ECO Platform EPD programmes (recognised operators)")
    operators = eco_platform_operators()
    st.caption(f"List checked {operators.get('list_checked')} · {operators.get('source')}")
    st.dataframe(pd.DataFrame([{"Programme": o["name"], "Country": o["country"], "Aliases": ", ".join(o["aliases"])}
                               for o in operators.get("operators", [])]), hide_index=True, width="stretch")

    st.subheader("Indicator UUIDs (ILCD+EPD master data)")
    registry = dictionary.get("ilcd_indicator_registry", {}).get("by_uuid", {})
    st.dataframe(pd.DataFrame([{"Code": v["code"], "UUID": k, "Name": v["name_en"], "Unit": v["unit"],
                                "Profiles": ", ".join(v["profiles"])} for k, v in registry.items()]),
                 hide_index=True, width="stretch")

    c1, c2 = st.columns(2)
    c1.download_button("Download epd_dictionary.json", json.dumps(dictionary, ensure_ascii=False, indent=1),
                       file_name="epd_dictionary.json", mime="application/json")


# ---------------------------------------------------------------------------
# Storage & Transfer
# ---------------------------------------------------------------------------

with storage_tab:
    st.header("Storage & transfer")
    status = store.status()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Backend", status["backend"])
    s2.metric("Stored EPDs", status.get("active", 0))
    s3.metric("Result rows", status.get("result_rows", 0))
    s4.metric("Index size", f"{status.get('index_bytes', 0) / 1e6:.2f} MB")
    st.write(f"**Location:** {status['location']}")
    st.write(f"**Last Drive snapshot:** {status.get('last_snapshot_at') or '—'} · "
             f"**last reconciliation:** {status.get('last_reconciled_at') or '—'}")
    for message in status.get("messages", []):
        st.info(message)

    b1, b2 = st.columns(2)
    if b1.button("Sync index snapshot now", disabled=status["backend"] != "gdrive"):
        st.success(store.sync_snapshot(force=True))
    if b2.button("Rebuild index from stored record documents"):
        with st.spinner("Rebuilding…"):
            count = store.rebuild_index()
        st.success(f"Index rebuilt from {count} record document(s).")

    st.subheader("Export")
    export_format = st.selectbox("Format", ["Transfer bundle (.zip: records + source files + CSV)",
                                            "Results CSV (long format)", "Summary CSV", "Records JSONL"])
    if st.button("Prepare export", disabled=status.get("active", 0) == 0):
        uids = store.index.all_uids(include_archived=False)
        with st.spinner("Preparing export…"):
            if export_format.startswith("Transfer"):
                st.session_state["export"] = (store.export_bundle(uids), "epd_transfer_bundle.zip", "application/zip")
            else:
                records = store.records(uids)
                if export_format.startswith("Results"):
                    st.session_state["export"] = (results_csv(records), "epd_results_long.csv", "text/csv")
                elif export_format.startswith("Summary"):
                    st.session_state["export"] = (summary_csv(records), "epd_summary.csv", "text/csv")
                else:
                    st.session_state["export"] = (jsonl(records), "epd_records.jsonl", "application/x-ndjson")
    if st.session_state.get("export"):
        data, name, mime = st.session_state["export"]
        st.download_button(f"Download {name}", data, file_name=name, mime=mime)

    st.subheader("Import a transfer bundle")
    bundle = st.file_uploader("Bundle exported from another installation (.zip)", type=["zip"], key="bundle_upload")
    if bundle is not None and st.button("Import bundle"):
        with st.spinner("Importing…"):
            summary = store.import_bundle(bundle.getvalue())
        st.success(f"Imported: {summary}")

    with st.expander("How to keep data on Render with Google Drive"):
        st.markdown(
            "Render's free web service has an ephemeral disk: files written by the app are lost on every "
            "deploy, restart or spin-down. Set these environment variables on Render to store everything in "
            "your Google Drive instead (see README → *Google Drive storage*):\n\n"
            "- `EPD_STORAGE_BACKEND=gdrive`\n- `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`\n"
            "- `GDRIVE_REFRESH_TOKEN` (create it once with `python tools/google_drive_auth.py`)\n\n"
            "Source files, extracted text and every record version are uploaded as separate files; the "
            "SQLite index is uploaded as a snapshot and restored automatically when the server starts.")
