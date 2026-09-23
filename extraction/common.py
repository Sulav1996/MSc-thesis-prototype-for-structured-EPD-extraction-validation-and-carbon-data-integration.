"""Shared parsing helpers for the EPD extractors (numbers, dates, text, provenance)."""

from __future__ import annotations

import re
from datetime import date

from dictionary import normalize_text

# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"^[+\-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[Ee][+\-]?\d+)?$")
_FOOTNOTE_RE = re.compile(r"(?<=\d)[*¹²³⁴⁵⁶⁷⁸⁹⁰†‡]+$|(?<=\d)\s*\d\)$")


def parse_number(raw):
    """
    Parse EPD numbers conservatively.

    Handles 1.04E+02, 2,88E+00, 2.88 E+00, −1,30E+00, 1 850, 1,234.5, 1.234,5 and
    trailing footnote markers (4.3E-3*). Returns None for anything else so that
    text such as MND/ND is never turned into a number.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip().replace("−", "-").replace("‐", "-").replace("–", "-")
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    text = _FOOTNOTE_RE.sub("", text).strip()
    text = re.sub(r"\s+(?=[Ee][+\-]?\d+$)", "", text)          # "2.88 E+00"
    text = re.sub(r"(?<=[Ee])\s+(?=[+\-]?\d+$)", "", text)      # "2.88E +00"
    text = re.sub(r"(?<=\d)[ ](?=\d{3}(?:\D|$))", "", text)     # "1 850" thousands space
    if not text:
        return None

    mantissa, exponent = text, ""
    exp_match = re.search(r"[Ee][+\-]?\d+$", text)
    if exp_match:
        mantissa, exponent = text[:exp_match.start()], exp_match.group(0)

    if "," in mantissa and "." in mantissa:
        # The right-most separator is the decimal separator.
        if mantissa.rfind(",") > mantissa.rfind("."):
            mantissa = mantissa.replace(".", "").replace(",", ".")
        else:
            mantissa = mantissa.replace(",", "")
    elif "," in mantissa:
        if mantissa.count(",") > 1:
            return None
        mantissa = mantissa.replace(",", ".")

    candidate = mantissa + exponent
    if not NUMBER_RE.match(candidate):
        return None
    try:
        return float(candidate)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def clean_cell(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\xa0", " ")).strip()


def clean_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()
        for line in (text or "").splitlines()
        if line.strip()
    ]


def sentence_around(text: str, start: int, end: int, limit: int = 320) -> str:
    """Return the sentence containing text[start:end], trimmed to ``limit`` characters."""
    left = max(text.rfind(".", 0, start), text.rfind("\n\n", 0, start))
    right_candidates = [pos for pos in (text.find(".", end), text.find("\n\n", end)) if pos != -1]
    right = min(right_candidates) if right_candidates else len(text)
    snippet = re.sub(r"\s+", " ", text[left + 1:right + 1]).strip()
    if len(snippet) > limit:
        middle = (start - left)
        lo = max(0, middle - limit // 2)
        snippet = snippet[lo:lo + limit].strip()
    return snippet


def find_page(raw_document: dict, needle: str) -> int | None:
    """Page number of the first page whose normalised text contains ``needle``."""
    target = normalize_text(needle)[:80]
    if not target:
        return None
    for page in raw_document.get("pages", []):
        if target in normalize_text(page.get("text", "")):
            return page["page_number"]
    return None


def evidence(page, source_text, method, confidence, **extra) -> dict:
    item = {
        "source_page": page,
        "source_text": (source_text or "")[:500] or None,
        "extraction_method": method,
        "confidence": round(float(confidence), 3) if confidence is not None else None,
    }
    item.update({key: value for key, value in extra.items() if value is not None})
    return item


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

_MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    # Danish / Norwegian
    "januar": 1, "februar": 2, "marts": 3, "mars": 3, "maj": 5, "juni": 6, "juli": 7,
    "oktober": 10, "desember": 12,
    # German
    "januar_de": 1, "märz": 3, "maerz": 3, "mai": 5, "dezember": 12,
}


def _safe_date(year: int, month: int, day: int):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(raw) -> dict | None:
    """
    Parse EPD date strings into ISO format.

    Returns {'iso', 'raw', 'format', 'ambiguous'} or None. Numeric day/month
    order is resolved European-style (dd/mm/yyyy) unless a part is > 12.
    A bare year returns the 1st of January with precision 'year'.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    low = normalize_text(text)

    match = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", low)
    if match:
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            return {"iso": parsed.isoformat(), "raw": text, "format": "yyyy-mm-dd", "ambiguous": False}

    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", low)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        if first > 12 and second <= 12:
            parsed, fmt, ambiguous = _safe_date(year, second, first), "dd/mm/yyyy", False
        elif second > 12 and first <= 12:
            parsed, fmt, ambiguous = _safe_date(year, first, second), "mm/dd/yyyy", False
        else:
            parsed, fmt, ambiguous = _safe_date(year, second, first), "dd/mm/yyyy", first != second
        if parsed:
            return {"iso": parsed.isoformat(), "raw": text, "format": fmt, "ambiguous": ambiguous}

    match = re.search(r"\b(\d{1,2})\.?\s+([a-zæøåäöü]+)\.?,?\s+(\d{4})\b", low)
    if match and match.group(2) in _MONTHS:
        parsed = _safe_date(int(match.group(3)), _MONTHS[match.group(2)], int(match.group(1)))
        if parsed:
            return {"iso": parsed.isoformat(), "raw": text, "format": "d month yyyy", "ambiguous": False}

    match = re.search(r"\b([a-zæøåäöü]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b", low)
    if match and match.group(1) in _MONTHS:
        parsed = _safe_date(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))
        if parsed:
            return {"iso": parsed.isoformat(), "raw": text, "format": "month d yyyy", "ambiguous": False}

    match = re.search(r"\b([a-zæøåäöü]+)\.?\s+(\d{4})\b", low)
    if match and match.group(1) in _MONTHS:
        parsed = _safe_date(int(match.group(2)), _MONTHS[match.group(1)], 1)
        if parsed:
            return {"iso": parsed.isoformat(), "raw": text, "format": "month yyyy", "ambiguous": False,
                    "precision": "month"}

    match = re.fullmatch(r"\s*(19|20)(\d{2})\s*", low)
    if match:
        return {"iso": f"{match.group(1)}{match.group(2)}-01-01", "raw": text, "format": "yyyy",
                "ambiguous": False, "precision": "year"}
    return None


def resolve_validity_dates(publication_raw, valid_raw) -> tuple[dict | None, dict | None]:
    """
    Parse issue and expiry dates together. When both are ambiguous numeric dates,
    prefer the interpretation that gives the usual ~5-year EN 15804 validity.
    """
    pub = parse_date(publication_raw)
    val = parse_date(valid_raw)
    if not (pub and val and pub.get("ambiguous") and val.get("ambiguous")):
        return pub, val

    def swap(item):
        y, m, d = map(int, item["iso"].split("-"))
        swapped = _safe_date(y, d, m)
        return {**item, "iso": swapped.isoformat(), "format": "mm/dd/yyyy"} if swapped else item

    def years_between(a, b):
        return (date.fromisoformat(b["iso"]) - date.fromisoformat(a["iso"])).days / 365.25

    best = (pub, val)
    best_score = abs(years_between(pub, val) - 5)
    alt = (swap(pub), swap(val))
    alt_score = abs(years_between(*alt) - 5)
    if alt_score + 0.05 < best_score:
        best = alt
    return best


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "English": {"the", "and", "of", "is", "for", "with", "are", "this", "product", "which"},
    "Danish": {"og", "af", "til", "er", "med", "som", "det", "på", "for", "produktet", "ikke"},
    "German": {"und", "der", "die", "das", "ist", "mit", "für", "von", "den", "nicht"},
    "Norwegian": {"og", "av", "til", "er", "med", "som", "det", "på", "ikke", "produktet"},
    "Swedish": {"och", "av", "till", "är", "med", "som", "det", "på", "för", "inte"},
    "French": {"le", "la", "les", "et", "des", "est", "pour", "avec", "une", "du"},
    "Dutch": {"de", "het", "en", "van", "is", "voor", "met", "een", "niet", "op"},
}


def detect_language(text: str) -> tuple[str | None, float]:
    words = re.findall(r"[a-zæøåäöüé]+", (text or "")[:20000].lower())
    if not words:
        return None, 0.0
    counts = {lang: sum(1 for word in words if word in stop) for lang, stop in _STOPWORDS.items()}
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    top, top_count = ranked[0]
    second_count = ranked[1][1] if len(ranked) > 1 else 0
    if top_count < 5:
        return None, 0.0
    confidence = min(0.95, 0.5 + (top_count - second_count) / max(top_count, 1) * 0.5)
    return top, round(confidence, 2)


# ---------------------------------------------------------------------------
# positioned words → lines → column-aware segments
# ---------------------------------------------------------------------------

def group_lines(words: list[dict]) -> list[list[dict]]:
    """Group positioned words into text lines (sorted top-to-bottom, left-to-right)."""
    if not words:
        return []
    heights = sorted(w["y1"] - w["y0"] for w in words if w["y1"] > w["y0"])
    typical = heights[len(heights) // 2] if heights else 8.0
    tolerance = typical * 0.55
    ordered = sorted(words, key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"]))
    lines, current, current_y = [], [], None
    for word in ordered:
        y = (word["y0"] + word["y1"]) / 2
        if current and abs(y - current_y) > tolerance:
            lines.append(sorted(current, key=lambda w: w["x0"]))
            current = []
        current.append(word)
        current_y = y if len(current) == 1 else (current_y * (len(current) - 1) + y) / len(current)
    if current:
        lines.append(sorted(current, key=lambda w: w["x0"]))
    return lines


def page_segments(page: dict) -> list[dict]:
    """
    Column-aware text segments of a page.

    Words on one visual line are split where the horizontal gap is large, so a
    two-column layout ("Declaration number   Scope:") yields two segments.
    Cached on the page dict.
    """
    if "_segments" in page:
        return page["_segments"]
    segments = []
    for line in group_lines(page.get("words") or []):
        heights = [w["y1"] - w["y0"] for w in line if w["y1"] > w["y0"]]
        height = max(heights) if heights else 8.0
        threshold = max(12.0, 1.6 * height)
        current = [line[0]]
        for word in line[1:]:
            if word["x0"] - current[-1]["x1"] > threshold:
                segments.append(current)
                current = []
            current.append(word)
        segments.append(current)
    out = []
    for words in segments:
        out.append({
            "text": " ".join(w["text"] for w in words),
            "x0": min(w["x0"] for w in words), "x1": max(w["x1"] for w in words),
            "y0": min(w["y0"] for w in words), "y1": max(w["y1"] for w in words),
        })
    page["_segments"] = out
    return out


def segment_below(segments: list[dict], segment: dict, max_lines: float = 3.0) -> dict | None:
    """Nearest segment below ``segment`` in the same column (horizontal overlap)."""
    height = max(segment["y1"] - segment["y0"], 6.0)
    best = None
    for other in segments:
        if other is segment or other["y0"] <= segment["y0"] + height * 0.5:
            continue
        if other["y0"] - segment["y1"] > height * max_lines:
            continue
        overlap = min(other["x1"], segment["x1"]) - max(other["x0"], segment["x0"])
        if overlap <= 0 and abs(other["x0"] - segment["x0"]) > 20:
            continue
        if best is None or other["y0"] < best["y0"]:
            best = other
    return best


def column_text_below(segments: list[dict], segment: dict, stop=None, max_chars: int = 1500) -> str:
    """Concatenate the segments below ``segment`` in the same column until ``stop(text)`` is true."""
    collected, current = [], segment
    while True:
        nxt = segment_below(segments, current, max_lines=2.5)
        if nxt is None or (stop and stop(nxt["text"])):
            break
        collected.append(nxt["text"])
        current = nxt
        if sum(len(item) for item in collected) > max_chars:
            break
    return " ".join(collected).strip()
