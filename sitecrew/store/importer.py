"""One-time legacy -> store import (P1.1). SKELETON: the machinery below is complete and tested against
a stub store (idempotency, kill-safe atomic units, markers, verbatim text, events, verification);
only the three `read_*` functions marked ADAPT map YOUR legacy shapes into the normalized chat dict.

Normalized chat dict (what every reader must produce):
    {"id": "<legacy chat id>", "title": str|None, "pinned": 0|1, "model": str|None,
     "created_at": ms|None, "updated_at": ms|None,
     "messages": [{"role": "user"|"assistant"|"system", "text": str, "ts": ms|None}, ...],   # in order
     "queued":   [{"text": str, "ts": ms|None}, ...]}                                          # unsent, in order

Design (amends my earlier "one transaction per source"; the chat is the atomic unit):
- one EventStore.append_batch per chat: chat row + messages + queue rows + the done-marker +
  message.created events and `system.import.chat` commit in ONE transaction, so a kill leaves a chat entirely imported or
  not at all; a rerun skips chats whose marker matches and redoes the rest (ids are deterministic and
  every insert is OR IGNORE, so redoing is harmless).
- one final append to the `office` stream writes the source-level marker + `system.import.completed`.
- the cross-source driver order is fixed queue_json -> ram -> recents; merged transcript chains
  intentionally follow that invocation order.
- message text is stored VERBATIM (never parsed and re-serialized); the byte-compare is on `text`.
- ids derive from (source, chat index, message index), never from content: duplicate messages stay
  separate rows. client_msg_id stays NULL (an empty string would collide under UNIQUE(chat_id, client_msg_id)).
- backups are copies (`<file>.pre-v2.bak`), never overwritten, never moved: flag off = full rollback.
"""
import hashlib
import json
import os
import shutil

from . import db as sdb


def _sha(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def imp_id(kind, *parts):
    return "imp-%s-%s" % (kind, _sha(*parts)[:24])


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def backup(path):
    """Copy path -> path.pre-v2.bak once. Returns (bak_path, sha256_of_original)."""
    bak = path + ".pre-v2.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak, file_sha256(bak)


def meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))


def chat_id_for(conn, raw, persist=True):
    """Use the store's one deterministic mapping for queue, RAM and posted recents."""
    return sdb.remap_legacy_chat_id(raw, conn, persist=persist)


# ---- ADAPT: map YOUR legacy shapes into normalized chat dicts ---------------------------------------
def read_queue_json(path):
    """logs/queue.json -> [normalized chat]. ADAPT."""
    raise NotImplementedError("map logs/queue.json to the normalized chat dict")


def read_ram_dump(path):
    """The read-only dump of the in-RAM transcript (taken BEFORE the APK install). ADAPT."""
    raise NotImplementedError("map the transcript dump to the normalized chat dict")


def read_recents(payload):
    """The localStorage recents the page POSTed once (a parsed JSON value). ADAPT."""
    raise NotImplementedError("map the recents payload to the normalized chat dict")
# -----------------------------------------------------------------------------------------------------


def _digest(chat):
    return _sha("chat", json.dumps(chat, sort_keys=True, ensure_ascii=True))


def chat_texts(chat):
    return [m["text"] for m in chat.get("messages", [])]


def import_chat(store, source, ci, chat, now_ms=None):
    """One atomic unit. Returns 'imported' | 'skipped'. Raises if the source changed under a marker."""
    conn = store.conn
    cid = chat_id_for(conn, chat["id"], persist=False)
    marker = "import:%s:chat:%d" % (source, ci)
    dig = _digest(chat)
    done = meta_get(conn, marker)
    if done is not None:
        if done != dig:
            raise RuntimeError("source changed since import: %s (refusing to guess)" % marker)
        return "skipped"
    now = now_ms if now_ms is not None else sdb.utc_ms()
    msgs, queued = chat.get("messages", []), chat.get("queued", [])
    items = [
        {"type": "message.created", "id": imp_id("m", source, ci, mi),
         "data": {"text": m["text"], "role": m["role"], "kind": m["role"],
                  "parent_id": imp_id("m", source, ci, mi - 1) if mi else None,
                  "origin": "import", "source": source},
         "ts": m.get("ts") or now}
        for mi, m in enumerate(msgs)
    ]
    items.extend(
        {"type": "message.created", "id": imp_id("qm", source, ci, qi),
         "data": {"text": qm["text"], "role": "user", "kind": "user", "parent_id": None,
                  "origin": "import", "source": source, "queued": True},
         "ts": qm.get("ts") or now}
        for qi, qm in enumerate(queued))
    items.append({"type": "system.import.chat", "data": {
        "origin": "import", "source": source, "chat_index": ci,
        "message_count": len(msgs), "queue_count": len(queued),
        "chat_digest": dig}, "ts": now})

    def writer(c, seq):
        chat_id_for(c, chat["id"])             # mapping commits with chat state + event
        existing = c.execute(
            "SELECT title,pinned,model,created_at,updated_at,head_message_id"
            " FROM chats WHERE id=?", (cid,)).fetchone()
        incoming_created = chat.get("created_at") if chat.get("created_at") is not None else now
        incoming_updated = chat.get("updated_at") if chat.get("updated_at") is not None else now
        if existing is None:
            c.execute(
                "INSERT INTO chats(id,title,pinned,model,mem_enabled,created_at,updated_at)"
                " VALUES(?,?,?,?,1,?,?)",
                (cid, chat.get("title"), int(bool(chat.get("pinned"))), chat.get("model"),
                 incoming_created, incoming_updated))
            prev = None
        else:
            created = min(value for value in (existing["created_at"], incoming_created)
                          if value is not None)
            updated = max(value for value in (existing["updated_at"], incoming_updated)
                          if value is not None)
            c.execute(
                "UPDATE chats SET title=?,pinned=?,model=?,created_at=?,updated_at=?"
                " WHERE id=?",
                (existing["title"] if existing["title"] is not None else chat.get("title"),
                 max(int(existing["pinned"] or 0), int(bool(chat.get("pinned")))),
                 existing["model"] if existing["model"] is not None else chat.get("model"),
                 created, updated, cid))
            prev = existing["head_message_id"]
        if msgs:
            # append_batch serializes data after state_writer: bind the first replay
            # parent to the head read in THIS transaction, including a live chat.
            items[0]["data"]["parent_id"] = prev
        for mi, m in enumerate(msgs):
            mid = imp_id("m", source, ci, mi)
            content = {"text": m["text"], "origin": "import", "source": source}
            c.execute(
                "INSERT OR IGNORE INTO messages(id,chat_id,parent_id,role,kind,content_json,status,"
                "client_msg_id,turn_id,created_at) VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
                (mid, cid, prev, m["role"], m["role"], sdb.dumps(content), "final",
                 m.get("ts") or now))
            prev = mid
        if msgs:
            c.execute("UPDATE chats SET head_message_id=? WHERE id=?", (prev, cid))
        next_position = c.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM queue WHERE chat_id=?",
            (cid,)).fetchone()[0]
        for qi, qm in enumerate(queued):
            mid = imp_id("qm", source, ci, qi)
            content = {"text": qm["text"], "origin": "import", "source": source}
            c.execute(
                "INSERT OR IGNORE INTO messages(id,chat_id,parent_id,role,kind,content_json,status,"
                "client_msg_id,turn_id,created_at) VALUES(?,?,NULL,'user','user',?,'final',NULL,NULL,?)",
                (mid, cid, sdb.dumps(content), qm.get("ts") or now))
            c.execute(
                "INSERT OR IGNORE INTO queue(id,chat_id,message_id,state,position) VALUES(?,?,?,?,?)",
                (imp_id("q", source, ci, qi), cid, mid, "pending", next_position + qi))
        meta_set(c, marker, dig)                 # marker commits WITH the data
        return None

    store.append_batch(cid, items, state_writer=writer, strict=True)
    return "imported"


def run_import(store, source, chats, source_sha256=None):
    """Import one source (list of normalized chats). Safe to rerun after a kill. Returns a report dict."""
    done_marker = "import:%s:done" % source
    rep = {"source": source, "chats": len(chats), "imported": 0, "skipped": 0,
           "messages": sum(len(c.get("messages", [])) for c in chats),
           "queued": sum(len(c.get("queued", [])) for c in chats),
           "source_sha256": source_sha256}
    for ci, chat in enumerate(chats):
        rep["imported" if import_chat(store, source, ci, chat) == "imported" else "skipped"] += 1
    if meta_get(store.conn, done_marker) is None:
        def writer(c, seq):
            meta_set(c, done_marker, source_sha256 or "n/a")
            return None
        store.append("office", "system.import.completed",
                     {"source": source, "chats": rep["chats"], "messages": rep["messages"],
                      "queued": rep["queued"], "source_sha256": source_sha256},
                     state_writer=writer, strict=True)
    return rep


def verify_import(conn, source, chats):
    """P1.1 evidence: counts in == counts out and byte-identical texts, per chat. Read-only."""
    out = {"source": source, "chats_in": len(chats), "chats_out": 0, "messages_in": 0,
           "messages_out": 0, "queue_in": 0, "queue_out": 0, "mismatches": [], "digests": {}}
    for ci, chat in enumerate(chats):
        cid = chat_id_for(conn, chat["id"], persist=False)
        if conn.execute("SELECT 1 FROM chats WHERE id=?", (cid,)).fetchone():
            out["chats_out"] += 1
        want = chat_texts(chat)
        got = []
        for mi in range(len(want)):
            row = conn.execute("SELECT content_json FROM messages WHERE id=?",
                               (imp_id("m", source, ci, mi),)).fetchone()
            got.append(json.loads(row[0])["text"] if row else None)
        out["messages_in"] += len(want)
        out["messages_out"] += sum(1 for g in got if g is not None)
        if got != want:
            out["mismatches"].append({"chat": chat["id"], "index": ci,
                                      "first_diff": next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), None)})
        h = hashlib.sha256()
        for t in got:
            b = (t or "").encode("utf-8")
            h.update(len(b).to_bytes(8, "big") + b)
        out["digests"][chat["id"]] = h.hexdigest()
        qin = chat.get("queued", [])
        out["queue_in"] += len(qin)
        # exact ids are deterministic, so look each queued message up by id
        qgot = []
        for qi in range(len(qin)):
            r = conn.execute("SELECT m.content_json FROM queue q JOIN messages m ON m.id=q.message_id WHERE q.id=?",
                             (imp_id("q", source, ci, qi),)).fetchone()
            qgot.append(json.loads(r[0])["text"] if r else None)
        out["queue_out"] += sum(1 for g in qgot if g is not None)
        if qgot != [x["text"] for x in qin]:
            out["mismatches"].append({"chat": chat["id"], "index": ci, "queue": True})
    out["ok"] = (out["chats_in"] == out["chats_out"] and out["messages_in"] == out["messages_out"]
                 and out["queue_in"] == out["queue_out"] and not out["mismatches"])
    return out
