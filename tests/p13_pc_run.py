"""P1.3 PC half (blueprint: committed event -> polling client <= 300 ms;
100 forced reconnects produce a DOM identical to an uninterrupted run).

Starts the real Flask app (threaded, SITECREW_STORE=1) on 127.0.0.1:18080,
runs tests/p13_client.js (the real integrated poll.js) for latency and
reconnect, and writes tests/evidence/p13_pc_report.json with the seed,
commit hash, p50/p95/max and the reconnect verdict.

Uses db=main (stream c_debug): the kill leg exclusively owns killtest.db /
c_kill / kill_acks.log — batch appends never touch the ACK side file.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BASE = "http://127.0.0.1:18080"
PORT = 18080
GIT = r"C:\Program Files\Git\cmd\git.exe"


def commit_hash():
    try:
        out = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip()[:12] or "uncommitted"
    except Exception:
        return "uncommitted"


def store_hashes():
    out = {}
    for name in ("sitecrew/store/db.py", "sitecrew/store/events.py",
                 "sitecrew/api_v2.py"):
        p = os.path.join(REPO, name.replace("/", os.sep))
        with open(p, "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def wait_server(timeout=90):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


def run_node(mode, seed, wait=None):
    env = dict(os.environ, NODE_NO_WARNINGS="1")
    timeout = 240 if mode == "latency" else 480
    cmd = ["node", os.path.join(REPO, "tests", "p13_client.cjs"),
           os.path.join(REPO, "server", "static", "poll.js"),
           BASE, mode, str(seed), "c_debug"]
    if wait:
        cmd.append(str(wait))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", cwd=REPO, env=env)
    lines = [l for l in (proc.stdout or "").splitlines() if l.startswith("{")]
    if not lines:
        raise RuntimeError("node %s produced no JSON (rc=%s)\n%s\n%s"
                           % (mode, proc.returncode, (proc.stdout or "")[-500:],
                              (proc.stderr or "")[-800:]))
    return json.loads(lines[-1])


def main():
    seed = random.randrange(2 ** 31)
    if os.environ.get("SC_SEED"):
        seed = int(os.environ["SC_SEED"])

    env = dict(os.environ, SITECREW_STORE="1", SITECREW_DEBUG="1",
               PYTHONUNBUFFERED="1")
    server = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c",
         "import sys; sys.path.insert(0, %r); "
         "from server.app import app; "
         "app.run(host='127.0.0.1', port=%d, threaded=True, debug=False)" % (REPO, PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(REPO, "tests", "evidence", "p13_server.log"),
                    "w", encoding="utf-8"),
        stderr=subprocess.STDOUT)
    try:
        if not wait_server():
            print(json.dumps({"pass": False, "error": "server did not start"}))
            return 1
        lat = run_node("latency", seed)
        rec = run_node("reconnect", seed, wait=25)   # boss: reconnects at wait=25
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except Exception:
            server.kill()

    # boss: every report self-records api_v2.py / poll.js / app.py hashes and
    # checks them against p13_sha256s.txt
    current = {
        "sitecrew/api_v2.py": store_hashes()["sitecrew/api_v2.py"],
        "server/static/poll.js": hashlib.sha256(
            open(os.path.join(REPO, "server", "static", "poll.js"), "rb").read()).hexdigest(),
        "server/app.py": hashlib.sha256(
            open(os.path.join(REPO, "server", "app.py"), "rb").read()).hexdigest(),
    }
    frozen = {}
    p13 = os.path.join(REPO, "tests", "evidence", "p13_sha256s.txt")
    import re as _re
    for line in open(p13, encoding="utf-8"):
        m = _re.match(r"^([0-9a-f]{64})\s+(\S+)", line.strip())
        if m:
            frozen[m.group(2)] = m.group(1)
    hash_checks = {name: bool(frozen.get(name) == h) for name, h in current.items()}

    checks = {
        "latency_p50_le_300": lat.get("p50_ms") is not None and lat["p50_ms"] <= 300,
        "latency_p95_le_300": lat.get("p95_ms") is not None and lat["p95_ms"] <= 300,
        "reconnect_forced_ge_100": rec.get("forced_reconnects", 0) >= 100,
        "reconnect_hashes_equal": bool(rec.get("hashes_equal")),
        "reconnect_cursors_equal": bool(rec.get("cursors_equal")),
        "reconnect_no_losses": rec.get("ref_items") == rec.get("chaos_items"),
        "self_recorded_hashes_match": all(hash_checks.values()),
    }
    report = {
        "pass": all(checks.values()),
        "gate": "P1.3 PC half: committed event -> polling client <= 300 ms; "
                "100 forced reconnects produce a DOM identical to an "
                "uninterrupted run (blueprint gates line 857)",
        "seed": seed,
        "commit": commit_hash(),
        "store_module_hashes": store_hashes(),
        "self_recorded_hashes": current,
        "hash_checks_vs_p13_list": hash_checks,
        "latency": lat,
        "reconnect": rec,
        "checks": checks,
    }
    out = os.path.join(REPO, "tests", "evidence", "p13_pc_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("pass", "checks", "seed", "commit")}, indent=2))
    print("latency:", {k: lat.get(k) for k in
                       ("samples", "received", "misses", "p50_ms", "p95_ms",
                        "max_ms")})
    print("reconnect:", {k: rec.get(k) for k in
                         ("forced_reconnects", "events_appended",
                          "chaos_dupes", "chaos_gaps", "hashes_equal")})
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
