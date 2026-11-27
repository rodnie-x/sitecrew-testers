"""Boss step-3b contract tests: gap re-poll + idempotent apply by (stream, seq).

(Parked poll concurrent with a normal request, and reset, live in
tests/test_eventstore.py — test_parked_poll_and_normal_request_concurrent,
test_poll_cursor_above_head_goes_to_reset.)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store.events import EventStore   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GapRePoll(unittest.TestCase):
    def test_gap_re_poll_returns_missed_range_in_order(self):
        """Client lost events 6-8 (cursor parked at 5 while head is 10):
        re-poll from 5 returns exactly 6..10, ascending, nothing older;
        re-poll from 10 returns empty; nothing is re-delivered ever."""
        tmp = tempfile.mkdtemp(prefix="sc_gap_", dir="D:/" if os.path.exists("D:/") else None)
        st = EventStore(os.path.join(tmp, "gap.db"))
        seqs = []
        for i in range(10):
            env = st.append("c_gap", "message.created", {"i": i}, strict=True)
            seqs.append(env["seq"])
        self.assertEqual(seqs, list(range(1, 11)))

        # the gap: client applies 1..5, "crashes", comes back with cursor 5
        first = st.poll({"c_gap": 5}, wait=0)
        got = [e["seq"] for e in first["events"]]
        self.assertEqual(got, [6, 7, 8, 9, 10])
        self.assertFalse(first["more"])
        self.assertEqual(first["reset"], [])
        self.assertEqual(first["heads"]["c_gap"], 10)

        # re-poll from the applied cursor: empty, no duplicates
        second = st.poll({"c_gap": 10}, wait=0)
        self.assertEqual(second["events"], [])
        self.assertEqual(second["heads"]["c_gap"], 10)

        # a client that re-asks from an OLD cursor still gets a clean,
        # ordered range it can dedup by (stream, seq) — never garbage order
        old = st.poll({"c_gap": 2}, wait=0)
        got_old = [e["seq"] for e in old["events"]]
        self.assertEqual(got_old, list(range(3, 11)))


class IdempotentApply(unittest.TestCase):
    def test_apply_idempotent_by_stream_seq(self):
        """The integrated poll.js CLIENT (real loop, stubbed transport): an
        event is applied at most once per (stream, seq); a seq hole never
        advances the cursor and is suppressed until the fill; replay after a
        reconnect is deduped; a reset re-delivery stays idempotent in the DOM
        reducer. Runs the REAL server/static/poll.js under node (the same
        file the WebView loads)."""
        node = "node"
        script = os.path.join(REPO, "tests", "apply_idempotence_check.cjs")
        poll_js = os.path.join(REPO, "server", "static", "poll.js")
        r = subprocess.run([node, script, poll_js], capture_output=True,
                           text=True, timeout=60, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            self.fail("poll.js idempotence check failed (rc=%d)\nstdout: %s\n"
                      "stderr: %s" % (r.returncode, out[-800:],
                                      (r.stderr or "")[-800:]))
        report = json.loads(out.splitlines()[-1])
        for key in ("hole_suppressed", "fill_in_order", "replay_deduped",
                    "reset_fired", "dom_exact_once", "cursor_final",
                    "gaps_seen"):
            self.assertTrue(report[key], "poll.js check failed: %s (stats: %s)"
                            % (key, report.get("stats")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
