"""FTS5 probe (blueprint §2.2 / boss order step 4): record SQLite version, FTS5
availability, WAL behavior — writes reports/fts5_probe.json.

Run on PC now; on the device it must run inside the app's own Python (Phase 1 device
leg) — the device result is recorded separately when the store ships in the APK.
"""
import json
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def probe():
    result = {"platform": sys.platform, "python": sys.version.split()[0]}
    result["sqlite_version"] = sqlite3.sqlite_version
    tmp = tempfile.mkdtemp(prefix="sc_fts5_")
    path = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(path)
    result["journal_mode_wal"] = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    compile_opts = [r[0] for r in conn.execute("PRAGMA compile_options")]
    result["fts5_in_compile_options"] = any(o == "ENABLE_FTS5" for o in compile_opts)
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(title, body)")
        conn.execute("INSERT INTO t(rowid, title, body) VALUES(1, 'hello', 'world')")
        hits = conn.execute("SELECT rowid FROM t WHERE t MATCH 'hello'").fetchall()
        result["fts5_create_and_match"] = len(hits) == 1
    except Exception as e:
        result["fts5_create_and_match"] = False
        result["fts5_error"] = str(e)[:200]
    conn.close()
    result["backend"] = "fts5" if result["fts5_create_and_match"] else "brain_terms"
    result["tested_at"] = "pc"
    return result


if __name__ == "__main__":
    r = probe()
    out = os.path.join(REPO, "reports")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "fts5_probe.json"), "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2)
    print(json.dumps(r, indent=2))
