import copy
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from dictionary import load_dictionary
from extraction.extractor import extract_epd
from storage import load_epds, save_epd, save_uploaded_pdf
from validation.validator import validate_epd


st.set_page_config(
    page_title="EPD Carbon Prototype",
    page_icon="🏗️",
    layout="wide",
)

st.title("EPD Carbon Prototype")
st.caption(
    "Phase 1 — Upload → Extract → Human Review → Store. "
    "No external AI API is used."
)

upload_tab, database_tab, dictionary_tab = st.tabs(
    ["Upload & Extract", "Saved EPD Database", "Dictionary"]
)


def _display_widget_value(value):
    """Convert saved Python values into text that Streamlit can edit."""
    if value is None:
        return ""

    if isinstance(value, (list, dict)):
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )

    return str(value)


def _parse_review_value(raw_value, field_type):
    """Convert reviewed text back to a useful Python value."""
    text = raw_value.strip()

    if text == "":
        return None

    if field_type == "number":
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return text

    if field_type in ("array", "object"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return text


def _field_storage(record, key):
    """
    Decide where a reviewed field belongs.
    """
    if key == "conversion_factor_to_1kg":
        return record.setdefault("physical_properties", {})

    if key in {"ilcd_mapping", "digital_signature"}:
        return record.setdefault("digital_template", {})

    return record.setdefault("metadata", {})


def metadata_editor(record):
    """
    Build the review form from thesis_required_fields in epd_dictionary.json.
    """
    st.subheader("1. Review EPD information")

    st.caption(
        "All thesis-required EPD fields are displayed below. "
        "A blank field means that the deterministic extractor did not "
        "find a sufficiently reliable value. You can enter or correct it "
        "before approval."
    )

    dictionary = load_dictionary()
    required_fields = dictionary.get("thesis_required_fields", {})

    section_names = {
        "header_identification": "1A. Header & Identification",
        "verification_compliance": "1B. Verification & Compliance",
        "product_company": "1C. Product & Company",
        "lca_methodology": "1D. LCA Methodology",
        "digital_template": "1E. Digital / ILCD Information",
    }

    long_text_fields = {
        "compliance_statement",
        "comparability_statement",
        "product_description",
        "product_composition",
        "technical_specifications",
        "packaging_materials",
        "manufacturing_process",
        "system_boundary",
        "cutoff_rules",
        "allocation",
        "data_quality",
        "ilcd_mapping",
    }

    sections_to_show = [
        "header_identification",
        "verification_compliance",
        "product_company",
        "lca_methodology",
        "digital_template",
    ]

    for section_key in sections_to_show:
        fields = required_fields.get(section_key, [])

        if not fields:
            continue

        st.markdown(f"#### {section_names.get(section_key, section_key)}")
        left, right = st.columns(2)

        for index, definition in enumerate(fields):
            key = definition["key"]
            label = definition["label"]
            field_type = definition.get("type", "string")

            storage = _field_storage(record, key)
            current_value = storage.get(key)
            widget_value = _display_widget_value(current_value)

            column = left if index % 2 == 0 else right

            with column:
                if key in long_text_fields:
                    reviewed_value = st.text_area(
                        label,
                        value=widget_value,
                        height=110,
                        key=f"review_{section_key}_{key}",
                    )
                else:
                    reviewed_value = st.text_input(
                        label,
                        value=widget_value,
                        key=f"review_{section_key}_{key}",
                    )

                if definition.get("extract_phase") == 2:
                    st.caption("Advanced field — may require manual review.")

                if (
                    key == "reference_service_life_years"
                    and record.get("metadata", {}).get("reference_service_life_status")
                    == "not_defined"
                ):
                    st.caption(
                        "Source EPD explicitly states that no Reference Service Life "
                        "(RSL) is defined."
                    )

            storage[key] = _parse_review_value(
                reviewed_value,
                field_type,
            )

        st.divider()

    return record


GWP_MODULES = [
    "A1-A3",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "B1",
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "B7",
    "C1",
    "C2",
    "C3",
    "C4",
    "D",
]


def gwp_indicators_for_record(record):
    """Return the GWP indicators appropriate to the detected EN 15804 profile."""
    profile = (
        record
        .get("document", {})
        .get("standard_profile", "unresolved")
    )

    results = record.get("results", {})

    if profile == "EN15804_A1_CML":
        return ["GWP"]

    if "GWP" in results and "GWP-total" not in results:
        return ["GWP"]

    return [
        "GWP-total",
        "GWP-fossil",
        "GWP-biogenic",
        "GWP-luluc",
    ]


def build_gwp_review_dataframe(record):
    """
    Always build a complete GWP matrix, even when some modules are not declared.

    The 'Declaration' column comes from the EPD system-boundary matrix:
      X   = included / declared
      MND = module or indicator not declared
      MNR = module not relevant
    """
    results = record.get("results", {})
    indicators = gwp_indicators_for_record(record)
    declarations = record.get("module_declarations", {})

    rows = []

    for module in GWP_MODULES:
        declaration = declarations.get(module, {}).get("status", "")

        row = {
            "Module": module,
            "Declaration": declaration,
        }

        for indicator in indicators:
            indicator_data = results.get(indicator, {})
            module_data = (
                indicator_data
                .get("modules", {})
                .get(module, {})
            )

            value = module_data.get("value")

            # Use NaN instead of Python None so Streamlit renders a blank numeric cell.
            row[indicator] = float("nan") if value is None else value

        rows.append(row)

    return pd.DataFrame(rows), indicators


def apply_gwp_review_edits(record, edited_df, indicators):
    """
    Store human-entered or corrected GWP values while preserving original extraction provenance.
    """
    results = record.setdefault("results", {})

    profile = (
        record
        .get("document", {})
        .get("standard_profile", "unresolved")
    )

    for _, row in edited_df.iterrows():
        module = str(row["Module"])

        for indicator in indicators:
            raw_value = row[indicator]

            if pd.isna(raw_value):
                value = None
            else:
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    value = None

            indicator_record = results.setdefault(
                indicator,
                {
                    "method_profile": profile,
                    "canonical_unit": "kg CO2 eq",
                    "modules": {},
                },
            )

            modules = indicator_record.setdefault("modules", {})
            existing = modules.get(module)

            if value is None and existing is None:
                continue

            if existing is None:
                modules[module] = {
                    "value": value,
                    "status": "declared" if value is not None else "not_recorded",
                    "provenance": "human_entered",
                    "raw_value": None,
                    "raw_unit": None,
                    "raw_indicator_label": None,
                    "raw_module_header": module,
                    "source_page": None,
                    "source_table": None,
                    "confidence": 1.0 if value is not None else None,
                    "extraction_method": "human_review",
                    "human_verified": True,
                }
            else:
                old_value = existing.get("value")

                if old_value != value:
                    if "original_extracted_value" not in existing:
                        existing["original_extracted_value"] = old_value

                    existing["value"] = value
                    existing["human_edited"] = True
                    existing["human_verified"] = True

    return record


def flatten_results(record):
    rows = []

    for indicator, indicator_data in record.get("results", {}).items():
        for module, value_data in indicator_data.get("modules", {}).items():
            rows.append(
                {
                    "Indicator": indicator,
                    "Module": module,
                    "Value": value_data.get("value"),
                    "Status": value_data.get("status"),
                    "Canonical unit": indicator_data.get("canonical_unit"),
                    "Raw value": value_data.get("raw_value"),
                    "Raw unit": value_data.get("raw_unit"),
                    "Source page": value_data.get("source_page"),
                    "Confidence": value_data.get("confidence"),
                }
            )

    return rows


def show_qa(record):
    st.subheader("5. Validation")

    errors = record.get("qa", {}).get("errors", [])
    warnings = record.get("qa", {}).get("warnings", [])

    if not errors and not warnings:
        st.success("No rule-based validation problems detected.")
    else:
        for error in errors:
            st.error(error)

        for warning in warnings:
            st.warning(warning)

    st.info(
        "Human approval is still required even when all rule-based checks pass."
    )


with upload_tab:
    st.header("Upload an EPD PDF")

    uploaded_file = st.file_uploader(
        "Choose an EPD PDF",
        type=["pdf"],
        help="Phase 1 supports native-text PDF EPDs. XML/ILCD import comes next.",
    )

    if uploaded_file is not None:
        st.write(f"**File:** {uploaded_file.name}")
        st.write(f"**Size:** {uploaded_file.size / 1024:.1f} KB")

        if st.button("Extract EPD", type="primary"):
            pdf_bytes = uploaded_file.getvalue()

            with st.spinner("Reading PDF and extracting EPD data..."):
                extracted = extract_epd(pdf_bytes, uploaded_file.name)
                extracted = validate_epd(extracted)

            st.session_state["epd_extracted_record"] = extracted
            st.session_state["epd_pdf_bytes"] = pdf_bytes
            st.session_state["epd_pdf_name"] = uploaded_file.name

    if "epd_extracted_record" in st.session_state:
        record = copy.deepcopy(
            st.session_state["epd_extracted_record"]
        )

        st.divider()
        st.header("Extraction review")

        doc = record.get("document", {})
        c1, c2, c3 = st.columns(3)

        c1.metric("Pages", doc.get("page_count", 0))
        c2.metric(
            "Standard profile",
            doc.get("standard_profile", "unresolved"),
        )
        c3.metric(
            "A1-A3 mode",
            record.get("a1_a3_reporting_mode", "none"),
        )

        pages_without_text = doc.get("pages_without_text", [])

        if pages_without_text:
            st.warning(
                "These pages had no extractable text layer: "
                + ", ".join(map(str, pages_without_text))
                + ". OCR is not enabled in this first version."
            )

        # 1. Metadata review
        record = metadata_editor(record)

        # 2. GWP review
        st.subheader("2. Review GWP results")

        st.caption(
            "Blank cells mean 'not extracted / not recorded' — NOT zero. "
            "Enter or correct values only when they are present in the source EPD."
        )

        st.info(
            "A reported A1-A3 aggregate is preserved exactly as reported. "
            "The prototype never divides it into A1, A2 and A3. "
            "Module D is stored and reported separately."
        )

        st.caption(
            "EPD declaration status: X = included/declared, "
            "MND = module or indicator not declared, "
            "MNR = module not relevant."
        )

        gwp_df, gwp_indicators = build_gwp_review_dataframe(record)

        gwp_column_config = {
            "Module": st.column_config.TextColumn("Lifecycle module"),
            "Declaration": st.column_config.TextColumn(
                "EPD declaration",
                help=(
                    "X = included/declared; "
                    "MND = module or indicator not declared; "
                    "MNR = module not relevant."
                ),
            ),
        }

        for indicator in gwp_indicators:
            gwp_column_config[indicator] = st.column_config.NumberColumn(
                indicator,
                format="%.6g",
                help=(
                    "kg CO₂ eq. per declared unit. "
                    "Leave blank if the EPD does not declare this value."
                ),
            )

        edited_gwp_df = st.data_editor(
            gwp_df,
            hide_index=True,
            width="stretch",
            height=650,
            disabled=["Module", "Declaration"],
            column_config=gwp_column_config,
            key="gwp_review_editor",
        )

        record = apply_gwp_review_edits(
            record,
            edited_gwp_df,
            gwp_indicators,
        )

        # 3. Other environmental indicators
        st.subheader("3. Other environmental indicators")

        all_rows = flatten_results(record)

        gwp_codes = {
            "GWP",
            "GWP-total",
            "GWP-fossil",
            "GWP-biogenic",
            "GWP-luluc",
        }

        other_rows = [
            row
            for row in all_rows
            if row["Indicator"] not in gwp_codes
        ]

        if other_rows:
            other_df = pd.DataFrame(other_rows)

            st.dataframe(
                other_df,
                hide_index=True,
                width="stretch",
            )
        else:
            st.info(
                "No other environmental indicators were automatically extracted yet."
            )

        # 4. Extraction evidence
        st.subheader("4. Extraction evidence")

        with st.expander("Metadata extraction evidence"):
            provenance = record.get("metadata_provenance", {})

            if provenance:
                metadata_evidence_rows = []

                for field, evidence in provenance.items():
                    metadata_evidence_rows.append(
                        {
                            "Field": field,
                            "Source page": evidence.get("source_page"),
                            "Method": evidence.get("extraction_method"),
                            "Confidence": evidence.get("confidence"),
                            "Source text": evidence.get("source_text"),
                        }
                    )

                st.dataframe(
                    metadata_evidence_rows,
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.info("No metadata provenance was recorded.")

        with st.expander("Environmental-result evidence"):
            result_evidence_rows = []

            for indicator, indicator_data in record.get("results", {}).items():
                for module, module_data in (
                    indicator_data.get("modules", {}).items()
                ):
                    result_evidence_rows.append(
                        {
                            "Indicator": indicator,
                            "Module": module,
                            "Value": module_data.get("value"),
                            "Raw value": module_data.get("raw_value"),
                            "Raw unit": module_data.get("raw_unit"),
                            "Source page": module_data.get("source_page"),
                            "Source table": module_data.get("source_table"),
                            "Method": module_data.get("extraction_method"),
                            "Confidence": module_data.get("confidence"),
                            "Human edited": module_data.get(
                                "human_edited",
                                False,
                            ),
                        }
                    )

            if result_evidence_rows:
                st.dataframe(
                    result_evidence_rows,
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.info(
                    "The parser did not identify any environmental-result cells."
                )

        # Revalidate after human edits.
        record = validate_epd(record)
        show_qa(record)

        with st.expander("View complete extracted JSON"):
            st.json(record)

        approve = st.checkbox(
            "I reviewed the extracted values against the source EPD.",
            key="approve_checkbox",
        )

        if st.button(
            "Approve and save EPD",
            disabled=not approve,
            type="primary",
        ):
            pdf_path = save_uploaded_pdf(
                st.session_state["epd_pdf_bytes"],
                st.session_state["epd_pdf_name"],
            )

            record["document"]["stored_pdf"] = str(
                pdf_path.relative_to(
                    Path(__file__).resolve().parent
                )
            )

            record.setdefault("review", {})
            record["review"]["status"] = "approved"
            record["review"]["human_verified"] = True

            save_epd(record)

            st.success("EPD and source PDF saved.")

            del st.session_state["epd_extracted_record"]
            del st.session_state["epd_pdf_bytes"]
            del st.session_state["epd_pdf_name"]

            st.rerun()


with database_tab:
    st.header("Saved EPD Database")

    try:
        saved_epds = load_epds()
    except (OSError, json.JSONDecodeError, ValueError) as error:
        st.error(f"Could not read database: {error}")
        saved_epds = []

    if not saved_epds:
        st.info("No approved EPDs have been saved yet.")
    else:
        search_text = st.text_input(
            "Search",
            placeholder="EPD ID, product, manufacturer, programme operator",
        ).strip().lower()

        matches = []

        for index, epd in enumerate(saved_epds):
            metadata = epd.get("metadata", {})

            searchable = " ".join(
                str(metadata.get(key) or "")
                for key in (
                    "registration_number",
                    "product_name",
                    "manufacturer",
                    "product_category",
                    "programme_operator",
                )
            ).lower()

            if search_text in searchable:
                matches.append((index, epd))

        st.write(
            f"Showing {len(matches)} of {len(saved_epds)} approved EPDs"
        )

        table_rows = []

        for index, epd in matches:
            metadata = epd.get("metadata", {})

            gwp = (
                epd.get("results", {}).get("GWP-total")
                or epd.get("results", {}).get("GWP", {})
            )

            modules = gwp.get("modules", {}) if gwp else {}

            def value(module):
                return modules.get(module, {}).get("value")

            table_rows.append(
                {
                    "Record": index + 1,
                    "EPD ID": metadata.get("registration_number"),
                    "Product": metadata.get("product_name"),
                    "Manufacturer": metadata.get("manufacturer"),
                    "Declared unit": metadata.get("declared_unit_raw"),
                    "A1-A3": value("A1-A3"),
                    "A4": value("A4"),
                    "A5": value("A5"),
                    "C3": value("C3"),
                    "C4": value("C4"),
                    "D": value("D"),
                    "Review": epd.get("review", {}).get("status"),
                }
            )

        if table_rows:
            st.dataframe(
                table_rows,
                hide_index=True,
                width="stretch",
            )

            selected = st.selectbox(
                "View complete record",
                options=range(len(matches)),
                format_func=lambda i: (
                    f"{matches[i][1].get('metadata', {}).get('registration_number') or 'No ID'}"
                    " | "
                    f"{matches[i][1].get('metadata', {}).get('product_name') or 'Unnamed product'}"
                ),
            )

            with st.expander(
                "Selected EPD JSON",
                expanded=True,
            ):
                st.json(matches[selected][1])
        else:
            st.info("No EPDs match your search.")


with dictionary_tab:
    st.header("EPD Extraction Dictionary")

    st.write(
        "The JSON dictionary is the normalization layer. "
        "It contains EN 15804 profiles, life-cycle modules, indicator aliases, "
        "units, ILCD/ILCD+EPD mappings, scenario vocabulary and thesis-required fields."
    )

    dictionary_path = (
        Path(__file__).resolve().parent
        / "data"
        / "epd_dictionary.json"
    )

    if dictionary_path.exists():
        dictionary_data = json.loads(
            dictionary_path.read_text(
                encoding="utf-8"
            )
        )

        st.write(
            f"**Dictionary version:** "
            f"{dictionary_data.get('version')}"
        )

        st.write(
            "**Impact profiles:** "
            + ", ".join(
                dictionary_data.get(
                    "impact_indicators",
                    {},
                ).keys()
            )
        )

        with st.expander("Reference basis"):
            st.json(
                dictionary_data.get(
                    "reference_basis",
                    {},
                )
            )

        with st.expander("Required thesis fields"):
            st.json(
                dictionary_data.get(
                    "thesis_required_fields",
                    {},
                )
            )
    else:
        st.error(
            "data/epd_dictionary.json was not found."
        )
