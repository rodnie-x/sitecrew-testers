"""P1.2 kill-harness writer v2 (boss rerun spec, docs/BOSS_PHASE1_ORDER.md).

- READY marker after opening the DB; BEGIN marker before each transaction; ACK after
  commit — all markers via unbuffered os.write (D1 approved on this condition).
- 2 KB+ payloads, interleaved across 3 chats + office (round-robin).
- Fault (after_state|after_event|before_commit|after_commit_before_ack) fires on the
  seeded k-th transaction so injected kills land after real history.
- Graceful STOP_AFTER for the final generation.

Scenarios alternate per stream: even message-count → message scenario (messages row +
message.created, events.id == messages.id), odd → tool scenario (turns + tool_calls
with committed_seq=seq + tool.committed). office stream emits tool.committed only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402

STREAMS = ["c_k1", "c_k2", "c_k3", "office"]


def emit(text):
    os.write(1, (text + "\n").encode("ascii"))   # unbuffered marker channel


def main():
    db_path, acks_path = sys.argv[1], sys.argv[2]
    fault = sys.argv[3] if len(sys.argv) > 3 else ""
    fault_k = int(sys.argv[4]) if len(sys.argv) > 4 else 0   # kill on k-th tx
    stop_after = int(os.environ.get("SITECREW_WRITER_STOP_AFTER", "0") or 0)

    def die():
        ackf.flush()                             # markers are a channel, not durability
        os._exit(9)                              # portable SIGKILL-equivalent (D2)

    conn, _v = sdb.open_db(db_path)
    ackf = open(acks_path, "a", encoding="ascii")
    emit("READY")

    counters = {s: 0 for s in STREAMS}
    # ids must continue across generations: seed txno from the DB's real history
    txno = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    initial_txno = txno
    done = 0
    while True:
        txno += 1
        stream = STREAMS[txno % len(STREAMS)]
        # the fault fires on the k-th transaction OF THIS GENERATION (boss ruling:
        # lands after the real history already in the DB)
        hit = bool(fault) and (txno - initial_txno) == fault_k
        payload = ("P" * 2048) + str(txno)         # ≥ 2 KB payloads (boss floor)

        emit("BEGIN %s" % stream)
        conn.execute("BEGIN IMMEDIATE")
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE chat_id=?",
            (stream,)).fetchone()[0]

        if stream != "office" and counters[stream] % 2 == 0:
            mid = "k_%s_%d" % (stream, txno)
            conn.execute(
                "INSERT OR IGNORE INTO chats(id, title, created_at, updated_at)"
                " VALUES(?,?,1,1)", (stream, stream))
            conn.execute(
                "INSERT INTO messages(id, chat_id, role, kind, content_json, status,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (mid, stream, "user", "user", sdb.dumps({"n": txno, "p": payload}),
                 "final", sdb.utc_ms()))
            etype, data, event_id = "message.created", {"n": txno, "p": payload}, mid
        else:
            _tc, _tu, _t = ("tc_%s_%d" % (stream, txno),
                            "tu_%s_%d" % (stream, txno),
                            "t_%s_%d" % (stream, txno))
            conn.execute(
                "INSERT INTO turns(id, chat_id, state, started_at) VALUES(?,?,?,?)",
                (_t, stream, "running", sdb.utc_ms()))
            conn.execute(
                "INSERT INTO tool_calls(id, turn_id, tool_use_id, name, status,"
                " committed_seq) VALUES(?,?,?,?,?,?)",
                (_tc, _t, _tu, "fs.read", "committed", seq))
            etype = "tool.committed"
            data = {"tool_use_id": _tu, "name": "fs.read", "verified": True,
                    "n": txno, "p": payload}
            event_id = sdb.new_id("e")

        if hit and fault == "after_state":
            die()
        conn.execute(
            "INSERT INTO events(chat_id, seq, id, turn_id, type, data_json, ts,"
            " case_id, task_id, run_id, agent, site) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (stream, seq, event_id, None, etype, sdb.dumps(data), sdb.utc_ms(),
             None, None, None, None, "phone" if stream != "office" else None))
        if hit and fault in ("after_event", "before_commit"):
            die()
        conn.execute("COMMIT")
        if hit and fault == "after_commit_before_ack":
            die()

        counters[stream] += 1
        ackf.write("%s:%d\n" % (stream, seq))
        ackf.flush()
        emit("ACK %s %d" % (stream, seq))
        done += 1
        if stop_after and done >= stop_after:
            ackf.close()
            conn.close()
            return 0


if __name__ == "__main__":
    sys.exit(main())
