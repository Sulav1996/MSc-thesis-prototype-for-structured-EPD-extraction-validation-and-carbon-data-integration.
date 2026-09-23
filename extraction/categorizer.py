"""
Step 2 — categorise an EPD as a building material or component.

Transparent, rule-based scoring (no AI): each category in
``material_categories`` (dictionary v4) has strong phrases, weak words, regex
patterns and PCR hints in English, Danish and German. Every match adds
``keyword weight × field weight``; fields are product name, ILCD
classification, PCR, product category, description, cover page and full text.

When a longer phrase of one category contains a shorter keyword of another
(e.g. "roof windows" contains "windows"), only the longer phrase counts, so a
VELUX roof window becomes "Skylight / roof window" (parent category "Window").

The result lists the evidence, a confidence value and alternatives so the
reviewer can confirm or override it in the UI.
"""

from __future__ import annotations

import re
from functools import lru_cache

from dictionary import material_categories, normalize_text


@lru_cache(maxsize=1)
def _compiled_categories():
    taxonomy = material_categories()
    compiled = []
    for category in taxonomy.get("categories", []):
        rules = []
        for kind in ("strong", "weak"):
            for keyword in category.get(kind, []):
                norm = normalize_text(keyword)
                pattern = re.compile(rf"(?<![\w-]){re.escape(norm)}(?![\w])")
                rules.append((kind, keyword, pattern, len(norm)))
        for regex in category.get("patterns", []):
            rules.append(("pattern", regex, re.compile(regex, re.IGNORECASE), 0))
        pcr = [normalize_text(hint) for hint in category.get("pcr_hints", [])]
        compiled.append((category, rules, pcr))
    return taxonomy, compiled


def _field_texts(record: dict, full_text: str) -> dict:
    metadata = record.get("metadata", {})
    ilcd_classes = " / ".join(metadata.get("ilcd_classification", []) or [])
    cover = ""
    pages = full_text.split("\f") if "\f" in full_text else [full_text[:3000]]
    if pages:
        cover = pages[0][:3000]
    return {
        "ilcd_classification": ilcd_classes,
        "product_name": metadata.get("product_name") or "",
        "pcr": metadata.get("pcr") or "",
        "product_category": metadata.get("product_category") or "",
        "product_description": (metadata.get("product_description") or "")[:3000],
        "cover_text": cover,
        "full_text": full_text[:60000],
    }


def categorize_epd(record: dict, full_text: str = "") -> dict:
    taxonomy, compiled = _compiled_categories()
    field_weights = taxonomy.get("field_weights", {})
    keyword_weights = taxonomy.get("keyword_weights", {"strong": 3.0, "weak": 1.0, "pattern": 2.5, "pcr_hint": 2.0})
    texts = {field: normalize_text(text) for field, text in _field_texts(record, full_text).items()}

    scores: dict[str, float] = {}
    evidence: dict[str, list] = {}

    for field, text in texts.items():
        if not text:
            continue
        weight = field_weights.get(field, 1.0)

        # 1) collect all keyword hits with their spans for every category
        hits = []
        for category, rules, _ in compiled:
            for kind, keyword, pattern, length in rules:
                for match in pattern.finditer(text):
                    hits.append((category["id"], kind, keyword, match.start(), match.end()))
                    if field == "full_text" and len(hits) > 4000:
                        break

        # 2) drop hits that lie inside a longer hit of another category
        by_start: dict[int, list] = {}
        for hit in hits:
            by_start.setdefault(hit[3], []).append(hit)
        kept = []
        for hit in hits:
            cid, kind, keyword, start, end = hit
            contained = False
            for offset in range(max(0, start - 60), start + 1):
                for other in by_start.get(offset, ()):
                    if other[0] != cid and other[4] >= end and (other[4] - other[3]) > (end - start):
                        contained = True
                        break
                if contained:
                    break
            if not contained:
                kept.append(hit)

        # 3) score, capping repeated hits of the same keyword per field
        per_keyword: dict[tuple, int] = {}
        for cid, kind, keyword, start, end in kept:
            key = (cid, keyword)
            per_keyword[key] = per_keyword.get(key, 0) + 1
            if per_keyword[key] > (1 if field != "full_text" else 5):
                continue
            points = keyword_weights.get(kind, 1.0) * weight
            scores[cid] = scores.get(cid, 0.0) + points
            if len(evidence.setdefault(cid, [])) < 12:
                evidence[cid].append({"field": field, "keyword": keyword, "kind": kind, "points": round(points, 2)})

        # 4) PCR hints (only on the PCR / category fields)
        if field in ("pcr", "product_category", "ilcd_classification"):
            for category, _, pcr_hints in compiled:
                for hint in pcr_hints:
                    if hint and hint in text:
                        points = keyword_weights.get("pcr_hint", 2.0) * weight / max(1, len(pcr_hints) // 2)
                        scores[category["id"]] = scores.get(category["id"], 0.0) + points
                        evidence.setdefault(category["id"], []).append(
                            {"field": field, "keyword": hint, "kind": "pcr_hint", "points": round(points, 2)})
                        break

    by_id = {category["id"]: category for category, _, _ in compiled}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    fallback = taxonomy.get("fallback", {"id": "other", "label": "Other / unclassified", "building_element": "Unknown"})

    if not ranked or ranked[0][1] < 3.0:
        return {
            "category": fallback["id"],
            "category_label": fallback["label"],
            "parent_category": None,
            "building_element": fallback.get("building_element"),
            "confidence": 0.0,
            "method": "keyword_weighted_v1",
            "evidence": [],
            "alternatives": [{"category": cid, "label": by_id[cid]["label"], "score": round(score, 2)}
                             for cid, score in ranked[:3]],
            "oekobaudat_categories": [],
            "cpa_hint": [],
            "user_override": None,
        }

    top_id, top_score = ranked[0]
    # Prefer a specialised child (e.g. "skylight") over its parent ("window") when the child is
    # named in the product name / ILCD classification and scores at least half of the parent.
    for cid, score in ranked[1:]:
        child = by_id.get(cid, {})
        if child.get("parent") == top_id and score >= 0.5 * top_score and any(
                e["field"] in ("product_name", "ilcd_classification") for e in evidence.get(cid, [])):
            top_id, top_score = cid, score
            break
    # A parent category (e.g. "window" for "skylight") supports its child, so the margin is
    # measured against the best competitor that is neither the parent nor a child.
    related = {by_id[top_id].get("parent")} | {cid for cid, c in by_id.items() if c.get("parent") == top_id}
    competitors = [score for cid, score in ranked if cid not in related and cid != top_id]
    second = competitors[0] if competitors else 0.0
    # Confidence grows with the margin to the runner-up and with the absolute score.
    margin = max(0.0, (top_score - second) / top_score)
    confidence = round(min(0.98, 0.45 + 0.4 * margin + 0.15 * min(1.0, top_score / 30.0)), 2)
    category = by_id[top_id]
    parent = category.get("parent")

    return {
        "category": top_id,
        "category_label": category["label"],
        "parent_category": parent,
        "parent_category_label": by_id[parent]["label"] if parent in by_id else None,
        "building_element": category.get("building_element"),
        "confidence": confidence,
        "score": round(top_score, 2),
        "method": "keyword_weighted_v1",
        "evidence": sorted(evidence.get(top_id, []), key=lambda e: e["points"], reverse=True),
        "alternatives": [{"category": cid, "label": by_id[cid]["label"], "score": round(score, 2)}
                         for cid, score in ranked if cid != top_id][:3],
        "oekobaudat_categories": category.get("oekobaudat_categories", []),
        "cpa_hint": category.get("cpa_hint", []),
        "user_override": None,
    }


def category_options() -> list[tuple[str, str]]:
    """(id, label) for every category, used by the review drop-down."""
    taxonomy, compiled = _compiled_categories()
    options = [(category["id"], category["label"]) for category, _, _ in compiled]
    fallback = taxonomy.get("fallback", {"id": "other", "label": "Other / unclassified"})
    return options + [(fallback["id"], fallback["label"])]


def apply_category_override(classification: dict, category_id: str | None) -> dict:
    """Store a reviewer's choice without losing the automatic suggestion."""
    if not category_id or category_id == classification.get("category"):
        return classification
    taxonomy, compiled = _compiled_categories()
    by_id = {category["id"]: category for category, _, _ in compiled}
    chosen = by_id.get(category_id)
    updated = dict(classification)
    updated["user_override"] = {
        "from": classification.get("category"),
        "to": category_id,
    }
    updated["category"] = category_id
    if chosen:
        updated.update({
            "category_label": chosen["label"],
            "parent_category": chosen.get("parent"),
            "building_element": chosen.get("building_element"),
            "oekobaudat_categories": chosen.get("oekobaudat_categories", []),
            "cpa_hint": chosen.get("cpa_hint", []),
        })
    else:
        fallback = taxonomy.get("fallback", {})
        updated.update({"category_label": fallback.get("label", category_id), "parent_category": None,
                        "building_element": fallback.get("building_element")})
    updated["confidence"] = 1.0
    updated["method"] = "human_review"
    return updated
