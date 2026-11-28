"""P1.4: identical Brain fixtures exercise FTS5 and forced brain_terms search."""
import json
import math
import os
import random
import re
import sys
import tempfile
import unicodedata
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402
from sitecrew.store.events import EventStore  # noqa: E402
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
    {"id": "e_under", "title": "under score and alpha beta", "body": "underscores",
     "tags": []},
    {"id": "e_cafe_nfc", "title": "café", "body": "NFC", "tags": []},
    {"id": "e_cafe_nfd", "title": "cafe\u0301", "body": "NFD", "tags": []},
    {"id": "e_istanbul", "title": "Istanbul İstanbul ISTANBUL", "body": "dotted I",
     "tags": []},
]


def put_entry(index, values):
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
        put_entry(index, values)

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

    def test_unicode_query_parity_matrix(self):
        """Twenty literal queries with independently specified expected ID sets."""
        cafes = {"e_cafe_nfc", "e_cafe_nfd"}
        matrix = [
            ("under_score", {"e_under"}),
            ("UNDER_SCORE", {"e_under"}),
            ("under score", {"e_under"}),
            ("alpha_beta", {"e_under"}),
            ("alpha beta", {"e_under"}),
            ("alpha_bet", set()),
            ("cafe", cafes),
            ("café", cafes),
            ("CAFÉ", cafes),
            ("cafe\u0301", cafes),
            ("CAFE\u0301", cafes),
            ("cafe cafe CAFÉ", cafes),
            ("cafés", set()),
            ("Istanbul", {"e_istanbul"}),
            ("İstanbul", {"e_istanbul"}),
            ("ISTANBUL", {"e_istanbul"}),
            ("I\u0307stanbul", {"e_istanbul"}),
            ("i\u0307stanbul", {"e_istanbul"}),
            ('"café"', cafes),
            ("___", set()),
        ]
        self.assertGreaterEqual(len(matrix), 15)
        for query, expected in matrix:
            canonical = {}
            for backend, index in self.indexes.items():
                with self.subTest(query=query, backend=backend):
                    hits = index.search(query, now_ms=NOW)
                    self.assertEqual({hit["id"] for hit in hits}, expected)
                    canonical[backend] = sdb.dumps(
                        sorted(hits, key=lambda hit: hit["id"]))
            self.assertEqual(canonical["fts5"], canonical["brain_terms"])

    def test_fts_is_rowid_keyed_and_delete_does_not_leave_aliases(self):
        fts = self.indexes["fts5"]
        columns = [row["name"] for row in fts.conn.execute("PRAGMA table_info(brain_fts)")]
        self.assertEqual(columns, ["title", "body", "tags"])
        self.put(fts, {"id": "e_delete_rowid", "title": "shared delete", "body": "same"})
        rowid = fts.conn.execute(
            "SELECT rowid FROM brain_entries WHERE id='e_delete_rowid'").fetchone()[0]
        indexed = fts.conn.execute(
            "SELECT rowid FROM brain_fts WHERE rowid=?", (rowid,)).fetchone()
        self.assertIsNotNone(indexed)
        fts.delete("e_delete_rowid")
        self.assertEqual(fts.conn.execute(
            "SELECT COUNT(*) FROM brain_fts WHERE rowid=?", (rowid,)).fetchone()[0], 0)
        self.assertEqual(fts.search("delete", now_ms=NOW), [])

    def test_query_terms_are_deduplicated_before_both_candidate_paths(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                method = "_fts_candidates" if backend == "fts5" else "_term_candidates"
                original = getattr(index, method)
                seen = []

                def capture(terms):
                    seen.append(terms)
                    return original(terms)

                setattr(index, method, capture)
                try:
                    index.search("needle NEEDLE needle", now_ms=NOW)
                finally:
                    setattr(index, method, original)
                self.assertEqual(seen, [["needle"]])

    def test_3000_term_query_uses_same_32_term_cap_on_both_backends(self):
        words = ["long%04d" % n for n in range(3000)]
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                self.put(index, {"id": "e_long_query",
                                 "title": " ".join(words[:32]), "body": ""})
                hits = index.search(" ".join(words), now_ms=NOW)
                self.assertEqual([hit["id"] for hit in hits], ["e_long_query"])

    def test_fallback_candidates_are_aggregated_in_sql(self):
        index = self.indexes["brain_terms"]
        statements = []
        index.conn.set_trace_callback(statements.append)
        try:
            hits = index.search("needle", now_ms=NOW)
        finally:
            index.conn.set_trace_callback(None)
        self.assertEqual({hit["id"] for hit in hits},
                         {"e_title", "e_body", "e_tags"})
        sql = [statement.upper() for statement in statements]
        self.assertTrue(any("GROUP BY T.ENTRY_ID" in statement
                            and "HAVING COUNT(DISTINCT" in statement
                            for statement in sql), sql)
        self.assertNotIn("SELECT TERM, ENTRY_ID FROM BRAIN_TERMS", sql)

    def test_tie_break_limit_clamp_filters_stale_and_query_quoting(self):
        for backend, index in self.indexes.items():
            with self.subTest(backend=backend):
                for n in range(10):
                    self.put(index, {
                        "id": "e_tie_%02d" % n, "title": "tieword", "body": "same",
                        "kind": "note" if n % 2 else "fact",
                        "site": "phone" if n < 5 else "hq",
                        "observed_at": NOW - 10000, "ttl_s": 1 if n == 0 else None})
                hits = index.search("tieword", limit=99, now_ms=NOW)
                self.assertEqual([hit["id"] for hit in hits],
                                 ["e_tie_%02d" % n for n in range(8)])
                self.assertEqual(index.search("tieword", limit=0, now_ms=NOW), [])
                self.assertEqual(index.search("tieword", limit=-4, now_ms=NOW), [])
                self.assertEqual(index.search("tieword", kinds=[], now_ms=NOW), [])
                notes = index.search("tieword", kinds=["note"], site="phone",
                                     now_ms=NOW)
                self.assertEqual([hit["id"] for hit in notes],
                                 ["e_tie_01", "e_tie_03"])
                self.assertTrue(hits[0]["stale"])
                self.assertFalse(hits[1]["stale"])
                self.put(index, {
                    "id": "e_quoting", "title": 'cafe AND OR NOT NEAR "quoted"',
                    "body": "punctuation: (works) * ^"})
                quoted = index.search('CAFÉ "AND" OR (NOT) NEAR * ^', now_ms=NOW)
                self.assertIn("e_quoting", {hit["id"] for hit in quoted})
                self.assertEqual(index.search('"', now_ms=NOW), [])

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


class RandomizedRelevance(unittest.TestCase):
    setUp = SearchBackends.setUp
    tearDown = SearchBackends.tearDown
    put = SearchBackends.put

    def reference_tokens(self, value):
        # Deliberately independent of search._tokens: this is the test oracle.
        decomposed = unicodedata.normalize("NFKD", value or "").casefold()
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        return re.findall(r"[^\W_]+", stripped, flags=re.UNICODE)

    def test_randomized_per_document_relevance_against_independent_bm25(self):
        rng = random.Random(1404)
        vocabulary = ["alpha", "beta", "gamma", "delta", "epsilon", "rare"]
        for n in range(36):
            if n % 9 == 0:
                words = []
            else:
                words = ["shared", "common"] + [
                    rng.choice(vocabulary) for _ in range(rng.randrange(0, 19))]
            cut = len(words) // 3
            put_entry(self.indexes["fts5"], {
                "id": "e_random_%02d" % n,
                "title": " ".join(words[:cut]),
                "body": " ".join(words[cut:]),
                "tags": words[-1:] if words else []})
            put_entry(self.indexes["brain_terms"], {
                "id": "e_random_%02d" % n,
                "title": " ".join(words[:cut]),
                "body": " ".join(words[cut:]),
                "tags": words[-1:] if words else []})

        entries = {row["id"]: dict(row) for row in self.indexes["fts5"].conn.execute(
            "SELECT * FROM brain_entries")}
        documents = {}
        for entry_id, entry in entries.items():
            text = " ".join((entry["title"], entry["body"],
                             " ".join(json.loads(entry["tags_json"] or "[]"))))
            documents[entry_id] = Counter(self.reference_tokens(text))
        total_docs = len(entries)  # Includes all empty FTS rows and brain_entries.
        average_length = sum(sum(doc.values()) for doc in documents.values()) / total_docs
        self.assertTrue(any(not doc for doc in documents.values()))
        common_df = sum("common" in doc for doc in documents.values())
        self.assertLess(math.log((total_docs - common_df + 0.5) / (common_df + 0.5)), 0)

        queries = ["common", "shared", "common shared", "common common SHARED"]
        queries += vocabulary
        queries += ["common " + term for term in vocabulary]
        queries += ["shared " + term for term in vocabulary]
        rng.shuffle(queries)
        for query in queries:
            terms = list(dict.fromkeys(self.reference_tokens(query)))
            frequencies = {
                term: sum(term in doc for doc in documents.values()) for term in terms}
            expected = {}
            for entry_id, doc in documents.items():
                if not all(term in doc for term in terms):
                    continue
                relevance = 0.0
                length = sum(doc.values())
                for term in terms:
                    df = frequencies[term]
                    idf = max(1e-6, math.log(
                        (total_docs - df + 0.5) / (df + 0.5)))
                    tf = doc[term]
                    denominator = tf + 1.2 * (
                        0.25 + 0.75 * length / average_length)
                    relevance += idf * tf * 2.2 / denominator
                expected[entry_id] = relevance
            for backend, index in self.indexes.items():
                with self.subTest(query=query, backend=backend):
                    method = (index._fts_candidates if backend == "fts5"
                              else index._term_candidates)
                    actual = {entry["id"]: relevance
                              for entry, relevance in method(terms)}
                    self.assertEqual(set(actual), set(expected))
                    for entry_id, relevance in actual.items():
                        self.assertAlmostEqual(
                            relevance, expected[entry_id], delta=1e-6,
                            msg="%s %s %s" % (backend, query, entry_id))


class BackendSwitching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_search_switch_")
        self.conn, _ = sdb.open_db(os.path.join(self.tmp, "switch.db"))

    def tearDown(self):
        self.conn.close()

    def canonical(self, index, query):
        return sdb.dumps(sorted(index.search(query, now_ms=NOW),
                                key=lambda hit: hit["id"]))

    def persisted_backend(self):
        return self.conn.execute(
            "SELECT value FROM meta WHERE key='search_backend'").fetchone()[0]

    def test_existing_caller_transaction_is_joined_not_committed(self):
        self.conn.execute("BEGIN IMMEDIATE")
        SearchIndex(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute("ROLLBACK")
        self.assertFalse(self.conn.in_transaction)

    def test_public_rebuild_repairs_vacuum_rowid_drift_and_orphans(self):
        index = SearchIndex(self.conn)
        for n in range(3):
            put_entry(index, {"id": "e_vac_%d" % n,
                              "title": "survivor %d" % n, "body": "vacuum"})
        index.delete("e_vac_0")
        self.conn.execute("DELETE FROM brain_entries WHERE id='e_vac_0'")
        self.conn.commit()
        before = {row["id"]: row["rowid"] for row in self.conn.execute(
            "SELECT id, rowid FROM brain_entries")}
        self.conn.execute("VACUUM")
        # VACUUM is permitted, but not required, to change rowids. Shift them
        # explicitly so this test deterministically exercises the required repair.
        self.conn.execute("UPDATE brain_entries SET rowid=rowid+10")
        self.conn.commit()
        after = {row["id"]: row["rowid"] for row in self.conn.execute(
            "SELECT id, rowid FROM brain_entries")}
        self.assertNotEqual(before, after)
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM brain_fts f LEFT JOIN brain_entries e"
            " ON e.rowid=f.rowid WHERE e.id IS NULL").fetchone()[0]
        self.assertGreater(orphans, 0)
        index.rebuild()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM brain_fts f LEFT JOIN brain_entries e"
            " ON e.rowid=f.rowid WHERE e.id IS NULL").fetchone()[0], 0)
        self.assertEqual({hit["id"] for hit in index.search("survivor", now_ms=NOW)},
                         {"e_vac_1", "e_vac_2"})

    def test_pc_to_phone_to_pc_persists_choice_and_rebuilds_every_entry(self):
        pc = SearchIndex(self.conn)
        self.assertEqual(pc.backend, "fts5")
        self.assertEqual(self.persisted_backend(), "fts5")
        for fixture in FIXTURES:
            put_entry(pc, fixture)
        put_entry(pc, {"id": "e_empty", "title": "", "body": "", "tags": []})
        before = self.canonical(pc, "café")

        phone = SearchIndex(self.conn, force_terms=True)
        self.assertEqual(phone.backend, "brain_terms")
        self.assertEqual(self.persisted_backend(), "brain_terms")
        self.assertEqual(self.canonical(phone, "CAFE\u0301"), before)
        self.assertNotEqual(before, "[]")
        put_entry(phone, {"id": "e_phone_write", "title": "phone only common",
                          "body": "written while forced", "tags": []})

        pc_again = SearchIndex(self.conn)
        self.assertEqual(pc_again.backend, "fts5")
        self.assertEqual(self.persisted_backend(), "fts5")
        self.assertEqual(self.canonical(pc_again, "CAFÉ"), before)
        self.assertEqual(pc_again.search("phone", now_ms=NOW)[0]["id"],
                         "e_phone_write")
        entry_count = self.conn.execute(
            "SELECT COUNT(*) FROM brain_entries").fetchone()[0]
        fts_count = self.conn.execute("SELECT COUNT(*) FROM brain_fts").fetchone()[0]
        self.assertEqual(fts_count, entry_count)  # Empty documents were rebuilt too.
        orphan_count = self.conn.execute(
            "SELECT COUNT(*) FROM brain_fts f LEFT JOIN brain_entries e"
            " ON e.rowid=f.rowid WHERE e.id IS NULL").fetchone()[0]
        self.assertEqual(orphan_count, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_temp_master"
            " WHERE name LIKE 'brain_fts_probe%'").fetchone()[0], 0)


class EventStoreIntegration(unittest.TestCase):
    def test_search_open_and_switch_leave_eventstore_append_ready(self):
        for initially_forced in (False, True):
            with self.subTest(initially_forced=initially_forced):
                tmp = tempfile.mkdtemp(prefix="sc_search_events_")
                store = EventStore(os.path.join(tmp, "events.db"))
                try:
                    index = SearchIndex(store.conn, force_terms=initially_forced)
                    self.assertFalse(store.conn.in_transaction)
                    first = store.append("c_search", "system.import.chat",
                                         {"stage": "initial"})
                    self.assertEqual(first["seq"], 1)
                    self.assertFalse(store.conn.in_transaction)

                    put_entry(index, {"id": "e_event_integration",
                                      "title": "integration", "body": "search"})
                    store.conn.commit()
                    switched = SearchIndex(
                        store.conn, force_terms=not initially_forced)
                    self.assertNotEqual(index.backend, switched.backend)
                    self.assertFalse(store.conn.in_transaction)
                    second = store.append("c_search", "system.import.chat",
                                          {"stage": "switched"})
                    self.assertEqual(second["seq"], 2)
                    self.assertFalse(store.conn.in_transaction)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
