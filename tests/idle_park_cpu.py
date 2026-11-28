"""Idle-park CPU measurement (P1.3 device half) — review-hardened.

Measures the app's parked-poll CPU on the phone against the pre-registered
rule (park-minus-baseline avg <= 1% of one core, p95 <= 3%, states matching).
Sampling is IN-APP via the debug-gated /api/v2/debug/proc-stat endpoint
(run-as reads of /proc/<pid>/stat are DENIED on Android 14 — measured).

Review fixes applied:
  #5  parker: aborts the RUN when the head fetch fails (a head=0 fallback
      caused a 200-event refetch loop that produced the bogus 67% readings),
      sleeps 1 s on errors (no PC-side spin), sends a stable cid per parker,
      records per-parker stats (polls/nonempty/errors/mean duration); a park
      window is INVALID unless every parker had nonempty == 0 and a mean
      duration ~= wait (the only proof the clients were actually parked).
  #6/#7 device_state() runs BEFORE the first tick sample; the endpoint
      returns a monotonic timestamp + user_hz; dt comes from mono; the pid
      is checked on every sample (restart/OOM aborts the window); negative
      deltas are discarded; svc power stayon usb keeps the screen on.
  #8  evidence goes to a NEW timestamped filename (never clobbers committed
      evidence); PARK_SLICE_S is imported from the deployed api_v2.
  states_match compares only wakefulness + charging (level recorded
      start/end); state captured at BOTH window ends.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from scripts.android import adb_helpers  # noqa: E402
from sitecrew.api_v2 import PARK_SLICE_S  # noqa: E402  (review #8: no assumption)

BASE = "http://127.0.0.1:18110"
PKG = "com.sitecrew.ai"
USER_HZ_FALLBACK = 100                   # Android kernel ABI constant


def proc_sample():
    """(mono, ticks, pid, user_hz) from the in-app endpoint; None on failure."""
    try:
        with urllib.request.urlopen(BASE + "/api/v2/debug/proc-stat",
                                    timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (float(d["mono"]), int(d["utime"]) + int(d["stime"]),
                int(d["pid"]), int(d.get("user_hz", USER_HZ_FALLBACK)))
    except Exception:
        return None


def device_state():
    """Boss rule: screen + charging recorded per window (level start/end)."""
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
    return out


def parker(cid, head, log, stats, stop):
    """One parked client (review #5). NEVER falls back to cursor 0; sleeps
    1 s on errors; stats: polls / nonempty / errors / mean duration."""
    polls = nonempty = errors = 0
    durations = []
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(
                    BASE + "/api/v2/poll?streams=c_debug:%d&wait=25&cid=%s"
                    % (head, cid), timeout=30) as r:
                body = json.loads(r.read().decode("utf-8"))
            polls += 1
            durations.append(round(time.monotonic() - t0, 2))
            if body.get("events"):
                nonempty += 1
        except Exception as e:
            errors += 1
            log.append({"client": cid, "error": str(e)[:120],
                        "at": round(time.monotonic() - t0, 2)})
            if stop.wait(1.0):              # no PC-side spin
                break
            continue
        if stop.wait(max(0.05, 25.0 - (time.monotonic() - t0))):
            break
    stats.append({"cid": cid, "polls": polls, "nonempty": nonempty,
                  "errors": errors,
                  "mean_duration_s": round(sum(durations) / len(durations), 2)
                  if durations else None})


def rates_from(points):
    """Per-interval CPU% from (mono, ticks, pid, user_hz) samples."""
    rates = []
    for a, b in zip(points, points[1:]):
        dt = b[0] - a[0]
        dticks = b[1] - a[1]
        if dt > 0 and dticks >= 0:          # negative = pid reuse: discard
            rates.append(round((dticks / b[3]) / dt * 100.0, 3))
    return rates


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def window(name, seconds, clients, head, interval=5.0):
    """One measurement window. INVALID unless: proc-stat answered at both
    boundaries, the pid never changed, ticks never went backwards, and (for
    park windows) every parker had nonempty == 0 with a mean duration ~= 25 s
    (review #5: the only proof the clients were actually parked)."""
    log = []
    stats = []
    stop = threading.Event()
    threads = []
    state_start = device_state()           # review #6: state BEFORE ticks
    ticks0 = proc_sample()
    wall0 = time.monotonic()
    points = [ticks0] if ticks0 is not None else []
    sampling_errors = 0
    for i in range(clients):
        t = threading.Thread(target=parker,
                             args=("park-%s-%d" % (name, i), head, log,
                                   stats, stop),
                             daemon=True)
        t.start()
        threads.append(t)
    deadline = wall0 + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        sample = proc_sample()
        if sample is None:
            sampling_errors += 1
        else:
            points.append(sample)
    ticks1 = proc_sample()
    if ticks1 is not None and (not points or
                               ticks1[0] - points[-1][0] >= interval / 2):
        points.append(ticks1)
    state_end = device_state()
    stop.set()
    for t in threads:
        t.join(timeout=40)

    invalid = None
    if ticks0 is None or ticks1 is None:
        invalid = "proc-stat unreachable at a window boundary"
    elif sampling_errors:
        invalid = "proc-stat missed %d in-window samples" % sampling_errors
    elif any(p[2] != ticks0[2] for p in points):
        invalid = "app pid changed mid-window (restart/OOM)"
    elif any(b[1] < a[1] for a, b in zip(points, points[1:])):
        invalid = "tick counter went backwards (pid reuse?)"

    cpu_pct = None
    interval_rates = rates_from(points)
    if invalid is None:
        dt = ticks1[0] - ticks0[0]
        if dt > 0:
            cpu_pct = round((ticks1[1] - ticks0[1]) / ticks1[3] / dt * 100.0, 3)

    parker_valid = (len(stats) == clients and all(
        s["nonempty"] == 0 and s["errors"] <= 2
        and 20.0 <= (s["mean_duration_s"] or 0) <= 27.0
        for s in stats))

    return {"window": name, "clients": clients, "wall_s": round(seconds, 1),
            "app_cpu_pct_of_one_core": cpu_pct,
            "app_cpu_p95_pct_of_one_core": pct(interval_rates, 0.95),
            "interval_rates_pct": interval_rates,
            "sample_count": len(points),
            "sampling_errors": sampling_errors,
            "state_start": state_start, "state_end": state_end,
            "states_match": (state_start.get("wakefulness") ==
                             state_end.get("wakefulness")
                             and state_start.get("charging_usb") ==
                             state_end.get("charging_usb")
                             and state_start.get("charging_ac") ==
                             state_end.get("charging_ac")),
            "level_start": state_start.get("level"),
            "level_end": state_end.get("level"),
            "parker_stats": stats,
            "parker_valid": parker_valid,
            "parker_log_tail": log[-10:],
            "invalid": invalid}


def measure_on_device(serial):
    # the head and the deployed slice length, from the device's own report
    with urllib.request.urlopen(
            BASE + "/api/v2/debug/store-state?db=main", timeout=10) as r:
        st = json.loads(r.read().decode("utf-8"))
    module_files = ("sitecrew/api_v2.py", "server/app.py",
                    "server/static/poll.js")
    device_hashes = st.get("store_modules_sha256") or {}
    local_hashes = {f: hashlib.sha256(
        open(os.path.join(REPO, *f.split("/")), "rb").read()).hexdigest()
        for f in module_files}
    mismatches = {f: {"local": local_hashes[f],
                      "device": device_hashes.get(f)}
                  for f in module_files
                  if device_hashes.get(f) != local_hashes[f]}
    if mismatches:
        print(json.dumps({"error": "device/PC module hash mismatch",
                          "mismatches": mismatches}, indent=2))
        return 1
    head = st.get("head", 0)
    if head <= 0:
        print(json.dumps({"error": "store head is 0 — refusing to measure "
                                   "(a head fetch failure invalidates the "
                                   "parkers)"}))
        return 1

    print("decay curve (12 min from a FRESH restart, no clients)...",
          flush=True)
    adb_helpers.force_stop()
    time.sleep(2)
    adb_helpers.launch()
    deadline = time.time() + 240
    up = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=3) as r:
                if r.status == 200:
                    up = True
                    break
        except Exception:
            time.sleep(3)
    if not up:
        print(json.dumps({"error": "server did not come up"}))
        return 1
    points = []
    decay_stamp = time.strftime("%Y%m%d_%H%M%S")
    decay_out = os.path.join(REPO, "tests", "evidence",
                             "idle_park_decay_%s.json" % decay_stamp)
    t_end = time.time() + 720
    while time.time() < t_end:
        s = proc_sample()
        if s:
            points.append(s)
        with open(decay_out, "w", encoding="utf-8") as f:
            json.dump({"window": "decay_from_restart", "complete": False,
                       "samples_mono_ticks_pid_user_hz": points}, f, indent=2)
        time.sleep(30)
    decay_rates = rates_from(points)
    decay = {"window": "decay_from_restart", "minutes": 12,
             "complete": True, "samples_mono_ticks_pid_user_hz": points,
             "rates_pct": decay_rates,
             "avg_pct": round(sum(decay_rates) / len(decay_rates), 3)
             if decay_rates else None,
             "p95_pct": pct(decay_rates, 0.95)}
    with open(decay_out, "w", encoding="utf-8") as f:
        json.dump(decay, f, indent=2)

    print("baseline (3 min, no parked poll)...", flush=True)
    base = window("baseline", 180, 0, head)
    print("baseline cpu:", base.get("app_cpu_pct_of_one_core"), flush=True)
    if base.get("invalid"):
        print(json.dumps({"error": "baseline invalid", "why": base["invalid"]}))
        return 1

    # Re-read after restart + baseline; never park on a stale cursor.
    with urllib.request.urlopen(
            BASE + "/api/v2/debug/store-state?db=main", timeout=10) as r:
        head = json.loads(r.read().decode("utf-8")).get("head", 0)
    if head <= 0:
        print(json.dumps({"error": "store head is 0 before park"}))
        return 1
    print("park (10 min, 2 parked clients, cid, wait=25)...", flush=True)
    park = window("park", 600, 2, head)
    if park.get("invalid"):
        print(json.dumps({"error": "park window invalid",
                          "why": park["invalid"]}))
        return 1

    b_cpu = base.get("app_cpu_pct_of_one_core") or 0
    p_cpu = park.get("app_cpu_pct_of_one_core") or 0
    delta = round(p_cpu - b_cpu, 3)
    park_p95 = park.get("app_cpu_p95_pct_of_one_core")
    rule = {
        "avg_delta_pct": delta,
        "park_p95_pct": park_p95,
        "thresholds": "park-minus-baseline avg <= 1%, park p95 <= 3% "
                      "(pre-registered; boss confirms)",
        "states_match": park.get("states_match"),
        "parker_valid": park.get("parker_valid"),
        "slices_pass_cpu_rule": bool(
            delta <= 1.0 and park_p95 is not None and park_p95 <= 3.0
            and park.get("states_match") and park.get("parker_valid")),
        "design": "1 s slices + cid supersede (PARK_SLICE_S=%s)" % PARK_SLICE_S,
    }
    report = {
        "device": {"serial_sha256": __import__("hashlib").sha256(
            serial.encode()).hexdigest()},
        "park_slice_s": PARK_SLICE_S,
        "local_module_sha256": local_hashes,
        "device_module_sha256": {f: device_hashes.get(f)
                                 for f in module_files},
        "parked_clients": 2,
        "head_at_start": head,
        "decay_from_restart": decay,
        "baseline": base,
        "park": park,
        "verdict_rule": rule,
        "verdict_note": ("machine-generated against the pre-registered rule; "
                         "the boss confirms. Park windows with nonempty != 0 "
                         "or short mean durations are INVALID by "
                         "construction (review #5)."),
    }
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(REPO, "tests", "evidence",
                       "idle_park_cpu_report_%s.json" % stamp)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("evidence:", out)
    print(json.dumps({"decay_avg": decay.get("avg_pct"),
                      "baseline_cpu": b_cpu, "park_cpu": p_cpu,
                      "delta": delta, "parker_valid": park.get("parker_valid"),
                      "parker_stats": park.get("parker_stats")}, indent=2))
    return 0


def main():
    serial = adb_helpers.serial()
    if not serial:
        print(json.dumps({"error": "no adb device — device run NOT RUN"}))
        return 1
    os.environ["ANDROID_SERIAL"] = serial
    forward_out, forward_rc = adb_helpers.forward(18110)
    if forward_rc != 0:
        print(json.dumps({"error": "adb forward failed",
                          "rc": forward_rc, "detail": forward_out[:200]}))
        return 1
    adb_helpers.adb("shell", "svc", "power", "stayon", "usb", timeout=15)
    try:
        return measure_on_device(serial)
    finally:
        adb_helpers.adb("shell", "svc", "power", "stayon", "false", timeout=15)


if __name__ == "__main__":
    sys.exit(main())
