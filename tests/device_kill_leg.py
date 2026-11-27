"""P1.2-D device kill leg — in-flight kills on ANY connected Android device.

Device-agnostic (boss-approved intent): the device is the first `adb devices`
entry in "device" state; no manufacturer/model/serial is hardcoded (the serial is
RECORDED, not assumed); standard adb only; every fact comes from the device.

Per cycle (on the leg's OWN db: killtest.db — never sitecrew.db):
  1. adb logcat -c                         (fresh buffer: no stale ACKs)
  2. start a STREAMING logcat Popen        (line-by-line; truly event-driven,
                                            not a -d re-dump polling loop)
  3. k = seeded rng kill point (6..40); log the seed
  4. POST /api/v2/debug/append {"flat": true, "db": "kill"}
     -> the app starts a BACKGROUND flat-out append loop through the production
        EventStore.append path, printing SC_STORE_ACK <stream> <seq> per commit
  5. when the k-th ACK of this cycle is observed: HARD-KILL the app WHILE THE
     LOOP IS STILL RUNNING (no stop is sent; k << the loop's output)
  6. verify the pid is gone; record kill method + exit status
  7. relaunch, wait for the server, assert the new pid differs
  8. per-cycle oracle (device's own SQLite, via the app):
       invariants all green AND last_acked <= head <= last_acked + 1
     (unacked = head - last_acked is 0 or 1 per kill)
Final pass:
  - integrity_check ok, foreign_key_check empty, 0 orphans both directions,
    seq contiguous per chat  (EventStore.check_invariants on the device)
  - head == initial_head + total ACKs + sum(unacked)   (accounting identity)
  - device facts recorded: manufacturer/model/SDK/serial, sqlite_version,
    WAL on the device DB, FTS5 probe, APK sha256 + versionName,
    SITECREW_STORE flag state, store module hashes
Degraded ACK wait NEVER passes: it marks the cycle invalid and fails the leg.
Evidence: tests/evidence/p12_device_report.json
"""
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.android import adb_helpers  # noqa: E402

BASE = "http://127.0.0.1:18110"
PKG = "com.sitecrew.ai"
CYCLES = 30
ACK_RE = re.compile(r"SC_STORE_ACK\s+(\S+)\s+(\d+)", re.IGNORECASE)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def http(method, path, payload=None, timeout=20):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_retry(method, path, payload=None, tries=6, timeout=20):
    """HTTP with retries — the phone link and app boot are known-flaky."""
    last = None
    for _ in range(tries):
        try:
            return http(method, path, payload, timeout=timeout)
        except Exception as e:                                   # noqa: PERF203
            last = e
            time.sleep(3)
    raise last


def wait_server(timeout=240):
    return adb_helpers.wait_server(BASE, timeout=timeout)


def dump_diagnostics(tag):
    """On failure: ONE logcat dump + process state (event-driven single capture)."""
    out_dir = os.path.join(REPO, "tests", "evidence")
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
            f.write(out[-300000:])       # tail: the boot attempt is at the end
    except Exception as e:
        with open(os.path.join(out_dir, tag + "_logcat.txt"), "w") as f:
            f.write("logcat failed: %s" % e)


class LogcatStream:
    """Streaming logcat Popen read line-by-line (event-driven, boss fix #3).

    Full unfiltered stream; ACK lines are regex-matched here so the case of the
    p4a log tag (python vs Python) can never hide a marker.
    """

    def __init__(self, serial):
        self.serial = serial
        self.q = queue.Queue()
        adb_helpers.adb("logcat", "-c", timeout=20)      # clear stale ACKs FIRST
        self.proc = subprocess.Popen(
            [adb_helpers.adb_path(), "-s", serial, "logcat", "-v", "time"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace")

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)                                 # stream closed

    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()

    def next_line(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def run_cycle(rng, serial, cycle, log):
    """One kill cycle. Returns a result dict; 'valid': False fails the leg."""
    res = {"cycle": cycle, "valid": True, "acks": 0, "last_acked": None,
           "kill": None}
    before_state = http_retry("GET", "/api/v2/debug/store-state?db=kill")
    before_head = before_state.get("head", 0)
    res["before_head"] = before_head

    stream = LogcatStream(serial)
    stream.start()
    k = rng.randint(6, 40)                    # kill after the k-th ACK
    res["k"] = k
    try:
        r = http_retry("POST", "/api/v2/debug/append",
                       {"flat": True, "db": "kill"})
        if not r.get("ok") or r.get("already_running"):
            res["valid"] = False
            res["error"] = "flat start rejected: %s" % r
            return res

        # stream ACKs; kill after the k-th, while the loop is still running
        acks = 0
        last_seq = None
        flat_done_early = False
        deadline = time.time() + 120          # degraded wait must FAIL, not pass
        while time.time() < deadline:
            line = stream.next_line(timeout=5)
            if line is None:
                if not adb_helpers.pid_alive(adb_helpers.pid()):
                    break                     # process died: done streaming
                continue
            m = ACK_RE.search(line)
            if m:
                acks += 1
                last_seq = int(m.group(2))
                if acks >= k:
                    pid = adb_helpers.pid()
                    method, rc = adb_helpers.hard_kill_pid(pid)
                    res["kill"] = {"method": method, "rc": rc, "pid": pid}
                    break
            if "SC_STORE_FLAT_DONE" in line:
                flat_done_early = True        # loop ended before the kill
                break
            if "SC_STORE_ERR" in line:
                res["valid"] = False
                res["error"] = "store error in app: %s" % line[:200]
                return res
        else:
            res["valid"] = False
            res["error"] = "degraded ACK wait: k=%d not seen in 120 s" % k
            adb_helpers.hard_kill_pid(adb_helpers.pid())
            return res
        if flat_done_early and res["kill"] is None:
            res["valid"] = False
            res["error"] = "flat loop finished before the kill (not in-flight)"
            return res

        # drain whatever the app printed before it died, then confirm death
        drain_end = time.time() + 8
        while time.time() < drain_end:
            line = stream.next_line(timeout=1)
            if line is None:
                if not adb_helpers.pid_alive(adb_helpers.pid()):
                    break
                continue
            m = ACK_RE.search(line)
            if m:
                acks += 1
                last_seq = int(m.group(2))
        if adb_helpers.pid_alive(res["kill"]["pid"]):
            res["valid"] = False
            res["error"] = "kill method %s left pid alive" % res["kill"]["method"]
            return res
        res["acks"] = acks
        res["last_acked"] = last_seq
        res["killed_in_flight"] = True        # kill landed while the loop ran
    finally:
        stream.stop()

    adb_helpers.launch()
    if not wait_server():
        res["valid"] = False
        res["error"] = "server did not return after relaunch"
        dump_diagnostics("cycle%d_boot_failure" % cycle)
        return res
    new_pid = adb_helpers.pid()
    res["new_pid"] = new_pid
    if not new_pid or new_pid == res["kill"]["pid"]:
        res["valid"] = False
        res["error"] = "pid did not change after relaunch"
        return res

    # per-cycle oracle on the DEVICE's own SQLite (via the app's endpoint):
    # after reopen, last_acked <= head <= last_acked + 1
    after = http_retry("GET", "/api/v2/debug/store-state?db=kill")
    head = after.get("head")
    res["after_head"] = head
    res["invariants_pass"] = after.get("invariants", {}).get("_pass")
    unacked = None if head is None or last_seq is None else head - last_seq
    res["unacked"] = unacked
    if not res["invariants_pass"]:
        res["valid"] = False
        res["error"] = "invariants failed after reopen"
    elif unacked is None or unacked not in (0, 1):
        res["valid"] = False
        res["error"] = "oracle violated: last_acked=%s head=%s unacked=%s" % (
            last_seq, head, unacked)
    return res


def collect_device_facts(serial):
    """Boss fix #4: the report must carry the device's own facts."""
    facts = {"serial": serial}
    props = adb_helpers.getprops()
    facts.update(props)
    pkg = adb_helpers.package_info()
    facts["versionName"] = pkg.get("versionName")
    facts["apk_path"] = pkg.get("apk_path")
    apk = os.path.join(REPO, "reports", "sitecrew-current.apk")
    if os.path.exists(apk):
        import hashlib
        h = hashlib.sha256()
        with open(apk, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        facts["apk_sha256"] = h.hexdigest()
        facts["apk_sha256_source"] = "reports/sitecrew-current.apk (installed artifact)"
    try:
        probe = http_retry("GET", "/api/v2/debug/fts5-probe", tries=3)
        facts["fts5_probe"] = probe
    except Exception as e:
        facts["fts5_probe"] = {"error": str(e)[:200]}
    try:
        st = http_retry("GET", "/api/v2/debug/store-state?db=kill&facts=1", tries=3)
        for key in ("sqlite_version", "journal_mode", "wal_bytes",
                    "store_flag", "user_version", "schema_sha256",
                    "store_modules_sha256"):
            if key in st:
                facts[key] = st[key]
    except Exception as e:
        facts["store_state_facts_error"] = str(e)[:200]
    return facts


def main():
    seed = random.randrange(2 ** 31)
    if os.environ.get("SC_SEED"):
        seed = int(os.environ["SC_SEED"])
    rng = random.Random(seed)
    serial = adb_helpers.serial()
    if not serial:
        print(json.dumps({"pass": False, "error": "no adb device"}))
        return 1
    adb_helpers.forward(18110)
    if not adb_helpers.wait_server(BASE, timeout=90):
        adb_helpers.launch()
        if not wait_server():
            dump_diagnostics("boot_failure")
            print(json.dumps({"pass": False,
                              "error": "server unreachable after launch"}))
            return 1

    initial = http_retry("GET", "/api/v2/debug/store-state?db=kill")
    initial_head = initial.get("head", 0)

    log = []
    ev = os.path.join(REPO, "tests", "evidence")
    os.makedirs(ev, exist_ok=True)
    report_path = os.path.join(ev, "p12_device_report.json")

    def save(done, final=None):
        report = {"cycles_attempted": done, "log": log,
                  "partial": final is None, "seed": seed}
        if final:
            report.update(final)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    total_acks = 0
    unacked_sum = 0
    invalid = 0
    for cycle in range(CYCLES):
        res = run_cycle(rng, serial, cycle, log)
        total_acks += res.get("acks") or 0
        if res.get("unacked"):
            unacked_sum += res["unacked"]
        if not res.get("valid"):
            invalid += 1
        log.append(res)
        save(cycle + 1)                       # evidence survives a drop
        print("cycle %d: acks %d, head %s -> %s, unacked %s, kill %s, valid %s"
              % (cycle, res.get("acks", 0), res.get("before_head"),
                 res.get("after_head"), res.get("unacked"),
                 (res.get("kill") or {}).get("method"), res.get("valid")),
              flush=True)

    if not wait_server():
        dump_diagnostics("final_boot_failure")
    try:
        final = http_retry("GET", "/api/v2/debug/store-state?db=kill")
        inv = final.get("invariants", {})
        final_head = final.get("head")
    except Exception as e:
        inv, final_head = {"_pass": False, "error": str(e)[:200]}, None

    facts = collect_device_facts(serial)
    # boss oracle: head == initial + acks + unacked (0 or 1 per kill)
    identity = (final_head == initial_head + total_acks + unacked_sum)
    report = {
        "pass": bool(inv.get("_pass")) and invalid == 0 and identity,
        "seed": seed,
        "cycles": CYCLES,
        "invalid_cycles": invalid,
        "initial_head": initial_head,
        "final_head": final_head,
        "total_acks": total_acks,
        "unacked_sum": unacked_sum,
        "identity_head_eq_acks_plus_unacked": identity,
        "invariants": inv,
        "device": facts,
        "log": log,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("pass", "invalid_cycles", "final_head", "total_acks",
                       "unacked_sum", "identity_head_eq_acks_plus_unacked")},
                     indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
