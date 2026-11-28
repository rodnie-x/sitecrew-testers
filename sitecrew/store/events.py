"""EventStore: gapless per-stream seq, one transaction for state+event, long-poll.

Boss rulings implemented (docs/BOSS_PHASE1_ORDER.md):
- seq is allocated inside the same BEGIN IMMEDIATE transaction as the state change it
  describes — "event exists ⇔ state committed" by construction;
- ONE process-wide writer lock per store; BEGIN IMMEDIATE under it;
- threading.Condition notify on append; check-then-wait under the SAME lock with a
  1 s belt (a missed notify costs at most 1 s — boss D3 ruling);
- poll contract: GET /api/v2/poll?streams=c_x:318,office:12&wait=25 →
  {"events":[envelopes], "heads":{...}, "more":bool, "reset":[streams]}; grouped by
  stream in request order, ascending seq, ≤ 200 per response; cursor above head →
  reset; office stream is the literal 'office';
- envelope v2 on output: v is a constant 2 with no column; data via one dumps() and
  one to_envelope(row);
- check_invariants(): integrity_check ok, foreign_key_check empty, seq contiguous
  1..head per stream, messages↔message.created and tool_calls.committed_seq↔
  tool.committed joined both directions, cases.turn_id ↔ turns.case_id agreement,
  no chats row with id 'office'.
"""
import json
import threading

from . import db

OFFICE_STREAM = "office"


def parse_streams(spec):
    """'c_x:318,office:12' → {'c_x': 318, 'office': 12} (cursor = last applied seq)."""
    streams = {}
    if not spec:
        return streams
    for part in str(spec).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, cur = part.rpartition(":")
        try:
            streams[name] = int(cur)
        except ValueError:
            continue
    return streams


def to_envelope(row):
    """One DB row → envelope v2 (v is a constant 2 on output, no column)."""
    return {
        "chat_id": row["chat_id"],
        "seq": row["seq"],
        "id": row["id"],
        "v": 2,
        "ts": row["ts"],
        "type": row["type"],
        "turn_id": row["turn_id"],
        "case_id": row["case_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "agent": row["agent"],
        "site": row["site"],
        "data": json.loads(row["data_json"]) if row["data_json"] else {},
    }


class EventStore:
    """One writer per process; readers use poll(). The engine owns the only writer."""

    def __init__(self, path):
        self.path = path
        self.conn, self.version = db.open_db(path)
        # reads go through a query_only connection (boss precondition: the writer
        # connection never serves reads; query_only refuses accidental writes)
        self._read = db.connect(path, read_only=True)
        self._writer = threading.Lock()          # process-wide writer lock
        self._cond = threading.Condition()       # notified on every append
        self._heads = {}
        self._proclock = db.ProcLock(path)
        try:
            self._proclock.acquire()
        except OSError:
            self._proclock.release()
            raise RuntimeError(db.DB_LOCKED)
        self._race_hook = None                   # test-only: injected between
        for row in self.conn.execute(           #   check and wait (deterministic race)
                "SELECT chat_id, MAX(seq) AS h FROM events GROUP BY chat_id"):
            self._heads[row["chat_id"]] = row["h"]

    # ------------------------------------------------------------ internals
    def _next_seq(self, conn, stream):
        row = conn.execute("SELECT MAX(seq) FROM events WHERE chat_id=?", (stream,)).fetchone()
        return (row[0] or 0) + 1

    # ------------------------------------------------------------ append
    def append(self, stream, type_name, data, turn_id=None, case_id=None,
               task_id=None, run_id=None, agent=None, site=None, state_writer=None,
               strict=False, ts=None, failpoint=None):
        """Write state + event in ONE transaction; returns the envelope.

        state_writer(conn, seq) runs inside the same transaction and writes the state
        row(s) the event describes (boss rule). It may return a string — an explicit
        event id (P1.2 oracle: messages.id k-i and events.id k-i are the same). With
        strict=True an unknown type raises (SITECREW_STRICT=1 in tests); production
        logs and writes.

        failpoint: deterministic kill hook for the P1.2 harness (boss rerun spec) —
        a callable(point) invoked at "after_state", "after_event", "before_commit"
        and "after_commit"; the harness SIGKILLs the process at the named point.
        Production passes None.
        """
        if not db.event_type_known(type_name):
            if strict:
                raise ValueError("unknown event type: %r" % type_name)
        eid = None
        with self._writer:
            with self._cond:
                self.conn.execute("BEGIN IMMEDIATE")
                committed = False
                try:
                    seq = self._next_seq(self.conn, stream)
                    if state_writer is not None:
                        override = state_writer(self.conn, seq)
                        if isinstance(override, str) and override:
                            eid = override
                    if failpoint is not None:
                        failpoint("after_state")
                    if eid is None:
                        eid = db.new_id("e")
                    self.conn.execute(
                        "INSERT INTO events(chat_id, seq, id, turn_id, type, data_json,"
                        " ts, case_id, task_id, run_id, agent, site)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (stream, seq, eid, turn_id, type_name, db.dumps(data),
                         ts if ts is not None else db.utc_ms(),
                         case_id, task_id, run_id, agent, site))
                    if failpoint is not None:
                        failpoint("after_event")
                    if failpoint is not None:
                        failpoint("before_commit")
                    self.conn.execute("COMMIT")
                    committed = True
                    if failpoint is not None:
                        failpoint("after_commit")
                except BaseException:
                    if not committed and self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise
                self._heads[stream] = seq
                self._cond.notify_all()
        return self.envelope_for(stream, seq)

    def append_batch(self, stream, items, state_writer=None, strict=False):
        """Write one state change and its ordered events in ONE transaction.

        Each item is a mapping with type/data and optional id, ts, turn_id, case_id,
        task_id, run_id, agent and site. state_writer(conn, first_seq) runs before
        the events. Sequence allocation, locking and notification match append().
        """
        items = list(items)
        if not items:
            raise ValueError("append_batch requires at least one event")
        if strict:
            for item in items:
                if not db.event_type_known(item["type"]):
                    raise ValueError("unknown event type: %r" % item["type"])
        with self._writer:
            with self._cond:
                self.conn.execute("BEGIN IMMEDIATE")
                committed = False
                try:
                    first_seq = self._next_seq(self.conn, stream)
                    if state_writer is not None:
                        state_writer(self.conn, first_seq)
                    for offset, item in enumerate(items):
                        self.conn.execute(
                            "INSERT INTO events(chat_id, seq, id, turn_id, type, data_json,"
                            " ts, case_id, task_id, run_id, agent, site)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (stream, first_seq + offset, item.get("id") or db.new_id("e"),
                             item.get("turn_id"), item["type"], db.dumps(item["data"]),
                             item["ts"] if item.get("ts") is not None else db.utc_ms(),
                             item.get("case_id"), item.get("task_id"), item.get("run_id"),
                             item.get("agent"), item.get("site")))
                    self.conn.execute("COMMIT")
                    committed = True
                except BaseException:
                    if not committed and self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise
                self._heads[stream] = first_seq + len(items) - 1
                self._cond.notify_all()
        return [self.envelope_for(stream, first_seq + offset)
                for offset in range(len(items))]

    def envelope_for(self, stream, seq):
        row = self._read.execute(
            "SELECT * FROM events WHERE chat_id=? AND seq=?", (stream, seq)).fetchone()
        return to_envelope(row)

    # ------------------------------------------------------------ read
    def head(self, stream):
        return self._heads.get(stream, 0)

    def events_after(self, stream, cursor, limit):
        rows = self._read.execute(
            "SELECT * FROM events WHERE chat_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (stream, cursor, limit)).fetchall()
        return [to_envelope(r) for r in rows]

    def poll(self, cursors, wait=25.0, max_events=200, wait_s=None):
        """Boss poll contract. cursors = {name: cursor}. Returns the response dict.

        Wakeup discipline (boss ruling, D3 fix): the head check and the wait happen
        under the SAME condition lock, with a 1 s belt — a missed notify costs at
        most 1 s, which meets the 300 ms P1.3 latency budget for the notify path.
        (wait= is the api_v2 signature; wait_s kept as an alias.)
        """
        if wait_s is not None:
            wait = wait_s
        cursors = {k: int(v) for k, v in cursors.items()}
        deadline_wait = max(0.0, min(float(wait or 0), 25.0))

        def fetch():
            events, heads, more = [], {}, False
            remaining = max_events
            for name in cursors:                       # request order preserved
                head = self.head(name)
                heads[name] = head
                if remaining <= 0:
                    if self.events_after(name, cursors[name], 1):
                        more = True
                    continue
                got = self.events_after(name, cursors[name], remaining + 1)
                if len(got) > remaining:
                    got = got[:remaining]
                    more = True
                remaining -= len(got)
                events.extend(got)
            return events, heads, more

        with self._cond:                               # check-then-wait, same lock
            events, heads, more = fetch()
            if (not events and not more and deadline_wait > 0):
                if self._race_hook is not None:        # deterministic race test hook
                    self._race_hook()
                waited = 0.0
                while waited < deadline_wait and not events and not more:
                    step = min(1.0, deadline_wait - waited)   # 1 s belt
                    self._cond.wait(step)
                    waited += step
                    events, heads, more = fetch()
        return self._respond(cursors, events, heads, more)

    def _respond(self, streams, events, heads, more):
        reset = [name for name, cur in streams.items() if cur > heads.get(name, 0)]
        return {"events": events, "heads": heads, "more": bool(more), "reset": reset}

    # ------------------------------------------------------------ invariants
    def check_invariants(self):
        """Returns dict of check → detail. PASS means every problem list is empty.
        Runs on the query_only read connection (boss precondition)."""
        problems = {}
        ro = self._read

        ic = ro.execute("PRAGMA integrity_check").fetchone()[0]
        problems["integrity_check"] = [] if ic == "ok" else [ic]
        fk = ro.execute("PRAGMA foreign_key_check").fetchall()
        problems["foreign_key_check"] = [tuple(r) for r in fk]

        # no chats row may be the literal office stream (boss ruling)
        bad = [r[0] for r in ro.execute("SELECT id FROM chats WHERE id='office'")]
        problems["office_chat_id"] = bad

        # seq contiguous 1..head per stream, and heads cache agrees
        gaps = []
        for row in ro.execute(
                "SELECT chat_id, MIN(seq) AS lo, MAX(seq) AS hi, COUNT(*) AS n"
                " FROM events GROUP BY chat_id"):
            if row["lo"] != 1 or row["n"] != row["hi"]:
                gaps.append({"stream": row["chat_id"], "min": row["lo"],
                             "max": row["hi"], "count": row["n"]})
            if self._heads.get(row["chat_id"]) != row["hi"]:
                gaps.append({"stream": row["chat_id"], "error": "head cache stale"})
        problems["seq_contiguous"] = gaps

        # messages ↔ message.created (same id), both directions
        orphans = []
        for r in ro.execute(
                "SELECT m.id, m.chat_id FROM messages m WHERE NOT EXISTS"
                " (SELECT 1 FROM events e WHERE e.chat_id=m.chat_id AND e.id=m.id"
                "  AND e.type='message.created')"):
            orphans.append({"message": r["id"], "chat": r["chat_id"]})
        for r in ro.execute(
                "SELECT e.id, e.chat_id FROM events e WHERE e.type='message.created'"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=e.chat_id"
                " AND m.id=e.id)"):
            orphans.append({"event": r["id"], "chat": r["chat_id"]})
        problems["message_links"] = orphans

        # tool_calls.committed_seq ↔ tool.committed event in the same chat stream
        tool_orphans = []
        for r in ro.execute(
                "SELECT tc.id, tc.committed_seq, t.chat_id FROM tool_calls tc"
                " JOIN turns t ON t.id=tc.turn_id"
                " WHERE tc.committed_seq IS NOT NULL AND NOT EXISTS"
                " (SELECT 1 FROM events e WHERE e.chat_id=t.chat_id"
                "  AND e.seq=tc.committed_seq AND e.type='tool.committed')"):
            tool_orphans.append({"tool_call": r["id"], "chat": r["chat_id"],
                                 "committed_seq": r["committed_seq"]})
        for r in ro.execute(
                "SELECT e.chat_id, e.seq, e.id FROM events e"
                " WHERE e.type='tool.committed' AND NOT EXISTS"
                " (SELECT 1 FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id"
                "  WHERE t.chat_id=e.chat_id AND tc.committed_seq=e.seq"
                "  AND tc.tool_use_id=json_extract(e.data_json,'$.tool_use_id'))"):
            tool_orphans.append({"event": r["id"], "chat": r["chat_id"], "seq": r["seq"]})
        problems["tool_links"] = tool_orphans

        # cases.turn_id ↔ turns.case_id agree wherever both are set (boss invariant)
        disagree = [dict(r) for r in ro.execute(
            "SELECT c.id, c.turn_id, t.case_id FROM cases c"
            " JOIN turns t ON t.id=c.turn_id"
            " WHERE c.turn_id IS NOT NULL AND t.case_id IS NOT NULL"
            " AND t.case_id != c.id")]
        problems["case_turn_agreement"] = disagree

        # unknown event types (strict catalog)
        unknown = []
        for r in ro.execute("SELECT DISTINCT type FROM events"):
            if not db.event_type_known(r["type"]):
                unknown.append(r["type"])
        problems["unknown_event_types"] = unknown

        problems["_pass"] = all(not v for k, v in problems.items() if k != "_pass")
        return problems

    def close(self):
        try:
            self._read.close()
        except Exception:
            pass
        try:
            self._proclock.release()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
