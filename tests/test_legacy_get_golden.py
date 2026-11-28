"""P1.5 pre-route golden gate for every legacy GET surface.

Run with the legacy store flag OFF:
    python -m unittest tests.test_legacy_get_golden -v

The normalization hashes below are independent pins, not values recomputed only
by the capture tool. Editing a rule to hide a later regression fails this test.
"""
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if importlib.util.find_spec("crew") is not None:
    from scripts import capture_legacy_get as golden  # noqa: E402
else:
    golden = None


@unittest.skipUnless(golden is not None, "full product modules are not in the public subset")
class LegacyGetGoldenTest(unittest.TestCase):
    def test_declared_contract_and_normalization_are_frozen(self):
        self.assertEqual(
            golden.sha256_bytes(golden.canonical_bytes(golden.NORMALIZATION)),
            "3bc472effd484a57b7518223a259b91526392e4416766f2798b664a859513ac3",
            "normalization drift: do not tune the baseline rules to obtain parity",
        )
        self.assertEqual(
            golden.sha256_bytes(golden.canonical_bytes(golden.fixture_definitions())),
            "17c48285f84576fcf908494791e815243766762a3b6778ac8360f4fba6fb184c",
            "fixture definitions or the captured static surface changed",
        )
        self.assertEqual(golden.NORMALIZATION["body_replacements"], [])
        self.assertEqual(golden.NORMALIZATION["json_pointer_replacements"], [])
        self.assertEqual(set(golden.NORMALIZATION["ignored_headers"]),
                         {"Date", "ETag", "Last-Modified"})
        headers = golden.normalize_headers({
            "Date": "now", "ETag": "mtime dependent", "Last-Modified": "yesterday",
            "Content-Type": "application/json", "Content-Length": "2",
        })
        self.assertEqual(headers, {"Content-Type": "application/json", "Content-Length": "2"})

    def test_controlled_state_fixtures_cover_limits_and_private_field_stripping(self):
        _manifest, records, _manifest_bytes = golden.load_capture()
        states = {record["fixture"]: json.loads(body.decode("utf-8"))
                  for record, body in records if record["route"] == "/api/state"}
        self.assertEqual(states["queued"]["queued"], 1)
        self.assertEqual(states["queued"]["activity"][0]["time"], "12:34:56")
        self.assertEqual(states["normal_full_metadata"]["usage"]["fake/small"]["calls"], 2)
        self.assertTrue(states["normal_full_metadata"]["models"])
        self.assertTrue(states["normal_full_metadata"]["tools"])
        for name in ("attachment_model_content_stripped", "restored"):
            self.assertNotIn("model_content", json.dumps(states[name]))
            self.assertNotIn("DO NOT LEAK", json.dumps(states[name]))
            self.assertNotIn("server only attachment text", json.dumps(states[name]))
        self.assertEqual(len(states["messages_over_60"]["messages"]), 60)
        self.assertEqual(states["messages_over_60"]["messages"][0]["sequence"], 5)
        self.assertEqual(states["messages_over_60"]["messages"][-1]["sequence"], 64)
        self.assertEqual(len(states["activity_over_100"]["activity"]), 100)
        self.assertEqual(states["activity_over_100"]["activity"][0]["action"], "overflow-025")
        self.assertEqual(states["activity_over_100"]["activity"][-1]["action"], "overflow-124")
        self.assertTrue(states["busy_offline"]["busy"])
        self.assertFalse(states["busy_offline"]["online"])
        self.assertEqual(states["busy_offline"]["busy_since"], 1700000000.25)

    def test_unchanged_legacy_app_reproduces_all_raw_response_bytes(self):
        manifest, records, _manifest_bytes = golden.load_capture()
        self.assertEqual(manifest["legacy_get_count"], 15)
        self.assertEqual(manifest["api_state_fixture_count"], 8)
        self.assertEqual(manifest["static_get_count"], 7)
        self.assertEqual(len(records), 15)
        errors = golden.check_capture()
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
