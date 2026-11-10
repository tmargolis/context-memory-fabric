"""B04: scripts/probe_endpoint.py classifies reachability and summarizes failure streaks."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile

import scripts.probe_endpoint as pe
from scripts.probe_endpoint import burst, check_tunnel, probe, summarize


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


class _FlakyTunnel(BaseHTTPRequestHandler):
    """Every other connection is dropped without an answer, like the
    degraded Spark tunnel of 2026-11-09."""
    count = 0
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802
        with _FlakyTunnel.lock:
            _FlakyTunnel.count += 1
            drop = _FlakyTunnel.count % 2 == 0
        if drop:
            self.close_connection = True
            self.connection.close()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def log_message(self, *args):
        pass


class TestBurst(unittest.TestCase):
    def test_a_burst_against_a_server_that_drops_every_other_connection_fails(self):
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyTunnel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/models"
            self.assertTrue(probe(url, timeout=5)["ok"])  # the first single request answers
            out = burst(url, n=6, timeout=5)
        finally:
            server.shutdown()
        self.assertFalse(out["ok"])
        self.assertEqual(out["requests"], 6)
        self.assertGreaterEqual(out["failed"], 2)
        self.assertTrue(out["error"])


def _fake_get(pattern):
    """probe() stand-in: answers ok/failed from pattern, one burst after another."""
    bursts = iter(pattern)
    state = {"left": 0, "ok": True}

    def get(url, timeout):
        if state["left"] == 0:
            state["ok"], state["left"] = next(bursts), pe.TUNNEL_REQUESTS
        state["left"] -= 1
        return {"ok": state["ok"], "status": 200 if state["ok"] else None,
                "error": None if state["ok"] else "RemoteDisconnected: closed"}
    return get


class TestCheckTunnel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "probe.jsonl"
        self.now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.restarts = []
        self._url = pe.model_list_url
        pe.model_list_url = lambda: "http://127.0.0.1:12345/v1/models"

    def tearDown(self):
        pe.model_list_url = self._url
        self._tmp.cleanup()

    def restart(self, label):
        self.restarts.append(label)
        return {"ok": True}

    def check(self, pattern, label="com.cmf.spark-tunnel"):
        return check_tunnel(self.log, label, self.now, get=_fake_get(pattern), restart=self.restart, sleep=lambda s: None)

    def test_healthy_tunnel_is_left_alone(self):
        row = self.check([True])
        self.assertTrue(row["ok"])
        self.assertEqual(self.restarts, [])

    def test_a_blip_that_clears_on_recheck_does_not_restart(self):
        row = self.check([False, True])
        self.assertFalse(row["ok"])
        self.assertTrue(row["recovered_on_recheck"])
        self.assertEqual(self.restarts, [])

    def test_two_failed_bursts_restart_the_tunnel_and_check_again(self):
        row = self.check([False, False, True])
        self.assertEqual(self.restarts, ["com.cmf.spark-tunnel"])
        self.assertEqual(row["restarted"], "com.cmf.spark-tunnel")
        self.assertEqual(row["after_restart"]["ok"], True)
        self.assertEqual(row["failed"], pe.TUNNEL_REQUESTS)

    def test_without_a_label_it_only_reports(self):
        row = self.check([False, False], label=None)
        self.assertFalse(row["ok"])
        self.assertNotIn("restarted", row)
        self.assertEqual(self.restarts, [])

    def test_restarts_at_most_once_per_cooldown(self):
        recent = (self.now - timedelta(minutes=10)).isoformat(timespec="seconds")
        self.log.write_text(json.dumps({"at": recent, "target": "tunnel", "ok": False, "restarted": "com.cmf.spark-tunnel"}) + "\n")
        row = self.check([False, False])
        self.assertEqual(self.restarts, [])
        self.assertIn("within 30 min", row["restart_skipped"])
        self.now += timedelta(minutes=25)
        self.check([False, False, True])
        self.assertEqual(self.restarts, ["com.cmf.spark-tunnel"])

    def test_no_local_model_no_row(self):
        pe.model_list_url = lambda: None
        self.assertIsNone(self.check([True]))


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
