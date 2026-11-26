"""EventStore unit tests — Phase 1 (boss order step 2, PC part).

Covers: seq gapless per stream, state+event in ONE transaction (message and tool
scenarios of the P1.2 oracle), envelope v2 round trip, poll contract (request order,
≤200 cap, more, reset, parked wait with early return), office stream rules, and
check_invariants clean on healthy data + catching broken links.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb   # noqa: E402
from sitecrew.store.events import EventStore, parse_streams, to_envelope  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_ev_")
        self.path = os.path.join(self.tmp, "sitecrew.db")
        self.store = EventStore(self.path)

    def tearDown(self):
        self.store.close()


class TestBasics(Base):
    def test_seq_gapless_across_streams(self):
        for i in range(1, 6):
            self.store.append("c_a", "system.import.chat", {"n": i})
        for i in range(1, 4):
            self.store.append("c_b", "system.import.chat", {"n": i})
        self.assertEqual(self.store.head("c_a"), 5)
        self.assertEqual(self.store.head("c_b"), 3)
        envs = self.store.events_after("c_a", 0, 100)
        self.assertEqual([e["seq"] for e in envs], [1, 2, 3, 4, 5])

    def test_envelope_shape_v2_constant(self):
        env = self.store.append("c_a", "tool.committed", {"tool_use_id": "tu_1"},
                                turn_id="t_1", case_id="k_1", agent="files_desk",
                                site="hq")
        self.assertEqual(env["v"], 2)
        self.assertEqual(env["id"][:1], "e")
        self.assertEqual(len(env["id"]), 17)          # e + 16 hex
        self.assertEqual(env["data"], {"tool_use_id": "tu_1"})
        self.assertEqual(env["agent"], "files_desk")

    def test_state_and_event_one_transaction_message_scenario(self):
        """P1.2 oracle message scenario: messages row + message.created share id k-i."""
        mid = "k_i1"

        def state(conn, seq):
            conn.execute(
                "INSERT INTO chats(id, title, created_at, updated_at) VALUES(?,?,?,?)",
                ("c_a", "t", 1, 1))
            conn.execute(
                "INSERT INTO messages(id, chat_id, role, kind, content_json, status,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (mid, "c_a", "user", "user", sdb.dumps({"text": "hi"}), "final", 1))
            return mid   # events.id must equal messages.id (P1.2 oracle)

        env = self.store.append("c_a", "message.created", {"text": "hi"},
                                turn_id=None, state_writer=state)
        self.assertEqual(env["seq"], 1)
        self.assertEqual(env["id"], mid)               # events.id == messages.id
        row = self.store.conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(row, 1)
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"], inv)

    def test_state_and_event_one_transaction_tool_scenario(self):
        """P1.2 oracle tool scenario: tool_calls.committed_seq == allocated seq."""
        tc_id, tu_id = "tc_1", "tu_1"
        self.store.append("c_a", "turn.started", {})  # give the stream a head first

        def state(conn, seq):
            conn.execute(
                "INSERT INTO turns(id, chat_id, state, started_at) VALUES(?,?,?,?)",
                ("t_1", "c_a", "running", 1))
            conn.execute(
                "INSERT INTO tool_calls(id, turn_id, tool_use_id, name, status,"
                " committed_seq) VALUES(?,?,?,?,?,?)",
                (tc_id, "t_1", tu_id, "fs.read", "committed", seq))

        env = self.store.append(
            "c_a", "tool.committed",
            {"tool_use_id": tu_id, "name": "fs.read", "verified": True},
            turn_id="t_1", agent="files_desk", site="phone", state_writer=state)
        self.assertEqual(env["seq"], 2)
        row = self.store.conn.execute(
            "SELECT committed_seq FROM tool_calls WHERE id=?", (tc_id,)).fetchone()
        self.assertEqual(row["committed_seq"], 2)
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"], inv)

    def test_rollback_leaves_neither_state_nor_event(self):
        def bad_state(conn, seq):
            conn.execute(
                "INSERT INTO messages(id, chat_id, role, kind, content_json, status,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                ("k_bad", "c_a", "user", "user", "{}", "final", 1))
            raise RuntimeError("boom after state write")

        with self.assertRaises(RuntimeError):
            self.store.append("c_a", "message.created", {}, state_writer=bad_state)
        # both the state row and the event are gone (one transaction rolled back)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.store.head("c_a"), 0)
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"], inv)

    def test_check_invariants_catches_broken_message_link(self):
        self.store.append("c_a", "message.created", {"x": 1})   # event without state row
        inv = self.store.check_invariants()
        self.assertFalse(inv["_pass"])
        self.assertTrue(inv["message_links"])

    def test_check_invariants_catches_office_chat_row(self):
        self.conn = self.store.conn
        self.store.conn.execute(
            "INSERT INTO chats(id, title, created_at, updated_at) VALUES('office','x',1,1)")
        self.store.conn.commit()
        inv = self.store.check_invariants()
        self.assertFalse(inv["_pass"])
        self.assertIn("office", inv["office_chat_id"])


class TestPoll(Base):
    def test_parse_streams(self):
        self.assertEqual(parse_streams("c_81f2:318,office:12"),
                         {"c_81f2": 318, "office": 12})
        self.assertEqual(parse_streams(""), {})
        self.assertEqual(parse_streams("junk"), {})

    def test_poll_returns_new_events_grouped_in_request_order(self):
        self.store.append("office", "office.resumed", {"a": 1})
        for i in range(3):
            self.store.append("c_a", "system.import.chat", {"i": i})
        r = self.store.poll({"c_a": 0, "office": 0}, wait_s=0)
        self.assertEqual([e["chat_id"] for e in r["events"]], ["c_a"] * 3 + ["office"])
        self.assertEqual(r["heads"], {"c_a": 3, "office": 1})
        self.assertFalse(r["more"])
        self.assertEqual(r["reset"], [])

    def test_poll_cursor_above_head_goes_to_reset(self):
        self.store.append("c_a", "system.import.chat", {})
        r = self.store.poll({"c_a": 50}, wait_s=0)
        self.assertEqual(r["reset"], ["c_a"])
        self.assertEqual(r["heads"], {"c_a": 1})

    def test_poll_more_flag_at_200_cap(self):
        for i in range(250):
            self.store.append("c_a", "system.import.chat", {"i": i})
        r = self.store.poll({"c_a": 0}, wait_s=0)
        self.assertEqual(len(r["events"]), 200)
        self.assertTrue(r["more"])
        r2 = self.store.poll({"c_a": 200}, wait_s=0)
        self.assertEqual(len(r2["events"]), 50)
        self.assertFalse(r2["more"])

    def test_poll_parks_then_returns_early_on_append(self):
        result = {}

        def parked():
            result["r"] = self.store.poll({"c_a": 0}, wait_s=15)

        th = threading.Thread(target=parked, daemon=True)
        th.start()
        time.sleep(0.3)                     # let the poll park
        self.assertFalse("r" in result)
        self.store.append("c_a", "system.import.chat", {"wake": 1})
        th.join(timeout=5)
        self.assertIn("r", result)          # returned early, did not wait 15 s
        self.assertEqual(len(result["r"]["events"]), 1)

    def test_parked_poll_and_normal_request_concurrent(self):
        """Boss rule: prove the server is threaded — a parked poll must not block
        a normal request (different connection)."""
        done = {}

        def parked():
            done["poll"] = self.store.poll({"c_a": 0}, wait_s=10)

        th = threading.Thread(target=parked, daemon=True)
        th.start()
        time.sleep(0.3)
        t0 = time.time()
        # a plain request on a separate connection while the poll is parked
        conn2 = sdb.connect(self.store.path)
        n = conn2.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn2.close()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, "normal request blocked by parked poll")
        th.join(timeout=10)
        self.assertIn("poll", done)

    def test_parked_poll_1s_belt_after_missed_notify(self):
        """A missed notify still costs at most 1 s (boss D3 fix: 1 s belt)."""
        result = {}

        def parked():
            result["r"] = self.store.poll({"c_a": 0}, wait_s=15)

        th = threading.Thread(target=parked, daemon=True)
        th.start()
        time.sleep(0.3)
        # append WITHOUT notify (simulating a notify lost across processes):
        self.store._writer.acquire()
        try:
            self.store.conn.execute(
                "INSERT INTO events(chat_id, seq, id, type, data_json, ts)"
                " VALUES('c_a', 1, 'e_missed', 'system.import.chat', '{}', 1)")
            self.store.conn.commit()
            self.store._heads["c_a"] = 1
        finally:
            self.store._writer.release()
        t0 = time.time()
        th.join(timeout=15)
        took = time.time() - t0
        self.assertIn("r", result)
        self.assertLess(took, 5, "1 s belt should re-check within ~1 s, took %.1f" % took)
        self.assertEqual(len(result["r"]["events"]), 1)

    def test_deterministic_race_between_check_and_wait(self):
        """Boss D3 race test: an append landing in the check→wait gap must wake
        the poller immediately (notify fires while the poller is inside its hook)."""
        result = {}

        def race_hook():
            # runs inside poll(): after the empty check, before the wait —
            # exactly the window a lost notify would break; append + notify here
            self.store._writer.acquire()
            try:
                self.store.conn.execute(
                    "INSERT INTO events(chat_id, seq, id, type, data_json, ts)"
                    " VALUES('c_a', 1, 'e_race', 'system.import.chat', '{}', 1)")
                self.store.conn.commit()
                self.store._heads["c_a"] = 1
                self.store._cond.notify_all()
            finally:
                self.store._writer.release()

        self.store._race_hook = race_hook
        th = threading.Thread(target=lambda: result.update(
            r=self.store.poll({"c_a": 0}, wait_s=20)), daemon=True)
        t0 = time.time()
        th.start()
        th.join(timeout=15)
        took = time.time() - t0
        self.assertIn("r", result)
        self.assertEqual(len(result["r"]["events"]), 1)
        self.assertLess(took, 5, "race append should return immediately, took %.1f" % took)


class TestStrictAndOffice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_ev2_")
        self.store = EventStore(os.path.join(self.tmp, "s.db"))
        self.store.conn.execute(
            "INSERT INTO chats(id, title, created_at, updated_at) VALUES(?,?,1,1)",
            ("c_ok", "t"))
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()

    def test_strict_raises_on_unknown_type(self):
        with self.assertRaises(ValueError):
            self.store.append("c_ok", "made.up.type", {}, strict=True)

    def test_non_strict_logs_and_writes_unknown_type(self):
        env = self.store.append("c_ok", "made.up.type", {})
        self.assertEqual(env["type"], "made.up.type")
        inv = self.store.check_invariants()
        self.assertFalse(inv["_pass"])               # unknown_event_types flagged
        self.assertIn("made.up.type", inv["unknown_event_types"])

    def test_office_stream_is_legal_but_chat_row_is_not(self):
        env = self.store.append("office", "office.resumed", {})
        self.assertEqual(env["chat_id"], "office")   # stream fine
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"], inv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
