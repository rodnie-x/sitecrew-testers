"""P1.4: identical Brain fixtures exercise FTS5 and forced brain_terms search."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402
from sitecrew.store.search import SearchIndex, ensure_search_backend  # noqa: E402

NOW = 1800000000000
FIXTURES = [
    {"id": "e_title", "title": "Needle in the title", "body": "A shared body.",
     "tags": ["database"], "case_id": "k_1", "updated_at": NOW - 1000},
    {"id": "e_body", "title": "A body match", "body": "This body contains needle once.",
     "tags": ["sqlite"], "case_id": "k_1", "updated_at": NOW - 1000},
    {"id": "e_tags", "title": "A tag match", "body": "A shared body.",
     "tags": ["needle", "phone"], "case_id": "k_2", "updated_at": NOW - 1000},
    {"id": "e_other", "title": "Unrelated", "body": "Nothing relevant here.",
     "tags": ["garden"], "case_id": "k_2", "updated_at": NOW - 1000},
]


class SearchBackends(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_search_")
        self.indexes = {}
        for backend in ("fts5", "brain_terms"):
            path = os.path.join(self.tmp, backend + ".db")
            conn, _ = sdb.open_db(path)
            index = SearchIndex(conn, force_terms=(backend == "brain_terms"))
            self.assertEqual(index.backend, backend)
            self.indexes[backend] = index
            for fixture in FIXTURES:
                self.put(index, fixture)

    def tearDown(self):
        for index in self.indexes.values():
            index.conn.close()

    def put(self, index, values):
        entry = {
            "id": values["id"], "kind": values.get("kind", "fact"),
            "title": values["title"], "body": values["body"],
            "tags_json": sdb.dumps(values.get("tags", [])), "author": "boss",
            "case_id": values.get("case_id"), "task_id": None, "run_id": None,
            "site": values.get("site"), "subject_key": None, "status": "active",
            "trust": "boss", "visibility": "office",
            "pinned": values.get("pinned", 0), "version": 1, "supersedes_id": None,
            "evidence_json": "[]", "observed_at": values.get("observed_at"),
            "ttl_s": values.get("ttl_s"),
            "created_at": values.get("updated_at", NOW - 1000),
            "updated_at": values.get("updated_at", NOW - 1000), "event_seq": None,
        }
        index.conn.execute(
            "INSERT INTO brain_entries(id,kind,title,body,tags_json,author,case_id,"
            "task_id,run_id,site,subject_key,status,trust,visibility,pinned,version,"
            "supersedes_id,evidence_json,observed_at,ttl_s,created_at,updated_at,event_seq)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(entry.values()))
        index.add(entry["id"])

    def pair(self, winner, loser):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                self.put(index, winner)
                self.put(index, loser)
                hits = index.search("rankword", now_ms=NOW)
                self.assertEqual(hits[0]["id"], winner["id"])

    def test_backend_probe_and_forced_fallback(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                expected = "brain_terms" if backend == "brain_terms" else index.backend
                self.assertEqual(ensure_search_backend(
                    index.conn, force_terms=(backend == "brain_terms")), expected)
        self.assertEqual(self.indexes["brain_terms"].conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='brain_fts'").fetchone()[0], 0)

    def test_title_body_and_tags_are_indexed(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                hits = index.search("needle", now_ms=NOW)
                self.assertEqual({hit["id"] for hit in hits},
                                 {"e_title", "e_body", "e_tags"})

    def test_add_update_delete_lifecycle(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                self.put(index, {"id": "e_life", "title": "oldword", "body": "body",
                                 "tags": ["oldtag"]})
                self.assertEqual(index.search("oldword", now_ms=NOW)[0]["id"], "e_life")
                index.conn.execute(
                    "UPDATE brain_entries SET title='newword', body='changed',"
                    " tags_json='[\"newtag\"]' WHERE id='e_life'")
                index.update("e_life")
                self.assertEqual(index.search("oldword newtag", now_ms=NOW), [])
                self.assertEqual(index.search("newtag", now_ms=NOW)[0]["id"], "e_life")
                index.delete("e_life")
                self.assertEqual(index.search("newword", now_ms=NOW), [])
                self.assertEqual(index.conn.execute(
                    "SELECT COUNT(*) FROM brain_entries WHERE id='e_life'").fetchone()[0], 1)

    def test_canonical_result_sets_byte_match(self):
        canonical = {}
        for backend, index in self.indexes.items():
            hits = index.search("needle", now_ms=NOW)
            canonical[backend] = sdb.dumps(sorted(hits, key=lambda hit: hit["id"]))
        self.assertEqual(canonical["fts5"], canonical["brain_terms"])
        self.assertNotEqual(canonical["fts5"], "[]")

    def test_unambiguous_relevance_winner(self):
        self.pair(
            {"id": "e_relevant", "title": "rankword rankword rankword",
             "body": "rankword rankword", "tags": ["rankword"]},
            {"id": "e_less_relevant", "title": "A passing mention",
             "body": "rankword appears once among many otherwise unrelated words",
             "tags": ["other"]})

    def test_unambiguous_recency_winner(self):
        self.pair(
            {"id": "e_new", "title": "rankword", "body": "same", "updated_at": NOW - 1000},
            {"id": "e_old", "title": "rankword", "body": "same",
             "updated_at": NOW - 400 * 86400000})

    def test_unambiguous_pin_winner(self):
        self.pair(
            {"id": "e_pinned", "title": "rankword", "body": "same", "pinned": 1},
            {"id": "e_unpinned", "title": "rankword", "body": "same", "pinned": 0})

    def test_unambiguous_same_case_winner(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                self.put(index, {"id": "e_same_case", "title": "rankword", "body": "same",
                                 "case_id": "k_current"})
                self.put(index, {"id": "e_other_case", "title": "rankword", "body": "same",
                                 "case_id": "k_other"})
                hits = index.search("rankword", case_id="k_current", now_ms=NOW)
                self.assertEqual(hits[0]["id"], "e_same_case")
                self.assertEqual({hit["id"] for hit in hits},
                                 {"e_same_case", "e_other_case"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
