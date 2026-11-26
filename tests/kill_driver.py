#!/usr/bin/env python3
"""P1.2 kill driver (parent side). Drives tests/kill_writer.py (production EventStore.append) on ONE
accumulating DB: N random external kills + 4 failpoints x M injected self-kills.

Why this exists: nothing inside EventStore can accumulate across generations (each generation is a
fresh process), so a harness that gets slower with generations is slow in the PARENT (per-kill
verification that scales with accumulated rows) or in DB-size-dependent work at child start (WAL
recovery). This driver keeps per-generation work proportional to the NEW rows, runs the O(DB)
checks on a fixed cadence, closes every connection, and logs per-phase timings.

  python tests/kill_driver.py --out tests/evidence/p12_kill_report.json [--seed N]
      [--random 200] [--per-fault 25] [--max-delay 0.4] [--full-every 50] [--db PATH]

Counting identity written to the report (and asserted):
  total_events == ack_lines + committed_unacked == COUNT(events) == sum(final per-stream heads)
  per generation committed_unacked in {0,1}; after_commit gens exactly 1; the three rollback modes
  exactly 0 with the in-flight state rows absent.
"""
import argparse
import contextlib
import faulthandler
import json
import os
import queue
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from sitecrew.store import db as sdb          # noqa: E402
from sitecrew.store.events import EventStore  # noqa: E402

WRITER = os.path.join(HERE, "kill_writer.py")
STREAMS = ["c_k1", "c_k2", "c_k3", "office"]
ROLLBACK_FAULTS = ["after_state", "after_event", "before_commit"]
FAULTS = ROLLBACK_FAULTS + ["after_commit"]

MSG_NO_STATE = ("SELECT COUNT(*) FROM events e WHERE e.chat_id=? AND e.seq>? "
                "AND e.type='message.created' "
                "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.id=e.id)")
TOOL_NO_STATE = ("SELECT COUNT(*) FROM events e WHERE e.chat_id=? AND e.seq>? "
                 "AND e.type='tool.committed' "
                 "AND NOT EXISTS (SELECT 1 FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id "
                 "WHERE tc.committed_seq=e.seq AND t.chat_id=e.chat_id)")
MSG_NO_EVENT = ("SELECT COUNT(*) FROM messages m WHERE m.rowid>? AND NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.id=m.id AND e.type='message.created')")
TOOL_NO_EVENT = ("SELECT COUNT(*) FROM tool_calls tc WHERE tc.rowid>? AND NOT EXISTS "
                 "(SELECT 1 FROM turns t JOIN events e ON e.chat_id=t.chat_id "
                 "AND e.seq=tc.committed_seq WHERE t.id=tc.turn_id AND e.type='tool.committed')")


@contextlib.contextmanager
def opened(path):
    con = sdb.connect(path)          # ADAPT if your factory has another signature
    try:
        yield con
    finally:
        con.close()


def one(con, sql, args=()):
    return con.execute(sql, args).fetchone()[0]


class Log:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1, encoding="utf-8")
        self.t0 = time.time()

    def __call__(self, msg):
        self.f.write("%8.2f %s\n" % (time.time() - self.t0, msg))
        self.f.flush()


def wal_bytes(path):
    try:
        return os.path.getsize(path + "-wal")
    except OSError:
        return 0


def checkpoint_truncate(path):
    plain = sqlite3.connect(path, timeout=30)
    try:
        busy = plain.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        plain.close()
    assert busy == 0, "wal_checkpoint(TRUNCATE) busy: a connection is still open"


def stderr_tail(path, n=600):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace").strip().splitlines()[-4:]
    except OSError:
        return []


def wait_ready(p, timeout):
    q = queue.Queue()
    threading.Thread(target=lambda: q.put(p.stdout.readline()), daemon=True).start()
    try:
        return q.get(timeout=timeout).strip() == b"READY"
    except queue.Empty:
        return False


def read_marks(path, offset):
    """-> (acks [(stream, seq)], end, inflight (stream, txno) | None)
    end: idle | between | inflight | torn_begin | acked_torn"""
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read()
    *lines, tail = raw.split(b"\n")
    rows = [l.split() for l in lines if l.strip()]
    acks = [(r[1].decode(), int(r[2])) for r in rows if r[0] == b"A"]
    tail = tail.strip()
    last = rows[-1] if rows else None
    inflight = None
    if tail.startswith(b"A"):
        end = "acked_torn"                 # commit finished, ack line torn
    elif tail.startswith(b"B"):
        end = "torn_begin"                 # killed while writing the BEGIN line
    elif last is None:
        end = "idle"
    elif last[0] == b"B":
        end, inflight = "inflight", (last[1].decode(), int(last[2]))
    else:
        end = "between"
    return acks, end, inflight


class Verifier:
    """Per-generation checks that touch only rows newer than the last verified high-water marks."""

    def __init__(self, path):
        self.path = path
        self.hw = dict.fromkeys(STREAMS, 0)
        self.mhw = 0
        self.thw = 0

    def check(self, acks, end, inflight, mode):
        with opened(self.path) as con:
            new_total, new_hw = 0, {}
            for s in STREAMS:
                hw = self.hw[s]
                n, lo, hi = con.execute(
                    "SELECT COUNT(*), MIN(seq), MAX(seq) FROM events WHERE chat_id=? AND seq>?",
                    (s, hw)).fetchone()
                if n:
                    assert lo == hw + 1 and hi == hw + n, (
                        "gap/dupe in %s: hw=%d lo=%s hi=%s n=%d" % (s, hw, lo, hi, n))
                mine = [q for (st, q) in acks if st == s]
                assert mine == list(range(hw + 1, hw + 1 + len(mine))), (
                    "acked seqs not contiguous in %s: %r" % (s, mine[:5]))
                assert n >= len(mine), "acked write LOST in %s (acked %d, found %d)" % (
                    s, len(mine), n)
                bad = one(con, MSG_NO_STATE, (s, hw)) + one(con, TOOL_NO_STATE, (s, hw))
                assert not bad, "events without state in %s: %d" % (s, bad)
                new_total += n
                new_hw[s] = hi if n else hw
            unacked = new_total - len(acks)
            assert unacked in (0, 1), "unacked commits in one generation: %d" % unacked
            assert not one(con, MSG_NO_EVENT, (self.mhw,)), "messages without event"
            assert not one(con, TOOL_NO_EVENT, (self.thw,)), "tool_calls without event"

            if end in ("idle", "between", "torn_begin"):
                assert unacked == 0, "end=%s but a commit exists without ack" % end
            elif end == "acked_torn":
                assert unacked == 1, "torn ack line but no unacked commit"
            if inflight:
                s, tx = inflight
                present = bool(one(
                    con,
                    "SELECT EXISTS(SELECT 1 FROM messages WHERE id=?) "
                    "OR EXISTS(SELECT 1 FROM tool_calls WHERE id=?) "
                    "OR EXISTS(SELECT 1 FROM turns WHERE id=?)",
                    ("k_%s_%d" % (s, tx), "tc_%s_%d" % (s, tx), "t_%s_%d" % (s, tx))))
                assert present == bool(unacked), (
                    "in-flight tx %s/%d: state present=%s but unacked=%d" % (s, tx, present, unacked))
            if mode in ROLLBACK_FAULTS:
                assert end == "inflight" and unacked == 0, (
                    "%s: end=%s unacked=%d (expected inflight/0)" % (mode, end, unacked))
            elif mode == "after_commit":
                assert end == "inflight" and unacked == 1, (
                    "after_commit: end=%s unacked=%d (expected inflight/1)" % (end, unacked))

            self.hw = new_hw
            self.mhw = one(con, "SELECT COALESCE(MAX(rowid),0) FROM messages")
            self.thw = one(con, "SELECT COALESCE(MAX(rowid),0) FROM tool_calls")
        return unacked


def full_check(path):
    """O(DB) checks: run on a cadence, never per kill."""
    with opened(path) as con:
        ic = con.execute("PRAGMA integrity_check").fetchall()
        assert len(ic) == 1 and ic[0][0] == "ok", "integrity_check: %r" % (ic[:3],)
        assert not con.execute("PRAGMA foreign_key_check").fetchall(), "foreign_key_check"
    es = EventStore(path)
    try:
        res = es.check_invariants()
    finally:
        es.close()
    # ADAPT: normalise whatever your check_invariants() returns into a list of problems
    problems = res if isinstance(res, list) else (
        res.get("problems", []) if isinstance(res, dict) else [])
    assert not problems, "check_invariants: %r" % (problems[:3],)
    checkpoint_truncate(path)


def run_gen(db_path, marks, kind, k, rng, max_delay, ready_timeout=30):
    off = os.path.getsize(marks) if os.path.exists(marks) else 0
    env = dict(os.environ, SITECREW_STORE="1", SITECREW_STRICT="1", PYTHONUNBUFFERED="1")
    err = open(marks + ".err", "ab")
    t0 = time.time()
    p = subprocess.Popen(
        [sys.executable, WRITER, db_path, marks, "" if kind == "random" else kind, str(k)],
        stdout=subprocess.PIPE, stderr=err, env=env)
    try:
        if not wait_ready(p, ready_timeout):
            p.kill()
            p.wait(timeout=30)
            err.flush()
            raise RuntimeError("writer never READY (rc=%s): %s" % (p.returncode, stderr_tail(marks + ".err")))
        t_ready = time.time()
        if kind == "random":
            time.sleep(rng.uniform(0.0, max_delay))
            if p.poll() is not None:                   # died by itself: NOT a kill (e.g. disk full) -> fail fast
                raise RuntimeError("writer died on its own (rc=%s): %s" % (p.returncode, stderr_tail(marks + ".err")))
            p.kill()                                   # external kill; portable (TerminateProcess on Windows)
        else:
            try:
                p.wait(timeout=120)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=30)
                raise RuntimeError("fault %s k=%d never fired" % (kind, k))
            if p.returncode != 9:
                raise RuntimeError("fault %s exited rc=%s (expected 9): %s" % (kind, p.returncode, stderr_tail(marks + ".err")))
        p.wait(timeout=30)                             # handles/locks fully released before we look
    finally:
        if p.stdout:
            p.stdout.close()
        err.close()
    t_dead = time.time()
    acks, end, inflight = read_marks(marks, off)
    return acks, end, inflight, t_ready - t0, t_dead - t_ready


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--db")
    ap.add_argument("--seed", type=int, default=int(time.time()))
    ap.add_argument("--random", type=int, default=200)
    ap.add_argument("--per-fault", type=int, default=25)
    ap.add_argument("--max-delay", type=float, default=0.4)
    ap.add_argument("--full-every", type=int, default=50)
    ap.add_argument("--workdir", help="parent dir for the temp DB (put it on the big drive)")
    ap.add_argument("--wal-cap-mb", type=float, default=32.0, help="TRUNCATE-checkpoint when the WAL exceeds this")
    ap.add_argument("--min-free-gb", type=float, default=2.0, help="abort before SQLite thrashes on a full disk")
    ap.add_argument("--deadline-min", type=float, default=25.0)
    ap.add_argument("--floor-events", type=int, default=10000)
    ap.add_argument("--floor-inflight-pct", type=float, default=25.0)
    a = ap.parse_args()

    db_path = a.db or os.path.join(tempfile.mkdtemp(prefix="p12_", dir=a.workdir), "kill.db")
    marks = db_path + ".marks"
    for suffix in ("", "-wal", "-shm", ".marks", ".marks.err"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(db_path + suffix)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    log = Log(a.out + ".phase.log")
    stack = open(a.out + ".stack.txt", "w")
    faulthandler.dump_traceback_later(120, repeat=True, file=stack)   # parent stall -> stacks in file

    rng = random.Random(a.seed)
    sched = [("random", 0)] * a.random + [
        (f, rng.randint(1, 200)) for f in FAULTS for _ in range(a.per_fault)]
    rng.shuffle(sched)
    ver = Verifier(db_path)
    rk = {"idle": 0, "between": 0, "inflight": 0, "torn_begin": 0, "acked_torn": 0}
    injected = dict.fromkeys(FAULTS, 0)
    commits, total_acks, total_unacked, ac_unacked = [], 0, 0, 0
    tm = {"spawn_ready": 0.0, "kill_wait": 0.0, "verify": 0.0, "full": 0.0}
    failure, gens_done = None, 0
    t_start = time.time()
    wal_max, wal_trunc, free_min = 0, 0, None
    disk_dir = os.path.dirname(os.path.abspath(db_path))

    try:
        for gi, (kind, k) in enumerate(sched, 1):
            free = shutil.disk_usage(disk_dir).free
            free_min = free if free_min is None else min(free_min, free)
            if free < a.min_free_gb * 2 ** 30:
                raise RuntimeError("disk free %.2f GB < %.2f GB on %s: aborting" % (
                    free / 2 ** 30, a.min_free_gb, disk_dir))
            if time.time() - t_start > a.deadline_min * 60:
                raise RuntimeError("deadline %g min exceeded after %d generations" % (a.deadline_min, gi - 1))
            acks, end, inflight, t_ready, t_kill = run_gen(
                db_path, marks, kind, k, rng, a.max_delay)
            wal_max = max(wal_max, wal_bytes(db_path))   # what the killed writer left behind (before our open cleans it)
            t0 = time.time()
            unacked = ver.check(acks, end, inflight, kind)
            t_ver = time.time() - t0
            gens_done = gi
            if wal_bytes(db_path) > a.wal_cap_mb * 2 ** 20:
                # Safety net. After ver.check() every connection is closed, so SQLite checkpoints and deletes
                # the WAL on last close; a WAL still over the cap means a connection is pinning it. TRUNCATE
                # then reports busy and the run fails here, loudly, instead of thrashing the disk for an hour.
                checkpoint_truncate(db_path)
                wal_trunc += 1
            commits.append(len(acks) + unacked)
            total_acks += len(acks)
            total_unacked += unacked
            if kind == "random":
                rk[end] += 1
            else:
                injected[kind] += 1
                if kind == "after_commit":
                    ac_unacked += unacked
            tm["spawn_ready"] += t_ready
            tm["kill_wait"] += t_kill
            tm["verify"] += t_ver
            log("gen %d %s k=%d commits=%d end=%s unacked=%d t(ready/kill/verify)=%.2f/%.2f/%.3f"
                % (gi, kind, k, len(acks) + unacked, end, unacked, t_ready, t_kill, t_ver))
            if gi % a.full_every == 0:
                t0 = time.time()
                full_check(db_path)
                tm["full"] += time.time() - t0
                log("full check ok after gen %d (%.1fs total in full checks)" % (gi, tm["full"]))
        t0 = time.time()
        full_check(db_path)
        tm["full"] += time.time() - t0
    except (AssertionError, RuntimeError, sqlite3.Error, OSError) as e:
        failure = "%s: %s" % (type(e).__name__, e)
        log("FAILURE gen %d: %s" % (gens_done + 1, failure))
    finally:
        faulthandler.cancel_dump_traceback_later()
        stack.close()

    try:
        with opened(db_path) as con:
            db_count = one(con, "SELECT COUNT(*) FROM events")
    except sqlite3.Error:
        db_count = 0                                   # aborted before the first generation created the schema
    total_events = total_acks + total_unacked
    inflight_random = rk["inflight"] + rk["torn_begin"]
    pct = 100.0 * inflight_random / max(1, a.random)
    floors = {
        "total_events>=%d" % a.floor_events: total_events >= a.floor_events,
        "random_inflight_pct>=%g" % a.floor_inflight_pct: pct >= a.floor_inflight_pct,
        "committed_unacked>=per_fault": total_unacked >= a.per_fault,
        "committed_unacked<=generations": total_unacked <= len(sched),
        "after_commit_unacked==per_fault": ac_unacked == a.per_fault,
        "rollback_modes_verified==3*per_fault":
            sum(injected[f] for f in ROLLBACK_FAULTS) == 3 * a.per_fault,
        "total_events==ack_lines+committed_unacked==db_count==sum(heads)":
            total_events == db_count == sum(ver.hw.values()),
        "elapsed<=%gmin" % a.deadline_min: time.time() - t_start <= a.deadline_min * 60,
    }
    ok = failure is None and all(floors.values())
    report = {
        "pass": ok, "failure": failure, "seed": a.seed, "db": db_path,
        "generations": {"planned": len(sched), "completed": gens_done, "random": a.random,
                        "injected": injected},
        "counts": {"ack_lines": total_acks, "committed_unacked": total_unacked,
                   "total_events": total_events, "db_count": db_count,
                   "final_heads": ver.hw, "after_commit_unacked": ac_unacked},
        "commits_per_generation": ({"min": min(commits), "median": statistics.median(commits),
                                    "max": max(commits)} if commits else None),
        "random_kills": dict(rk, inflight_pct=round(pct, 1)),
        "floors": floors, "phase_seconds": {k: round(v, 2) for k, v in tm.items()},
        "resources": {"elapsed_s": round(time.time() - t_start, 1), "wal_bytes_max": wal_max,
                      "wal_truncations": wal_trunc, "wal_cap_mb": a.wal_cap_mb,
                      "db_bytes_final": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
                      "disk_free_min_bytes": free_min},
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
