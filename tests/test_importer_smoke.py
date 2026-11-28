import os, sys, sqlite3, json, tempfile, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store.events import EventStore
from sitecrew.store import importer as imp

def mk_chats():
    rng = random.Random(5)
    nasty = ["", "same", "same", "  trailing  ", "line1\r\nline2\n", "emoji \U0001F600 \U0001D11E", "rtl \u05e9\u05dc\u05d5\u05dd", "x" * 1_000_000, "\u2028sep", 'quote " and \\ slash']
    chats = []
    for i in range(12):
        msgs = [{"role": rng.choice(["user", "assistant"]), "text": rng.choice(nasty), "ts": 1000 + j} for j in range(rng.randint(1, 9))]
        chats.append({"id": ("legacy chat/%d" % i) if i % 4 == 0 else "c_%d" % i, "title": "t%d" % i, "pinned": i % 2,
                      "messages": msgs, "queued": [{"text": "q%d_%d" % (i, k)} for k in range(i % 3)]})
    return chats

d = tempfile.mkdtemp(); path = os.path.join(d, "t.db")
chats = mk_chats()
st = EventStore(path)
r1 = imp.run_import(st, "queue_json", chats, "abc")
v = imp.verify_import(st.conn, "queue_json", chats)
assert v["ok"], v
assert r1["imported"] == 12 and r1["skipped"] == 0, r1
n_events = st.conn.execute("select count(*) from events").fetchone()[0]
expected_events = (12 + 1 + sum(len(c["messages"]) + len(c["queued"]) for c in chats))
assert n_events == expected_events, n_events             # every message + per-chat/source markers
assert st.check_invariants()["_pass"], st.check_invariants()
# rerun is a no-op
r2 = imp.run_import(st, "queue_json", chats, "abc")
assert r2["imported"] == 0 and r2["skipped"] == 12, r2
assert st.conn.execute("select count(*) from events").fetchone()[0] == n_events
assert st.conn.execute("select count(*) from messages").fetchone()[0] == v["messages_out"] + sum(len(c["queued"]) for c in chats)
# duplicate 'same' messages stayed separate rows (position-based ids)
# kill-safety: import only half, then a "fresh process" finishes
d2 = tempfile.mkdtemp(); p2 = os.path.join(d2, "t.db"); st2 = EventStore(p2)
for ci, c in enumerate(chats[:5]): imp.import_chat(st2, "queue_json", ci, c)
st2.close(); st3 = EventStore(p2)
r3 = imp.run_import(st3, "queue_json", chats, "abc")
assert r3["skipped"] == 5 and r3["imported"] == 7, r3
assert imp.verify_import(st3.conn, "queue_json", chats)["ok"]
# a source that changed under a marker is refused
chats[0]["messages"][0]["text"] += "!"
try: imp.run_import(st3, "queue_json", chats, "abc"); raise SystemExit("should have refused")
except RuntimeError as e: assert "source changed" in str(e)
# a mismatch is detected by verify
st3.conn.execute("UPDATE messages SET content_json=? WHERE id=?", (json.dumps({"text": "TAMPERED"}), imp.imp_id("m", "queue_json", 1, 0)))
chats[0]["messages"][0]["text"] = chats[0]["messages"][0]["text"][:-1]
assert not imp.verify_import(st3.conn, "queue_json", chats)["ok"]
print("importer smoke ok:", {k: v[k] for k in ("chats_in","chats_out","messages_in","messages_out","queue_in","queue_out")})
