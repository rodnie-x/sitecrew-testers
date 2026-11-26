"""P1.2 kill-harness writer v4 (hang/accuracy fix). Same PRODUCTION EventStore.append path as v3.

What changed vs v3
- stdout carries exactly ONE line (READY). v3 also wrote BEGIN/ACK lines to stdout: once the parent
  stops reading after READY, the pipe fills and the writer blocks mid-run, which skews commits per
  cycle and the in-flight statistics.
- BEGIN/ACK markers go to the marks file through unbuffered O_APPEND os.write, so a kill
  (os._exit or Popen.kill) can neither lose nor tear a marker line.
      "B <stream> <txno>\n"   written before store.append
      "A <stream> <seq>\n"    written after store.append returned (commit done)
- If EventStore() construction stalls (lock, WAL recovery) the child dumps its own stack to stderr
  after 25 s, so a "never READY" generation explains itself.
- Fault mode + seeded k fire the failpoint inside append (after_state / after_event /
  before_commit / after_commit); os._exit(9) = portable self-kill (D2, approved).

argv: db_path marks_path [fault [fault_k]]
"""
import faulthandler
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb   # noqa: E402
from sitecrew.store.events import EventStore  # noqa: E402

STREAMS = ["c_k1", "c_k2", "c_k3", "office"]
_FD = -1


def mark(line):
    os.write(_FD, line.encode("ascii"))


def die():
    os._exit(9)


def main():
    global _FD
    db_path, marks_path = sys.argv[1], sys.argv[2]
    fault = sys.argv[3] if len(sys.argv) > 3 else ""
    fault_k = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    stop_after = int(os.environ.get("SITECREU_STOP_AFTER",
                     os.environ.get("SITECREW_WRITER_STOP_AFTER", "0")) or 0)

    faulthandler.dump_traceback_later(25, exit=False)      # stderr; cancelled once READY
    store = EventStore(db_path)
    _FD = os.open(marks_path,
                  os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0))
    faulthandler.cancel_dump_traceback_later()
    os.write(1, b"READY\n")

    counters = {s: 0 for s in STREAMS}
    txno = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    initial_txno = txno
    done = 0
    while True:
        txno += 1
        stream = STREAMS[txno % len(STREAMS)]
        hit = bool(fault) and (txno - initial_txno) == fault_k
        payload = ("P" * 2048) + str(txno)

        mark("B %s %d\n" % (stream, txno))

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
            _tx = txno
            etype = "tool.committed"
            data = {"tool_use_id": _tu, "name": "fs.read", "verified": True,
                    "n": txno, "p": payload}

            def state_writer(conn, seq, _s=stream, _n=_tx, _tuid=_tu):
                _t = "t_%s_%d" % (_s, _n)
                _tc = "tc_%s_%d" % (_s, _n)
                conn.execute(
                    "INSERT INTO turns(id, chat_id, state, started_at) VALUES(?,?,?,?)",
                    (_t, _s, "running", sdb.utc_ms()))
                conn.execute(
                    "INSERT INTO tool_calls(id, turn_id, tool_use_id, name, status,"
                    " committed_seq) VALUES(?,?,?,?,?,?)",
                    (_tc, _t, _tuid, "fs.read", "committed", seq))

        failpoint = None
        if hit:
            def failpoint(point, _fault=fault):
                if point == _fault:
                    die()

        env = store.append(stream, etype, data, state_writer=state_writer,
                           failpoint=failpoint,
                           site="phone" if stream != "office" else None,
                           strict=True)

        counters[stream] += 1
        mark("A %s %d\n" % (stream, env["seq"]))
        done += 1
        if stop_after and done >= stop_after:
            os.close(_FD)
            store.close()
            return 0


if __name__ == "__main__":
    sys.exit(main())
