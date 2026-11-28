"""P1.5 legacy mirror adapter — snapshot in, minimal diff events out.

Isolated store adapter for the ONE legacy mutation boundary (docs/
P15_DESIGN_RULING.md). Nothing imports this yet: production wiring and the
flip are explicitly out of scope for this increment, and the legacy routes
remain the source of truth while mirroring.

Boss rulings implemented here:
- stable live chat id: allocated once with new_id('c'), persisted in meta
  ('legacy:live_chat_id') and reused forever — reruns are deterministic;
- canonical legacy snapshot: every public AND internal field of the legacy
  message dicts (content, model_content, attachments, job, elapsed_s,
  workers, tools_used, ...) is preserved verbatim; the snapshot hash is
  canonical JSON (sorted keys) through the store's ONE dumps();
- snapshot in -> diff events out: each mutation diffs the canonical snapshot
  against the active projection keyed by per-row content hash and emits ONLY
  the difference through the frozen EventStore API (append_batch, ONE batch
  per mutation). No blind replace, no scattered writes, no DELETE anywhere;
- deterministic mutation idempotency: key = sha256("<mutation seq>|<snapshot
  hash>"); applied keys are recorded in meta and any re-delivery is a no-op;
- boot reconcile: compares the stored snapshot hash with the live legacy hash
  and replays a legacy-write/mirror crash gap through the same diff;
- no physical history deletion: removals are message.tombstoned (undo and
  generic kinds) or a watermark batch (clear.applied / compression.applied /
  restore.applied carry the covered ids); message.restored compensates;
  content is always retained;
- tools_used uses message.annotated; the original message.created event is
  never mutated;
- active projection: a versioned function of the events (rebuild_projection);
  the materialized rows mirror it exactly;
- queue FIFO: each queue entry gets its own message row (status 'queued') +
  queue row; positions are allocated contiguously (MAX+1, never reused) and
  flushes mark state='sent' in FIFO order — never a physical delete;
- mirror errors never break the legacy response: mirror() catches, counts
  and returns them; any nonzero count fails parity.

Diff strategies (deterministic, per mutation kind):
- "clear" / "restore" / "compression" replace the whole transcript: hash-set
  occurrence matching (i-th occurrence to i-th occurrence, no content dedupe);
  unmatched active rows are covered by the kind's watermark event, unmatched
  snapshot rows are restored from an inactive twin (restore only) or created;
  the watermark also records "order" — the resulting transcript order —
  because a replacement may reorder rows (the compression summary is CREATED
  after the rows it precedes), and the active projection replays it;
- every other kind ("exchange", "enqueue", "dequeue", "undo", "annotate",
  "reconcile", ...) mutates the tail: longest common prefix, then the first
  tail pair is an in-place annotation iff same role and the old content is a
  value-equal sub-dict of the new (the tools_used pattern); remaining tail
  rows are created (snapshot side) or tombstoned (projection side).

Known limitations (declared, not hidden):
- a legacy transcript REORDER is not a legacy operation; the tail strategy
  mirrors it as remove+create (still zero content loss, parity restored);
- the in-place annotation pair is detected only when the old row content is
  a value-equal sub-dict of the new (the tools_used-addition pattern); any
  other tail change mirrors as tombstone+create — history stays complete and
  parity holds, but such a change is not recorded as message.annotated;
- mirror bookkeeping (mut_seq, snapshot hash, applied keys) grows one meta
  row per mutation;
- legacy messages carry no timestamps, so created_at is the mirror commit
  time; annotations keep the original created ts;
- concurrent mirror callers are serialized per adapter; a cross-process
  double-apply is stopped by the events.id UNIQUE index (the loser's batch
  rolls back and is counted as a mirror error, legacy response untouched);
- tombstones retain content: privacy erasure is an OPEN OWNER DECISION and
  out of scope here (docs/P15_DESIGN_RULING.md).
"""
import hashlib
import json
import logging
import threading

from . import db

log = logging.getLogger("sitecrew.store.legacy")

META_LIVE_CHAT = "legacy:live_chat_id"
META_MUT_SEQ = "legacy:mut_seq"
META_SNAPSHOT_HASH = "legacy:snapshot_hash"
META_LAST_KEY = "legacy:last_idem_key"
META_KEY_PREFIX = "legacy:key:"

ACTIVE = "final"             # messages.status: in the active projection
TOMBSTONED = "tombstoned"    # removed (undo / clear / restore watermark)
COMPRESSED = "compressed"    # hidden by compression.applied, content retained
QUEUED = "queued"            # queue-entry message row (never in the transcript)

# kinds that replace the whole transcript -> one watermark event covers removals
REPLACE_KINDS = ("clear", "restore", "compression")
WATERMARK_TYPE = {"clear": "clear.applied", "restore": "restore.applied",
                  "compression": "compression.applied"}


# ------------------------------------------------------------- canonical form

def _sorted(obj):
    """Recursively sort dict keys so one canonical string exists per value."""
    if isinstance(obj, dict):
        return {k: _sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_sorted(x) for x in obj]
    return obj


def canonical_dumps(obj):
    """Canonical JSON for hashing/comparison — still the store's ONE dumps()."""
    return db.dumps(_sorted(obj))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_message(m):
    """The canonical row content of one legacy transcript message: EVERY field,
    public and internal, verbatim (boss ruling: preserve public and internal
    fields; compare canonical JSON at every mutation)."""
    if not isinstance(m, dict):
        raise ValueError("legacy message must be a dict, got %r" % type(m).__name__)
    return dict(m)


def canonical_entry(e):
    """The canonical content of one legacy queue entry (message + file_context)."""
    if not isinstance(e, dict):
        raise ValueError("legacy queue entry must be a dict, got %r" % type(e).__name__)
    return {"message": str(e.get("message", "") or ""),
            "file_context": str(e.get("file_context") or "")}


def canonical_snapshot(messages, queue):
    """The canonical legacy snapshot: ordered transcript rows + FIFO queue."""
    return {"messages": [canonical_message(m) for m in (messages or [])],
            "queue": [canonical_entry(e) for e in (queue or [])]}


def snapshot_hash(snap):
    return _sha(canonical_dumps(snap))


def content_hash(content):
    return _sha(canonical_dumps(content))


def idempotency_key(mut_seq, shash):
    """Boss ruling: idempotency key derived from mutation seq and snapshot hash."""
    return _sha("%d|%s" % (int(mut_seq), shash))


def _subdict(old, new):
    """True when every old key/value is present-equal in new (annotation shape)."""
    try:
        return all(k in new and canonical_dumps(old[k]) == canonical_dumps(new[k])
                   for k in old)
    except TypeError:
        return False


# ------------------------------------------------------------- projection I/O

def _row(content, status, ts, row_id):
    return {"id": row_id, "content": content, "status": status, "ts": ts,
            "hash": content_hash(content)}


def _projection_rows(ro, chat_id):
    """All message rows of the stream that have a message.created event,
    in creation (seq) order, with their content hash."""
    rows = ro.execute(
        "SELECT m.id AS id, m.content_json AS cj, m.status AS status,"
        " m.created_at AS ts FROM messages m"
        " JOIN events e ON e.chat_id=m.chat_id AND e.id=m.id"
        " AND e.type='message.created' WHERE m.chat_id=? ORDER BY e.seq",
        (chat_id,)).fetchall()
    return [_row(json.loads(r["cj"] or "{}"), r["status"], r["ts"], r["id"])
            for r in rows]


def _projection_queue(ro, chat_id):
    """Pending queue rows in FIFO (position) order, entry content from the
    entry's own message row (status 'queued' — never in the transcript)."""
    rows = ro.execute(
        "SELECT q.id AS queue_id, q.message_id AS message_id, q.position AS position,"
        " m.content_json AS cj FROM queue q LEFT JOIN messages m ON m.id=q.message_id"
        " WHERE q.chat_id=? AND q.state='pending' ORDER BY q.position",
        (chat_id,)).fetchall()
    out = []
    for r in rows:
        c = json.loads(r["cj"]) if r["cj"] else {}
        out.append({"queue_id": r["queue_id"], "message_id": r["message_id"],
                    "position": r["position"], "message": c.get("message", ""),
                    "file_context": c.get("file_context", "")})
    return out


def transcript_order(ro, chat_id, rows):
    """Transcript order of the stream's rows. Creation (seq) order is the
    default; a whole-transcript replacement may REORDER rows (a compression
    summary is created after the rows it precedes), so each watermark event
    carries the resulting order and the newest one wins."""
    by_id = {r["id"]: r for r in rows}
    order = [r["id"] for r in rows]
    markers = ("clear.applied", "compression.applied", "restore.applied")
    for r in ro.execute(
            "SELECT data_json FROM events WHERE chat_id=? AND type IN (?,?,?)"
            " ORDER BY seq", (chat_id,) + markers).fetchall():
        d = json.loads(r["data_json"] or "{}")
        if isinstance(d.get("order"), list):
            order = [i for i in d["order"] if i in by_id]
    known = set(order)
    order.extend(r["id"] for r in rows if r["id"] not in known)
    return order


def rebuild_projection(envelopes):
    """The active projection as a pure function of events (boss ruling).

    Pass ONE stream's envelopes in ascending seq (events_after already sorts).
    Returns {"messages": [active rows], "queue": [pending rows]} — the exact
    shape MirrorAdapter.projection() materializes into the state tables.
    """
    msgs = {}                                    # id -> row dict (creation order)
    order = []                                   # transcript order (see watermark)
    q = {}                                       # queue_id -> entry dict
    for env in sorted(envelopes, key=lambda e: e["seq"]):
        t = env["type"]
        d = env.get("data") or {}
        if t == "message.created":
            content = d.get("content") or {}
            status = QUEUED if content.get("queued") else ACTIVE
            msgs[env["id"]] = {"id": env["id"], "content": content,
                               "status": status, "ts": env["ts"]}
            if env["id"] not in order:
                order.append(env["id"])
        elif t in ("clear.applied", "compression.applied", "restore.applied"):
            if isinstance(d.get("order"), list):
                order = list(d["order"])          # whole-transcript reorder
            if t == "compression.applied":
                for mid in d.get("covers") or []:
                    row = msgs.get(mid)
                    if row is not None:
                        row["status"] = COMPRESSED    # hidden, content kept
            else:
                for mid in d.get("covered") or []:
                    row = msgs.get(mid)
                    if row is not None:
                        row["status"] = TOMBSTONED
        elif t == "message.annotated":
            row = msgs.get(d.get("message_id"))
            if row is not None:
                content = dict(row["content"])
                content.update(d.get("fields") or {})
                for k in d.get("removed") or []:
                    content.pop(k, None)
                row["content"] = content          # original created event untouched
        elif t == "message.restored":
            row = msgs.get(d.get("message_id"))
            if row is not None:
                row["status"] = ACTIVE            # compensates a tombstone
        elif t == "message.tombstoned":
            row = msgs.get(d.get("message_id"))
            if row is not None:
                row["status"] = TOMBSTONED
        elif t == "message.queued":
            entry = d.get("entry") or {}
            q[d.get("queue_id")] = {"queue_id": d.get("queue_id"),
                                    "message_id": d.get("message_id"),
                                    "position": d.get("position"),
                                    "message": entry.get("message", ""),
                                    "file_context": entry.get("file_context", "")}
        elif t == "message.dequeued":
            q.pop(d.get("queue_id"), None)
    order.extend(i for i in msgs if i not in order)   # safety: never drop a row
    active_ids = {i for i, m in msgs.items() if m["status"] == ACTIVE}
    return {"messages": [msgs[i] for i in order if i in active_ids],
            "queue": sorted(q.values(), key=lambda r: r["position"])}


# ---------------------------------------------------------------- the adapter

class MirrorAdapter:
    """One adapter per EventStore, owned by the ONE legacy mutation boundary.

    apply() emits at most ONE append_batch per legacy mutation, through the
    frozen EventStore API only. mirror() is the never-raise wrapper the
    boundary calls after each legacy write.
    """

    def __init__(self, store):
        self.store = store
        self.mirror_errors = 0
        self._lock = threading.Lock()            # serializes mirror calls
        self._cid = None

    # ---- stable live chat id (persisted in meta, allocated exactly once)
    def _resolve_chat_id(self):
        if self._cid:
            return self._cid
        row = self.store._read.execute(
            "SELECT value FROM meta WHERE key=?", (META_LIVE_CHAT,)).fetchone()
        if row:
            self._cid = row[0]
            return self._cid
        cid = db.new_id("c")
        with self.store._writer:                 # same lock the batches use
            conn = self.store.conn
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?,?)",
                         (META_LIVE_CHAT, cid))
            cid = conn.execute("SELECT value FROM meta WHERE key=?",
                               (META_LIVE_CHAT,)).fetchone()[0]
            conn.commit()
        self._cid = cid
        return cid

    @property
    def live_chat_id(self):
        return self._resolve_chat_id()

    def _meta_get(self, key):
        row = self.store._read.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ---- public API
    def mirror(self, messages, queue, kind="generic", **kw):
        """Never-raise wrapper for the mutation boundary (boss ruling: mirror
        errors are caught, logged, counted — they never break the response)."""
        try:
            return self.apply(messages, queue, kind=kind, **kw)
        except Exception as e:                   # noqa: BLE001 — containment IS the job
            self.mirror_errors += 1
            log.error("legacy mirror failed (kind=%s): %s", kind, e)
            return {"ok": False, "error": str(e)[:200], "kind": kind,
                    "mirror_errors": self.mirror_errors}

    def apply(self, messages, queue, kind="generic", mutation_seq=None,
              reason=None, ts=None, failpoint=None):
        """Diff one legacy snapshot against the projection; emit the difference.

        mutation_seq: the caller's mutation ordinal (required to be strictly
        increasing per legacy mutation); default = stored + 1. failpoint:
        test/kill hook called with "after_state" inside the batch transaction
        (same pattern as db.migrate / EventStore.append).
        """
        with self._lock:
            snap = canonical_snapshot(messages, queue)
            shash = snapshot_hash(snap)
            stored = self._meta_get(META_MUT_SEQ)
            mut = int(mutation_seq) if mutation_seq is not None else int(stored or 0) + 1
            key = idempotency_key(mut, shash)
            if self._meta_get(META_KEY_PREFIX + key) is not None:
                # re-delivery of an already-committed mutation: record says done
                return self._result(snap, mut, key, duplicate=True,
                                    note="idempotent re-delivery")
            cid = self.live_chat_id
            ro = self.store._read
            rows = _projection_rows(ro, cid)
            by_id = {r["id"]: r for r in rows}
            active = [by_id[i] for i in transcript_order(ro, cid, rows)
                      if by_id[i]["status"] == ACTIVE]
            pending = _projection_queue(ro, cid)
            next_pos = ro.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM queue WHERE chat_id=?",
                (cid,)).fetchone()[0]
            plan = self._plan(snap, rows, active, pending, next_pos,
                              kind=kind, reason=reason, mut=mut, key=key,
                              shash=shash, ts=ts, failpoint=failpoint)
            if not plan["items"]:
                # projection already equals the snapshot: nothing to emit, no
                # batch, no bookkeeping change (reruns never duplicate rows)
                return self._result(snap, mut, key, noop=True,
                                    note="projection equals snapshot")
            envelopes = self.store.append_batch(
                cid, plan["items"], state_writer=plan["writer"], strict=True)
            counts = plan["counts"]
            counts["event_count"] = len(envelopes)
            result = self._result(snap, mut, key, applied=True,
                                  events=envelopes, counts=counts)
            if not result["parity"]:             # committed but diverged: fails parity
                self.mirror_errors += 1
                result["mirror_errors"] = self.mirror_errors
                log.error("legacy mirror parity divergence after commit (kind=%s)",
                          kind)
            return result

    def reconcile(self, messages, queue, kind="reconcile", mutation_seq=None,
                  reason=None, ts=None):
        """Boot reconcile (boss ruling: compare legacy/projection hashes and
        repair any crash gap). A hash match is a no-op; a mismatch replays the
        gap through the same deterministic diff."""
        snap = canonical_snapshot(messages, queue)
        shash = snapshot_hash(snap)
        if self._meta_get(META_SNAPSHOT_HASH) == shash:
            return self._result(snap, note="boot hash match — nothing to reconcile",
                                 noop=True)
        return self.apply(messages, queue, kind=kind, mutation_seq=mutation_seq,
                          reason=reason or "reconcile", ts=ts)

    def projection(self):
        """The active projection from the materialized state tables (read-only
        connection): active transcript rows in transcript order + pending
        queue in FIFO order. Equal to rebuild_projection(events)."""
        cid = self.live_chat_id
        ro = self.store._read
        rows = _projection_rows(ro, cid)
        by_id = {r["id"]: r for r in rows}
        messages = [{"id": by_id[i]["id"], "content": by_id[i]["content"],
                     "status": by_id[i]["status"], "ts": by_id[i]["ts"]}
                    for i in transcript_order(ro, cid, rows)
                    if by_id[i]["status"] == ACTIVE]
        return {"chat_id": cid, "messages": messages,
                "queue": _projection_queue(ro, cid)}

    def _parity(self, snap):
        """Canonical comparison of projection vs snapshot at every mutation
        (boss ruling: internal AND public fields)."""
        proj = self.projection()
        msgs_equal = [m["content"] for m in proj["messages"]] == snap["messages"]
        queue_equal = [(q["message"], q["file_context"]) for q in proj["queue"]] == \
                      [(e["message"], e["file_context"]) for e in snap["queue"]]
        return bool(msgs_equal and queue_equal)

    def _result(self, snap, mut=None, key=None, applied=False, duplicate=False,
                noop=False, events=None, counts=None, note=None):
        base = {"created": 0, "annotated": 0, "tombstoned": 0, "restored": 0,
                "queued": 0, "dequeued": 0, "covered": 0, "event_count": 0}
        return {"ok": True, "chat_id": self.live_chat_id, "applied": applied,
                "duplicate": duplicate, "noop": noop, "mut_seq": mut,
                "idempotency_key": key, "snapshot_hash": snapshot_hash(snap),
                "events": events or [], "parity": self._parity(snap),
                "mirror_errors": self.mirror_errors, "note": note,
                **base, **(counts or {})}

    # ---- diff planner (pure; emits append_batch items + state ops)
    def _plan(self, snap, rows, active, pending, next_pos, kind, reason, mut,
              key, shash, ts, failpoint):
        now = ts if ts is not None else db.utc_ms()
        cid = self.live_chat_id
        S = [{"content": c, "hash": content_hash(c), "row_id": None,
              "action": None, "old_content": None} for c in snap["messages"]]
        items = []
        ops = []
        counts = {"created": 0, "annotated": 0, "tombstoned": 0, "restored": 0,
                  "queued": 0, "dequeued": 0, "covered": 0}
        head_before = active[-1]["id"] if active else None

        # ---------------------------------------------------- message rows
        if kind in REPLACE_KINDS:
            # hash-set occurrence matching over the whole transcript
            matched_s = [False] * len(S)
            matched_p = set()
            for i, p in enumerate(active):
                for j, s in enumerate(S):
                    if not matched_s[j] and s["hash"] == p["hash"]:
                        matched_s[j] = True
                        s["row_id"] = p["id"]
                        s["action"] = "match"
                        matched_p.add(i)
                        break
            removed = [p for i, p in enumerate(active) if i not in matched_p]
            inactive = [r for r in rows if r["status"] != ACTIVE] \
                if kind == "restore" else []
            used_inactive = set()
            for s in S:
                if s["row_id"] is not None:
                    continue
                for i, r in enumerate(inactive):
                    if i not in used_inactive and r["hash"] == s["hash"]:
                        used_inactive.add(i)
                        s["row_id"] = r["id"]
                        s["action"] = "restore"
                        break
                else:
                    s["action"] = "create"
        else:
            # tail strategy: longest common prefix + in-place annotation pair
            k = 0
            while k < len(active) and k < len(S) \
                    and active[k]["hash"] == S[k]["hash"]:
                k += 1
            for i in range(k):
                S[i]["row_id"] = active[i]["id"]
                S[i]["action"] = "match"
            p_tail, s_tail = active[k:], S[k:]
            paired = False
            if p_tail and s_tail:
                old, new = p_tail[0], s_tail[0]
                if old["content"].get("role") == new["content"].get("role") \
                        and _subdict(old["content"], new["content"]) \
                        and old["hash"] != new["hash"]:
                    paired = True
                    new["row_id"] = old["id"]
                    new["action"] = "annotate"
                    new["old_content"] = old["content"]
            if paired:
                for s in s_tail[1:]:
                    s["action"] = "create"
                removed = list(p_tail[1:])
            else:
                for s in s_tail:
                    s["action"] = "create"
                removed = list(p_tail)

        # deterministic row ids for created rows (mut strictly increases per
        # committed batch, so (mut, snapshot index) is unique — a crash retry
        # reproduces the exact same ids)
        for j, s in enumerate(S):
            if s["action"] == "create":
                s["row_id"] = "lgm%d_%d" % (mut, j)
        resolved = [s["row_id"] for s in S]
        removed_ids = [p["id"] for p in removed]

        for j, s in enumerate(S):
            if s["action"] == "create":
                if j > 0 and resolved[j - 1]:
                    parent = resolved[j - 1]
                elif kind not in REPLACE_KINDS and head_before \
                        and head_before not in removed_ids:
                    parent = head_before
                else:
                    parent = None
                role = str(s["content"].get("role") or "user")
                items.append({"type": "message.created", "id": s["row_id"],
                              "data": {"content": s["content"], "origin": "legacy"},
                              "ts": now})
                ops.append((
                    "INSERT OR IGNORE INTO messages(id, chat_id, parent_id, role,"
                    " kind, content_json, status, client_msg_id, turn_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (s["row_id"], cid, parent, role, role,
                     canonical_dumps(s["content"]), ACTIVE, None, None, now)))
                counts["created"] += 1
            elif s["action"] == "annotate":
                items.append({
                    "type": "message.annotated",
                    "data": {"message_id": s["row_id"],
                             "fields": {k: v for k, v in s["content"].items()
                                        if k not in s["old_content"]
                                        or canonical_dumps(s["old_content"][k])
                                        != canonical_dumps(v)},
                             "removed": [k for k in s["old_content"]
                                         if k not in s["content"]]},
                    "ts": now})
                ops.append(("UPDATE messages SET content_json=? WHERE id=?",
                            (canonical_dumps(s["content"]), s["row_id"])))
                counts["annotated"] += 1
            elif s["action"] == "restore":
                items.append({"type": "message.restored",
                              "data": {"message_id": s["row_id"],
                                       "reason": reason or kind},
                              "ts": now})
                ops.append(("UPDATE messages SET status=? WHERE id=?",
                            (ACTIVE, s["row_id"])))
                counts["restored"] += 1

        for p in removed:
            if kind in REPLACE_KINDS:
                continue                       # covered by the watermark below
            items.append({"type": "message.tombstoned",
                          "data": {"message_id": p["id"], "reason": reason or kind},
                          "ts": now})
            ops.append(("UPDATE messages SET status=? WHERE id=?",
                        (TOMBSTONED, p["id"])))
            counts["tombstoned"] += 1

        # ---------------------------------------------------- queue (FIFO)
        p_entries = [(q["message"], q["file_context"]) for q in pending]
        s_entries = [(e["message"], e["file_context"]) for e in snap["queue"]]
        pre = 0
        while pre < len(p_entries) and pre < len(s_entries) \
                and p_entries[pre] == s_entries[pre]:
            pre += 1
        suf = 0
        while suf < len(p_entries) - pre and suf < len(s_entries) - pre \
                and p_entries[len(p_entries) - 1 - suf] \
                == s_entries[len(s_entries) - 1 - suf]:
            suf += 1
        for q in pending[pre:len(p_entries) - suf]:
            items.append({"type": "message.dequeued",
                          "data": {"queue_id": q["queue_id"],
                                   "message_id": q["message_id"],
                                   "position": q["position"],
                                   "reason": reason or kind},
                          "ts": now})
            ops.append(("UPDATE queue SET state='sent' WHERE id=?",
                        (q["queue_id"],)))
            counts["dequeued"] += 1
        for off, e in enumerate(snap["queue"][pre:len(s_entries) - suf]):
            qid = "lgq%d_%d" % (mut, off)
            mid = "lgqm%d_%d" % (mut, off)
            pos = next_pos + off
            entry_content = {"role": "user", "message": e["message"],
                             "file_context": e["file_context"], "queued": True,
                             "origin": "legacy-queue"}
            items.append({"type": "message.created", "id": mid,
                          "data": {"content": entry_content, "origin": "legacy"},
                          "ts": now})
            ops.append((
                "INSERT OR IGNORE INTO messages(id, chat_id, parent_id, role,"
                " kind, content_json, status, client_msg_id, turn_id, created_at)"
                " VALUES(?,?,NULL,'user','user',?,'queued',NULL,NULL,?)",
                (mid, cid, canonical_dumps(entry_content), now)))
            items.append({"type": "message.queued",
                          "data": {"queue_id": qid, "message_id": mid,
                                   "position": pos,
                                   "entry": {"message": e["message"],
                                             "file_context": e["file_context"]}},
                          "ts": now})
            ops.append((
                "INSERT OR IGNORE INTO queue(id, chat_id, message_id, state,"
                " position) VALUES(?,?,?,'pending',?)", (qid, cid, mid, pos)))
            counts["queued"] += 1
            counts["created"] += 1               # the entry's own message row

        # ---------------------------------------------------- watermark batch
        if kind in REPLACE_KINDS:
            # "order" records the resulting transcript order — a replacement
            # may REORDER rows (the compression summary precedes the kept rows
            # but is created after them); the projection replays it.
            new_order = [s["row_id"] for s in S]
            if kind == "clear":
                data = {"clear_id": "lgc%d" % mut, "covered": removed_ids,
                        "count": len(removed_ids), "reason": reason or "clear",
                        "order": new_order}
            elif kind == "compression":
                summary_id = None
                for s in S:
                    if s["action"] == "create" \
                            and s["content"].get("role") == "system":
                        summary_id = s["row_id"]
                        break
                if summary_id is None:
                    summary_id = next((s["row_id"] for s in S
                                       if s["action"] == "create"), None)
                data = {"compression_id": "lgz%d" % mut, "covers": removed_ids,
                        "summary_id": summary_id, "count": len(removed_ids),
                        "order": new_order}
            else:
                data = {"restore_id": "lgr%d" % mut, "snapshot_hash": shash,
                        "covered": removed_ids, "count": len(removed_ids),
                        "reason": reason or "restore", "order": new_order}
            items.append({"type": WATERMARK_TYPE[kind], "data": data, "ts": now})
            if removed_ids:
                placeholders = ",".join("?" * len(removed_ids))
                hidden = COMPRESSED if kind == "compression" else TOMBSTONED
                ops.append(("UPDATE messages SET status=? WHERE id IN (%s)"
                            % placeholders, [hidden] + removed_ids))
            counts["covered"] = len(removed_ids)

        # ---------------------------------------------------- batch writer
        def writer(conn, first_seq):
            conn.execute(
                "INSERT OR IGNORE INTO chats(id, mem_enabled, created_at,"
                " updated_at) VALUES(?,?,?,?)", (cid, 1, now, now))
            for sql, params in ops:
                conn.execute(sql, params)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                         (META_MUT_SEQ, str(mut)))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                         (META_SNAPSHOT_HASH, shash))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                         (META_LAST_KEY, key))
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?,?)",
                         (META_KEY_PREFIX + key, "1"))
            conn.execute("UPDATE chats SET head_message_id=?, updated_at=?"
                         " WHERE id=?",
                         (S[-1]["row_id"] if S else None, now, cid))
            if failpoint is not None:
                failpoint("after_state")
            return None

        return {"items": items, "writer": writer, "counts": counts}

    # ---- mirror-specific invariants (boss ruling: non-created events must
    # refer to existing message.created events; tombstones retain content)
    def check_mirror_invariants(self):
        ro = self.store._read
        cid = self.live_chat_id
        problems = {"mirror_referents": [], "tombstone_content_retained": [],
                    "queue_positions": [], "annotation_shape": []}

        created = {r["id"] for r in ro.execute(
            "SELECT id FROM events WHERE chat_id=? AND type='message.created'",
            (cid,)).fetchall()}
        for r in ro.execute("SELECT seq, type, data_json FROM events"
                            " WHERE chat_id=? ORDER BY seq", (cid,)).fetchall():
            d = json.loads(r["data_json"] or "{}")
            refs = []
            if r["type"] in ("message.annotated", "message.tombstoned",
                             "message.restored", "message.queued",
                             "message.dequeued"):
                if d.get("message_id"):
                    refs.append(d["message_id"])
            elif r["type"] in ("clear.applied", "restore.applied"):
                refs.extend(d.get("covered") or [])
            elif r["type"] == "compression.applied":
                refs.extend(d.get("covers") or [])
                if d.get("summary_id"):
                    refs.append(d["summary_id"])
            for ref in refs:
                if ref not in created:
                    problems["mirror_referents"].append(
                        {"seq": r["seq"], "type": r["type"], "ref": ref})
            if r["type"] == "message.annotated" and (
                    not d.get("message_id") or "fields" not in d):
                problems["annotation_shape"].append({"seq": r["seq"]})

        problems["tombstone_content_retained"] = [
            r["id"] for r in ro.execute(
                "SELECT id FROM messages WHERE chat_id=? AND"
                " (content_json IS NULL OR content_json='')", (cid,)).fetchall()]

        dupes = [dict(r) for r in ro.execute(
            "SELECT position, COUNT(*) AS n FROM queue WHERE chat_id=?"
            " GROUP BY position HAVING n > 1", (cid,)).fetchall()]
        lo = ro.execute("SELECT MIN(position) FROM queue WHERE chat_id=?",
                        (cid,)).fetchone()[0]
        if lo is not None and lo < 0:
            dupes.append({"position": lo, "n": "negative"})
        problems["queue_positions"] = dupes

        problems["_pass"] = all(not v for v in problems.values())
        return problems
