"""Fictional input isolation, visible disclosure, and real MCP restart persistence."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener
from http.server import ThreadingHTTPServer

EVENT = Path(__file__).parents[1]
sys.path.insert(0, str(EVENT / "src"))
from demo_fixture import load_demo_fixture, demo_snapshot, require_case_mode, MODE, SOURCE
from demo_server import MCPClient, handler
from recall_handoff import Ledger, candidates, parse_snapshot, source_url

FIXTURE = EVENT / "preparation/synthetic-demo-v1.json"


class SyntheticDemoTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_demo_fixture(FIXTURE)

    def test_explicit_fixture_is_offline_and_has_fictional_provenance(self):
        with patch("recall_handoff.urlopen", side_effect=AssertionError("No public requests")):
            snapshot = demo_snapshot(self.fixture, "Demo Manufacturer 01")
            found = candidates(snapshot, "Demo Manufacturer 01", "SAMPLE-01")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["model_token_found"])
        self.assertEqual(snapshot["mode"], MODE)
        self.assertEqual(snapshot["source"], SOURCE)
        self.assertEqual(snapshot["query_status"], "SYNTHETIC_FIXTURE_ONLY")

    def test_production_parser_still_rejects_fictional_source(self):
        with self.assertRaises(ValueError): source_url(SOURCE)
        with self.assertRaises(ValueError):
            parse_snapshot(json.dumps(self.fixture["records"]).encode(), "Demo", "test")

    def test_unlabelled_or_official_looking_demo_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            for mutate in (lambda value: value.pop("mode"),
                           lambda value: value["records"][0].update(URL="https://www.cpsc.gov/Recalls/fake")):
                value = json.loads(FIXTURE.read_text(encoding="utf-8")); mutate(value)
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError): load_demo_fixture(path)

    def test_source_modes_cannot_resume_or_modify_one_another(self):
        for mode, demo in ((MODE, False), (None, True)):
            with self.assertRaises(ValueError): require_case_mode({"snapshot": {"mode": mode}}, demo)
        self.assertEqual(require_case_mode({"snapshot": {}}, False), {"snapshot": {}})

    def test_open_case_preserves_original_snapshot_check_and_note_after_new_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "ledger.sqlite"
            ledger = Ledger(db)
            case = ledger.create(demo_snapshot(self.fixture, "Demo Manufacturer 01"),
                                 "Demo Manufacturer 01", "SAMPLE-01", 1, True)
            saved = ledger.advance(case["id"], "notice_conditions_checked", "Compared fictional label", True)
            resumed = Ledger(db).load(case["id"])
            self.assertEqual(resumed, saved)
            self.assertEqual(resumed["status"], "REVIEW_OPEN")
            self.assertFalse(resumed["steps"]["manufacturer_verification_done"])
            self.assertEqual(resumed["snapshot"]["mode"], MODE)

    def test_demo_html_has_permanent_disclosure_and_no_third_party_name(self):
        class NoTools:
            def call(self, name, arguments):
                if name != "resume_case" or arguments != {"case_id": "saved-fictional-case"}:
                    raise AssertionError("Only saved-source retrieval may call a tool")
                return {"snapshot": {"mode": MODE, "sha256": "saved-original-hash"},
                        "notice": {"Title": "Original fictional title"}}
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(NoTools(), None, "test-token", self.fixture))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            opener = build_opener(ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_port}"
            with opener.open(base) as response: page = response.read().decode()
            self.assertIn('id="mode-banner"', page)
            self.assertIn("position:sticky", page)
            self.assertIn("SYNTHETIC DEMONSTRATION", page)
            self.assertIn("Load a fictional sample", page)
            self.assertNotIn("an fictional", page)
            self.assertIn('value="Demo Manufacturer 01"', page)
            self.assertIn("demoMode=true", page)
            self.assertIn('id="saved-progress"', page)
            for name in ("Anker", "A1647", "Alexa", "Qwen", "CPSC", "__DEMO_MODE__"):
                self.assertNotIn(name, page)
            with opener.open(base + "/demo-source") as response: source = json.load(response)
            self.assertEqual(source, self.fixture)
            with opener.open(base + "/demo-source?case_id=saved-fictional-case") as response: saved_source = json.load(response)
            self.assertEqual(saved_source["sha256"], "saved-original-hash")
            self.assertEqual(saved_source["records"], [{"Title": "Original fictional title"}])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "Optional MCP dependency not installed")
    def test_real_mcp_restart_restores_open_case_and_allows_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cases.sqlite"
            saved = None
            for run in (1, 2, 3):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
                with (Path(directory) / f"mcp-{run}.log").open("w", encoding="utf-8") as log:
                    command = [sys.executable, "-B", str(EVENT / "src/mcp_server.py"), "--db", str(db), "--port", str(port)]
                    if run != 3:
                        command.extend(["--demo-fixture", str(FIXTURE)])
                    process = subprocess.Popen(command, stdout=log, stderr=log)
                    try:
                        client = MCPClient(port); deadline = time.monotonic() + 25
                        while True:
                            try: client.initialize(); break
                            except OSError:
                                if process.poll() is not None or time.monotonic() >= deadline: raise
                                time.sleep(.1)
                        if run == 1:
                            found = client.call("search_notices", {"brand": "Demo Manufacturer 01", "model": "SAMPLE-01"})
                            self.assertEqual(found["query_status"], "SYNTHETIC_FIXTURE_ONLY")
                            case = client.call("save_case", {"snapshot_id": found["snapshot_id"], "brand": "Demo Manufacturer 01",
                                "model": "SAMPLE-01", "recall_id": 1, "user_confirmed": True})
                            saved = client.call("record_action", {"case_id": case["id"], "step": "notice_conditions_checked",
                                "note": "Compared fictional label before interruption", "user_confirmed": True})
                            self.assertEqual(saved["status"], "REVIEW_OPEN")
                        elif run == 2:
                            resumed = client.call("resume_case", {"case_id": saved["id"]})
                            self.assertEqual(resumed, saved)
                            resumed = client.call("record_action", {"case_id": saved["id"], "step": "manufacturer_verification_done",
                                "note": "Second fictional check after restart", "user_confirmed": True})
                            self.assertTrue(resumed["steps"]["manufacturer_verification_done"])
                            self.assertFalse(resumed["steps"]["remedy_request_recorded"])
                            closed = client.call("close_review", {"case_id": saved["id"], "reason": "STOPPED",
                                "note": "End fictional demonstration", "user_confirmed": True})
                            self.assertEqual(closed["status"], "USER_REPORTED_STOPPED")
                            with self.assertRaises(ValueError):
                                client.call("record_action", {"case_id": saved["id"], "step": "remedy_request_recorded",
                                    "note": "Must remain refused", "user_confirmed": True})
                        else:
                            # Starting production mode with the same database must
                            # not relabel or mutate the saved fictional record.
                            for name, arguments in (("resume_case", {"case_id": saved["id"]}),
                                    ("record_action", {"case_id": saved["id"], "step": "outcome_recorded", "note": "Refused", "user_confirmed": True}),
                                    ("close_review", {"case_id": saved["id"], "reason": "STOPPED", "note": "Refused", "user_confirmed": True})):
                                with self.assertRaisesRegex(ValueError, "different source mode"):
                                    client.call(name, arguments)
                            self.assertEqual(Ledger(db).load(saved["id"]), closed)
                    finally:
                        if sys.platform == "win32":
                            # A Windows venv launcher can have a separate runtime
                            # child. Stop only the process tree created above.
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                        else:
                            process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
