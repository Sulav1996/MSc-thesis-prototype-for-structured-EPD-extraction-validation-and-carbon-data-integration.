import json
import re
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "data" / "epd_dictionary.json"


@lru_cache(maxsize=1)
def load_dictionary():
    with DICTIONARY_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def normalize_text(value):
    """Normalize text only for matching. Raw source text is preserved elsewhere."""
    if value is None:
        return ""
    value = str(value)
    replacements = {
        "–": "-", "—": "-", "−": "-", "‐": "-",
        "₂": "2", "₃": "3", "²": "2", "³": "3",
        "\u00ad": "",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = value.lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_unit(raw_unit):
    """Return canonical unit label when an alias is known."""
    if raw_unit is None:
        return None

    dictionary = load_dictionary()
    normalized = normalize_text(raw_unit)

    for canonical, aliases in dictionary.get("unit_normalization", {}).items():
        candidates = [canonical] + aliases
        if any(normalize_text(candidate) == normalized for candidate in candidates):
            return canonical

    return raw_unit.strip()


def metadata_aliases(field_key):
    dictionary = load_dictionary()
    field = dictionary.get("metadata_fields", {}).get(field_key, {})
    return field.get("aliases_en", [])


def detect_standard_profile(text):
    """
    Detect EN 15804 profile conservatively.
    +A2 can be detected without knowing EF3.0 vs EF3.1.
    """
    dictionary = load_dictionary()
    normalized = normalize_text(text)

    # First, check specific profiles containing EF version wording.
    for profile in ("EN15804_A2_EF3.1", "EN15804_A2_EF3.0", "EN15804_A1_CML"):
        terms = dictionary["parser_rules"]["standard_detection"].get(profile, [])
        for term in terms:
            if normalize_text(term) in normalized:
                # Prevent generic "+A2" term from prematurely deciding EF3.0/3.1.
                if profile.startswith("EN15804_A2_EF") and "ef 3." not in normalize_text(term) and "ef3." not in normalize_text(term):
                    continue
                return profile

    for term in dictionary["parser_rules"]["standard_detection"].get(
        "EN15804_A2_UNKNOWN_EF", []
    ):
        if normalize_text(term) in normalized:
            return "EN15804_A2_UNKNOWN_EF"

    # Unit/indicator based hints from the dictionary.
    if "gwp-total" in normalized or "gwp fossil" in normalized or "gwp-fossil" in normalized:
        return "EN15804_A2_UNKNOWN_EF"
    if "mol h+ eq" in normalized or "kg nmvoc eq" in normalized:
        return "EN15804_A2_UNKNOWN_EF"

    return "unresolved"


def module_alias_map():
    dictionary = load_dictionary()
    mapping = {}

    for module, details in dictionary.get("life_cycle_modules", {}).items():
        for alias in details.get("aliases", []) + [module]:
            mapping[normalize_text(alias)] = module

    for module, details in dictionary.get("aggregate_modules", {}).items():
        for alias in details.get("aliases", []) + [module]:
            mapping[normalize_text(alias)] = module

    return mapping


def normalize_module(raw_header):
    normalized = normalize_text(raw_header).replace(" ", "")
    aliases = module_alias_map()

    # Direct normalized lookup, allowing spaces in aliases to disappear.
    compact_map = {key.replace(" ", ""): value for key, value in aliases.items()}
    return compact_map.get(normalized)


def _indicator_profiles_for(profile):
    dictionary = load_dictionary()
    all_profiles = dictionary.get("impact_indicators", {})

    if profile == "EN15804_A1_CML":
        return [("EN15804_A1_CML", all_profiles.get("EN15804_A1_CML", {}))]

    if profile in ("EN15804_A2_EF3.0", "EN15804_A2_EF3.1", "EN15804_A2_UNKNOWN_EF"):
        # v2 stores the common A2 vocabulary under EN15804_A2_EF3.0 with UUID-by-method.
        candidates = []
        for name, values in all_profiles.items():
            if name.startswith("EN15804_A2"):
                candidates.append((name, values))
        return candidates

    return list(all_profiles.items())


def _indicator_label_match(
    raw_label,
    candidate
):
    """
    Conservative PDF-label matching.

    Returns:
        0     = no match
        1.00  = exact normalized match
        0.92  = candidate followed by unit / punctuation / footnote
        0.88  = long canonical phrase contained in PDF cell
    """

    label = normalize_text(
        raw_label
    )

    candidate_norm = normalize_text(
        candidate
    )

    if (
        not label
        or not candidate_norm
    ):
        return 0

    if label == candidate_norm:
        return 1.00

    # Remove trailing footnote numbers.
    label_without_footnote = re.sub(
        r"[\s¹²³0-9]+$",
        "",
        label
    ).strip()

    if (
        label_without_footnote
        == candidate_norm
    ):
        return 0.95

    # Example:
    # "gwp-total [kg co2 eq]"
    # "gwp-total (kg co2 eq)"
    # "gwp-total: kg co2 eq"
    prefix_patterns = (
        candidate_norm + " ",
        candidate_norm + "[",
        candidate_norm + "(",
        candidate_norm + ":",
        candidate_norm + ";",
    )

    if any(
        label.startswith(pattern)
        for pattern
        in prefix_patterns
    ):
        return 0.92

    # Allow long descriptive names inside a cell,
    # but avoid unsafe matching of short codes like GWP or AP.
    if (
        len(candidate_norm) >= 12
        and candidate_norm in label
    ):
        return 0.88

    return 0


def match_indicator(
    raw_label,
    profile="unresolved",
    raw_unit=None,
):

    dictionary = load_dictionary()

    if not raw_label:
        return None

    matches = []

    for (
        profile_name,
        indicators
    ) in _indicator_profiles_for(
        profile
    ):

        for (
            code,
            details
        ) in indicators.items():

            candidates = [
                code,
                details.get(
                    "canonical_name",
                    ""
                ),
            ]

            candidates += details.get(
                "aliases_en",
                []
            )

            best_score = 0

            for candidate in candidates:

                if not candidate:
                    continue

                score = _indicator_label_match(
                    raw_label,
                    candidate
                )

                best_score = max(
                    best_score,
                    score
                )

            if best_score > 0:

                matches.append({

                    "code":
                        code,

                    "profile":
                        profile_name,

                    "canonical_unit":
                        details.get(
                            "unit"
                        ),

                    "confidence":
                        min(
                            0.98,
                            best_score
                        ),
                })


    for (
        code,
        details
    ) in dictionary.get(
        "shared_resource_waste_output_indicators",
        {}
    ).items():

        candidates = [
            code,
            details.get(
                "canonical_name",
                ""
            ),
            details.get(
                "short_name",
                ""
            ),
        ]

        candidates += details.get(
            "aliases_en",
            []
        )

        best_score = 0

        for candidate in candidates:

            if not candidate:
                continue

            best_score = max(
                best_score,
                _indicator_label_match(
                    raw_label,
                    candidate
                )
            )

        if best_score > 0:

            matches.append({

                "code":
                    code,

                "profile":
                    "shared",

                "canonical_unit":
                    details.get(
                        "unit"
                    ),

                "confidence":
                    min(
                        0.98,
                        best_score
                    ),
            })


    if not matches:
        return None


    # Prefer compatible unit when available.
    if raw_unit:

        unit_norm = normalize_text(
            normalize_unit(
                raw_unit
            )
        )

        compatible = [

            match

            for match in matches

            if normalize_text(
                match.get(
                    "canonical_unit"
                )
            )
            == unit_norm
        ]

        if compatible:

            return max(
                compatible,
                key=lambda item:
                    item["confidence"]
            )


    return max(
        matches,
        key=lambda item:
            item["confidence"]
    )


def missing_value_status(raw_value):
    dictionary = load_dictionary()
    normalized = normalize_text(raw_value)
    groups = dictionary["parser_rules"]["missing_value_tokens"]

    for token in groups.get("not_declared", []):
        if normalized == normalize_text(token):
            return "not_declared"
    for token in groups.get("not_relevant", []):
        if normalized == normalize_text(token):
            return "not_relevant"
    for token in groups.get("not_applicable", []):
        if normalized == normalize_text(token):
            return "not_applicable"
    for token in groups.get("not_available", []):
        if normalized == normalize_text(token):
            return "not_available"

    return None
