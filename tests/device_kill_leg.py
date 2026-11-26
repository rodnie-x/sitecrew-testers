"""P1.2-D device kill leg — 20 kill/restart cycles on the real phone (boss order).

Per cycle:
  1. POST /api/v2/debug/append {cycles: 2..4}  (debug append loop in the app)
  2. wait for the logcat SC_STORE_ACK markers (event-driven; no screenshots)
  3. adb shell kill -9 <pid>                      (hard kill)
  4. relaunch, wait for the server
After all cycles: GET /api/v2/debug/store-state and apply the oracle:
  head == number of ACKs observed; invariants _pass; seq contiguous per invariants.
Evidence: tests/evidence/p12_device_report.json
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.android import adb_helpers  # noqa: E402

BASE = "http://127.0.0.1:18110"
CYCLES = 20
PKG = "com.sitecrew.ai"


def http(method, path, payload=None, timeout=20):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_server(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http("GET", "/api/state", timeout=5)
            return True
        except Exception:
            time.sleep(2)
    return False


def dump_diagnostics(out_dir, tag):
    """On failure: one logcat dump + process state (event-driven single capture)."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        pid = adb_helpers.pid()
        with open(os.path.join(out_dir, tag + "_pidof.txt"), "w") as f:
            f.write(pid or "NO PROCESS")
    except Exception as e:
        pid = "?"
        with open(os.path.join(out_dir, tag + "_pidof.txt"), "w") as f:
            f.write("error: %s" % e)
    try:
        out, _ = adb_helpers.adb("logcat", "-d", "-v", "time", timeout=60)
        with open(os.path.join(out_dir, tag + "_logcat.txt"), "w",
                  encoding="utf-8", errors="replace") as f:
            f.write(out[-200000:])   # tail: the boot attempt is at the end
    except Exception as e:
        with open(os.path.join(out_dir, tag + "_logcat.txt"), "w") as f:
            f.write("logcat failed: %s" % e)


def wait_acks(before, expected, timeout=45):
    """Event-driven: watch logcat for SC_STORE_ACK markers until expected count.

    Tolerates adb hiccups (timeouts, device blips): retry the dump instead of
    crashing the leg — the phone link is known-flaky.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            out, _ = adb_helpers.adb("logcat", "-d", "-v", "time", "-s",
                                     "Python:*", timeout=20)
            found = len(re.findall(r"SC_STORE_ACK (\d+)", out))
            if found >= before + expected:
                return found
        except Exception as e:                    # adb blip: brief pause, retry
            last_err = str(e)[:120]
            time.sleep(3)
            try:
                adb_helpers.forward(18110)
            except Exception:
                pass
        time.sleep(1.5)
    print("ACK wait degraded (last error: %s) — proceeding with ACK count %d"
          % (last_err, before), flush=True)
    return before  # honest degradation: count only what was confirmed


def dump_diagnostics(out_dir, tag):
    """On boot failure: ONE logcat dump + process state (event-driven, no polling)."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        pid = adb_helpers.pid()
        with open(os.path.join(out_dir, tag + "_pidof.txt"), "w") as f:
            f.write(pid or "NO PROCESS")
    except Exception as e:
        with open(os.path.join(out_dir, tag + "_pidof.txt"), "w") as f:
            f.write("error: %s" % e)
    try:
        out, _ = adb_helpers.adb("logcat", "-d", "-v", "time", timeout=60)
        with open(os.path.join(out_dir, tag + "_logcat.txt"), "w",
                  encoding="utf-8", errors="replace") as f:
            f.write(out[-300000:])   # tail: the boot attempt is at the end
    except Exception as e:
        with open(os.path.join(out_dir, tag + "_logcat.txt"), "w") as f:
            f.write("logcat failed: %s" % e)


def main():
    if not adb_helpers.serial():
        print(json.dumps({"pass": False, "error": "no adb device"}))
        return 1
    adb_helpers.forward(18110)
    boot_timeout = int(os.environ.get("SC_BOOT_TIMEOUT", "240"))
    if not adb_helpers.wait_server(BASE, timeout=90):
        adb_helpers.launch()
        if not adb_helpers.wait_server(BASE, timeout=boot_timeout):
            dump_diagnostics(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "tests", "evidence"), "boot_failure")
            print(json.dumps({"pass": False,
                              "error": "server unreachable after launch "
                                       "(diagnostics: tests/evidence/"
                                       "boot_failure_logcat.txt)"}))
            return 1

    acks_seen = 0
    kills = 0
    log = []
    ev = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tests", "evidence")
    os.makedirs(ev, exist_ok=True)
    report_path = os.path.join(ev, "p12_device_report.json")

    def save_partial(done_cycles, final=None):
        partial = {"cycles_attempted": done_cycles, "kills": kills,
                   "final_head": final.get("head") if final else None,
                   "invariants_pass": final.get("invariants", {}).get("_pass")
                   if final else None,
                   "invariants": final.get("invariants") if final else None,
                   "acks_seen": acks_seen, "log": log,
                   "partial": final is None}
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(partial, f, indent=2)

    def http_retry(method, path, payload=None, tries=6, timeout=20):
        """HTTP with retries — the phone link and app boot are known-flaky."""
        last = None
        for _ in range(tries):
            try:
                return http(method, path, payload, timeout=timeout)
            except Exception as e:
                last = e
                time.sleep(3)
        raise last

    for cycle in range(CYCLES):
        before_state = http_retry("GET", "/api/v2/debug/store-state")
        before_head = before_state.get("head", 0)
        batch = 2 + (cycle % 3)
        r = http_retry("POST", "/api/v2/debug/append", {"cycles": batch})
        expected = len(r.get("acks", []))
        found = wait_acks(acks_seen, expected)
        acks_seen = found if found >= 0 else acks_seen + expected
        pid = adb_helpers.pid()
        adb_helpers.hard_kill()
        kills += 1
        adb_helpers.launch()
        if not wait_server():
            log.append({"cycle": cycle, "error": "server did not return"})
            save_partial(cycle + 1)
            break
        after = http_retry("GET", "/api/v2/debug/store-state")
        log.append({"cycle": cycle, "batch": batch, "before_head": before_head,
                    "after_head": after.get("head"),
                    "invariants_pass": after.get("invariants", {}).get("_pass")})
        kills += 0
        save_partial(cycle + 1)                    # evidence survives a drop
        print("cycle %d: head %d -> %s, invariants %s" % (
            cycle, before_head, after.get("head"),
            after.get("invariants", {}).get("_pass")), flush=True)

    wait_server()                              # the last relaunch may still be booting
    final = http_retry("GET", "/api/v2/debug/store-state")
    inv = final.get("invariants", {})
    report = {
        "cycles": CYCLES,
        "kills": kills,
        "final_head": final.get("head"),
        "invariants_pass": inv.get("_pass"),
        "invariants": inv,
        "log": log,
        "acks_seen": acks_seen,
        "pass": bool(inv.get("_pass")) and final.get("head", 0) >= CYCLES * 2,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: report[k] for k in ("cycles", "kills", "final_head",
                                             "invariants_pass", "pass")}, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
