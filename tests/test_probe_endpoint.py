"""B04: scripts/probe_endpoint.py classifies reachability and summarizes failure streaks."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from scripts.probe_endpoint import probe, summarize


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = {"/ok": 200, "/missing": 404, "/bad": 502}[self.path]
        self.send_response(code)
        self.end_headers()

    def log_message(self, *args):
        pass


class TestProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_answers_below_500_count_as_reachable(self):
        self.assertEqual((probe(self.base + "/ok")["ok"], probe(self.base + "/missing")["ok"]), (True, True))

    def test_5xx_and_connection_errors_are_failures(self):
        bad = probe(self.base + "/bad")
        self.assertEqual((bad["ok"], bad["status"]), (False, 502))
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        down = probe(f"http://127.0.0.1:{port}/", timeout=2)
        self.assertFalse(down["ok"])
        self.assertIsNone(down["status"])
        self.assertIn("URLError", down["error"])


class TestSummary(unittest.TestCase):
    def test_availability_and_streaks(self):
        def row(at, ok, status=200, error=None):
            return {"at": at, "target": "public", "ok": ok, "status": status, "error": error}
        rows = [row("2026-10-10T10:00:00", True), row("2026-10-10T10:05:00", False, 502),
                row("2026-10-10T10:10:00", False, None, "URLError: eof"), row("2026-10-10T10:15:00", True),
                row("2026-10-11T10:00:00", True)]
        s = summarize(rows)["public"]
        self.assertEqual((s["probes"], s["failures"]), (5, 2))
        self.assertEqual(s["by_day"]["2026-10-10"], {"probes": 4, "failures": 2, "availability": 0.5})
        self.assertEqual(len(s["failure_streaks"]), 1)
        streak = s["failure_streaks"][0]
        self.assertEqual((streak["start"], streak["end"], streak["probes"]), ("2026-10-10T10:05:00", "2026-10-10T10:10:00", 2))
        self.assertEqual(streak["errors"], ["502", "URLError: eof"])


if __name__ == "__main__":
    unittest.main()
