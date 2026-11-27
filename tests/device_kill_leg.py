"""P1.2-D device kill leg — in-flight kills on ANY connected Android device.

Device-agnostic (boss-approved intent): the device is the first `adb devices`
entry in "device" state; no manufacturer/model/serial is hardcoded (the serial
is RECORDED, not assumed); standard adb only; every fact comes from the device.

Per cycle (on the leg's OWN db: killtest.db — never sitecrew.db):
  1. adb logcat -c                          (fresh buffer: no stale ACKs)
  2. start a STREAMING logcat Popen         (line-by-line; truly event-driven)
  3. k = seeded rng kill point (6..40); log the seed
  4. POST /api/v2/debug/append {"flat": true, "db": "kill"}
     -> the app runs a BACKGROUND flat-out append loop through the production
        EventStore.append path, alternating message.created / tool.committed
        by seq parity, printing SC_STORE_ACK <stream> <seq> per commit AND
        doing one unbuffered os.write of the seq to kill_acks.log
  5. when the k-th ACK of this cycle is observed (logcat = the TRIGGER only):
        HARD-KILL the app WHILE THE LOOP IS STILL RUNNING (no stop is sent)
  6. verify the pid is gone; record kill method + exit status
  7. relaunch, wait for the server, assert the new pid differs
  8. per-cycle oracle (device's own SQLite, via the app's store-state):
        invariants all green AND last_acked <= head <= last_acked + 1
     where last_acked = the ACK SIDE FILE's last seq (page cache survives
     SIGKILL — boss amendment 1; logcat is only the trigger + a diagnostic)
     and unacked = head - last_acked is 0 or 1 per kill.
  9. continuity: before_head[c] == after_head[c-1] (boss amendment 3)
Final pass (named keys, boss amendment 4):
  - integrity_check ok / foreign_key_check empty / both orphan counts 0 /
    seq contiguous per chat  (EventStore.check_invariants on the device DB)
  - head == initial_head + ack_file_lines_added + sum(unacked)
  - device store_modules_sha256 == the frozen hashes in p12_sha256s.txt
  - server confirmed bound to 127.0.0.1 during the run (/proc/net/tcp)
Claims (boss amendment 5): the report states the run used a debug-flagged
build, that on-device in-flight rollback is STATISTICAL (unacked-cycle count
reported; deterministic coverage is owned by the PC injected modes), and the
APK hashes come from adb-pulled pm path APKs (split-APK safe).
PASS needs all 30 cycles valid and every named check green.
Evidence: tests/evidence/p12_device_report.json
"""
import hashlib
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
    p4a log tag (python vs Python) can never hide a marker. The stream is the
    TRIGGER for the kill; the oracle's last_acked comes from the ACK side file.
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


def run_cycle(rng, serial, cycle, expected_before_head):
    """One kill cycle. Returns a result dict; 'valid': False fails the leg."""
    res = {"cycle": cycle, "valid": True, "acks": 0, "kill": None}
    before_state = http_retry("GET", "/api/v2/debug/store-state?db=kill")
    before_head = before_state.get("head", 0)
    res["before_head"] = before_head
    if before_head != expected_before_head:
        res["valid"] = False
        res["error"] = ("continuity broken: before_head %d != after_head of "
                        "previous cycle %d" % (before_head, expected_before_head))
        return res

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
        logcat_seqs = []
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
                logcat_seqs.append(int(m.group(2)))
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
                logcat_seqs.append(int(m.group(2)))
        res["acks"] = acks
        res["logcat_last_seq"] = logcat_seqs[-1] if logcat_seqs else None
        res["logcat_seqs"] = logcat_seqs      # diagnostic only (boss amendment 3)
        # crash-path guard (boss amendment 3): died on its own, never killed
        if res["kill"] is None:
            if not adb_helpers.pid_alive(adb_helpers.pid()):
                res["valid"] = False
                res["error"] = ("process died on its own before the kill "
                                "(acks=%d, last logcat seq=%s)"
                                % (acks, res["logcat_last_seq"]))
                dump_diagnostics("cycle%d_own_death" % cycle)
                return res
            res["valid"] = False
            res["error"] = "kill never issued and process still alive"
            return res
        if adb_helpers.pid_alive(res["kill"]["pid"]):
            res["valid"] = False
            res["error"] = "kill method %s left pid alive" % res["kill"]["method"]
            return res
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
    # after reopen, last_acked (from the ACK SIDE FILE) <= head <= last_acked + 1
    after = http_retry("GET", "/api/v2/debug/store-state?db=kill")
    head = after.get("head")
    res["after_head"] = head
    res["invariants_pass"] = after.get("invariants", {}).get("_pass")
    ack_file = after.get("ack_file", {})
    last_acked = ack_file.get("last_seq")
    res["file_last_seq"] = last_acked
    res["file_lines"] = ack_file.get("lines")
    res["logcat_vs_file_delta"] = (None if last_acked is None or
                                   res["logcat_last_seq"] is None
                                   else last_acked - res["logcat_last_seq"])
    unacked = None if head is None or last_acked is None else head - last_acked
    res["unacked"] = unacked
    if not res["invariants_pass"]:
        res["valid"] = False
        res["error"] = "invariants failed after reopen"
    elif unacked is None or unacked not in (0, 1):
        res["valid"] = False
        res["error"] = "oracle violated: file_last_seq=%s head=%s unacked=%s" % (
            last_acked, head, unacked)
    return res


def pull_apk_hashes(out_dir):
    """Boss amendment 4: hash the APK(s) actually installed (pm path + adb pull;
    split-APK safe) — never a PC-side copy."""
    out, _ = adb_helpers.adb("shell", "pm", "path", PKG, timeout=15)
    device_paths = [l.split("package:", 1)[1].strip()
                    for l in out.splitlines() if l.startswith("package:")]
    hashes = []
    for i, rp in enumerate(device_paths):
        local = os.path.join(out_dir, "apk_pull_%d.apk" % i)
        adb_helpers.adb("pull", rp, local, timeout=180)
        h = hashlib.sha256()
        with open(local, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        hashes.append({"device_path": rp, "sha256": h.hexdigest(),
                       "bytes": os.path.getsize(local)})
    return hashes


def frozen_hashes():
    """Parse tests/evidence/p12_sha256s.txt -> {path: sha256}."""
    frozen = {}
    path = os.path.join(REPO, "tests", "evidence", "p12_sha256s.txt")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^([0-9a-f]{64})\s+(\S+)", line.strip())
            if m:
                frozen[m.group(2)] = m.group(1)
    return frozen


def server_bind_evidence(serial):
    """Boss amendment 5: confirm 127.0.0.1:8080 LISTEN during the run, from the
    kernel's /proc/net/tcp (works on every Android; 8080 = hex 1F90,
    127.0.0.1 = 0100007F)."""
    out, _ = adb_helpers.adb("shell", "cat", "/proc/net/tcp", timeout=20)
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[3] == "0A" and parts[1].endswith(":1F90"):
            rows.append({"local": parts[1], "state": parts[3]})
    return {"port_hex": "1F90", "listeners": rows,
            "binds_127_0_0_1": any(r["local"].startswith("0100007F:") for r in rows)}


def collect_device_facts(serial):
    """Boss fix #4 + amendments: the report must carry the device's own facts."""
    facts = {"serial": serial}
    props = adb_helpers.getprops()
    facts.update(props)
    pkg = adb_helpers.package_info()
    facts["versionName"] = pkg.get("versionName")
    try:
        facts["apk_hashes"] = pull_apk_hashes(
            os.path.join(REPO, "tests", "evidence", "apk_pull"))
    except Exception as e:
        facts["apk_hashes"] = [{"error": str(e)[:200]}]
    try:
        probe = http_retry("GET", "/api/v2/debug/fts5-probe", tries=3)
        facts["fts5_probe"] = probe
    except Exception as e:
        facts["fts5_probe"] = {"error": str(e)[:200]}
    try:
        st = http_retry("GET", "/api/v2/debug/store-state?db=kill", tries=3)
        for key in ("sqlite_version", "journal_mode", "wal_bytes",
                    "store_flag", "user_version", "schema_sha256",
                    "store_modules_sha256", "ack_file"):
            if key in st:
                facts[key] = st[key]
    except Exception as e:
        facts["store_state_facts_error"] = str(e)[:200]
    facts["server_bind"] = server_bind_evidence(serial)
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
    initial_ack_lines = (initial.get("ack_file") or {}).get("lines") or 0

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
    unacked_cycles = 0
    invalid = 0
    expected_before_head = initial_head
    for cycle in range(CYCLES):
        res = run_cycle(rng, serial, cycle, expected_before_head)
        total_acks += res.get("acks") or 0
        if res.get("unacked"):
            unacked_sum += res["unacked"]
            unacked_cycles += 1
        if not res.get("valid"):
            invalid += 1
        expected_before_head = res.get("after_head", expected_before_head)
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
        final_ack_lines = (final.get("ack_file") or {}).get("lines") or 0
        device_mods = final.get("store_modules_sha256") or {}
    except Exception as e:
        inv, final_head, final_ack_lines = (
            {"_pass": False, "error": str(e)[:200]}, None, 0)
        device_mods = {}

    facts = collect_device_facts(serial)

    # boss amendment 4: device store module hashes must equal the frozen ones
    frozen = frozen_hashes()
    mod_checks = {}
    for name in ("sitecrew/store/db.py", "sitecrew/store/events.py",
                 "sitecrew/api_v2.py"):
        want = frozen.get(name)
        got = device_mods.get(name)
        mod_checks[name] = bool(want and got == want)
    hashes_match = all(mod_checks.values())

    # boss amendment 4: named checks, not only _pass
    checks = {
        "all_cycles_valid": invalid == 0,
        "invariants_pass": bool(inv.get("_pass")),
        "integrity_check_ok": inv.get("integrity_check") == [],
        "foreign_key_check_empty": inv.get("foreign_key_check") == [],
        "message_orphans_zero": inv.get("message_links") == [],
        "tool_orphans_zero": inv.get("tool_links") == [],
        "seq_contiguous": inv.get("seq_contiguous") == [],
        "identity_head_eq_acks_plus_unacked":
            final_head == initial_head + (final_ack_lines - initial_ack_lines)
            + unacked_sum,
        "store_module_hashes_match": hashes_match,
        "server_binds_127_0_0_1":
            bool((facts.get("server_bind") or {}).get("binds_127_0_0_1")),
    }
    report = {
        # boss amendment 5: constrained claims
        "build_flagging": "debug-flagged run (SITECREW_DEBUG on device)",
        "in_flight_note": ("on-device in-flight rollback is STATISTICAL; the "
                           "unacked-cycle count is reported and deterministic "
                           "coverage is owned by the PC injected modes"),
        "pass": all(checks.values()),
        "checks": checks,
        "module_hash_checks": mod_checks,
        "seed": seed,
        "cycles": CYCLES,
        "invalid_cycles": invalid,
        "unacked_cycles": unacked_cycles,
        "initial_head": initial_head,
        "final_head": final_head,
        "initial_ack_file_lines": initial_ack_lines,
        "final_ack_file_lines": final_ack_lines,
        "total_acks": total_acks,
        "unacked_sum": unacked_sum,
        "invariants": inv,
        "device": facts,
        "log": log,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("pass", "checks", "invalid_cycles", "unacked_cycles",
                       "final_head")}, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
