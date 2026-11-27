"""One-shot PC storm run (boss P1.3-device ruling, 'find out on the PC first'):
start the real Flask app, run the wait=25 forced-reconnect storm for 90 s,
report 503 counts + longest 503 window + time-to-first-event after reconnects.
Writes tests/evidence/p13_storm_report.json."""
import hashlib
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:18080"
PORT = 18080


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


def main():
    env = dict(os.environ, SITECREW_STORE="1", SITECREW_DEBUG="1",
               PYTHONUNBUFFERED="1")
    server = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c",
         "import sys; sys.path.insert(0, %r); "
         "from server.app import app; "
         "app.run(host='127.0.0.1', port=%d, threaded=True, debug=False)" % (REPO, PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(REPO, "tests", "evidence", "p13_storm_server.log"),
                    "w", encoding="utf-8"),
        stderr=subprocess.STDOUT)
    try:
        if not wait_server():
            print(json.dumps({"pass": False, "error": "server did not start"}))
            return 1
        proc = subprocess.run(
            ["node", os.path.join(REPO, "tests", "p13_client.cjs"),
             os.path.join(REPO, "server", "static", "poll.js"),
             BASE, "storm", "424242", "c_debug"],
            capture_output=True, text=True, timeout=180, encoding="utf-8",
            errors="replace", cwd=REPO, env=env)
        lines = [l for l in (proc.stdout or "").splitlines() if l.startswith("{")]
        if not lines:
            raise RuntimeError("storm produced no JSON (rc=%s)\n%s" % (
                proc.returncode, (proc.stderr or "")[-600:]))
        report = json.loads(lines[-1])
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except Exception:
            server.kill()

    report["self_recorded_hashes"] = {
        "api_v2": hashlib.sha256(
            open(os.path.join(REPO, "sitecrew", "api_v2.py"), "rb").read()).hexdigest(),
        "poll_js": hashlib.sha256(
            open(os.path.join(REPO, "server", "static", "poll.js"), "rb").read()).hexdigest(),
        "app_py": hashlib.sha256(
            open(os.path.join(REPO, "server", "app.py"), "rb").read()).hexdigest(),
    }
    # boss: check the self-recorded hashes against p13_sha256s.txt
    import re as _re
    frozen = {}
    for line in open(os.path.join(REPO, "tests", "evidence", "p13_sha256s.txt"),
                     encoding="utf-8"):
        m = _re.match(r"^([0-9a-f]{64})\s+(\S+)", line.strip())
        if m:
            frozen[m.group(2)] = m.group(1)
    report["hash_checks_vs_p13_list"] = {
        "sitecrew/api_v2.py": frozen.get("sitecrew/api_v2.py") == report["self_recorded_hashes"]["api_v2"],
        "server/static/poll.js": frozen.get("server/static/poll.js") == report["self_recorded_hashes"]["poll_js"],
        "server/app.py": frozen.get("server/app.py") == report["self_recorded_hashes"]["app_py"],
    }
    report["pass_note"] = (
        "machine-generated metrics only; verdict belongs to the boss. "
        "slots_free_promptly iff http_503 == 0.")
    out = os.path.join(REPO, "tests", "evidence", "p13_storm_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
