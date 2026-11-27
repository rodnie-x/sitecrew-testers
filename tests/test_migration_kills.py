"""Migration kill test (boss rerun spec): 50 kill cycles during open_db migration.

Every kill must leave the DB fully at the old version (0/1) or fully at
version 2, with integrity_check ok — never torn. Cycles where cycle % 10 == 0
use a deterministic AFTER-COMMIT injection point (boss note on the PC leg:
evidence that a clean v2 state survives a kill): the migrator prints
MIG_COMMITTED after the migration transaction commits, the test kills at that
exact point, and the oracle then requires version 2 with all v2 tables.
Evidence: tests/evidence/migration_kill_report.json
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATOR = os.path.join(REPO, "tests", "migration_writer.py")
CYCLES = 50


class MigrationKills(unittest.TestCase):
    def test_50_migration_kill_cycles(self):
        big_drive = "D:/" if os.path.exists("D:/") else None
        tmp = tempfile.mkdtemp(prefix="sc_mig_", dir=big_drive)
        seed = random.randrange(2 ** 31)
        rng = random.Random(seed)
        results = []
        for cycle in range(CYCLES):
            db_path = os.path.join(tmp, "mig_%d.db" % cycle)   # fresh DB every cycle
            mode = "after" if cycle % 10 == 0 else "random"
            after_commit = False
            if mode == "after":
                proc = subprocess.Popen(
                    [sys.executable, MIGRATOR, db_path, "after"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
                marker = proc.stdout.readline()   # blocks until the commit marker
                after_commit = marker.startswith("MIG_COMMITTED")
                if not after_commit:
                    proc.kill()
                    proc.wait(timeout=30)
                    self.fail("after-commit migrator produced no MIG_COMMITTED "
                              "marker (got: %r)" % marker[:120])
            else:
                proc = subprocess.Popen(
                    [sys.executable, MIGRATOR, db_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(rng.uniform(0.0, 0.05))                 # kill mid-migration
            proc.kill()
            proc.wait(timeout=30)
            conn = sdb.connect(db_path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            ok = (version in (0, 1, 2) and integrity == "ok"
                  and ((version == 2 and {"chats", "events", "brain_entries",
                                          "cases"} <= tables)
                       or (version in (0, 1) and "brain_entries" not in tables
                           and (version == 0 or "chats" in tables))))
            if mode == "after" and after_commit:
                ok = ok and version == 2      # committed migration survived
            results.append({"cycle": cycle, "mode": mode, "version": version,
                            "integrity": integrity, "ok": ok})
            self.assertTrue(ok, "kill cycle %d left version=%s integrity=%s tables=%s"
                            % (cycle, version, integrity, sorted(tables)[:8]))
        report = {"seed": seed, "cycles": CYCLES, "results": results, "pass": True}
        os.makedirs(os.path.join(REPO, "tests", "evidence"), exist_ok=True)
        with open(os.path.join(REPO, "tests", "evidence",
                               "migration_kill_report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
