import json
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "epds.json"
UPLOAD_DIR = BASE_DIR / "uploads"


def load_epds():
    if not DATA_FILE.exists():
        return []

    text = DATA_FILE.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("data/epds.json must contain a JSON list.")
    return data


def _safe_filename(name):
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "epd.pdf"


def save_uploaded_pdf(pdf_bytes, original_name):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{timestamp}_{_safe_filename(original_name)}"
    path = UPLOAD_DIR / filename
    path.write_bytes(pdf_bytes)
    return path


def save_epd(record):
    database = load_epds()
    database.append(record)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(database, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
