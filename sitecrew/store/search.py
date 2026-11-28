"""Brain search indexes (P1.4): FTS5 when available, brain_terms otherwise.

SearchIndex maintains only the search index for existing brain_entries rows; Brain
writes/events and HTTP routes are Phase 5 concerns. The caller owns transactions.
Both backends return the same compact-card shape. Ranking is BM25 followed by an
explainable recency decay, pin boost, same-Case boost, and entry-id tie break.
"""
import json
import math
import re
import sqlite3
from collections import Counter

from . import db

_TOKEN = re.compile(r"\w+", re.UNICODE)
_DAY_MS = 86400000


def _tokens(value):
    return [token.lower() for token in _TOKEN.findall(value or "")]


def _tags(entry):
    tags = entry["tags_json"] or "[]"
    if not isinstance(tags, str):
        return tags
    return json.loads(tags)


def _entry(conn, entry):
    if isinstance(entry, str):
        row = conn.execute(
            "SELECT * FROM brain_entries WHERE id=?", (entry,)).fetchone()
        if row is None:
            raise ValueError("unknown brain entry: %s" % entry)
        return dict(row)
    return entry


def ensure_search_backend(conn, force_terms=False):
    """Create the FTS index when possible and return 'fts5' or 'brain_terms'.

    force_terms=True selects the phone-compatible path even on FTS5-capable PCs.
    An actual CREATE is the feature probe; compile_options alone is insufficient.
    """
    if not force_terms:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS brain_fts USING"
                " fts5(entry_id UNINDEXED, title, body, tags)")
            return "fts5"
        except sqlite3.OperationalError:
            pass
    return "brain_terms"


class SearchIndex:
    """Synchronize and query one of the two interchangeable Brain indexes."""

    def __init__(self, conn, force_terms=False):
        self.conn = conn
        self.backend = ensure_search_backend(conn, force_terms=force_terms)

    def add(self, entry):
        """Index a brain_entries row (or its mapping); repeated adds replace terms."""
        entry = _entry(self.conn, entry)
        entry_id = entry["id"]
        text = (entry["title"], entry["body"],
                " ".join(str(tag) for tag in _tags(entry)))
        if self.backend == "fts5":
            self.conn.execute("DELETE FROM brain_fts WHERE entry_id=?", (entry_id,))
            self.conn.execute(
                "INSERT INTO brain_fts(entry_id, title, body, tags) VALUES(?,?,?,?)",
                (entry_id,) + text)
        else:
            self.conn.execute("DELETE FROM brain_terms WHERE entry_id=?", (entry_id,))
            terms = _tokens(" ".join(text))
            self.conn.executemany(
                "INSERT INTO brain_terms(term, entry_id) VALUES(?,?)",
                [(term, entry_id) for term in terms])

    def update(self, entry):
        """Refresh an existing row after its title, body, or tags change."""
        self.add(entry)

    def delete(self, entry_id):
        """Remove only index content; Brain retraction/hard-delete policy is Phase 5."""
        if self.backend == "fts5":
            self.conn.execute("DELETE FROM brain_fts WHERE entry_id=?", (entry_id,))
        else:
            self.conn.execute("DELETE FROM brain_terms WHERE entry_id=?", (entry_id,))

    def search(self, query, kinds=None, case_id=None, site=None, limit=8, now_ms=None):
        """Return at most eight matching compact cards, best ranked first."""
        terms = _tokens(query)
        if not terms:
            return []
        limit = max(0, min(int(limit), 8))
        if not limit:
            return []
        now = db.utc_ms() if now_ms is None else int(now_ms)
        if self.backend == "fts5":
            ranked = self._fts_candidates(terms)
        else:
            ranked = self._term_candidates(terms)
        results = []
        for entry, relevance in ranked:
            if kinds is not None and entry["kind"] not in kinds:
                continue
            if site is not None and entry["site"] != site:
                continue
            age = max(0, now - (entry["updated_at"] or entry["created_at"]))
            recency = 1.0 / (1.0 + age / (30.0 * _DAY_MS))
            score = (relevance + recency + (1.0 if entry["pinned"] else 0.0)
                     + (1.0 if case_id is not None and entry["case_id"] == case_id else 0.0))
            observed_at, ttl_s = entry["observed_at"], entry["ttl_s"]
            stale = (observed_at is not None and ttl_s is not None
                     and now > observed_at + ttl_s * 1000)
            results.append((score, entry, {
                "id": entry["id"],
                "kind": entry["kind"],
                "title": entry["title"],
                "snippet": entry["body"][:200],
                "author": entry["author"],
                "age": age,
                "stale": stale,
                "trust": entry["trust"],
            }))
        results.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [card for score, entry, card in results[:limit]]

    def _fts_candidates(self, terms):
        match = " AND ".join('"%s"' % term for term in terms)
        rows = self.conn.execute(
            "SELECT e.*, -bm25(brain_fts, 0.0, 1.0, 1.0, 1.0) AS relevance"
            " FROM brain_fts f JOIN brain_entries e ON e.id=f.entry_id"
            " WHERE brain_fts MATCH ?", (match,)).fetchall()
        return [(dict(row), row["relevance"]) for row in rows]

    def _term_candidates(self, terms):
        documents = {}
        for row in self.conn.execute("SELECT term, entry_id FROM brain_terms"):
            documents.setdefault(row["entry_id"], Counter())[row["term"]] += 1
        candidate_ids = [entry_id for entry_id, counts in documents.items()
                         if all(term in counts for term in terms)]
        if not candidate_ids:
            return []
        total_docs = len(documents)
        average_length = (sum(sum(counts.values()) for counts in documents.values())
                          / float(total_docs))
        frequencies = {term: sum(1 for counts in documents.values() if term in counts)
                       for term in terms}
        idfs = {term: math.log(1.0 + (total_docs - frequencies[term] + 0.5)
                               / (frequencies[term] + 0.5)) for term in terms}
        placeholders = ",".join("?" for entry_id in candidate_ids)
        rows = self.conn.execute(
            "SELECT * FROM brain_entries WHERE id IN (%s)" % placeholders,
            candidate_ids).fetchall()
        ranked = []
        for row in rows:
            counts = documents[row["id"]]
            length = sum(counts.values())
            relevance = 0.0
            for term in terms:
                frequency = counts[term]
                denominator = frequency + 1.2 * (
                    1.0 - 0.75 + 0.75 * length / average_length)
                relevance += idfs[term] * frequency * 2.2 / denominator
            ranked.append((dict(row), relevance))
        return ranked
