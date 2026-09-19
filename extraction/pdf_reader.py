import pymupdf as fitz


def read_pdf(pdf_bytes):
    """
    Extract:
    1. page text
    2. positioned text blocks
    3. detected tables

    OCR is intentionally excluded from Phase 1.
    """

    document = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    pages = []
    all_text = []

    for page_index, page in enumerate(document):
        page_number = page_index + 1
        text = page.get_text("text") or ""
        blocks = page.get_text("blocks") or []

        tables = []
        try:
            finder = page.find_tables()
            for table_index, table in enumerate(finder.tables, start=1):
                extracted = table.extract()
                tables.append({
                    "table_number": table_index,
                    "cells": extracted,
                })
        except Exception:
            # Table detection can fail on irregular layouts.
            # Extraction continues from text and blocks.
            tables = []

        pages.append({
            "page_number": page_number,
            "text": text,
            "blocks": [
                {
                    "x0": block[0],
                    "y0": block[1],
                    "x1": block[2],
                    "y1": block[3],
                    "text": block[4],
                }
                for block in blocks
                if len(block) >= 5
            ],
            "tables": tables,
            "has_text": bool(text.strip()),
        })

        all_text.append(text)

    return {
        "page_count": len(document),
        "text": "\n".join(all_text),
        "pages": pages,
        "pages_without_text": [
            page["page_number"] for page in pages if not page["has_text"]
        ],
    }
