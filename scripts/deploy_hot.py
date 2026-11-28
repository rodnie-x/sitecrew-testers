"""Hash-verified hot-deploy for the installed Android APK (PC driver only).

Deploys exact source bytes into the app-private `files/app/` tree, verifies the
on-device SHA-256 hashes, and optionally restarts the app. Every step fails
closed: missing/outside files, failed push/copy/backup/hash, mixed multi-file
deploys, or a stale process after "restart" are all reported as failures.

Examples:
  python scripts/deploy_hot.py --dry-run server/app.py
  python scripts/deploy_hot.py --restart --backup server/app.py sitecrew/api_v2.py
  python scripts/deploy_hot.py --report tests/evidence/deploy_last.json -- server/app.py

This is a deployment tool, not permission to change frozen store/event semantics.
"""
import argparse
import datetime
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from scripts.android import adb_helpers  # noqa: E402

PKG = adb_helpers.PKG
ACTIVITY = adb_helpers.ACTIVITY
TMP_PREFIX = "/data/local/tmp/sc_hot_"
APP_ROOT = "files/app"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def resolve_local(rel):
    """Resolve one repo-relative path without allowing symlink/drive escape."""
    if not rel or os.path.isabs(rel):
        return None, "path must be repo-relative"
    local_abs = os.path.realpath(os.path.join(REPO, rel))
    try:
        inside = os.path.commonpath([REPO, local_abs]) == REPO
    except ValueError:
        inside = False
    if not inside:
        return None, "resolved path escapes the repository"
    if not os.path.isfile(local_abs):
        return None, "missing local file"
    return local_abs, None


def run_adb(serial, *args, timeout=60):
    cmd = [adb_helpers.adb_path(), "-s", serial, *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return {"rc": p.returncode, "stdout": (p.stdout or "")[-1200:],
            "stderr": (p.stderr or "")[-1200:]}


def serial_hash(serial):
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]


def quote(value):
    return shlex.quote(str(value))


def shell_run_as(serial, command, timeout=60):
    return run_adb(serial, "shell", "run-as", PKG, "sh", "-c", command,
                   timeout=timeout)


def remote_hash(serial, remote_rel):
    """Hash one app-private file. A malformed/absent hash is explicit failure."""
    target = posixpath.join(APP_ROOT, remote_rel)
    out = shell_run_as(serial, "sha256sum " + quote(target))
    if out["rc"]:
        return {"ok": False, "exists": False, "error": out}
    line = out["stdout"].strip().splitlines()[0] if out["stdout"].strip() else ""
    got, _, name = line.partition(" ")
    got = got.strip().lower()
    if not SHA_RE.fullmatch(got):
        return {"ok": False, "exists": False, "error": "malformed sha256 output",
                "line": line}
    return {"ok": True, "exists": True, "sha256": got, "line": line}


def backup_remote(serial, remote_rel, pre_hash):
    """Keep one pristine backup; never overwrite an existing backup."""
    target = posixpath.join(APP_ROOT, remote_rel)
    bak = target + ".pre-hot.bak"
    existing = remote_hash(serial, bak)
    if existing.get("ok"):
        if existing["sha256"] != pre_hash:
            return {"ok": False, "status": "existing-backup-mismatch",
                    "backup": bak, "error": existing}
        return {"ok": True, "status": "already-present", "backup": bak,
                "sha256": existing["sha256"]}
    cp = shell_run_as(serial, "cp %s %s" % (quote(target), quote(bak)))
    if cp["rc"]:
        return {"ok": False, "status": "copy-failed", "backup": bak, "error": cp}
    check = remote_hash(serial, bak)
    if not check.get("ok") or check["sha256"] != pre_hash:
        return {"ok": False, "status": "backup-hash-mismatch", "backup": bak,
                "error": check}
    return {"ok": True, "status": "created", "backup": bak,
            "sha256": check["sha256"]}


def deploy_one(serial, local_rel, local_abs, local_sha):
    remote_rel = os.path.relpath(local_abs, REPO).replace("\\", "/")
    remote_rel = posixpath.normpath(remote_rel)
    target = posixpath.join(APP_ROOT, remote_rel)
    staged = target + ".new"
    tmp = TMP_PREFIX + ("%d_%s" % (os.getpid(), os.path.basename(remote_rel)))

    push = run_adb(serial, "push", local_abs, tmp)
    if push["rc"]:
        return {"ok": False, "local": local_rel, "error": push}
    cp = shell_run_as(serial, "cp %s %s" % (quote(tmp), quote(staged)))
    if cp["rc"]:
        run_adb(serial, "shell", "rm", "-f", tmp)
        return {"ok": False, "local": local_rel, "error": cp}
    mv = shell_run_as(serial, "mv -f %s %s" % (quote(staged), quote(target)))
    rm = run_adb(serial, "shell", "rm", "-f", tmp)
    if mv["rc"]:
        return {"ok": False, "local": local_rel, "error": mv}
    check = remote_hash(serial, remote_rel)
    if not check.get("ok") or check["sha256"] != local_sha:
        return {"ok": False, "local": local_rel, "error": check,
                "expected": local_sha, "rm": rm}
    return {"ok": True, "local": local_rel, "remote": remote_rel,
            "sha256": check["sha256"], "rm": rm}


def pids(serial):
    out = run_adb(serial, "shell", "pidof", PKG, timeout=15)
    if out["rc"]:
        return []
    return [p for p in out["stdout"].split() if p.isdigit()]


def forward(serial, local=18110, remote=8080):
    return run_adb(serial, "forward", "tcp:%d" % local, "tcp:%d" % remote)


def wait_server(base, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1)
    return False


def restart_and_verify(serial, report, base):
    before = pids(serial)
    fwd = forward(serial)
    stop = run_adb(serial, "shell", "am", "force-stop", PKG)
    launch = run_adb(serial, "shell", "am", "start", "-n", ACTIVITY)
    reachable = wait_server(base)
    after = pids(serial)
    fresh = bool(after) and not (set(before) & set(after))
    result = {
        "pids_before": before,
        "pids_after": after,
        "pids_changed": fresh,
        "forward": fwd,
        "force_stop": stop,
        "launch": launch,
        "server_reachable": reachable,
    }
    result["ok"] = (fwd["rc"] == 0 and stop["rc"] == 0 and launch["rc"] == 0
                    and reachable and fresh
                    and "Error" not in launch.get("stdout", "")
                    and "Error" not in launch.get("stderr", ""))
    report["restart"] = result
    return result["ok"]


def perform_deploy(report, args, serial, prepared_override=None):
    prepared = prepared_override if prepared_override is not None else []
    if prepared_override is None:
        for rel in args.paths:
            local_abs, error = resolve_local(rel)
            if error:
                report["files"].append({"ok": False, "local": rel,
                                        "error": error})
                report["ok"] = False
                return False
            with open(local_abs, "rb") as f:
                data = f.read()
            prepared.append({"local": rel, "local_abs": local_abs,
                             "sha256": sha256_bytes(data), "bytes": len(data)})

    # Preflight every backup before any file is overwritten.
    backups = {}
    if args.backup:
        for item in prepared:
            remote_rel = os.path.relpath(item["local_abs"], REPO).replace("\\", "/")
            remote_rel = posixpath.normpath(remote_rel)
            target = posixpath.join(APP_ROOT, remote_rel)
            pre = remote_hash(serial, target)
            if not pre.get("ok"):
                report["files"].append({
                    "ok": False, "local": item["local"],
                    "error": "target missing or unreadable before backup",
                    "pre_hash": pre,
                })
                return False
            backup = backup_remote(serial, remote_rel, pre["sha256"])
            backups[item["local"]] = backup
            report["files"].append({"local": item["local"], **backup})
            if not backup["ok"]:
                report["ok"] = False
                return False

    for item in prepared:
        item["deploy"] = deploy_one(serial, item["local"], item["local_abs"],
                                    item["sha256"])
        report["files"].append(item["deploy"])
        if not item["deploy"]["ok"]:
            report["ok"] = False
            return False

    report["deployed"] = [
        {"local": item["local"], "remote": item["deploy"]["remote"],
         "sha256": item["deploy"]["sha256"], "bytes": item["bytes"]}
        for item in prepared
    ]
    if backups:
        report["backups"] = {local: backup["backup"]
                             for local, backup in backups.items()}
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base", default="http://127.0.0.1:18110")
    args = ap.parse_args()

    report = {
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "restart": args.restart,
        "backup": args.backup,
        "dry_run": args.dry_run,
        "paths": args.paths,
        "files": [],
        "ok": False,
    }
    selected = None
    try:
        serials = adb_helpers.devices()
        if len(serials) != 1:
            report["error"] = ("expected exactly one adb device in state device; "
                               "got %r" % len(serials))
        else:
            selected = serials[0]
            report["device_count"] = 1
            report["serial_sha256_16"] = serial_hash(selected)
            report["ok"] = True
            report["files"] = []
            for rel in args.paths:
                local_abs, error = resolve_local(rel)
                if error:
                    report["files"].append({"ok": False, "local": rel,
                                            "error": error})
                    report["ok"] = False
                else:
                    with open(local_abs, "rb") as f:
                        data = f.read()
                    report["files"].append({
                        "ok": True, "local": rel,
                        "sha256": sha256_bytes(data), "bytes": len(data),
                    })

            if report["ok"] and not args.dry_run:
                report["ok"] = perform_deploy(report, args, selected)
            if report["ok"] and args.restart and not args.dry_run:
                report["ok"] = restart_and_verify(selected, report, args.base)
    except Exception as exc:
        report["ok"] = False
        report["exception"] = "%s: %s" % (type(exc).__name__, str(exc)[:500])
    finally:
        if args.report:
            os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
            temporary = args.report + ".tmp"
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            os.replace(temporary, args.report)
        print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
