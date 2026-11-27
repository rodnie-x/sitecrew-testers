"""Boss P1.3 ruling: the parked-slot hold must die with the client.

A poll client that disconnects mid-park (network drop / chaos abort) used to
hold its parked slot until the full wait expired — a reconnecting client then
saw 503 busy for up to 25 s. The api_v2 park slices + client-liveness fix
must free the slot within ~1 slice.

Runs the REAL blueprint over REAL sockets on an ephemeral port:
  1. client A parks (wait=25, max_parked=1 — the ONLY slot)
  2. client A disconnects 0.5 s into the park
  3. within 3 s, client B must get a 200 (not 503)
Also pins the X-Liveness-Path header (feature-detected: "werkzeug-socket-peek"
on the werkzeug dev server, "none" where no socket is exposed).
"""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
from sitecrew.api_v2 import register_v2            # noqa: E402
from sitecrew.store.events import EventStore       # noqa: E402

REPO = __file__.rsplit("tests", 1)[0]


class SlotFreedOnDisconnect(unittest.TestCase):
    def test_disconnected_client_frees_slot_within_3s(self):
        from flask import Flask
        tmp = tempfile.mkdtemp(prefix="sc_slot_", dir="D:/" if __import__("os").path.exists("D:/") else None)
        store = EventStore(__import__("os").path.join(tmp, "slot.db"))
        app = Flask(__name__)
        register_v2(app, lambda: store, max_parked=1)   # ONE slot only

        port = 18327
        threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                                   debug=False, use_reloader=False),
            daemon=True).start()
        base = "http://127.0.0.1:%d" % port
        deadline = time.time() + 30
        while time.time() < deadline:                   # wait for the server
            try:
                urllib.request.urlopen(base + "/api/v2/poll?streams=s:0", timeout=2)
                break
            except urllib.error.HTTPError:
                break                                   # 200/503 both = server up
            except Exception:
                time.sleep(0.5)

        # client A: park, then disconnect mid-park
        a = socket.create_connection(("127.0.0.1", port), timeout=5)
        a.sendall(b"GET /api/v2/poll?streams=s:0&wait=25 HTTP/1.1\r\n"
                  b"Host: localhost\r\n\r\n")
        time.sleep(0.5)                                 # A holds the only slot, parked
        a.shutdown(socket.SHUT_RDWR)
        a.close()                                       # network drop
        gone_at = time.monotonic()

        # client B: must acquire the freed slot within 3 s (no 503)
        last_err = None
        while time.monotonic() - gone_at < 3.0:
            try:
                with urllib.request.urlopen(
                        base + "/api/v2/poll?streams=s:0&wait=0", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    liveness = r.headers.get("X-Liveness-Path", "missing")
                    self.assertIn(liveness,
                                  ("werkzeug-socket-peek", "none"),
                                  "unexpected liveness path: %r" % liveness)
                    self.assertLess(time.monotonic() - gone_at, 3.0,
                                    "slot took >3 s to free after disconnect")
                    return
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 503:
                    time.sleep(0.2)                     # slot still held: retry
                    continue
                raise
        self.fail("client B never got the slot within 3 s (last error: %s)"
                  % last_err)

    def test_poll_contract_unchanged(self):
        """The slice loop must keep the exact response contract (events/heads/
        more/reset) and deliver promptly on append."""
        from flask import Flask
        import os
        tmp = tempfile.mkdtemp(prefix="sc_slot2_", dir="D:/" if os.path.exists("D:/") else None)
        store = EventStore(os.path.join(tmp, "c.db"))
        app = Flask(__name__)
        register_v2(app, lambda: store)
        port = 18328
        threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                                   debug=False, use_reloader=False),
            daemon=True).start()
        base = "http://127.0.0.1:%d" % port
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base + "/api/v2/poll?streams=s:0", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.5)

        # parked poll in a thread; append after 0.3 s; must return < 2 s
        out = {}

        def park():
            with urllib.request.urlopen(
                    base + "/api/v2/poll?streams=s:0&wait=25", timeout=30) as r:
                out["status"] = r.status
                out["body"] = json.loads(r.read().decode("utf-8"))
                out["liveness"] = r.headers.get("X-Liveness-Path")

        t = threading.Thread(target=park, daemon=True)
        t.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        store.append("s", "message.created", {"i": 1},
                     state_writer=lambda conn, seq: (
                         conn.execute("INSERT OR IGNORE INTO chats(id, title,"
                                      " created_at, updated_at)"
                                      " VALUES('s','t',1,1)"),
                         conn.execute("INSERT INTO messages(id, chat_id, role,"
                                      " kind, content_json, status, created_at)"
                                      " VALUES(?,?,?,?,?,?,1)",
                                      ("m1", "s", "user", "user", "{}", "final")),
                         "m1"),
                     strict=True)
        t.join(timeout=10)
        self.assertLess(time.monotonic() - t0, 2.0,
                        "parked poll did not wake promptly on append")
        self.assertEqual(out["status"], 200)
        body = out["body"]
        self.assertEqual(sorted(body.keys()), ["events", "heads", "more", "reset"])
        self.assertEqual([e["seq"] for e in body["events"]], [1])
        self.assertEqual(body["heads"], {"s": 1})
        self.assertEqual(body["reset"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
