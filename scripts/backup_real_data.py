"""D8 (boss ruling): back up the REAL legacy data before any P1.1 import.

Pulls logs/queue.json + the WebView localStorage files from the phone via run-as
(the debug build), computes sha256 + timestamps, writes tests/evidence/
real_data_backup.json. The install was an in-place update (same package, same
signing key, installed over the existing build) — app data survived.

Run when the phone is connected:  python scripts/backup_real_data.py
"""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.android import adb_helpers  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = os.path.join(REPO, "tests", "evidence")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not adb_helpers.serial():
        print(json.dumps({"ok": False, "error": "no adb device"}))
        return 1
    out_dir = os.path.join(EVIDENCE, "real_data_backup")
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    files = {}

    # 1. legacy queue.json (the durable queued messages)
    out, code = adb_helpers.adb("shell", "run-as", "com.sitecrew.ai",
                                "cat", "files/app/logs/queue.json", timeout=60)
    if code == 0 and out.strip():
        p = os.path.join(out_dir, "queue.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(out)
        files["queue.json"] = {"sha256": sha256_file(p), "bytes": len(out.encode())}

    # 2. WebView localStorage (sc_live_id / sc_recents live in the WebView leveldb)
    out, code = adb_helpers.adb(
        "shell", "run-as", "com.sitecrew.ai",
        "tar", "-cf", "-", "app_webview/Default/Local Storage", timeout=120)
    if code == 0 and len(out) > 100:
        p = os.path.join(out_dir, "local_storage.tar")
        with open(p, "wb") as f:
            f.write(out.encode("utf-8", errors="surrogateescape")
                    if isinstance(out, str) else out)
        files["local_storage.tar"] = {"sha256": sha256_file(p),
                                      "bytes": os.path.getsize(p)}

    # 3. the in-RAM transcript is NOT recoverable after a process death (boss
    #    correction accepted): P1.1's real corpus is queue.json + recents only.
    report = {
        "backed_up_at": now,
        "install_was_in_place_update": True,
        "in_ram_transcript": "GONE — replacing the package killed the process "
                             "(boss correction accepted); not part of the corpus",
        "files": files,
        "p1_1_real_corpus": "queue.json + localStorage recents only",
    }
    os.makedirs(EVIDENCE, exist_ok=True)
    with open(os.path.join(EVIDENCE, "real_data_backup.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
