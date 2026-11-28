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
import unicodedata

from . import db

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_DAY_MS = 86400000


def _tokens(value):
    """The one tokenization contract shared by both indexes and their queries."""
    normalized = unicodedata.normalize("NFKD", value or "").casefold()
    without_diacritics = "".join(
        char for char in normalized if not unicodedata.combining(char))
    return _TOKEN.findall(without_diacritics)


def _token_text(value):
    return " ".join(_tokens(value))


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


def _entry_text(entry):
    return (_token_text(entry["title"]), _token_text(entry["body"]),
            _token_text(" ".join(str(tag) for tag in _tags(entry))))


def _index_entry(conn, backend, entry):
    entry_id = entry["id"]
    text = _entry_text(entry)
    if backend == "fts5":
        row = conn.execute(
            "SELECT rowid FROM brain_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("unknown brain entry: %s" % entry_id)
        rowid = row[0]
        conn.execute("DELETE FROM brain_fts WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO brain_fts(rowid, title, body, tags) VALUES(?,?,?,?)",
            (rowid,) + text)
    else:
        conn.execute("DELETE FROM brain_terms WHERE entry_id=?", (entry_id,))
        terms = _tokens(" ".join(text))
        conn.executemany(
            "INSERT INTO brain_terms(term, entry_id) VALUES(?,?)",
            [(term, entry_id) for term in terms])


def _rebuild(conn, backend):
    """Replace the selected index from every entry, including empty documents."""
    if backend == "fts5":
        conn.execute("DELETE FROM brain_fts")
    else:
        conn.execute("DELETE FROM brain_terms")
    rows = conn.execute("SELECT * FROM brain_entries")
    for row in rows:
        _index_entry(conn, backend, dict(row))


def _probe_fts5(conn):
    """Exercise a temporary FTS5 table; compile options are not a usable probe."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.brain_fts_probe USING"
            " fts5(value, tokenize='unicode61 remove_diacritics 0')")
        conn.execute("INSERT INTO temp.brain_fts_probe(value) VALUES('probe')")
        found = conn.execute(
            "SELECT rowid FROM temp.brain_fts_probe"
            " WHERE brain_fts_probe MATCH 'probe'").fetchone()
        if found is None:
            raise sqlite3.OperationalError("FTS5 probe MATCH returned no row")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.brain_fts_probe")


def ensure_search_backend(conn, force_terms=False):
    """Select, persist, and if necessary rebuild 'fts5' or 'brain_terms'.

    force_terms=True selects the phone-compatible path even on FTS5-capable PCs.
    A changed selection is rebuilt from all brain_entries so a database can move
    in either direction between FTS5-capable and fallback-only runtimes.
    """
    backend = "brain_terms" if force_terms or not _probe_fts5(conn) else "fts5"
    if backend == "fts5":
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS brain_fts USING"
            " fts5(title, body, tags, tokenize='unicode61 remove_diacritics 0')")
    previous = conn.execute(
        "SELECT value FROM meta WHERE key='search_backend'").fetchone()
    if previous is None or previous[0] != backend:
        _rebuild(conn, backend)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('search_backend', ?)",
            (backend,))
    return backend


class SearchIndex:
    """Synchronize and query one of the two interchangeable Brain indexes."""

    def __init__(self, conn, force_terms=False):
        self.conn = conn
        self.backend = ensure_search_backend(conn, force_terms=force_terms)

    def add(self, entry):
        """Index a brain_entries row (or its mapping); repeated adds replace terms."""
        _index_entry(self.conn, self.backend, _entry(self.conn, entry))

    def update(self, entry):
        """Refresh an existing row after its title, body, or tags change."""
        self.add(entry)

    def delete(self, entry_id):
        """Remove only index content; Brain retraction/hard-delete policy is Phase 5."""
        if self.backend == "fts5":
            self.conn.execute(
                "DELETE FROM brain_fts WHERE rowid="
                "(SELECT rowid FROM brain_entries WHERE id=?)", (entry_id,))
        else:
            self.conn.execute("DELETE FROM brain_terms WHERE entry_id=?", (entry_id,))

    def search(self, query, kinds=None, case_id=None, site=None, limit=8, now_ms=None):
        """Return at most eight matching compact cards, best ranked first."""
        terms = list(dict.fromkeys(_tokens(query)))
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
            "SELECT e.*, -bm25(brain_fts, 1.0, 1.0, 1.0) AS relevance"
            " FROM brain_fts f JOIN brain_entries e ON e.rowid=f.rowid"
            " WHERE brain_fts MATCH ?", (match,)).fetchall()
        return [(dict(row), row["relevance"]) for row in rows]

    def _term_candidates(self, terms):
        """Aggregate candidates in SQL; Python sees matches, never the whole index."""
        total_docs = self.conn.execute(
            "SELECT COUNT(*) FROM brain_entries").fetchone()[0]
        if not total_docs:
            return []
        total_length = self.conn.execute(
            "SELECT COUNT(*) FROM brain_terms").fetchone()[0]
        average_length = total_length / float(total_docs)
        placeholders = ",".join("?" for term in terms)
        frequencies = dict(self.conn.execute(
            "SELECT term, COUNT(DISTINCT entry_id) FROM brain_terms"
            " WHERE term IN (%s) GROUP BY term" % placeholders,
            terms).fetchall())
        idfs = {
            term: max(1e-6, math.log(
                (total_docs - frequencies.get(term, 0) + 0.5)
                / (frequencies.get(term, 0) + 0.5)))
            for term in terms
        }
        term_counts = "," + ",".join(
            " SUM(CASE WHEN t.term=? THEN 1 ELSE 0 END) AS tf_%d" % n
            for n in range(len(terms)))
        rows = self.conn.execute(
            "SELECT e.*, COUNT(t.term) AS doc_length" + term_counts +
            " FROM brain_terms t JOIN brain_entries e ON e.id=t.entry_id"
            " GROUP BY t.entry_id"
            " HAVING COUNT(DISTINCT CASE WHEN t.term IN (" + placeholders +
            ") THEN t.term END)=?",
            terms + terms + [len(terms)]).fetchall()
        ranked = []
        for row in rows:
            length = row["doc_length"]
            relevance = 0.0
            for n, term in enumerate(terms):
                frequency = row["tf_%d" % n]
                denominator = frequency + 1.2 * (
                    1.0 - 0.75 + 0.75 * length / average_length)
                relevance += idfs[term] * frequency * 2.2 / denominator
            ranked.append((dict(row), relevance))
        return ranked
