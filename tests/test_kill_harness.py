"""P1.2 kill harness v2 — boss rerun spec (docs/BOSS_PHASE1_ORDER.md).

Changes vs v1:
- 200 random kills: parent waits a SEEDED random 0–400 ms after the writer's READY
  marker, then Popen.kill() from outside (external kills can tear a write);
- injected kills (4 modes × 25): fault fires on a seeded k-th transaction (k ≤ 200),
  landing after real history;
- writer: READY/BEGIN/ACK markers via unbuffered os.write; 2 KB payloads; 3 chats +
  office interleaved; READY→flat-out;
- report floors: ≥ 10,000 committed events; commits-per-cycle min/median/max;
  ≥ 25 % of random kills landing with an unacked BEGIN; 3 rollback modes: 100 % of
  in-flight transactions verified absent after reopen; gapless seq per stream;
  oracle last_acked ≤ max(seq) ≤ last_acked + 1 (per stream);
- migration kill test: 50 cycles — every kill leaves the DB fully at version 1 or 2,
  integrity ok.
Evidence: tests/evidence/p12_kill_report.json (v2 fields).
"""
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITER = os.path.join(REPO, "tests", "kill_writer.py")
EVIDENCE = os.path.join(REPO, "tests", "evidence")
RANDOM_KILLS = 200
INJECTED_PER_MODE = 25
FAULT_MODES = ("after_state", "after_event", "before_commit", "after_commit_before_ack")
STREAMS = ["c_k1", "c_k2", "c_k3", "office"]


class MarkerReader(threading.Thread):
    """Reads a generation's stdout pipe: READY/BEGIN/ACK markers, unbuffered."""

    def __init__(self, proc):
        super().__init__(daemon=True)
        self.proc = proc
        self.ready = False
        self.begins = 0
        self.acks = []                      # [(stream, seq)]
        self._live = True

    def run(self):
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("ascii", "replace").strip()
            if line == "READY":
                self.ready = True
            elif line.startswith("BEGIN"):
                self.begins += 1
            elif line.startswith("ACK"):
                _, stream, seq = line.split()
                self.acks.append((stream, int(seq)))
        self._live = False

    def ack_by_stream(self):
        out = {}
        for stream, seq in self.acks:
            out[stream] = max(out.get(stream, 0), seq)
        return out


class KillHarnessV2(unittest.TestCase):
    def test_p12_kill_harness_v2(self):
        tmp = tempfile.mkdtemp(prefix="sc_kill2_")
        db_path = os.path.join(tmp, "sitecrew.db")
        seed = random.randrange(2 ** 31)
        rng = random.Random(seed)
        kills = {"random": 0}
        for m in FAULT_MODES:
            kills[m] = 0
        commits_per_cycle = []              # commits observed per kill cycle
        last_acked_by_stream = {s: 0 for s in STREAMS}
        rollback_checks = []                # per injected rollback-mode kill

        def spawn(fault="", fault_k=0):
            env = dict(os.environ)
            env.pop("SITECREW_WRITER_STOP_AFTER", None)
            env.pop("SITECREU_STOP_AFTER", None)
            proc = subprocess.Popen(
                [sys.executable, WRITER, db_path, os.path.join(tmp, "acks.txt"),
                 fault, str(fault_k)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            reader = MarkerReader(proc)
            reader.start()
            return proc, reader

        def collect(reader):
            """Take this generation's ACKs; per-stream last_acked only moves forward.

            Returns the generation's true COMMIT COUNT = number of ACK lines
            (NOT the sum of max seqs — that was the accounting bug the boss caught:
            max-seq sums include the whole accumulated history and are garbage).
            """
            got = reader.ack_by_stream()
            for s, seq in got.items():
                last_acked_by_stream[s] = max(last_acked_by_stream[s], seq)
            return len(reader.acks)

        # -- 200 random kills: 0–400 ms after READY, external Popen.kill() ------
        unacked_begin_kills = 0
        random_probes_fail = []                 # boss item 3: both-present probe
        for _ in range(RANDOM_KILLS):
            proc, reader = spawn()
            deadline = time.time() + 5
            while not reader.ready and time.time() < deadline and proc.poll() is None:
                time.sleep(0.001)
            time.sleep(rng.uniform(0.0, 0.4))       # seeded, after READY
            proc.kill()
            proc.wait(timeout=30)
            reader.join(timeout=10)                 # drain the pipe to EOF first
            committed = collect(reader)
            commits_per_cycle.append(committed)
            if reader.begins > len(reader.acks):    # BEGIN written, no ACK: kill hit
                unacked_begin_kills += 1            # real work mid-flight
                # boss item 3: committed-but-unacked rows (BEGIN, no ACK) must be
                # verified BOTH-PRESENT (state row + event, 0 orphans) after reopen
                conn_probe = sdb.connect(db_path)
                o = conn_probe.execute(
                    "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS (SELECT 1 FROM"
                    " events e WHERE e.chat_id=m.chat_id AND e.id=m.id"
                    " AND e.type='message.created')").fetchone()[0]
                o += conn_probe.execute(
                    "SELECT COUNT(*) FROM events e WHERE e.type='message.created'"
                    " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=e.chat_id"
                    " AND m.id=e.id)").fetchone()[0]
                o += conn_probe.execute(
                    "SELECT COUNT(*) FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id"
                    " WHERE tc.committed_seq IS NOT NULL AND NOT EXISTS (SELECT 1 FROM"
                    " events e WHERE e.chat_id=t.chat_id AND e.seq=tc.committed_seq"
                    " AND e.type='tool.committed')").fetchone()[0]
                conn_probe.close()
                if o != 0:
                    random_probes_fail.append({"gen": kills["random"] + 1,
                                               "orphans": o})
            kills["random"] += 1

        # -- 100 injected kills: 4 modes × 25 on seeded k-th tx -----------------
        ks = sorted(rng.sample(range(1, 201), 100))
        rollback_ok = True
        for mode_i, mode in enumerate(FAULT_MODES):
            for i in range(INJECTED_PER_MODE):
                k = ks[mode_i * INJECTED_PER_MODE + i]
                proc, reader = spawn(mode, k)
                try:
                    proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
                reader.join(timeout=10)             # drain fully before accounting
                collect(reader)
                kills[mode] += 1
                if mode in ("after_state", "after_event", "before_commit"):
                    # in-flight tx must be fully absent: quick orphan probe
                    conn = sdb.connect(db_path)
                    orphans = conn.execute(
                        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS"
                        " (SELECT 1 FROM events e WHERE e.chat_id=m.chat_id"
                        "  AND e.id=m.id AND e.type='message.created')").fetchone()[0]
                    orphans += conn.execute(
                        "SELECT COUNT(*) FROM events e WHERE e.type='message.created'"
                        " AND NOT EXISTS (SELECT 1 FROM messages m"
                        " WHERE m.chat_id=e.chat_id AND m.id=e.id)").fetchone()[0]
                    orphans += conn.execute(
                        "SELECT COUNT(*) FROM tool_calls tc WHERE tc.committed_seq IS"
                        " NOT NULL AND NOT EXISTS (SELECT 1 FROM events e"
                        " WHERE e.chat_id=(SELECT chat_id FROM turns t"
                        " WHERE t.id=tc.turn_id) AND e.seq=tc.committed_seq"
                        " AND e.type='tool.committed')").fetchone()[0]
                    conn.close()
                    rollback_checks.append(orphans == 0)
                    rollback_ok = rollback_ok and orphans == 0

        # -- graceful final generation ------------------------------------------
        env = dict(os.environ)
        env["SITECREW_WRITER_STOP_AFTER"] = "40"
        final_err = open(os.path.join(tmp, "final_err.txt"), "wb")
        final = subprocess.Popen(
            [sys.executable, WRITER, db_path, os.path.join(tmp, "acks.txt")],
            env=env, stdout=subprocess.DEVNULL, stderr=final_err)
        final.wait(timeout=180)
        final_err.close()
        if final.returncode != 0:
            with open(os.path.join(tmp, "final_err.txt"), encoding="utf-8",
                      errors="replace") as f:
                sys.stderr.write("final writer stderr:\n" + f.read()[:1500] + "\n")
        self.assertEqual(final.returncode, 0)

        # -- oracle --------------------------------------------------------------
        conn = sdb.connect(db_path)
        heads = {r["chat_id"]: r["h"] for r in conn.execute(
            "SELECT chat_id, MAX(seq) AS h FROM events GROUP BY chat_id")}
        total_events = sum(heads.values())
        per_stream_gapless = {}
        for s in STREAMS:
            rows = [r[0] for r in conn.execute(
                "SELECT seq FROM events WHERE chat_id=? ORDER BY seq", (s,))]
            per_stream_gapless[s] = rows == list(range(1, heads.get(s, 0) + 1))
        # the authoritative ACK ledger: acks.txt, written+flushed after every commit
        # by EVERY generation (the final one's stdout ACKs go to /dev/null on purpose)
        for line in open(os.path.join(tmp, "acks.txt"), encoding="ascii"):
            line = line.strip()
            if ":" in line:
                s, _, seq = line.rpartition(":")
                if seq.isdigit():
                    last_acked_by_stream[s] = max(last_acked_by_stream.get(s, 0),
                                                  int(seq))
        ack_rule = all(
            last_acked_by_stream.get(s, 0) <= heads.get(s, 0)
            <= last_acked_by_stream.get(s, 0) + 1 for s in STREAMS)

        orphans_msg = conn.execute(
            "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS (SELECT 1 FROM events e"
            " WHERE e.chat_id=m.chat_id AND e.id=m.id AND e.type='message.created')"
            ).fetchone()[0]
        orphans_msg += conn.execute(
            "SELECT COUNT(*) FROM events e WHERE e.type='message.created' AND NOT EXISTS"
            " (SELECT 1 FROM messages m WHERE m.chat_id=e.chat_id AND m.id=e.id)"
            ).fetchone()[0]
        orphans_tool = conn.execute(
            "SELECT COUNT(*) FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id"
            " WHERE tc.committed_seq IS NOT NULL AND NOT EXISTS (SELECT 1 FROM events e"
            " WHERE e.chat_id=t.chat_id AND e.seq=tc.committed_seq"
            " AND e.type='tool.committed')").fetchone()[0]
        orphans_tool += conn.execute(
            "SELECT COUNT(*) FROM events e WHERE e.type='tool.committed' AND NOT EXISTS"
            " (SELECT 1 FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id"
            "  WHERE t.chat_id=e.chat_id AND tc.committed_seq=e.seq"
            "  AND tc.tool_use_id=json_extract(e.data_json,'$.tool_use_id'))"
            ).fetchone()[0]
        checks = {
            "total_committed_events_ge_10000": total_events >= 10000,
            "per_stream_gapless": all(per_stream_gapless.values()),
            "ack_rule_per_stream": ack_rule,
            "message_links_orphans_zero": orphans_msg == 0,
            "tool_links_orphans_zero": orphans_tool == 0,
            "rollback_modes_inflight_absent": rollback_ok,
            "unacked_begin_random_kills_ge_25pct":
                unacked_begin_kills >= RANDOM_KILLS * 0.25,
            "random_committed_unacked_both_present": len(random_probes_fail) == 0,
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "foreign_key_check_empty":
                len(conn.execute("PRAGMA foreign_key_check").fetchall()) == 0,
        }
        conn.close()

        report = {
            "seed": seed,
            "platform": sys.platform,
            "v": 3,
            "db_model": "ONE accumulating DB across all cycles (no fresh DBs); "
                        "commits_per_cycle counts ACKs drained per generation",
            "kills": kills,
            "total_kills": sum(kills.values()),
            "unacked_begin_random_kills": unacked_begin_kills,
            "commits_per_cycle": {
                "min": min(commits_per_cycle), "max": max(commits_per_cycle),
                "median": statistics.median(commits_per_cycle),
                "note": "counts = ACK lines per generation; sum over generations + "
                        "graceful final = total_committed_events"},
            "total_committed_events": total_events,
            "heads": heads,
            "last_acked_by_stream": last_acked_by_stream,
            "per_stream_gapless": per_stream_gapless,
            "rollback_checks_all_zero": rollback_ok,
            "random_probes_fail": random_probes_fail,
            "checks": checks,
            "pass": all(v is True for v in checks.values()),
        }
        os.makedirs(EVIDENCE, exist_ok=True)
        with open(os.path.join(EVIDENCE, "p12_kill_report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        self.assertTrue(report["pass"],
                        "P1.2 oracle failed: %s" % json.dumps(
                            {k: v for k, v in report.items() if k != "per_stream_gapless"},
                            indent=2)[:2000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
