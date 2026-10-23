"""Local recall-review workflow. Candidate retrieval never certifies a product safe."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import uuid

API = "https://www.saferproducts.gov/RestWebServices/Recall"
MAX_BYTES = 2_000_000


def now():
    return datetime.now(timezone.utc).isoformat()


def token(value):
    return re.findall(r"[a-z0-9]+", value.casefold())


def text(value, name, required=True):
    if not isinstance(value, str) or len(value) > 160 or (required and not value.strip()):
        raise ValueError(f"{name}: use 1–160 characters" if required else f"{name}: invalid text")
    return value.strip()


def source_url(value):
    u = urlparse(value if isinstance(value, str) else "")
    if u.scheme != "https" or u.hostname not in {"www.cpsc.gov", "cpsc.gov"} or u.username or u.password or u.port not in (None, 443):
        raise ValueError("Recall must link to an HTTPS CPSC notice")
    return value


def parse_snapshot(payload, query, fetched_at, query_field="ProductName"):
    if len(payload) > MAX_BYTES:
        raise ValueError("Response exceeds size limit; results would be incomplete")
    rows = json.loads(payload)
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("Unexpected CPSC response; review stopped")
    clean = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("RecallID"), int):
            raise ValueError("Malformed recall record")
        source_url(row.get("URL"))
        for key in ("Title", "Description", "RecallNumber", "RecallDate"):
            if not isinstance(row.get(key), str):
                raise ValueError(f"Missing recall field: {key}")
        for key in ("Products", "Hazards", "Remedies"):
            if not isinstance(row.get(key), list) or not all(isinstance(x, dict) for x in row[key]):
                raise ValueError(f"Malformed recall field: {key}")
        if row.get("Manufacturers") is not None and (
                not isinstance(row["Manufacturers"], list) or not all(isinstance(x, dict) for x in row["Manufacturers"])):
            raise ValueError("Malformed recall field: Manufacturers")
        clean.append(row)
    return {"query": query, "fetched_at": fetched_at, "sha256": hashlib.sha256(payload).hexdigest(),
            "source": API + "?" + urlencode({"format": "json", query_field: query}),
            "coverage": "CPSC ProductName query only; no completeness or product-safety guarantee", "records": clean}


def fetch_snapshot(query):
    query = text(query, "brand")
    records, sources, errors = {}, [], []
    for field in ["ProductName", "RecallTitle", "Manufacturer"]:
        url = API + "?" + urlencode({"format": "json", field: query})
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "RecallHandoffPrototype/0.2"})
        try:
            with urlopen(req, timeout=20) as response:
                if response.status != 200:
                    raise ValueError("CPSC query did not return HTTP 200")
                snapshot = parse_snapshot(response.read(MAX_BYTES + 1), query, now(), field)
        except (OSError, ValueError) as error:
            errors.append({"field": field, "error": type(error).__name__})
            continue
        sources.append({"field": field, "url": url, "sha256": snapshot["sha256"],
                        "returned_rows": len(snapshot["records"])})
        for row in snapshot["records"]:
            if row["RecallID"] in records and records[row["RecallID"]] != row:
                raise ValueError("The same recall has conflicting versions across query responses; retry the source")
            records[row["RecallID"]] = row
    if not sources:
        raise ValueError("All CPSC queries failed; no fresh recall result is available")
    if len(records) > 500:
        raise ValueError("Combined CPSC response exceeds the review limit")
    combined = {"query": query, "query_sources": sources, "query_errors": errors,
                "records": sorted(records.values(), key=lambda row: row["RecallID"])}
    digest = hashlib.sha256(json.dumps(combined, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**combined, "fetched_at": now(), "sha256": digest, "source": sources[0]["url"],
            "query_status": "PARTIAL_QUERY_SET" if errors else "REQUESTED_QUERY_SET_COMPLETE",
            "coverage": "CPSC ProductName/RecallTitle/Manufacturer query union; " +
                        ("one or more requested queries failed; " if errors else "") +
                        "no completeness or product-safety guarantee"}


def candidates(snapshot, brand, model):
    brand = text(brand, "brand")
    model = text(model, "model", required=False)
    brand_tokens, model_tokens = token(brand), token(model)
    if not brand_tokens or (model and not model_tokens):
        raise ValueError("Use a brand/model containing letters or digits")
    result = []
    for row in snapshot["records"]:
        product_text = " ".join(str(x.get("Name", "")) + " " + str(x.get("Model", "")) for x in row["Products"])
        words = token(row["Title"] + " " + row["Description"] + " " + product_text)
        if not all(x in words for x in brand_tokens):
            continue  # Avoid Anker / Trankerloop substring collisions.
        model_in_text = bool(model_tokens) and any(words[i:i + len(model_tokens)] == model_tokens for i in range(len(words)))
        result.append({"recall_id": row["RecallID"], "title": row["Title"], "notice_url": row["URL"],
                       "notice_date": row["RecallDate"], "model_token_found": model_in_text,
                       "manufacturer_names": [str(item.get("Name", "")) for item in (row.get("Manufacturers") or [])],
                       "company_name_review": "Compare the full manufacturer name; similar brand words can identify different companies",
                       "next_action": "Compare model, serial/batch and purchase-region conditions in the official notice",
                       "status": "REQUIRES_PRODUCT_VERIFICATION"})
    return sorted(result, key=lambda x: (x["model_token_found"], x["notice_date"]), reverse=True)


class Ledger:
    """A separate local SQLite file per user/demo; no external account actions."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    @contextmanager
    def transaction(self):
        # sqlite3's context manager commits/rolls back, but does not close the
        # connection. Close explicitly so Windows releases the database handle
        # immediately on both successful operations and rejected state changes.
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, case_id):
        with self.transaction() as db:
            row = db.execute("SELECT body FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown case")
        return json.loads(row[0])

    def store(self, case):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO cases VALUES (?,?)", (case["id"], json.dumps(case)))
        return case

    def create(self, snapshot, brand, model, recall_id, confirmed=False):
        if confirmed is not True:
            raise ValueError("Confirm saving this review case first")
        allowed = {x["recall_id"] for x in candidates(snapshot, brand, model)}
        if recall_id not in allowed:
            raise ValueError("Recall is not a candidate for this brand")
        row = next(x for x in snapshot["records"] if x["RecallID"] == recall_id)
        return self.store({"id": uuid.uuid4().hex, "brand": brand, "model": model,
            "status": "REVIEW_OPEN", "created_at": now(), "notice": row,
            "snapshot": {k: v for k, v in snapshot.items() if k != "records"},
            "steps": {"notice_conditions_checked": False, "manufacturer_verification_done": False,
                      "remedy_request_recorded": False, "outcome_recorded": False}, "events": []})

    def advance(self, case_id, step, note, confirmed=False):
        if confirmed is not True:
            raise ValueError("A user must confirm each recorded action")
        note = text(note, "note")
        return self._change(case_id, step, note)

    def close(self, case_id, reason, note, confirmed=False):
        if confirmed is not True:
            raise ValueError("A user must confirm closing a review")
        if reason not in {"NOT_AFFECTED", "STOPPED", "MANUFACTURER_UNRESOLVED"}:
            raise ValueError("Unknown review outcome")
        return self._change(case_id, None, text(note, "note"), reason)

    def _change(self, case_id, step, note, reason=None):
        # Acquire the write reservation before reading so two tool calls cannot
        # validate the same old state and silently overwrite one another.
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown case")
            case = json.loads(row[0])
            if case["status"] != "REVIEW_OPEN":
                raise ValueError("Review is already closed")
            if reason:
                if reason == "NOT_AFFECTED" and not case["steps"]["manufacturer_verification_done"]:
                    raise ValueError("Record manufacturer verification before reporting not affected")
                case["status"] = "USER_REPORTED_" + reason
                case["events"].append({"step": "review_closed", "reason": reason, "note": note, "at": now(), "kind": "USER_REPORTED"})
            else:
                self._advance_case(case, step, note)
            db.execute("UPDATE cases SET body=? WHERE id=?", (json.dumps(case), case_id))
        return case

    @staticmethod
    def _advance_case(case, step, note):
        steps = list(case["steps"])
        if step not in steps:
            raise ValueError("Unknown workflow step")
        index = steps.index(step)
        if not all(case["steps"][k] for k in steps[:index]):
            raise ValueError("Record earlier verification steps first")
        if case["steps"][step]:
            raise ValueError("Step already recorded; duplicate action refused")
        case["steps"][step] = True
        case["events"].append({"step": step, "note": note, "at": now(), "kind": "USER_REPORTED"})
        case["status"] = "USER_REPORTED_COMPLETE" if all(case["steps"].values()) else "REVIEW_OPEN"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--snapshot", type=Path, help="Replay saved CPSC JSON; does not refresh it")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = parse_snapshot(args.snapshot.read_bytes(), args.brand, "UNKNOWN_REPLAY_TIME") if args.snapshot else fetch_snapshot(args.brand)
    report = {"mode": "REPLAY" if args.snapshot else "LIVE_PUBLIC_QUERY", "snapshot": {k: v for k, v in snapshot.items() if k != "records"},
              "returned_rows": len(snapshot["records"]), "candidates": candidates(snapshot, args.brand, args.model),
              "product_safety_determination": "NOT_MADE", "model_inference": "NOT_RUN"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"returned_rows": report["returned_rows"], "candidates": len(report["candidates"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
