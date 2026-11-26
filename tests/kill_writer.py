"""P1.2 kill-harness writer v3 (boss ruling): uses the PRODUCTION EventStore.append
path — state_writer callback, rollback branch, _heads cache, lock order — with the
deterministic failpoint hook inside append for the injected kill modes.

- READY marker after constructing EventStore (unbuffered os.write — D1 condition).
- BEGIN marker before each append; ACK after append returns (commit done).
- 2 KB payloads, interleaved across 3 chats + office.
- Fault mode + seeded k fire the failpoint at the named point inside append.
- os._exit(9) = portable SIGKILL (D2, approved); random kills external Popen.kill().
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb   # noqa: E402
from sitecrew.store.events import EventStore  # noqa: E402

STREAMS = ["c_k1", "c_k2", "c_k3", "office"]


def emit(text):
    os.write(1, (text + "\n").encode("ascii"))


def main():
    db_path, acks_path = sys.argv[1], sys.argv[2]
    fault = sys.argv[3] if len(sys.argv) > 3 else ""
    fault_k = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    stop_after = int(os.environ.get("SITECREU_STOP_AFTER",
                     os.environ.get("SITECREW_WRITER_STOP_AFTER", "0")) or 0)

    def die(stream):
        emit("DIE %s" % stream)               # parent: assert rollback vs commit
        ackf.flush()
        os._exit(9)

    store = EventStore(db_path)
    ackf = open(acks_path, "a", encoding="ascii")
    emit("READY")

    counters = {s: 0 for s in STREAMS}
    txno = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    initial_txno = txno
    done = 0
    while True:
        txno += 1
        stream = STREAMS[txno % len(STREAMS)]
        hit = bool(fault) and (txno - initial_txno) == fault_k
        payload = ("P" * 2048) + str(txno)

        emit("BEGIN %s" % stream)

        cycle = counters[stream]
        if stream != "office" and cycle % 2 == 0:
            mid = "k_%s_%d" % (stream, txno)
            etype = "message.created"
            data = {"n": txno, "p": payload}

            def state_writer(conn, seq, _mid=mid, _s=stream, _p=payload):
                conn.execute(
                    "INSERT OR IGNORE INTO chats(id, title, created_at, updated_at)"
                    " VALUES(?,?,1,1)", (_s, _s))
                conn.execute(
                    "INSERT INTO messages(id, chat_id, role, kind, content_json,"
                    " status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (_mid, _s, "user", "user", sdb.dumps({"p": _p}), "final",
                     sdb.utc_ms()))
                return _mid                       # events.id == messages.id
        else:
            _tu = "tu_%s_%d" % (stream, txno)
            etype = "tool.committed"
            data = {"tool_use_id": _tu, "name": "fs.read", "verified": True,
                    "n": txno, "p": payload}

            def state_writer(conn, seq, _s=stream, _p=payload):
                _t = "t_%s_%d" % (_s, txno)
                _tc = "tc_%s_%d" % (_s, txno)
                conn.execute(
                    "INSERT INTO turns(id, chat_id, state, started_at) VALUES(?,?,?,?)",
                    (_t, _s, "running", sdb.utc_ms()))
                conn.execute(
                    "INSERT INTO tool_calls(id, turn_id, tool_use_id, name, status,"
                    " committed_seq) VALUES(?,?,?,?,?,?)",
                    (_tc, _t, _tu, "fs.read", "committed", seq))

        failpoint = None
        if hit:
            def failpoint(point, _fault=fault, _stream=stream):
                if point == _fault:
                    die(_stream)

        env = store.append(stream, etype, data, state_writer=state_writer,
                           failpoint=failpoint,
                           site="phone" if stream != "office" else None,
                           strict=True)

        counters[stream] += 1
        ackf.write("%s:%d\n" % (stream, env["seq"]))
        ackf.flush()
        emit("ACK %s %d" % (stream, env["seq"]))
        done += 1
        if stop_after and done >= stop_after:
            ackf.close()
            store.close()
            return 0


if __name__ == "__main__":
    sys.exit(main())
