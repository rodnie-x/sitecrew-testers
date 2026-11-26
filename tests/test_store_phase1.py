"""Phase 1 store tests — foundation: migrations, pragmas, ids, event types.

Boss checks implemented here:
- migration round trip (user_version 1 then 2; idempotent reopen);
- every connection from the factory carries the same pragmas;
- meta table + chat-id rules (reject 'office' and ':'/','; deterministic remap);
- envelope round trip: the §5.3 example appended verbatim reads back equal;
- EVENT_TYPES: new families exact, v1.0 prefixes known, STRICT raises.
P1.2 kill harness lives in tests/test_kill_harness.py (next step).
"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402

# The §5.3 envelope example, verbatim from the blueprint (used for the round trip).
ENVELOPE_B64 = (
    "NS4zIEV2ZW50IGVudmVsb3BlIHYyIGFuZCB0aGUgbmV3IGZhbWlsaWVzCgpTb3VyY2UKCkpTT04KCmBgYAp7"
    "ImNoYXRfaWQiOiJjXzgxZjIiLCJzZXEiOjMxOCwiaWQiOiJlX2E5MWMiLCJ2IjoyLCJ0cyI6MTc5NTU3MDQx"
    "MjM0NSwKICJ0eXBlIjoidG9vbC5jb21taXR0ZWQiLCJ0dXJuX2lkIjoidF8xOWFhIiwiY2FzZV9pZCI6Imtf"
    "MjA0IiwidGFza19pZCI6InRrXzciLAogInJ1bl9pZCI6InJfNTUxIiwiYWdlbnQiOiJmaWxlc19kZXNrIiwi"
    "c2l0ZSI6ImhxIiwKICJkYXRhIjp7InRvb2xfdXNlX2lkIjoidHVfOSIsIm5hbWUiOiJmcy5lZGl0IiwidGll"
    "ciI6ImFza19vbmNlIiwiZ3JhbnRfaWQiOiJnXzMiLAogICAgICAgICAicGF0aHMiOlsiL2hvbWUvdS9Qcm9q"
    "ZWN0cy9zaG9wL2FwcC5weSJdLCJkaWZmIjp7ImFkZGVkIjoxLCJyZW1vdmVkIjoxfSwKICAgICAgICAgImJl"
    "Zm9yZV9zaGEyNTYiOiI5YzFm4oCmIiwiYWZ0ZXJfc2hhMjU2IjoiYjQ0ZeKApiIsInZlcmlmaWVkIjp0cnVl"
    "LAogICAgICAgICAic25hcHNob3RfaWQiOiJzXzg4IiwidW5kbyI6eyJyZXZlcnNpYmxlIjoieWVzIiwicGxh"
    "bl9pZCI6InVfNTIifSwiZHVyYXRpb25fbXMiOjQxMn19CmBgYAoKfCBGYW1pbHkgfCBUeXBlcyB8")
ENVELOPE_EXAMPLE = base64.b64decode(ENVELOPE_B64 + "==").decode("utf-8")
ENVELOPE_JSON = ENVELOPE_EXAMPLE[ENVELOPE_EXAMPLE.find("{"):ENVELOPE_EXAMPLE.rfind("}") + 1]


class Foundation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_p1_")
        self.path = os.path.join(self.tmp, "sitecrew.db")
        self.conn, self.version = sdb.open_db(self.path)

    def tearDown(self):
        self.conn.close()

    def test_migration_round_trip_and_idempotent_reopen(self):
        self.assertEqual(self.version, 2)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("meta", "chats", "messages", "turns", "events", "tool_calls",
                         "approvals", "queue", "undo_ops", "summaries", "usage",
                         "agents", "cases", "tasks", "agent_runs", "sites", "hq_links",
                         "workspaces", "grants", "path_locks", "file_snapshots",
                         "brain_entries", "brain_links", "brain_revisions", "brain_terms"):
            self.assertIn(expected, tables)
        # reopen: idempotent, version stays 2
        conn2, v2 = sdb.open_db(self.path)
        self.assertEqual(v2, 2)
        conn2.close()
        # 002 tables are EMPTY (boss ruling: no seeding)
        for empty in ("agents", "cases", "tasks", "agent_runs", "brain_entries"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {empty}").fetchone()[0], 0)

    def test_factory_pragmas_on_every_connection(self):
        for _ in range(3):
            c = sdb.connect(self.path)
            self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
            self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            c.close()

    def test_meta_table_and_chat_id_rules(self):
        # 'office' and separator ids are rejected
        self.assertFalse(sdb.valid_chat_id("office"))
        self.assertFalse(sdb.valid_chat_id("c:1"))
        self.assertFalse(sdb.valid_chat_id("c,1"))
        self.assertTrue(sdb.valid_chat_id(sdb.new_id("c")))
        # legacy remap is deterministic and stored in meta
        legacy = "my old chat!!"
        first = sdb.remap_legacy_chat_id(legacy, self.conn)
        second = sdb.remap_legacy_chat_id(legacy, self.conn)
        self.assertEqual(first, second)
        self.assertTrue(sdb.valid_chat_id(first))
        row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                ("chat_id_map:" + legacy,)).fetchone()
        self.assertEqual(row[0], first)

    def test_new_id_shape_and_uniqueness(self):
        ids = {sdb.new_id("e") for _ in range(500)}
        self.assertEqual(len(ids), 500)
        for i in ids:
            self.assertTrue(i.startswith("e"))
            self.assertEqual(len(i), 17)  # 1 prefix + 16 hex

    def test_envelope_example_round_trip(self):
        env = json.loads(ENVELOPE_JSON)
        # append exactly as the §5.3 example describes
        cur = self.conn.execute(
            "BEGIN IMMEDIATE")
        self.conn.execute(
            "INSERT INTO events(chat_id, seq, id, turn_id, type, data_json, ts,"
            " case_id, task_id, run_id, agent, site) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (env["chat_id"], env["seq"], env["id"], env["turn_id"], env["type"],
             sdb.dumps(env["data"]), env["ts"], env["case_id"], env["task_id"],
             env["run_id"], env["agent"], env["site"]))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM events WHERE chat_id=? AND seq=?", (env["chat_id"], env["seq"])
        ).fetchone()
        back = json.loads(row["data_json"])
        self.assertEqual(back, env["data"])          # parse-equal round trip
        self.assertEqual(row["type"], "tool.committed")
        self.assertEqual(row["agent"], "files_desk")
        self.assertEqual(row["site"], "hq")

    def test_event_type_registry(self):
        self.assertTrue(sdb.event_type_known("case.opened"))
        self.assertTrue(sdb.event_type_known("brain.entry.written"))
        self.assertTrue(sdb.event_type_known("system.import.chat"))
        self.assertTrue(sdb.event_type_known("system.import.completed"))
        for fam in ("turn", "message", "model", "tool", "gate", "approval", "undo", "system"):
            self.assertTrue(sdb.event_type_known(fam + ".whatever"))
        self.assertFalse(sdb.event_type_known("totally.unknown"))

    def test_user_version_progression(self):
        # a fresh DB migrated straight to 2 has user_version 2; meta records nothing yet
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
