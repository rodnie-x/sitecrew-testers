"""P1.1 regressions: imported message invariants and shared legacy-id mappings."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402
from sitecrew.store import importer as imp  # noqa: E402
from sitecrew.store.events import EventStore  # noqa: E402


class ImportRegression(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="sc_imp_regression_"), "t.db")
        self.store = EventStore(self.path)

    def tearDown(self):
        self.store.close()

    def test_import_emits_linked_events_and_satisfies_invariants(self):
        chat = {"id": "legacy chat/shared", "title": "shared",
                "messages": [{"role": "user", "text": "one", "ts": 1001},
                             {"role": "assistant", "text": "two", "ts": 1002}],
                "queued": [{"text": "later", "ts": 1003}]}
        self.assertEqual(imp.import_chat(self.store, "queue_json", 0, chat, now_ms=2000),
                         "imported")
        cid = imp.chat_id_for(self.store.conn, chat["id"], persist=False)
        events = self.store.events_after(cid, 0, 10)
        self.assertEqual([event["type"] for event in events],
                         ["message.created"] * 3 + ["system.import.chat"])
        self.assertEqual([event["seq"] for event in events], [1, 2, 3, 4])
        self.assertEqual([event["id"] for event in events[:3]],
                         [imp.imp_id("m", "queue_json", 0, 0),
                          imp.imp_id("m", "queue_json", 0, 1),
                          imp.imp_id("qm", "queue_json", 0, 0)])
        self.assertEqual(events[0]["data"], {
            "text": "one", "role": "user", "kind": "user", "parent_id": None,
            "origin": "import", "source": "queue_json"})
        self.assertEqual(events[1]["data"]["parent_id"], events[0]["id"])
        self.assertEqual((events[1]["data"]["role"], events[1]["data"]["kind"]),
                         ("assistant", "assistant"))
        self.assertNotIn("queued", events[0]["data"])
        self.assertEqual(events[2]["data"], {
            "text": "later", "role": "user", "kind": "user", "parent_id": None,
            "origin": "import", "source": "queue_json", "queued": True})
        invariants = self.store.check_invariants()
        self.assertTrue(invariants["_pass"], invariants)
        before = self.store.head(cid)
        self.assertEqual(imp.import_chat(self.store, "queue_json", 0, chat), "skipped")
        self.assertEqual(self.store.head(cid), before)

    def test_legacy_mapping_is_shared_and_deterministic_across_sources(self):
        raw = "legacy chat/shared"
        chat = {"id": raw, "title": "shared", "messages": [], "queued": []}
        imp.import_chat(self.store, "queue_json", 0, chat)
        imported = imp.chat_id_for(self.store.conn, raw)
        ram = imp.chat_id_for(self.store.conn, raw)
        recents = sdb.remap_legacy_chat_id(raw, self.store.conn)
        self.assertEqual(imported, ram)
        self.assertEqual(imported, recents)
        self.assertTrue(imported.startswith("c_imp_"))
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key='idmap:' || ?", (raw,)).fetchone()[0], 0)
        other, _ = sdb.open_db(os.path.join(os.path.dirname(self.path), "other.db"))
        try:
            self.assertEqual(sdb.remap_legacy_chat_id(raw, other, persist=False), imported)
            self.assertEqual(imp.chat_id_for(other, raw, persist=False), imported)
        finally:
            other.close()

    def test_same_chat_merges_three_sources_without_content_dedupe(self):
        sources = [
            ("queue_json", {"id": "legacy chat/shared", "title": None, "model": None,
                            "pinned": 0, "created_at": 300, "updated_at": 300,
                            "messages": [{"role": "user", "text": "same"},
                                         {"role": "assistant", "text": "queue only"}],
                            "queued": [{"text": "q same"}]}),
            ("ram", {"id": "legacy chat/shared", "title": "RAM title", "model": None,
                     "pinned": 1, "created_at": 200, "updated_at": 500,
                     "messages": [{"role": "user", "text": "same"}],
                     "queued": [{"text": "q same"}]}),
            ("recents", {"id": "legacy chat/shared", "title": "Later title", "model": "m1",
                         "pinned": 0, "created_at": 100, "updated_at": 400,
                         "messages": [{"role": "assistant", "text": "recent"}],
                         "queued": [{"text": "q recent"}]}),
        ]
        cid = imp.chat_id_for(self.store.conn, "legacy chat/shared", persist=False)
        for source, chat in sources:
            self.assertEqual(imp.import_chat(self.store, source, 0, chat), "imported")
            invariants = self.store.check_invariants()
            self.assertTrue(invariants["_pass"], invariants)
            self.assertTrue(imp.verify_import(self.store.conn, source, [chat])["ok"])
        expected_chain = [imp.imp_id("m", "queue_json", 0, 0),
                          imp.imp_id("m", "queue_json", 0, 1),
                          imp.imp_id("m", "ram", 0, 0),
                          imp.imp_id("m", "recents", 0, 0)]
        rows = [self.store.conn.execute(
            "SELECT parent_id,content_json FROM messages WHERE id=?", (mid,)).fetchone()
            for mid in expected_chain]
        self.assertEqual([row["parent_id"] for row in rows],
                         [None] + expected_chain[:-1])
        self.assertEqual([json.loads(row["content_json"])["text"] for row in rows],
                         ["same", "queue only", "same", "recent"])
        chat_row = self.store.conn.execute(
            "SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        self.assertEqual(chat_row["head_message_id"], expected_chain[-1])
        self.assertEqual((chat_row["title"], chat_row["model"], chat_row["pinned"],
                          chat_row["created_at"], chat_row["updated_at"]),
                         ("RAM title", "m1", 1, 100, 500))
        positions = [row[0] for row in self.store.conn.execute(
            "SELECT position FROM queue WHERE chat_id=? ORDER BY position", (cid,))]
        self.assertEqual(positions, [0, 1, 2])
        all_text = [json.loads(row[0])["text"] for row in self.store.conn.execute(
            "SELECT content_json FROM messages WHERE chat_id=?", (cid,))]
        self.assertEqual(all_text.count("same"), 2)
        self.assertEqual(all_text.count("q same"), 2)
        events = self.store.events_after(cid, 0, 100)
        transcript_events = [event for event in events
                             if event["type"] == "message.created"
                             and not event["data"].get("queued")]
        self.assertEqual([event["data"]["parent_id"] for event in transcript_events],
                         [None] + expected_chain[:-1])
        for source, chat in sources:
            source_events = [event for event in events
                             if event["data"].get("source") == source]
            self.assertEqual(len(source_events), len(chat["messages"]) + len(chat["queued"]) + 1)
            self.assertTrue(all(event["data"]["origin"] == "import"
                                for event in source_events))
        counts = (self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                  self.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        for source, chat in sources:
            self.assertEqual(imp.import_chat(self.store, source, 0, chat), "skipped")
        self.assertEqual(
            (self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
             self.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]), counts)

    def test_import_appends_to_live_chain_and_existing_queue(self):
        def first_live(conn, seq):
            conn.execute("INSERT INTO chats(id,title,pinned,model,created_at,updated_at,"
                         "head_message_id) VALUES('c_live','Live title',0,NULL,500,500,'live_1')")
            conn.execute("INSERT INTO messages(id,chat_id,parent_id,role,kind,content_json,"
                         "status,created_at) VALUES('live_1','c_live',NULL,'user','user',?,"
                         "'final',1)", (sdb.dumps({"text": "live one"}),))
            return "live_1"

        def second_live(conn, seq):
            conn.execute("INSERT INTO messages(id,chat_id,parent_id,role,kind,content_json,"
                         "status,created_at) VALUES('live_2','c_live','live_1','assistant',"
                         "'assistant',?,'final',2)", (sdb.dumps({"text": "live two"}),))
            conn.execute("UPDATE chats SET head_message_id='live_2' WHERE id='c_live'")
            return "live_2"

        def live_queue(conn, seq):
            conn.execute("INSERT INTO messages(id,chat_id,role,kind,content_json,status,"
                         "created_at) VALUES('live_q','c_live','user','user',?,'final',3)",
                         (sdb.dumps({"text": "live queued"}),))
            conn.execute("INSERT INTO queue(id,chat_id,message_id,state,position)"
                         " VALUES('q_live','c_live','live_q','pending',7)")
            return "live_q"

        self.store.append("c_live", "message.created", {"text": "live one"}, strict=True,
                          state_writer=first_live)
        self.store.append("c_live", "message.created", {"text": "live two"}, strict=True,
                          state_writer=second_live)
        self.store.append("c_live", "message.created", {"text": "live queued"}, strict=True,
                          state_writer=live_queue)
        chat = {"id": "c_live", "title": None, "model": "import model", "pinned": 1,
                "created_at": 100, "updated_at": 900,
                "messages": [{"role": "user", "text": "imported"}],
                "queued": [{"text": "import queued"}]}
        imp.import_chat(self.store, "queue_json", 0, chat)
        imported_id = imp.imp_id("m", "queue_json", 0, 0)
        imported = self.store.conn.execute(
            "SELECT parent_id FROM messages WHERE id=?", (imported_id,)).fetchone()
        self.assertEqual(imported["parent_id"], "live_2")
        imported_event = self.store.conn.execute(
            "SELECT data_json FROM events WHERE id=?", (imported_id,)).fetchone()
        self.assertEqual(json.loads(imported_event["data_json"])["parent_id"], "live_2")
        chat_row = self.store.conn.execute(
            "SELECT * FROM chats WHERE id='c_live'").fetchone()
        self.assertEqual(chat_row["head_message_id"], imported_id)
        self.assertEqual((chat_row["title"], chat_row["model"], chat_row["pinned"],
                          chat_row["created_at"], chat_row["updated_at"]),
                         ("Live title", "import model", 1, 100, 900))
        positions = [row[0] for row in self.store.conn.execute(
            "SELECT position FROM queue WHERE chat_id='c_live' ORDER BY position")]
        self.assertEqual(positions, [7, 8])
        live_text = self.store.conn.execute(
            "SELECT content_json FROM messages WHERE id='live_1'").fetchone()[0]
        self.assertEqual(json.loads(live_text), {"text": "live one"})
        invariants = self.store.check_invariants()
        self.assertTrue(invariants["_pass"], invariants)

    def test_append_batch_rolls_back_state_and_all_events(self):
        self.store.append("c_atomic", "system.import.chat", {"before": True})
        before = self.store.head("c_atomic")

        def broken(conn, first_seq):
            conn.execute("INSERT INTO messages(id,chat_id,role,kind,content_json,status,"
                         "created_at) VALUES('m_bad','c_atomic','user','user','{}','final',1)")
            raise RuntimeError("abort batch")

        items = [{"type": "message.created", "id": "m_bad", "data": {}},
                 {"type": "system.import.chat", "data": {}}]
        with self.assertRaisesRegex(RuntimeError, "abort batch"):
            self.store.append_batch("c_atomic", items, state_writer=broken, strict=True)
        self.assertEqual(self.store.head("c_atomic"), before)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE id='m_bad'").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id='c_atomic'").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
