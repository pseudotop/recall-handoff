import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

spec = importlib.util.spec_from_file_location("recall_handoff", Path(__file__).parents[1] / "src/recall_handoff.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def fixture():
    """Invented test records only: not official recall evidence."""
    return [{"RecallID": i, "RecallNumber": str(i), "Title": title, "Description": description,
             "RecallDate": "2026-01-01", "URL": "https://www.cpsc.gov/Recalls/test",
             "Products": [], "Hazards": [], "Remedies": []}
            for i, title, description in [(1, "ExampleCo chargers", "Models X100 and X200; verify serial"),
                                           (2, "NotExampleCo chairs", "X1000 only")]]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = module.Ledger(self.db)
        self.snapshot = module.parse_snapshot(json.dumps(fixture()).encode(), "ExampleCo", "2026-01-01T00:00:00Z")

    def test_substring_brand_is_not_a_match(self):
        self.assertEqual([x["recall_id"] for x in module.candidates(self.snapshot, "ExampleCo", "X100")], [1])

    def test_model_prefix_is_not_exact_model_evidence(self):
        c = module.candidates(self.snapshot, "NotExampleCo", "X100")[0]
        self.assertFalse(c["model_token_found"])
        self.assertEqual(c["status"], "REQUIRES_PRODUCT_VERIFICATION")

    def test_no_hits_is_not_a_clearance(self):
        self.assertEqual(module.candidates(self.snapshot, "Unknown", "X100"), [])
        self.assertIn("no completeness", self.snapshot["coverage"])

    def test_missing_manufacturer_does_not_become_a_company_match(self):
        rows = fixture(); rows[0]["Manufacturers"] = None
        snapshot = module.parse_snapshot(json.dumps(rows).encode(), "ExampleCo", "test")
        self.assertEqual(module.candidates(snapshot, "ExampleCo", "X100")[0]["manufacturer_names"], [])
        rows[0]["Manufacturers"] = "not an array"
        with self.assertRaises(ValueError): module.parse_snapshot(json.dumps(rows).encode(), "ExampleCo", "test")

    def test_save_requires_confirmation_and_matching_candidate(self):
        with self.assertRaises(ValueError):
            self.ledger.create(self.snapshot, "ExampleCo", "X100", 1)
        with self.assertRaises(ValueError):
            self.ledger.create(self.snapshot, "ExampleCo", "X100", 2, True)

    def test_resume_preserves_snapshot_and_rejects_shortcuts(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        with self.assertRaises(ValueError):
            self.ledger.advance(case["id"], "outcome_recorded", "done", True)
        updated = self.ledger.advance(case["id"], "notice_conditions_checked", "Compared label", True)
        resumed = module.Ledger(self.db).load(case["id"])
        self.assertEqual(resumed, updated)
        self.assertEqual(resumed["snapshot"]["sha256"], self.snapshot["sha256"])
        self.assertEqual(resumed["status"], "REVIEW_OPEN")

    def test_actions_require_confirmation_and_refuse_duplicates(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        with self.assertRaises(ValueError):
            self.ledger.advance(case["id"], "notice_conditions_checked", "done")
        self.ledger.advance(case["id"], "notice_conditions_checked", "done", True)
        with self.assertRaises(ValueError):
            self.ledger.advance(case["id"], "notice_conditions_checked", "done", True)

    def test_external_or_script_notice_url_rejected(self):
        for url in ["javascript:alert(1)", "https://www.cpsc.gov.evil.test/", "https://www.cpsc.gov@evil.test/", "http://www.cpsc.gov/", "https://www.cpsc.gov:123/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                module.source_url(url)

    def test_malformed_or_oversized_data_stops_review(self):
        for data in [b'{}', b'[null]', b' ' * (module.MAX_BYTES + 1)]:
            with self.assertRaises(ValueError):
                module.parse_snapshot(data, "ExampleCo", "test")

    def test_all_actions_are_user_reported_not_a_remedy_success_claim(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        for step in case["steps"]:
            case = self.ledger.advance(case["id"], step, "User recorded this action", True)
        self.assertEqual(case["status"], "USER_REPORTED_COMPLETE")
        self.assertTrue(all(x["kind"] == "USER_REPORTED" for x in case["events"]))

    def test_nonaffected_outcome_does_not_require_an_invented_remedy(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        with self.assertRaises(ValueError):
            self.ledger.close(case["id"], "NOT_AFFECTED", "not checked", True)
        for step in ["notice_conditions_checked", "manufacturer_verification_done"]:
            self.ledger.advance(case["id"], step, "Compared synthetic conditions", True)
        case = self.ledger.close(case["id"], "NOT_AFFECTED", "Synthetic manufacturer says serial not affected", True)
        self.assertFalse(case["steps"]["remedy_request_recorded"])
        self.assertEqual(case["status"], "USER_REPORTED_NOT_AFFECTED")

    def test_stop_requires_confirmation_and_closed_review_cannot_advance(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        with self.assertRaises(ValueError):
            self.ledger.close(case["id"], "STOPPED", "stop")
        self.ledger.close(case["id"], "STOPPED", "User chose to stop", True)
        with self.assertRaises(ValueError):
            self.ledger.advance(case["id"], "notice_conditions_checked", "done", True)

    def test_concurrent_duplicate_action_has_one_committed_event(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        barrier = threading.Barrier(2)
        def advance():
            barrier.wait(timeout=5)
            try:
                module.Ledger(self.db).advance(case["id"], "notice_conditions_checked", "Concurrent synthetic action", True)
                return "saved"
            except ValueError:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: advance(), range(2)))
        self.assertCountEqual(results, ["saved", "refused"])
        self.assertEqual(len(self.ledger.load(case["id"])["events"]), 1)

    def test_successful_operations_close_connections_without_waiting_for_garbage_collection(self):
        connections = []
        native_connect = module.sqlite3.connect
        def capture(*args, **kwargs):
            connection = native_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch.object(module.sqlite3, "connect", side_effect=capture):
            ledger = module.Ledger(self.db)
            case = ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
            ledger.load(case["id"])
            ledger.advance(case["id"], "notice_conditions_checked", "Synthetic check", True)
        self.assertEqual(len(connections), 4)
        for connection in connections:
            with self.assertRaises(module.sqlite3.ProgrammingError): connection.execute("SELECT 1")

    def test_rejected_transaction_rolls_back_and_closes_its_connection(self):
        case = self.ledger.create(self.snapshot, "ExampleCo", "X100", 1, True)
        connection = self.ledger.connect()
        with patch.object(self.ledger, "connect", return_value=connection):
            with self.assertRaises(ValueError):
                self.ledger.advance(case["id"], "outcome_recorded", "Invalid shortcut", True)
        with self.assertRaises(module.sqlite3.ProgrammingError): connection.execute("SELECT 1")
        self.assertEqual(self.ledger.load(case["id"]), case)


class QueryCoverageTests(unittest.TestCase):
    class Response:
        status = 200
        def __init__(self, rows): self.rows = rows
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return json.dumps(self.rows).encode()[:size]

    def test_union_finds_title_only_notice_without_counting_duplicate_ids(self):
        seen = []
        def query(request, timeout):
            fields = parse_qs(urlparse(request.full_url).query); seen.append(fields)
            return self.Response(fixture() if "RecallTitle" in fields else fixture()[:1])
        with patch.object(module, "urlopen", side_effect=query): result = module.fetch_snapshot("ExampleCo")
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["query_status"], "REQUESTED_QUERY_SET_COMPLETE")
        self.assertEqual(len(seen), 3)
        self.assertEqual({next(key for key in fields if key != "format") for fields in seen},
                         {"ProductName", "RecallTitle", "Manufacturer"})

    def test_one_failed_query_remains_partial_and_is_not_a_zero_count(self):
        def query(request, timeout):
            if "Manufacturer=" in request.full_url: raise OSError("source unavailable")
            return self.Response(fixture())
        with patch.object(module, "urlopen", side_effect=query): result = module.fetch_snapshot("ExampleCo")
        self.assertEqual(result["query_status"], "PARTIAL_QUERY_SET")
        self.assertEqual(result["query_errors"], [{"field": "Manufacturer", "error": "OSError"}])
        self.assertEqual(len(result["query_sources"]), 2)

    def test_all_failed_queries_raise_instead_of_returning_empty_success(self):
        with patch.object(module, "urlopen", side_effect=OSError("source unavailable")):
            with self.assertRaises(ValueError): module.fetch_snapshot("ExampleCo")

    def test_conflicting_source_versions_stop_the_union(self):
        def query(request, timeout):
            rows = fixture()
            if "RecallTitle=" in request.full_url: rows[0]["Description"] = "Changed source version"
            return self.Response(rows)
        with patch.object(module, "urlopen", side_effect=query):
            with self.assertRaises(ValueError): module.fetch_snapshot("ExampleCo")


if __name__ == "__main__":
    unittest.main()
