"""Boss P1.3-device condition: measure idle-park CPU on the phone.

The 0.25 s park slices wake the server 4x/second per parked poll. This runs
on the real device:
  baseline ~3 min  — no parked poll (app idles)
  park     10 min  — one client parked continuously (wait=25, immediate re-park)
CPU% is (utime+stime ticks)/USER_HZ per wall second of the app process,
sampled every 5 s via run-as cat /proc/<pid>/stat (same uid: readable).
Verdict belongs to the boss: negligible -> keep 0.25 s slices; else 1 s
slices + cid supersede.
Evidence: tests/evidence/idle_park_cpu_report.json
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from scripts.android import adb_helpers  # noqa: E402

BASE = "http://127.0.0.1:18110"
PKG = "com.sitecrew.ai"
USER_HZ = 100                            # Android kernel: CLK_TCK = 100


def find_main_pid():
    """The MAIN python process of the app (informational only now — sampling
    goes through the in-app proc-stat endpoint; the server IS the measured
    process). pidof can return several pids; pick the one whose cmdline
    contains 'python'."""
    out, _ = adb_helpers.adb("shell", "pidof", PKG, timeout=15)
    for p in out.split():
        if not p.isdigit():
            continue
        cmd, _ = adb_helpers.adb("shell", "cat", "/proc/%s/cmdline" % p,
                                 timeout=15)
        if "python" in cmd:
            return p
    return out.split()[0] if out.split() else None


def sample_ticks(pid=None):
    """(utime+stime) ticks of the server process, read IN-APP via the
    debug-gated /api/v2/debug/proc-stat endpoint (run-as reads of
    /proc/<pid>/stat are DENIED on Android 14 — measured on the device)."""
    import urllib.request
    try:
        with urllib.request.urlopen(BASE + "/api/v2/debug/proc-stat",
                                    timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        return int(d["utime"]) + int(d["stime"])
    except Exception:
        return None


def device_state():
    """Boss rule: screen + charging (+thermal) must be recorded and match
    between the baseline and parked windows."""
    out = {}
    bat, _ = adb_helpers.adb("shell", "dumpsys", "battery", timeout=20)
    for key, pat in (("charging_usb", r"USB powered: (true|false)"),
                     ("charging_ac", r"AC powered: (true|false)"),
                     ("level", r"level: (\d+)"),
                     ("status", r"status: (\d+)")):
        m = re.search(pat, bat)
        if m:
            out[key] = m.group(1)
    pw, _ = adb_helpers.adb("shell", "dumpsys", "power", timeout=20)
    m = re.search(r"mWakefulness=(\w+)", pw)
    out["wakefulness"] = m.group(1) if m else "unknown"
    th, _ = adb_helpers.adb("shell", "dumpsys", "thermalservice", timeout=20)
    m = re.search(r"mIsStatusOverride=(\w+)|ThermalStatus:(\w+)", th)
    out["thermal_hint"] = (m.group(1) or m.group(2)) if m else "unavailable"
    return out


def measure(phase_name, seconds, interval=5.0, park=False):
    ticks0 = sample_ticks(PID)
    wall0 = time.time()
    samples = []
    stop = threading.Event()
    state = device_state()                 # captured per window (boss rule)

    def parker():
        head = 0
        try:
            head = json.loads(urllib.request.urlopen(
                BASE + "/api/v2/debug/store-state?db=main", timeout=10)
                .read().decode())["head"]
        except Exception:
            pass
        while not stop.is_set():
            try:
                urllib.request.urlopen(
                    BASE + "/api/v2/poll?streams=c_debug:%d&wait=25" % head,
                    timeout=30)
            except Exception:
                pass

    threads = []
    if park:
        for _ in range(2):                 # two parked clients (ref + chaos stand-in)
            t = threading.Thread(target=parker, daemon=True)
            t.start()
            threads.append(t)
    next_t = time.time()
    prev = (time.time(), ticks0)
    while time.time() - wall0 < seconds:
        next_t += interval
        time.sleep(max(0.0, next_t - time.time()))
        now = time.time()
        t = sample_ticks(PID)
        if t is not None and prev is not None:
            dt = now - prev[0]
            if dt > 0:
                per_interval = ((t - prev[1]) / USER_HZ) / dt * 100.0
                samples.append((now, t, round(per_interval, 3)))
            prev = (now, t)
    stop.set()
    for t in threads:
        pass                               # daemons; they die with the phase flag
    rates = [s[2] for s in samples]
    rates_sorted = sorted(rates)
    p95 = rates_sorted[min(len(rates_sorted) - 1,
                           int(len(rates_sorted) * 0.95))] if rates_sorted else None
    avg = round(sum(rates) / len(rates), 3) if rates else None
    return {"phase": phase_name, "wall_s": round(seconds, 1),
            "cpu_pct_avg": avg, "cpu_pct_p95": p95,
            "intervals": len(rates),
            "state": state,
            "error": None if rates else "sampling failed"}


def main():
    global PID
    serial = adb_helpers.serial()
    if not serial:
        print(json.dumps({"error": "no adb device"}))
        return 1
    os.environ["ANDROID_SERIAL"] = serial
    adb_helpers.forward(18110)
    PID = find_main_pid()
    if not PID:
        print(json.dumps({"error": "app process not found"}))
        return 1
    # prove the sampling works BEFORE spending 13 minutes
    probe = sample_ticks(PID)
    if probe is None:
        print(json.dumps({"error": "proc-stat sampling probe failed "
                                  "(debug endpoint unreachable?)"}))
        return 1
    print("sampling via in-app proc-stat endpoint, probe ticks %s"
          % probe, flush=True)

    print("baseline (3 min, no parked poll)...", flush=True)
    base = measure("baseline", 180)
    print("baseline:", base, flush=True)
    print("park (10 min, 2 parked clients at wait=25)...", flush=True)
    park = measure("park", 600, park=True)
    print("park:", park, flush=True)

    delta_avg = (park.get("cpu_pct_avg") or 0) - (base.get("cpu_pct_avg") or 0)
    # boss verdict rule (pre-registered): avg delta <= 1% AND p95 <= 3%,
    # with device states matching between the windows
    states_match = (json.dumps(base.get("state", {}), sort_keys=True) ==
                    json.dumps(park.get("state", {}), sort_keys=True))
    rule = {
        "avg_delta_pct": round(delta_avg, 2),
        "park_p95_pct": park.get("cpu_pct_p95"),
        "states_match": states_match,
        "keep_025s_slices": (delta_avg <= 1.0
                             and (park.get("cpu_pct_p95") or 99) <= 3.0
                             and states_match),
        "else": "1 s slices + cid supersede (then re-run the storm + slot-freed test)",
    }
    report = {
        "device": {"serial_sha256": __import__("hashlib").sha256(
            serial.encode()).hexdigest()},
        "park_slices_s": 0.25,
        "parked_clients": 2,
        "baseline": base,
        "park": park,
        "verdict_rule": rule,
        "verdict_note": ("machine-generated against the boss's pre-registered "
                         "rule; the boss confirms."),
        "user_hz_assumption": USER_HZ,
    }
    out = os.path.join(REPO, "tests", "evidence", "idle_park_cpu_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
