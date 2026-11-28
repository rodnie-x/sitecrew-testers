"""Re-runnable PC-only mocks for the P1.3 device tooling.

These tests make no physical-device claim. They cover the review-reproduced
failures without requiring adb, a phone, or a real WebView.
"""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["SITECREW_STORE"] = "1"
os.environ["SITECREW_DEBUG"] = "1"

from scripts.webview_cdp import CDP  # noqa: E402
from tests.idle_park_cpu import pct, rates_from  # noqa: E402


class FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)

    def send(self, _):
        pass

    def settimeout(self, _):
        pass

    def recv(self):
        if not self.frames:
            raise AssertionError("unexpected websocket recv")
        return self.frames.pop(0)


def reply(mid):
    return json.dumps({"id": mid, "result": {}})


def load_event():
    return json.dumps({"method": "Page.loadEventFired"})


class SamplerMath(unittest.TestCase):
    def test_rates_and_negative_delta(self):
        points = [(0.0, 0, 10, 100), (5.0, 50, 10, 100),
                  (10.0, 100, 10, 100)]
        self.assertEqual(rates_from(points), [10.0, 10.0])
        self.assertEqual(pct(rates_from(points), 0.95), 10.0)
        self.assertEqual(rates_from(
            [(0.0, 100, 10, 100), (5.0, 50, 10, 100)]), [])


class ProbePage(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("crew") is not None,
                         "full product modules are not in the public subset")
    def test_run_id_and_stale_result_clearing(self):
        from server.app import app
        client = app.test_client()
        response = client.get("/api/v2/debug/probe-page?run=pc-smoke-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"run_id: new URLSearchParams(location.search).get('run')",
            response.data)
        self.assertEqual(client.get(
            "/api/v2/debug/probe-results").get_json(), {"empty": True})


class CDPNavigation(unittest.TestCase):
    def test_event_buffer(self):
        cdp = CDP()
        cdp.ws = FakeWS([load_event(), reply(1)])
        self.assertEqual(cdp.send("Page.enable"), {})
        self.assertTrue(cdp._wait_load(timeout=1))

    def test_away_back_reload_consumes_each_own_load(self):
        """about:blank -> app -> reload cannot consume a stale load event."""
        cdp = CDP()
        cdp.ws = FakeWS([
            reply(1), load_event(),             # about:blank
            reply(2), load_event(),             # app
            reply(3), load_event(),             # reload
        ])
        cdp.navigate("about:blank", settle=False)
        cdp.navigate("http://localhost:8080/", settle=False)
        cdp.reload(settle=False)
        self.assertEqual(cdp.ws.frames, [])
        self.assertEqual(list(cdp._events), [])

    def test_probe_page_has_custom_readiness(self):
        cdp = CDP()
        cdp.target_url = "http://localhost:8080/"
        seen = []

        def evaluate(expression, **kwargs):
            seen.append(expression)
            return json.dumps({
                "rs": "complete",
                "href": "http://localhost:8080/api/v2/debug/probe-page?run=x",
                "hasApp": True,
            })

        cdp.evaluate = evaluate
        self.assertTrue(cdp._settle(
            url_prefix="http://localhost:8080/", timeout=1,
            ready_js="document.getElementById('out')"))
        self.assertIn("document.getElementById('out')", seen[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
