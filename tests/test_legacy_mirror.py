"""P1.5 mirror adapter tests (docs/P15_DESIGN_RULING.md) — isolated adapter only.

Boss test list, one class each (or folded into the shared step harness which
re-asserts parity + both invariant sets + events-only rebuild after EVERY
mutation):

- normal exchange: user+assistant snapshot -> minimal message.created diff;
- queue/dequeue: busy/offline queueing, FIFO flush, contiguous positions;
- undo: tail tombstones, content retained, compensable;
- clear: clear.applied watermark, queue untouched (legacy behavior), no delete;
- restore: restore.applied with the snapshot hash; message.restored undoes;
- compression: compression.applied {covers, summary_id}, originals hidden;
- tools_used: message.annotated, original message.created never mutated;
- repeated snapshot: deterministic idempotency (noop + re-delivery skip);
- crash/reconcile: legacy-write/mirror crash repaired at boot (in-process
  rollback containment + hard os._exit kills in a child process);
- events-only rebuild equals the materialized projection;
- canonical internal/public parity at every mutation;
- store + mirror invariants (every created row has its message.created).

Run: python -m unittest tests.test_legacy_mirror -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb            # noqa: E402
from sitecrew.store import legacy as lg         # noqa: E402
from sitecrew.store.events import EventStore    # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Child for the hard-kill legs: applies one snapshot against the shared DB,
# either dying INSIDE the batch transaction (os._exit mid-tx -> rollback) or
# committing fully and dying before the caller could learn the outcome.
CHILD = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from sitecrew.store.events import EventStore
from sitecrew.store import legacy as lg
db_path, snap_path, mode = sys.argv[2], sys.argv[3], sys.argv[4]
with open(snap_path, encoding="utf-8") as f:
    snap = json.load(f)
store = EventStore(db_path)
adapter = lg.MirrorAdapter(store)

def failpoint(stage):
    if mode == "kill_in_batch" and stage == "after_state":
        os._exit(9)                              # hard kill inside the tx

r = adapter.mirror(snap["messages"], snap["queue"], kind="exchange",
                   failpoint=failpoint)
if mode not in ("kill_in_batch", "after_commit") or not r.get("ok"):
    sys.exit(3)
os._exit(9)                                      # crash after the attempt
'''


def msg(role, content, **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return m


class Base(unittest.TestCase):
    """Harness: a simulated legacy STATE plus the adapter under test. Every
    step() re-checks parity, both invariant sets, and events-only rebuild."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_lg_")
        self.path = os.path.join(self.tmp, "sitecrew.db")
        self.store = EventStore(self.path)
        self.adapter = lg.MirrorAdapter(self.store)
        self.msgs = []
        self.queue = []
        self.mut = 0

    def tearDown(self):
        if self.store is not None:
            self.store.close()

    # ---- legacy simulation helpers
    def step(self, kind, mutation_seq=None, ts=None):
        """Apply the current legacy state as one mutation; assert everything."""
        self.mut += 1
        r = self.adapter.apply(self.msgs, self.queue, kind=kind,
                               mutation_seq=mutation_seq if mutation_seq is not None
                               else self.mut, ts=ts)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["parity"], r)
        self.assert_healthy()
        return r

    def meta(self, key):
        row = self.store._read.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def envelopes(self):
        return self.store.events_after(self.adapter.live_chat_id, 0, 100000)

    def assert_healthy(self):
        """The boss's per-mutation assertions: frozen invariants pass, mirror
        invariants pass, and the events-only rebuild equals the projection."""
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"], inv)
        minv = self.adapter.check_mirror_invariants()
        self.assertTrue(minv["_pass"], minv)
        rebuilt = lg.rebuild_projection(self.envelopes())
        proj = self.adapter.projection()
        self.assertEqual([{ "id": m["id"], "content": m["content"],
                            "status": m["status"], "ts": m["ts"]}
                          for m in rebuilt["messages"]],
                         [{"id": m["id"], "content": m["content"],
                           "status": m["status"], "ts": m["ts"]}
                          for m in proj["messages"]])
        self.assertEqual(rebuilt["queue"], proj["queue"])

    def assert_canonical_parity(self):
        """Projection contents equal the canonical snapshot EXACTLY (internal
        fields included) and the public view matches the legacy public view."""
        snap = lg.canonical_snapshot(self.msgs, self.queue)
        proj = self.adapter.projection()
        self.assertEqual([m["content"] for m in proj["messages"]], snap["messages"])
        self.assertEqual([(q["message"], q["file_context"]) for q in proj["queue"]],
                         [(e["message"], e["file_context"]) for e in snap["queue"]])
        public_legacy = [{k: v for k, v in m.items() if k != "model_content"}
                         for m in self.msgs]
        public_store = [{k: v for k, v in m["content"].items()
                         if k != "model_content"} for m in proj["messages"]]
        self.assertEqual(public_store, public_legacy)


class TestNormalExchange(Base):
    def test_exchange_emits_minimal_created_events(self):
        r0 = self.adapter.apply([], [], kind="exchange", mutation_seq=1)
        self.assertTrue(r0["ok"] and r0["noop"] and not r0["applied"])
        self.assertEqual(self.store.head(self.adapter.live_chat_id), 0)

        self.msgs = [msg("user", "hello"), msg("assistant", "hi there")]
        r = self.step("exchange")
        self.assertTrue(r["applied"])
        self.assertEqual(r["created"], 2)
        self.assertEqual([e["type"] for e in r["events"]],
                         ["message.created", "message.created"])
        self.assertEqual([e["seq"] for e in r["events"]], [1, 2])
        # P1.2 oracle convention: message.created event id == messages row id
        self.assertEqual([e["id"] for e in r["events"]],
                         ["lgm1_0", "lgm1_1"])
        self.assertEqual([m["id"] for m in self.adapter.projection()["messages"]],
                         ["lgm1_0", "lgm1_1"])
        cid = self.adapter.live_chat_id
        chat = self.store._read.execute(
            "SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        self.assertIsNotNone(chat)
        self.assertEqual(chat["head_message_id"], "lgm1_1")
        self.assertEqual(self.meta("legacy:mut_seq"), "1")

        # second exchange appends only the two new rows (minimal diff)
        self.msgs += [msg("user", "again"), msg("assistant", "ok")]
        r2 = self.step("exchange")
        self.assertEqual(r2["created"], 2)
        self.assertEqual([e["id"] for e in r2["events"]],
                         ["lgm2_2", "lgm2_3"])
        self.assertEqual(self.store.head(cid), 4)
        self.assert_canonical_parity()

    def test_first_job_born_with_tools_used_creates_not_annotates(self):
        # offline-intent shape: the reply is created WITH tools_used in one go
        self.msgs = [msg("user", "check battery"),
                     msg("assistant", "Battery: 55%", tools_used={"battery": 1})]
        r = self.step("exchange")
        self.assertEqual(r["created"], 2)
        self.assertEqual(r["annotated"], 0)
        self.assertEqual(r["events"][1]["data"]["content"]["tools_used"],
                         {"battery": 1})


class TestQueueDequeueFIFO(Base):
    def test_enqueue_then_fifo_flush(self):
        self.msgs = [msg("user", "hello"), msg("assistant", "hi")]
        self.step("exchange")

        # busy/offline: the message enters the transcript AND the queue
        self.msgs.append(msg("user", "queued one",
                             model_content="queued one"))
        self.queue.append({"message": "queued one", "file_context": ""})
        r1 = self.step("enqueue")
        self.assertEqual(r1["created"], 2)       # transcript row + entry row
        self.assertEqual(r1["queued"], 1)
        types = [e["type"] for e in r1["events"]]
        self.assertEqual(types.count("message.created"), 2)
        self.assertEqual(types.count("message.queued"), 1)
        qe = [e for e in r1["events"] if e["type"] == "message.queued"][0]
        self.assertEqual(qe["data"]["position"], 0)   # contiguous allocation
        self.assertEqual(qe["data"]["entry"]["message"], "queued one")
        row = self.store._read.execute(
            "SELECT message_id, state FROM queue WHERE chat_id=? AND position=0",
            (self.adapter.live_chat_id,)).fetchone()
        self.assertEqual(row["state"], "pending")
        entry_status = self.store._read.execute(
            "SELECT status FROM messages WHERE id=?",
            (row["message_id"],)).fetchone()[0]
        self.assertEqual(entry_status, "queued")  # entry rows never in transcript

        self.queue.append({"message": "queued two", "file_context": ""})
        self.msgs.append(msg("user", "queued two", model_content="queued two"))
        r2 = self.step("enqueue")
        qe2 = [e for e in r2["events"] if e["type"] == "message.queued"][0]
        self.assertEqual(qe2["data"]["position"], 1)
        self.assert_canonical_parity()

        # flush pops the FRONT (FIFO): first entry dequeued, second stays
        self.queue.pop(0)
        r3 = self.step("dequeue")
        self.assertEqual(r3["dequeued"], 1)
        self.assertEqual(r3["created"], 0)
        de = [e for e in r3["events"] if e["type"] == "message.dequeued"][0]
        self.assertEqual(de["data"]["position"], 0)
        self.assertEqual(de["data"]["message_id"], qe["data"]["message_id"])
        state = self.store._read.execute(
            "SELECT state FROM queue WHERE id=?", (de["data"]["queue_id"],)
        ).fetchone()[0]
        self.assertEqual(state, "sent")
        self.assertEqual([q["message"] for q in self.adapter.projection()["queue"]],
                         ["queued two"])

        # the flushed job's reply appends; second flush empties the queue
        self.msgs.append(msg("assistant", "done"))
        self.step("exchange")
        self.queue.pop(0)
        r4 = self.step("dequeue")
        self.assertEqual(r4["dequeued"], 1)
        self.assertEqual(self.adapter.projection()["queue"], [])
        # positions were allocated contiguously 0..1 (two entries) and the
        # dequeued rows are kept, never reused or deleted
        pos = [r[0] for r in self.store._read.execute(
            "SELECT position FROM queue WHERE chat_id=? ORDER BY position",
            (self.adapter.live_chat_id,)).fetchall()]
        self.assertEqual(pos, [0, 1])


class TestUndo(Base):
    def test_undo_tombstones_tail_and_keeps_history(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1"),
                     msg("user", "u2"), msg("assistant", "a2")]
        self.step("exchange")
        before = self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]

        self.msgs = self.msgs[:2]                # legacy undo pops the exchange
        r = self.step("undo")
        self.assertEqual(r["tombstoned"], 2)
        self.assertEqual([e["type"] for e in r["events"]],
                         ["message.tombstoned", "message.tombstoned"])
        reasons = [e["data"]["reason"] for e in r["events"]]
        self.assertEqual(reasons, ["undo", "undo"])
        ids = [e["data"]["message_id"] for e in r["events"]]
        self.assertEqual(ids, ["lgm1_2", "lgm1_3"])
        # no physical deletion: rows remain with their content
        after = self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(after, before)
        for mid in ids:
            row = self.store._read.execute(
                "SELECT status, content_json FROM messages WHERE id=?",
                (mid,)).fetchone()
            self.assertEqual(row["status"], "tombstoned")
            self.assertEqual(json.loads(row["content_json"])["content"],
                             "u2" if mid == "lgm1_2" else "a2")
        self.assertEqual([m["content"]["content"]
                          for m in self.adapter.projection()["messages"]],
                         ["u1", "a1"])


class TestClear(Base):
    def test_clear_watermark_keeps_rows_and_queue(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1")]
        self.step("exchange")                    # rows lgm1_0, lgm1_1
        self.msgs.append(msg("user", "q1", model_content="q1\n\nfc"))
        self.queue.append({"message": "q1", "file_context": "fc"})
        self.step("enqueue")                     # transcript row lgm2_2 + entry
        before = self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]

        self.msgs = []                           # legacy /api/clear: queue KEPT
        r = self.step("clear")
        self.assertEqual(r["covered"], 3)
        marker = [e for e in r["events"] if e["type"] == "clear.applied"]
        self.assertEqual(len(marker), 1)
        self.assertEqual(marker[0]["data"]["covered"],
                         ["lgm1_0", "lgm1_1", "lgm2_2"])
        self.assertEqual(marker[0]["data"]["count"], 3)
        self.assertIsNotNone(marker[0]["data"]["clear_id"])
        after = self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(after, before)          # nothing physically deleted
        statuses = {r[0]: r[1] for r in self.store._read.execute(
            "SELECT id, status FROM messages").fetchall()}
        self.assertEqual(set(statuses.values()), {"tombstoned", "queued"})
        for mid in ("lgm1_0", "lgm1_1", "lgm2_2"):
            cj = self.store._read.execute(
                "SELECT content_json FROM messages WHERE id=?", (mid,)
            ).fetchone()[0]
            self.assertTrue(cj)                  # tombstones retain content
        # legacy clear leaves the queue alone
        self.assertEqual([q["message"] for q in self.adapter.projection()["queue"]],
                         ["q1"])
        self.assertEqual(self.adapter.projection()["messages"], [])


class TestRestore(Base):
    def test_restore_replaces_transcript_and_restores_undo(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1"),
                     msg("user", "u2"), msg("assistant", "a2")]
        self.step("exchange")
        self.msgs = self.msgs[:2]
        self.step("undo")

        # backup restore brings the undone exchange back (same content)
        pre_undo = [msg("user", "u1"), msg("assistant", "a1"),
                    msg("user", "u2"), msg("assistant", "a2")]
        self.msgs = [dict(m) for m in pre_undo]
        r = self.step("restore")
        self.assertEqual(r["restored"], 2)       # message.restored compensates
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["covered"], 0)
        marker = [e for e in r["events"] if e["type"] == "restore.applied"]
        self.assertEqual(len(marker), 1)
        self.assertEqual(marker[0]["data"]["snapshot_hash"], r["snapshot_hash"])
        for mid in ("lgm1_2", "lgm1_3"):
            status = self.store._read.execute(
                "SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0]
            self.assertEqual(status, "final")
        self.assert_canonical_parity()

        # a restore with brand-new content tombstones the old transcript
        self.msgs = [msg("user", "restored convo")]
        r2 = self.step("restore")
        self.assertEqual(r2["covered"], 4)
        self.assertEqual(r2["created"], 1)
        self.assertEqual([m["content"]["content"]
                          for m in self.adapter.projection()["messages"]],
                         ["restored convo"])


class TestCompression(Base):
    def test_compression_hides_originals_and_creates_summary(self):
        self.msgs = [msg("user", "u%d" % i, model_content="u%d" % i)
                     for i in range(1, 6)]
        self.step("exchange")
        before = self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]

        older = self.msgs[:3]
        self.msgs = [msg("system", "Summary of earlier conversation: users 1-3")] \
            + self.msgs[3:]
        r = self.step("compression")
        self.assertEqual(r["covered"], 3)
        self.assertEqual(r["created"], 1)
        marker = [e for e in r["events"] if e["type"] == "compression.applied"]
        self.assertEqual(len(marker), 1)
        self.assertEqual(marker[0]["data"]["covers"],
                         ["lgm1_0", "lgm1_1", "lgm1_2"])
        summary_id = marker[0]["data"]["summary_id"]
        self.assertEqual(summary_id, "lgm2_0")
        summary_row = self.store._read.execute(
            "SELECT role, status FROM messages WHERE id=?", (summary_id,)
        ).fetchone()
        self.assertEqual(summary_row["role"], "system")
        self.assertEqual(summary_row["status"], "final")
        # originals remain, hidden from the active projection
        self.assertEqual(self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], before + 1)
        for mid in ("lgm1_0", "lgm1_1", "lgm1_2"):
            row = self.store._read.execute(
                "SELECT status, content_json FROM messages WHERE id=?",
                (mid,)).fetchone()
            self.assertEqual(row["status"], "compressed")
            self.assertTrue(row["content_json"])
        self.assertEqual([m["content"]["content"]
                          for m in self.adapter.projection()["messages"]],
                         ["Summary of earlier conversation: users 1-3", "u4", "u5"])


class TestToolsUsedAnnotation(Base):
    def test_annotation_patches_last_row_never_the_created_event(self):
        self.msgs = [msg("user", "hello"),
                     msg("assistant", "hi", job=1, elapsed_s=1.2,
                         workers=["operator"])]
        self.step("exchange")
        created_before = [e for e in self.envelopes()
                          if e["type"] == "message.created"]

        self.msgs[-1]["tools_used"] = {"battery": 2}   # legacy finally-block
        r = self.step("annotate")
        self.assertEqual(r["annotated"], 1)
        self.assertEqual(r["created"], 0)
        ev = [e for e in r["events"] if e["type"] == "message.annotated"][0]
        self.assertEqual(ev["data"]["message_id"], "lgm1_1")
        self.assertEqual(ev["data"]["fields"], {"tools_used": {"battery": 2}})
        self.assertEqual(ev["data"]["removed"], [])
        # the original created event is byte-identical (never mutated)
        created_after = [e for e in self.envelopes()
                         if e["type"] == "message.created"]
        self.assertEqual(created_after, created_before)
        # the row content carries the annotation; projection parity holds
        content = self.store._read.execute(
            "SELECT content_json FROM messages WHERE id='lgm1_1'").fetchone()[0]
        self.assertEqual(json.loads(content)["tools_used"], {"battery": 2})
        self.assert_canonical_parity()


class TestRepeatedSnapshot(Base):
    def test_same_snapshot_is_idempotent(self):
        self.msgs = [msg("user", "hello"), msg("assistant", "hi")]
        r1 = self.step("exchange")
        head = self.store.head(self.adapter.live_chat_id)

        # same state again (caller retry): empty diff -> no events, no batch
        r2 = self.adapter.apply(self.msgs, self.queue, kind="exchange",
                                mutation_seq=2)
        self.assertTrue(r2["ok"] and r2["noop"] and not r2["applied"])
        self.assertEqual(r2["events"], [])
        self.assertEqual(self.store.head(self.adapter.live_chat_id), head)

        # re-delivery of the ALREADY-COMMITTED mutation seq: key skip
        r3 = self.adapter.apply(self.msgs, self.queue, kind="exchange",
                                mutation_seq=1)
        self.assertTrue(r3["ok"] and r3["duplicate"] and not r3["applied"])
        self.assertEqual(r3["events"], [])
        self.assertEqual(self.store.head(self.adapter.live_chat_id), head)
        # the idempotency key sha256("<seq>|<shash>") is recorded in meta
        self.assertEqual(self.meta("legacy:key:" + r1["idempotency_key"]), "1")
        self.assertEqual(self.meta("legacy:last_idem_key"),
                         r1["idempotency_key"])
        self.assertEqual(self.meta("legacy:snapshot_hash"), r1["snapshot_hash"])

        # a real change still applies and advances the mutation seq
        self.msgs.append(msg("user", "next"))
        r4 = self.step("exchange")
        self.assertTrue(r4["applied"])
        self.assertEqual(r4["mut_seq"], 2)
        self.assertEqual(self.meta("legacy:mut_seq"), "2")

    def test_identical_text_repeats_are_separate_rows(self):
        self.msgs = [msg("user", "same")]
        self.step("exchange")
        self.msgs.append(msg("user", "same"))
        r = self.step("exchange")
        self.assertEqual(r["created"], 1)        # no content dedupe
        self.assertNotEqual(self.adapter.projection()["messages"][0]["id"],
                            self.adapter.projection()["messages"][1]["id"])


class TestCrashReconcile(Base):
    def test_injected_mirror_failure_is_contained_then_reconciled(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1")]

        def boom(stage):
            raise RuntimeError("injected mirror failure")

        # mirror blows up mid-batch: legacy response unaffected (mirror never
        # raises), nothing partially committed, error counted
        r = self.adapter.mirror(self.msgs, self.queue, kind="exchange",
                                mutation_seq=1, failpoint=boom)
        self.assertFalse(r["ok"])
        self.assertEqual(r["mirror_errors"], 1)
        self.assertEqual(self.adapter.mirror_errors, 1)
        self.assertEqual(self.store.head(self.adapter.live_chat_id), 0)
        self.assertEqual(self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertTrue(self.store.check_invariants()["_pass"])

        # boot reconcile replays the gap with the SAME deterministic ids
        rec = self.adapter.reconcile(self.msgs, self.queue)
        self.assertTrue(rec["applied"])
        self.assertEqual([e["id"] for e in rec["events"]],
                         ["lgm1_0", "lgm1_1"])
        self.assertTrue(rec["parity"])
        self.assert_healthy()
        self.assert_canonical_parity()

    def _run_child(self, mode, snap):
        snap_path = os.path.join(self.tmp, "snap.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        self.store.close()
        self.store = None
        proc = subprocess.run([sys.executable, "-c", CHILD, REPO, self.path,
                               snap_path, mode], capture_output=True,
                              text=True, timeout=180)
        return proc

    def _reopen(self):
        self.store = EventStore(self.path)
        self.adapter = lg.MirrorAdapter(self.store)

    def test_kill_inside_batch_rolls_back_and_boot_reconciles(self):
        snap = {"messages": [{"role": "user", "content": "u1"},
                             {"role": "assistant", "content": "a1"}],
                "queue": []}
        proc = self._run_child("kill_in_batch", snap)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self._reopen()
        # the killed batch left NOTHING behind (transaction rolled back)
        self.assertEqual(self.store.head(self.adapter.live_chat_id), 0)
        self.assertEqual(self.store._read.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertTrue(self.store.check_invariants()["_pass"])
        # boot reconcile repairs the crash gap deterministically
        rec = self.adapter.reconcile(snap["messages"], snap["queue"])
        self.assertTrue(rec["applied"])
        self.assertEqual([e["id"] for e in rec["events"]],
                         ["lgm1_0", "lgm1_1"])
        self.assertTrue(rec["parity"])
        self.assert_healthy()

    def test_kill_after_commit_reconciles_to_noop(self):
        snap = {"messages": [{"role": "user", "content": "u1"},
                             {"role": "assistant", "content": "a1"}],
                "queue": []}
        proc = self._run_child("after_commit", snap)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self._reopen()
        head = self.store.head(self.adapter.live_chat_id)
        self.assertEqual(head, 2)                # the batch DID commit
        rec = self.adapter.reconcile(snap["messages"], snap["queue"])
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["noop"] and not rec["applied"])
        self.assertEqual(rec["events"], [])
        self.assertEqual(self.store.head(self.adapter.live_chat_id), head)
        self.assertTrue(rec["parity"])
        self.assert_healthy()


class TestEventsOnlyRebuild(Base):
    def test_full_mutation_scenario_rebuild_equals_projection(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1")]
        self.step("exchange")
        self.msgs.append(msg("user", "uq", model_content="uq"))
        self.queue.append({"message": "uq", "file_context": ""})
        self.step("enqueue")
        self.queue.pop(0)
        self.step("dequeue")
        self.msgs.append(msg("assistant", "a2", job=2, elapsed_s=0.4,
                             workers=["operator"]))
        self.step("exchange")
        self.msgs[-1]["tools_used"] = {"battery": 1}
        self.step("annotate")
        self.msgs = self.msgs[:2]                # undo pops a2 + uq
        self.step("undo")
        self.msgs = [msg("user", "u1"), msg("assistant", "a1"),
                     msg("user", "uq", model_content="uq"),
                     msg("assistant", "a2", job=2, elapsed_s=0.4,
                         workers=["operator"], tools_used={"battery": 1})]
        self.step("restore")
        self.msgs = [msg("system", "Summary: u1/a1"),
                     msg("user", "uq", model_content="uq"),
                     msg("assistant", "a2", job=2, elapsed_s=0.4,
                         workers=["operator"], tools_used={"battery": 1})]
        self.step("compression")
        self.msgs = []
        self.step("clear")

        rebuilt = lg.rebuild_projection(self.envelopes())
        proj = self.adapter.projection()
        self.assertEqual(rebuilt["messages"], proj["messages"])
        self.assertEqual(rebuilt["queue"], proj["queue"])
        # one coherent append chain: every event landed in the live stream
        self.assertEqual(self.store.head(proj["chat_id"]),
                         len(self.envelopes()))
        self.assertTrue(self.meta("legacy:snapshot_hash")
                        == lg.snapshot_hash(lg.canonical_snapshot([], [])))


class TestCanonicalParity(Base):
    def test_internal_and_public_fields_survive_every_mutation(self):
        self.msgs = [msg("user", "with file", model_content="with file\n\nDATA",
                         attachments=[{"name": "a.txt", "size": 3}]),
                     msg("assistant", "reply", job=1, elapsed_s=0.5,
                         workers=["files_desk"], tools_used={"fs.read": 1})]
        self.step("exchange")
        self.assert_canonical_parity()
        proj = self.adapter.projection()
        # internal field preserved verbatim in the store rows
        row = json.loads(self.store._read.execute(
            "SELECT content_json FROM messages WHERE id=?",
            (proj["messages"][0]["id"],)).fetchone()[0])
        self.assertEqual(row["model_content"], "with file\n\nDATA")
        self.assertEqual(row["attachments"], [{"name": "a.txt", "size": 3}])
        # canonical round trip is lossless (dict-equal to the legacy dict)
        self.assertEqual(proj["messages"][0]["content"], self.msgs[0])
        self.assertEqual(proj["messages"][1]["content"], self.msgs[1])


class TestStableLiveChatId(Base):
    def test_live_chat_id_persisted_in_meta_and_reused(self):
        cid = self.adapter.live_chat_id
        self.assertTrue(sdb.valid_chat_id(cid))
        self.assertEqual(self.meta("legacy:live_chat_id"), cid)
        self.assertEqual(self.adapter.live_chat_id, cid)   # stable in-process
        self.store.close()
        self.store = EventStore(self.path)
        self.adapter = lg.MirrorAdapter(self.store)
        self.assertEqual(self.adapter.live_chat_id, cid)   # stable across boot
        # and the id never collides with the office stream or import remaps
        self.assertNotEqual(cid, "office")


class TestInvariants(Base):
    def test_every_created_row_has_message_created_both_directions(self):
        self.msgs = [msg("user", "u1"), msg("assistant", "a1")]
        self.msgs.append(msg("user", "uq"))
        self.queue.append({"message": "uq", "file_context": ""})
        self.step("exchange")
        self.step("enqueue")
        cid = self.adapter.live_chat_id
        orphans = self.store._read.execute(
            "SELECT COUNT(*) FROM messages m WHERE m.chat_id=? AND NOT EXISTS"
            " (SELECT 1 FROM events e WHERE e.chat_id=m.chat_id AND e.id=m.id"
            "  AND e.type='message.created')", (cid,)).fetchone()[0]
        ghosts = self.store._read.execute(
            "SELECT COUNT(*) FROM events e WHERE e.chat_id=? AND"
            " e.type='message.created' AND NOT EXISTS (SELECT 1 FROM messages m"
            " WHERE m.chat_id=e.chat_id AND m.id=e.id)", (cid,)).fetchone()[0]
        self.assertEqual(orphans, 0)
        self.assertEqual(ghosts, 0)

    def test_mirror_invariants_catch_dangling_referent(self):
        cid = self.adapter.live_chat_id
        self.store.append(cid, "message.tombstoned",
                          {"message_id": "ghost", "reason": "bad"}, strict=True)
        minv = self.adapter.check_mirror_invariants()
        self.assertFalse(minv["_pass"])
        self.assertEqual(minv["mirror_referents"],
                         [{"seq": 1, "type": "message.tombstoned",
                           "ref": "ghost"}])
        inv = self.store.check_invariants()
        self.assertTrue(inv["_pass"])            # frozen checker scope unchanged

    def test_no_physical_deletion_sql_in_adapter(self):
        with open(os.path.join(REPO, "sitecrew", "store", "legacy.py"),
                  encoding="utf-8") as f:
            src = f.read().upper()
        for banned in ("DELETE FROM", "DROP TABLE", "DROP COLUMN"):
            self.assertNotIn(banned, src)


class TestMirrorErrorContainment(Base):
    def test_bad_snapshot_fails_mirror_only(self):
        self.msgs = [msg("user", "hello"), msg("assistant", "hi")]
        self.step("exchange")
        head = self.store.head(self.adapter.live_chat_id)
        bad = dict(self.msgs)
        bad[0] = {"role": "user", "content": "x", "bad": {1, 2}}  # unserializable
        r = self.adapter.mirror(bad, self.queue, kind="exchange")
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        self.assertEqual(self.adapter.mirror_errors, 1)
        # legacy response intact: head and projection untouched, store healthy
        self.assertEqual(self.store.head(self.adapter.live_chat_id), head)
        self.assertTrue(self.store.check_invariants()["_pass"])
        # the next good mutation still applies
        self.msgs.append(msg("user", "next"))
        r2 = self.step("exchange")
        self.assertTrue(r2["applied"])
        self.assertEqual(self.adapter.mirror_errors, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
