"""Hash-verified hot-deploy for the installed Android APK (PC driver only).

This deploys exact source bytes into the app-private `files/app/` tree, verifies
the on-device SHA-256 hashes, and optionally restarts the app. It never invents
success: every step is checked and the JSON report records both PASS and FAIL.

Examples:
  python scripts/deploy_hot.py --dry-run server/app.py
  python scripts/deploy_hot.py --restart server/app.py sitecrew/api_v2.py cpu_diag.py
  python scripts/deploy_hot.py --report tests/evidence/deploy_last.json -- server/app.py

Do not use this to change store/event semantics without a boss-approved plan.
"""
import argparse
import datetime
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from scripts.android import adb_helpers  # noqa: E402

PKG = "com.sitecrew.ai"
ACTIVITY = PKG + "/org.kivy.android.PythonActivity"
TMP_PREFIX = "/data/local/tmp/sc_hot_"
APP_ROOT = "files/app"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_adb(*args, timeout=60):
    p = subprocess.run([adb_helpers.adb_path(), *args], capture_output=True,
                       text=True, timeout=timeout, encoding="utf-8",
                       errors="replace")
    return {"rc": p.returncode, "stdout": (p.stdout or "")[-1000:],
            "stderr": (p.stderr or "")[-1000:]}


def device_hash(remote_rel):
    """Hash one app-private file. Missing files are explicit, not empty hashes."""
    out = run_adb("shell", "run-as", PKG, "sha256sum",
                  posixpath.join(APP_ROOT, remote_rel))
    if out["rc"]:
        return {"ok": False, "error": out}
    line = out["stdout"].strip().splitlines()[0] if out["stdout"].strip() else ""
    got, _, name = line.partition(" ")
    return {"ok": bool(got), "sha256": got, "line": line}


def backup_remote(remote_rel):
    target = posixpath.join(APP_ROOT, remote_rel)
    bak = target + ".pre-hot.bak"
    out = run_adb("shell", "run-as", PKG, "sh", "-c",
                  "'[ -f %s ] && cp %s %s || true'" % (target, target, bak))
    return {"ok": out["rc"] == 0, "rc": out["rc"], "backup": bak}


def deploy_one(local_rel):
    """Push then run-as cp one repo-relative file into its same app-relative path."""
    local_abs = os.path.normpath(os.path.join(REPO, local_rel))
    if not os.path.isfile(local_abs):
        return {"ok": False, "local": local_rel, "error": "missing local file"}
    if os.path.commonpath([REPO, local_abs]) != REPO:
        return {"ok": False, "local": local_rel, "error": "outside repo"}
    remote_rel = posixpath.normpath(
        local_abs[len(REPO) + 1:].replace("\\", "/"))
    tmp = TMP_PREFIX + os.path.basename(remote_rel)
    push = run_adb("push", local_abs, tmp)
    if push["rc"]:
        return {"ok": False, "local": local_rel, "error": push}
    target = posixpath.join(APP_ROOT, remote_rel)
    cp = run_adb("shell", "run-as", PKG, "cp", tmp, target)
    rm = run_adb("shell", "rm", tmp)
    if cp["rc"]:
        return {"ok": False, "local": local_rel, "error": cp}
    check = device_hash(remote_rel)
    if not check.get("ok") or check.get("sha256") != sha256_file(local_abs):
        return {"ok": False, "local": local_rel, "error": check,
                "expected": sha256_file(local_abs)}
    return {"ok": True, "local": local_rel, "remote": remote_rel,
            "sha256": check.get("sha256"), "push": push, "rm": rm}


def wait_server(base, timeout=60):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base", default="http://127.0.0.1:18110")
    args = ap.parse_args()

    serials = adb_helpers.devices()
    report = {
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "serials": serials,
        "restart": args.restart,
        "backup": args.backup,
        "paths": args.paths,
        "files": [],
    }
    if not serials:
        report["ok"] = False
        report["error"] = "no adb device in state device"
    elif args.dry_run:
        report["ok"] = True
        report["dry_run"] = True
        for rel in args.paths:
            local_abs = os.path.join(REPO, rel)
            report["files"].append({
                "local": rel, "exists": os.path.isfile(local_abs),
                "sha256": sha256_file(local_abs) if os.path.isfile(local_abs) else None,
                "size": os.path.getsize(local_abs) if os.path.isfile(local_abs) else None,
            })
    else:
        for rel in args.paths:
            if args.backup:
                remote_rel = posixpath.normpath(
                    os.path.relpath(os.path.join(REPO, rel),
                                    REPO).replace("\\", "/"))
                report["files"].append({"local": rel,
                                        **backup_remote(remote_rel)})
            item = deploy_one(rel)
            report["files"].append(item)
        if args.restart and all(x.get("ok") for x in report["files"]):
            stop = run_adb("shell", "am", "force-stop", PKG)
            launch = run_adb("shell", "am", "start", "-n", ACTIVITY)
            report["restart"] = {"force_stop": stop, "launch": launch,
                                 "server_reachable": wait_server(args.base)}
        report["ok"] = (bool(report["files"])
                        and all(x.get("ok") for x in report["files"])
                        and (not args.restart or report.get("restart", {}).get("server_reachable")))

    if args.report:
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
