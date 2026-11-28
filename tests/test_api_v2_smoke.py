import os, sys, threading, time
os.environ["SITECREW_TEST"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask
from sitecrew.api_v2 import parse_streams, parse_wait, register_v2, MAX_WAIT

# parser unit tests (no flask needed for these)
assert parse_streams("c_81f2:318,office:12") == {"c_81f2": 318, "office": 12}
for bad in ["", "office", "office:", "office:-1", "office:1.5", "a:1,a:2", "a b:1", "../x:1", "a:" + "9" * 16, ":3"] + [",".join("s%d:0" % i for i in range(40))]:
    try: parse_streams(bad); raise SystemExit("accepted bad streams %r" % bad[:30])
    except ValueError: pass
assert parse_wait(None) == 0 and parse_wait("100") == MAX_WAIT and parse_wait("-3") == 0
for bad in ["nan", "inf", "abc"]:
    try: parse_wait(bad); raise SystemExit("accepted wait " + bad)
    except ValueError: pass

class FakeStore:
    def __init__(self): self.calls = []; self.gate = threading.Event(); self.boom = False
    def poll(self, cursors, wait=0):
        self.calls.append((cursors, wait))
        if self.boom: raise RuntimeError("x")
        if wait >= 1: self.gate.wait(5)                     # a parked long-poll
        return {"events": [], "heads": {s: 0 for s in cursors}, "more": False, "reset": []}
fs = FakeStore()
app = Flask(__name__); register_v2(app, lambda: fs, max_parked=2)
c = app.test_client()
r = c.get("/api/v2/poll?streams=c_1:0,office:4&wait=0"); assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
assert fs.calls[-1] == ({"c_1": 0, "office": 4}, 0.0)
assert c.get("/api/v2/poll?streams=bad").status_code == 400
assert c.get("/api/v2/poll?streams=a:1&wait=zzz").status_code == 400
calls_before = len(fs.calls)
r = c.get("/api/v2/poll?streams=a:1&wait=999"); fs.gate.set()   # wait clamped to 25
assert fs.calls[calls_before][1] == 1.0  # parse_wait test above proves the 25 s clamp; route slices at 1 s
assert r.headers["X-Wake-Reason"] == "timeout"
# Immediate event and catch-up responses identify why the parked request woke.
class EventStoreFake:
    def __init__(self, result): self.result = result
    def poll(self, cursors, wait=0): return self.result
for result, reason in [
    ({"events": [{"seq": 1}], "heads": {"a": 1}, "more": False, "reset": []}, "events"),
    ({"events": [], "heads": {"a": 2}, "more": True, "reset": []}, "more"),
]:
    event_app = Flask(__name__); register_v2(event_app, lambda: EventStoreFake(result))
    assert event_app.test_client().get("/api/v2/poll?streams=a:0&wait=1").headers["X-Wake-Reason"] == reason
fs.gate.clear()
# busy: 2 parked polls hold both slots, the 3rd is 503 with Retry-After, a normal wait=0 request is also refused only when slots are full
res = []
def parked(): res.append(app.test_client().get("/api/v2/poll?streams=a:1&wait=5").status_code)
ts = [threading.Thread(target=parked) for _ in range(2)]; [t.start() for t in ts]; time.sleep(0.3)
r = c.get("/api/v2/poll?streams=a:1&wait=5"); assert r.status_code == 503 and r.headers["Retry-After"] == "1", r.status_code
fs.gate.set(); [t.join() for t in ts]; assert res == [200, 200], res
# slots are released afterwards
assert c.get("/api/v2/poll?streams=a:1&wait=0").status_code == 200
fs.boom = True; assert c.get("/api/v2/poll?streams=a:1").status_code == 500
# test hooks
assert c.post("/api/v2/_test/report", json={"applied": 5}).status_code == 200
assert c.get("/api/v2/_test/report").get_json() == {"applied": 5}
print("api_v2 smoke ok")
