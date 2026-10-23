"""Loopback web client for the real MCP workflow and optional offline excerpt ranker."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener
from urllib.parse import parse_qs, urlsplit

from local_assistant import LocalAssistant, MODEL_ID
from demo_fixture import load_demo_fixture, DISCLAIMER

TOOLS = {"search_notices", "save_case", "resume_case", "record_action", "close_review"}
SOURCE = Path(__file__).resolve().parent


class MCPClient:
    def __init__(self, port):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.opener = build_opener(ProxyHandler({}))  # Only this loopback transport skips the proxy.
        self.counter = 0; self.lock = threading.Lock(); self.session = None

    def request(self, method, params=None, notification=False):
        with self.lock:
            self.counter += 1
            payload = {"jsonrpc": "2.0", "method": method}
            if not notification: payload["id"] = self.counter
            if params is not None: payload["params"] = params
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-11-25"}
            if self.session: headers["Mcp-Session-Id"] = self.session
            with self.opener.open(Request(self.url, json.dumps(payload).encode(), headers), timeout=75) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000: raise ValueError("MCP response exceeds the client limit")
            if not raw: return None
            result = json.loads(raw)
            if "error" in result: raise ValueError("MCP request failed")
            return result.get("result")

    def initialize(self):
        value = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "recall-handoff-web", "version": "0.2"}})
        if value.get("protocolVersion") != "2025-11-25":
            raise ValueError("The required MCP protocol was not negotiated")
        self.request("notifications/initialized", notification=True)
        if {tool["name"] for tool in self.request("tools/list")["tools"]} != TOOLS:
            raise ValueError("The expected workflow tools were not available")

    def call(self, name, arguments):
        if name not in TOOLS or not isinstance(arguments, dict):
            raise ValueError("Unknown workflow operation")
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            message = next((item.get("text") for item in result.get("content", []) if item.get("type") == "text"), "Workflow operation failed")
            raise ValueError(message[:400])
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def handler(client, assistant, token, fixture=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        @property
        def origin(self):
            return f"http://127.0.0.1:{self.server.server_port}"

        def send(self, status, value, content_type="application/json"):
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == self.origin.removeprefix("http://")

        def do_GET(self):
            if not self.valid_host(): return self.send(403, {"error": "Invalid local host"})
            if self.path == "/":
                page = (SOURCE / "ui.html").read_text(encoding="utf-8").replace("__LOCAL_TOKEN__", json.dumps(token))
                page = page.replace("__DEMO_MODE__", json.dumps(fixture is not None))
                page = page.replace("__MODE_BANNER__", DISCLAIMER if fixture is not None else "Live public-source mode — results require product verification.")
                if fixture is not None:
                    page = page.replace('value="Anker"', 'value="Demo Manufacturer 01"').replace('value="A1647"', 'value="SAMPLE-01"')
                    page = page.replace("Search CPSC notices", "Load fictional sample")
                    page = page.replace("Find an official notice", "Load a fictional sample")
                    page = page.replace("official notice", "fictional sample").replace("official source", "fictional source")
                    page = page.replace("CPSC coverage and notice conditions vary.", "This is an offline fictional exercise.")
                    page = page.replace("This frontend demonstrates the workflow without an Alexa connection.", "A real local tool server stores the review; the model is optional.")
                return self.send(200, page, "text/html; charset=utf-8")
            if self.path == "/demo-source" and fixture is not None:
                return self.send(200, fixture)
            if urlsplit(self.path).path == "/demo-source" and fixture is not None:
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    if set(query) != {"case_id"} or len(query["case_id"]) != 1:
                        raise ValueError("Choose one saved fictional review")
                    case = client.call("resume_case", {"case_id": query["case_id"][0]})
                    return self.send(200, {**case["snapshot"], "records": [case["notice"]]})
                except (OSError, ValueError, TypeError, KeyError) as error:
                    return self.send(400, {"error": str(error)[:400]})
            if self.path == "/api/config":
                return self.send(200, {"model_available": assistant is not None,
                                      "model_id": MODEL_ID if assistant else None, "mcp": "CONNECTED",
                                      "source_mode": "SYNTHETIC_DEMO" if fixture is not None else "LIVE_PUBLIC_QUERY"})
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if (not self.valid_host() or self.headers.get("Origin") != self.origin or
                    not secrets.compare_digest(self.headers.get("X-Local-Token", ""), token)):
                return self.send(403, {"error": "Open the local app before performing an action"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Content-Type") != "application/json":
                    return self.send(400, {"error": "Invalid request size or format"})
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict): raise ValueError("Request must be an object")
                if self.path == "/api/tool":
                    return self.send(200, client.call(data.get("name"), data.get("arguments")))
                if self.path == "/api/explain":
                    if assistant is None:
                        return self.send(503, {"error": "Local model is not configured; open the source notice directly"})
                    case = client.call("resume_case", {"case_id": data.get("case_id")})
                    return self.send(200, assistant.explain(case["notice"]))
                return self.send(404, {"error": "Not found"})
            except (OSError, ValueError, TypeError, KeyError) as error:
                return self.send(400, {"error": str(error)[:400]})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--demo-fixture", type=Path, help="Explicit fictional offline input; no CPSC requests")
    args = parser.parse_args()
    fixture = load_demo_fixture(args.demo_fixture) if args.demo_fixture else None
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); mcp_port = probe.getsockname()[1]
    with args.db.with_suffix(".mcp.log").open("a") as log:
        command = [sys.executable, str(SOURCE / "mcp_server.py"), "--db", str(args.db), "--port", str(mcp_port)]
        if args.demo_fixture:
            command.extend(["--demo-fixture", str(args.demo_fixture)])
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            client = MCPClient(mcp_port); deadline = time.monotonic() + 25
            while True:
                try:
                    client.initialize(); break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("The local MCP server did not start; inspect its log")
                    time.sleep(.1)
            assistant = LocalAssistant(args.model_dir) if args.model_dir else None
            server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(client, assistant, secrets.token_urlsafe(24), fixture))
            print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "model": assistant is not None}), flush=True)
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    main()
