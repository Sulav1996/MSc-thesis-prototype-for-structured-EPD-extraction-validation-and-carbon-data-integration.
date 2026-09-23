"""
PDF reading layer ("what physically exists in the PDF").

Returns, for every page: plain text, positioned text blocks, positioned words
and detected tables. Interpretation of that content happens in the extractors.

Backends
--------
* PyMuPDF (``pymupdf``) – primary backend, used on Render and locally.
* pdfplumber – optional fallback when PyMuPDF is not installed. It produces the
  same structure, so the extractors work unchanged.

OCR is intentionally not included: pages without a text layer are reported in
``pages_without_text`` so the reviewer knows they need manual entry.
"""

from __future__ import annotations

import io

try:  # PyMuPDF >= 1.24 exposes "pymupdf"; older builds only "fitz".
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - depends on the installed wheel
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

try:
    import pdfplumber  # type: ignore
except ImportError:  # pragma: no cover
    pdfplumber = None


def available_backend() -> str | None:
    if fitz is not None:
        return "pymupdf"
    if pdfplumber is not None:
        return "pdfplumber"
    return None


def _clean_table(rows) -> list[list[str]]:
    return [["" if cell is None else str(cell) for cell in row] for row in rows or []]


def _pymupdf_title_lines(page) -> list[dict]:
    """Text lines of a page with their largest font size (used to find the cover title)."""
    lines = []
    try:
        data = page.get_text("dict")
    except Exception:
        return lines
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
            if not spans:
                continue
            text = " ".join(span["text"].strip() for span in spans).strip()
            lines.append({"text": text, "size": round(max(span.get("size", 0) for span in spans), 1),
                          "y0": line.get("bbox", [0, 0])[1]})
    return lines


def _plumber_title_lines(page) -> list[dict]:
    lines = []
    try:
        words = page.extract_words(extra_attrs=["size"])
    except Exception:
        return lines
    rows: dict = {}
    for word in words:
        key = round(word["top"] / 3)
        rows.setdefault(key, []).append(word)
    for key in sorted(rows):
        group = sorted(rows[key], key=lambda w: w["x0"])
        lines.append({"text": " ".join(w["text"] for w in group),
                      "size": round(max(w.get("size", 0) for w in group), 1), "y0": group[0]["top"]})
    return lines


def _read_with_pymupdf(pdf_bytes: bytes, detect_tables: bool = True) -> dict:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    title_lines: list[dict] = []
    try:
        for page_index, page in enumerate(document):
            text = page.get_text("text") or ""
            blocks = page.get_text("blocks") or []
            words = page.get_text("words") or []

            tables = []
            if detect_tables and text.strip():
                try:
                    finder = page.find_tables()
                    for table_index, table in enumerate(finder.tables, start=1):
                        tables.append({"table_number": table_index, "cells": _clean_table(table.extract()),
                                       "bbox": list(table.bbox)})
                except Exception:
                    # Table detection can fail on irregular layouts; text/words remain usable.
                    tables = []

            if page_index == 0:
                title_lines = _pymupdf_title_lines(page)

            pages.append({
                "page_number": page_index + 1,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "text": text,
                "blocks": [
                    {"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3], "text": b[4]}
                    for b in blocks if len(b) >= 5
                ],
                "words": [
                    {"x0": w[0], "y0": w[1], "x1": w[2], "y1": w[3], "text": w[4]}
                    for w in words if len(w) >= 5
                ],
                "tables": tables,
                "has_text": bool(text.strip()),
            })
        metadata = dict(document.metadata or {})
    finally:
        document.close()
    return {"pages": pages, "pdf_metadata": metadata, "title_lines": title_lines}


def _read_with_pdfplumber(pdf_bytes: bytes, detect_tables: bool = True) -> dict:
    pages = []
    title_lines: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_index, page in enumerate(pdf.pages):
            if page_index == 0:
                title_lines = _plumber_title_lines(page)
            text = page.extract_text() or ""
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
            tables = []
            if detect_tables and text.strip():
                try:
                    for table_index, table in enumerate(page.find_tables(), start=1):
                        tables.append({"table_number": table_index, "cells": _clean_table(table.extract()),
                                       "bbox": list(table.bbox)})
                except Exception:
                    tables = []
            pages.append({
                "page_number": page_index + 1,
                "width": float(page.width),
                "height": float(page.height),
                "text": text,
                "blocks": [],
                "words": [
                    {"x0": w["x0"], "y0": w["top"], "x1": w["x1"], "y1": w["bottom"], "text": w["text"]}
                    for w in words
                ],
                "tables": tables,
                "has_text": bool(text.strip()),
            })
        metadata = dict(pdf.metadata or {})
    return {"pages": pages, "pdf_metadata": metadata, "title_lines": title_lines}


def read_pdf(pdf_bytes: bytes, detect_tables: bool = True) -> dict:
    """
    Extract page text, positioned blocks, positioned words and tables.

    Raises RuntimeError when no PDF backend is installed.
    """
    backend = available_backend()
    if backend == "pymupdf":
        result = _read_with_pymupdf(pdf_bytes, detect_tables)
    elif backend == "pdfplumber":
        result = _read_with_pdfplumber(pdf_bytes, detect_tables)
    else:
        raise RuntimeError("No PDF reader installed. Install PyMuPDF: pip install pymupdf")

    pages = result["pages"]
    return {
        "backend": backend,
        "page_count": len(pages),
        "text": "\n".join(page["text"] for page in pages),
        "pages": pages,
        "pages_without_text": [page["page_number"] for page in pages if not page["has_text"]],
        "pdf_metadata": result.get("pdf_metadata", {}),
        "title_lines": result.get("title_lines", []),
    }
