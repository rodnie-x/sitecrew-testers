"""Migration subprocess for the migration kill test: runs open_db (0 → 1 → 2).

argv[2] == "after": deterministic AFTER-COMMIT injection point (boss note on
the PC leg) — prints MIG_COMMITTED once the migration transaction has
committed, then holds the process so the test can kill at that exact point,
evidencing that a clean v2 state survives a kill.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sitecrew.store import db as sdb  # noqa: E402

if __name__ == "__main__":
    conn, version = sdb.open_db(sys.argv[1])
    conn.close()
    print("MIG_COMMITTED %d" % version, flush=True)
    if len(sys.argv) > 2 and sys.argv[2] == "after":
        time.sleep(120)   # hold: the test kills after seeing the marker
