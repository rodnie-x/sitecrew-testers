"""G0 device characterization — runs the PRESERVED behaviors on the real phone.

Executed by scripts/run_gate.py --device (or standalone):
    python tests/test_characterization_device.py [base_url]

Device-only criteria that need docs/TEST_TARGETS.md values are marked NOT RUN here,
never PASS. Network-dependent steps tolerate the queue path (offline queuing is itself
a preserved behavior).
"""
import json
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles: never die on emoji
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18110"
RESULTS = []


def call(path, payload=None, timeout=20):
    data = json.dumps(payload or {}).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:200]))
    print(("PASS " if ok else "FAIL ") + name + (" — " + str(detail)[:160] if detail else ""))


def connectivity_preflight(host="integrate.api.nvidia.com", port=443, timeout=5):
    """D7 (boss ruling): device crew tests only run when the phone can actually
    reach the NVIDIA API; otherwise report NOT RUN — never FAIL."""
    import socket
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def wait_idle(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not call("/api/state")["busy"]:
            return
        time.sleep(2)


def run():
    s = call("/api/state")
    check("state endpoint alive", all(k in s for k in
          ("messages", "busy", "queued", "online", "has_key")), sorted(s)[:8])
    check("device reports phone_mode", s.get("phone_mode") is True, s.get("phone_mode"))
    check("device has key configured", s.get("has_key") is True, s.get("has_key"))

    call("/api/clear", {})   # explicit POST body — the route is POST-only
    j = call("/api/chat", {"message": "battery"})
    last = call("/api/state")["messages"][-1]
    check("battery answered on-device (zero data)", j.get("local") is True
          and "⚡" in last["content"] and "battery" in last["content"].lower(),
          last["content"][:80])
    check("battery reply carries real tools_used",
          last.get("tools_used", {}).get("battery") == 1, last.get("tools_used"))

    j = call("/api/chat", {"message": "what tools can you use?"})
    last = call("/api/state")["messages"][-1]
    check("tools tour deterministic", j.get("local") is True
          and "phone tools" in last["content"], last["content"][:60])

    j = call("/api/undo", {})
    check("undo removes exchange", j.get("ok") and j.get("removed") == 2, j)

    # conversational ask reaches the crew (uses data; the queue path also proves offline)
    if connectivity_preflight():
        j = call("/api/chat", {"message": "Check my battery and tell me if I should charge soon, please."})
        check("conversational ask goes to crew", j.get("ok") and not j.get("local"), j)
        wait_idle()
        last = call("/api/state")["messages"][-1]
        check("crew reply has battery facts + chip",
              "battery" in last["content"].lower() and "%" in last["content"]
              and last.get("tools_used", {}).get("battery") == 1, last["content"][:90])
    else:
        RESULTS.append(("conversational ask goes to crew", None,
                        "NOT RUN — connectivity preflight failed (phone offline)"))
        RESULTS.append(("crew reply has battery facts + chip", None,
                        "NOT RUN — connectivity preflight failed (phone offline)"))
        print("NOT RUN conversational crew checks — connectivity preflight failed"
              " (phone offline); offline queuing is the expected behavior")

    # offline queuing: flap the network from the phone side is NOT done here (needs svc
    # privileges); instead we assert the queue endpoint semantics already covered on PC.
    RESULTS.append(("offline flap on device", None, "NOT RUN — needs airplane-mode toggle"))
    print("NOT RUN offline flap on device — needs airplane-mode toggle (human or root)")
    RESULTS.append(("sensitive device effects", None,
                    "NOT RUN — docs/TEST_TARGETS.md has no values yet"))
    print("NOT RUN sensitive device effects — no TEST_TARGETS")

    call("/api/clear", {})
    return RESULTS


if __name__ == "__main__":
    run()
