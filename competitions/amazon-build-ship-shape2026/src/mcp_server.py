"""Real MCP tools for local recall evidence and resumable review; no Alexa connection."""
from pathlib import Path
import argparse
from mcp.server.fastmcp import FastMCP
from recall_handoff import Ledger, fetch_snapshot, candidates
from demo_fixture import load_demo_fixture, demo_snapshot, require_case_mode


def build_server(db_path, port=8765, demo_fixture=None):
    ledger = Ledger(db_path)
    fixture = load_demo_fixture(demo_fixture) if demo_fixture is not None else None
    snapshots = {}
    server = FastMCP("Recall Handoff", host="127.0.0.1", port=port, stateless_http=True, json_response=True)

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    def search_notices(brand: str, model: str = "") -> dict:
        """Retrieve public-source candidates, or fictional candidates only in explicit demo mode. Never a safety clearance."""
        snapshot = demo_snapshot(fixture, brand) if fixture is not None else fetch_snapshot(brand)
        key = snapshot["sha256"]
        snapshots[key] = snapshot
        while len(snapshots) > 16:
            del snapshots[next(iter(snapshots))]
        return {"snapshot_id": key, "fetched_at": snapshot["fetched_at"], "coverage": snapshot["coverage"],
                "query_status": snapshot.get("query_status", "SINGLE_QUERY_REPLAY"),
                "query_sources": snapshot.get("query_sources", []), "query_errors": snapshot.get("query_errors", []),
                "candidates": candidates(snapshot, brand, model), "product_safety_determination": "NOT_MADE"}

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def save_case(snapshot_id: str, brand: str, model: str, recall_id: int, user_confirmed: bool = False) -> dict:
        """Only after explicit user confirmation, save a candidate to the local review ledger."""
        if snapshot_id not in snapshots:
            raise ValueError("Search again; this snapshot is no longer available")
        return ledger.create(snapshots[snapshot_id], brand, model, recall_id, user_confirmed)

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def resume_case(case_id: str) -> dict:
        """Resume previously saved evidence and user-reported actions after a server restart."""
        return require_case_mode(ledger.load(case_id), fixture is not None)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def record_action(case_id: str, step: str, note: str, user_confirmed: bool = False) -> dict:
        """Record an action the USER performed; does not contact a manufacturer or apply for a refund."""
        require_case_mode(ledger.load(case_id), fixture is not None)
        return ledger.advance(case_id, step, note, user_confirmed)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def close_review(case_id: str, reason: str, note: str, user_confirmed: bool = False) -> dict:
        """Record user-reported NOT_AFFECTED, STOPPED or MANUFACTURER_UNRESOLVED without inventing a remedy request."""
        require_case_mode(ledger.load(case_id), fixture is not None)
        return ledger.close(case_id, reason, note, user_confirmed)

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo-fixture", type=Path, help="Explicit fictional offline input; no CPSC requests")
    args = parser.parse_args()
    build_server(args.db, args.port, args.demo_fixture).run(transport="streamable-http")
