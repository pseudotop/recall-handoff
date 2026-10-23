"""Explicit, offline fictional input. Never used by the production CPSC parser."""
import hashlib
import json
from pathlib import Path

from recall_handoff import MAX_BYTES, now, text

MODE = "SYNTHETIC_DEMO"
SOURCE = "https://example.invalid/recall-handoff/synthetic-demo-v1"
DISCLAIMER = "SYNTHETIC DEMONSTRATION — fictional data; no actual recall, owned product or safety determination."


def load_demo_fixture(path):
    with Path(path).open("rb") as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("Synthetic fixture exceeds the size limit")
    document = json.loads(payload)
    if not isinstance(document, dict) or document.get("mode") != MODE:
        raise ValueError("An explicitly labelled synthetic demo fixture is required")
    rows = document.get("records")
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("The synthetic demo requires one fictional record")
    row = rows[0]
    if not isinstance(row, dict) or type(row.get("RecallID")) is not int or row.get("URL") != SOURCE:
        raise ValueError("Synthetic data must use its non-official example.invalid source")
    for key in ("Title", "Description", "RecallNumber", "RecallDate"):
        if not isinstance(row.get(key), str):
            raise ValueError("Malformed synthetic record")
    for key in ("Products", "Hazards", "Remedies", "Manufacturers"):
        if not isinstance(row.get(key), list) or not all(isinstance(item, dict) for item in row[key]):
            raise ValueError("Malformed synthetic record")
    if row["Manufacturers"] != [{"Name": "Demo Manufacturer 01"}] or row["Products"] != [{"Name": "Sample appliance 01", "Model": "SAMPLE-01"}]:
        raise ValueError("Use only the declared fictional manufacturer and product")
    return {"mode": MODE, "source": SOURCE, "sha256": hashlib.sha256(payload).hexdigest(),
            "coverage": DISCLAIMER, "query_status": "SYNTHETIC_FIXTURE_ONLY",
            "fetched_at": "NOT_FETCHED_SYNTHETIC_FIXTURE", "records": rows}


def demo_snapshot(fixture, query):
    return {**fixture, "query": text(query, "brand"), "loaded_at": now()}


def require_case_mode(case, demo_mode):
    synthetic = case.get("snapshot", {}).get("mode") == MODE
    if synthetic != bool(demo_mode):
        raise ValueError("This review belongs to a different source mode; use its original mode/database")
    return case
