"""Phone-local CPU diagnostic — docs/DEVICE_CPU_DIAG_SPEC.md, frozen edit set.

STATUS: PC-IMPLEMENTED / DEVICE NOT RUN. No CPU result, root-cause claim, or
pass follows from this module (spec header + §12). Samples carry raw ticks and
counters only; validity classification lives in offline helpers (``classify``)
and the pass/classification rule (§9) belongs to the later offline analyzer.

What runs where
---------------
- ``diag-sampler`` thread (one per run, the run thread itself): appends JSONL
  records (header/sample/snapshot/window_marker/footer) to an app-private file
  under ``<base_dir>/logs/diag/<run_id>.jsonl`` with flush+fsync per record, so
  sampling survives adb loss / SIGKILL. Intervals: 5 s inside windows, 30 s in
  decay runs.
- ``diagclient-<i>`` threads: N phone-LOCAL clients using ``http.client``
  against the app's own loopback port, ``wait=25`` long-polls with a random
  cid each, immediate re-poll, and they ALWAYS read and close responses (the
  labeled ``leaky_legacy`` arm is the deliberate, informational-only exception
  and can never satisfy a pass). No parked request ever crosses adb.
- ``diag-snapshot`` threads (decay runs only, at process uptime 60/300/600 s
  ±5 s): read ``/proc/self/task/*/stat`` twice 5 s apart, rank by tick delta,
  and for the top 3 capture five ``sys._current_frames()`` samples 100 ms
  apart (six frames max, rendered ``func@basename:line``), mapping native_id
  through ``threading.enumerate()``. Unmapped TIDs are ``native:<comm>`` with
  no guessed Python frames.

Endpoints (registered by ``register_diag``, gated by the COMPILE-TIME
``build_flags.DEBUG_ENDPOINTS`` constant — inert/404 when off):

    POST /api/v2/debug/diag/plan      (optional start_after_uptime_s; boot plan file too)
    GET  /api/v2/debug/diag/status    (recomputed at most once per 60 s during a run)
    GET  /api/v2/debug/diag/result?run_id=...
    POST /api/v2/debug/diag/abort

server-only CPU = total process ticks − diagclient-* ticks − diag-sampler
ticks − other diag thread ticks. Every sample therefore records the raw
utime/stime plus per-bucket diag thread ticks (decay samples also carry the
full per-thread TID/name/ticks table).

Known limitation (spec §3): the WebView ``about:blank`` in diag mode cannot be
set from Python — the design RECORDS the expectation in every header
(``webview`` block) and validity relies on ``parked_other == 0``; the CDP
precondition (scripts/webview_cdp.py) verifies the loaded page later. Recorded
here per the approved implementation note; not a pass/fail input by itself.

This module never touches EventStore/db/poll semantics: it only observes the
existing /api/v2/poll + /api/v2/debug surfaces and its own process.
"""
import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid

import platform as _platform

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_ID = "sitecrew-cpu-diag/1"

# Spec §1 frozen edit set + the served poll.js (§4 header requirement).
FROZEN_MODULES = (
    "sitecrew/api_v2.py",
    "server/app.py",
    "server/static/poll.js",
    "cpu_diag.py",
    "build_flags.py",
)

DIAG_WAIT_S = 25                # spec §2: mirror the real poll shape (wait=25)
DIAG_HTTP_TIMEOUT_S = DIAG_WAIT_S + 5
CLIENT_ERROR_BACKOFF_S = 1.0    # review #5 lesson: never spin on errors
CLIENT_STREAM = "c_debug"       # same stream the device legs use (head is real)

SNAPSHOT_GAP_S = 5.0            # two /proc/self/task reads 5 s apart
SNAPSHOT_TOLERANCE_S = 5.0      # uptime targets 60/300/600 (±5 s)
FRAME_SAMPLES = 5               # five sys._current_frames() samples...
FRAME_GAP_S = 0.1               # ...100 ms apart
MAX_FRAMES = 6                  # six frames maximum per stack sample
TOP_N = 3                       # rank top 3 threads by tick delta

CLK_TCK_FALLBACK = 100          # Android kernel ABI constant (same as app.py)
STATUS_CACHE_S = 60.0           # spec §7: status polling at most once per 60 s during a run
BOOT_PLAN_FILE = "diag_plan.json"
RUNS_DIR_NAME = os.path.join("logs", "diag")
LAUNCH_GAP_MAX_S = 15.0         # spec §8 rule 6

# /proc root and the frame source are module attributes ONLY so PC tests can
# fake them (Windows has no /proc). Never changed at runtime in production.
_PROC_ROOT = "/proc"
_current_frames = sys._current_frames

_RUN_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Spec §3 limitation: recorded, verified later via CDP (not settable from Python).
WEBVIEW_REASON = ("WebView URL cannot be set from Python; diag mode expects "
                  "about:blank and the CDP precondition check verifies the "
                  "loaded page later (scripts/webview_cdp.py). parked_other "
                  "== 0 is the in-run validity signal.")


# ---------------------------------------------------------------------------
# Plans (spec §6) — canonical, hash-covered definitions
# ---------------------------------------------------------------------------

_PW_WINDOWS = [
    # cold order 0,2,1,0; zero windows 300 s, client windows 600 s; each after
    # a recorded/excluded 60 s settle.
    {"name": "cold-0", "clients": 0, "seconds": 300, "settle_s": 60, "interval_s": 5},
    {"name": "cold-2", "clients": 2, "seconds": 600, "settle_s": 60, "interval_s": 5},
    {"name": "cold-1", "clients": 1, "seconds": 600, "settle_s": 60, "interval_s": 5},
    {"name": "cold-0b", "clients": 0, "seconds": 300, "settle_s": 60, "interval_s": 5},
    # At uptime >= 1800 s, warm repeat 0,2,0.
    {"name": "warm-0", "clients": 0, "seconds": 300, "settle_s": 60, "interval_s": 5,
     "after_uptime_s": 1800},
    {"name": "warm-2", "clients": 2, "seconds": 600, "settle_s": 60, "interval_s": 5,
     "after_uptime_s": 1800},
    {"name": "warm-0b", "clients": 0, "seconds": 300, "settle_s": 60, "interval_s": 5,
     "after_uptime_s": 1800},
    # Sensitivity window: 2 clients, 60 s sampling, average-only analysis.
    {"name": "sensitivity-2", "clients": 2, "seconds": 600, "settle_s": 60,
     "interval_s": 60, "average_only": True},
    # Optional leaky_legacy arm LAST: informational only, can never satisfy a
    # pass (spec §2).
    {"name": "leaky-legacy", "clients": 2, "seconds": 600, "settle_s": 60,
     "interval_s": 5, "leaky": True, "informational_only": True, "optional": True},
]

PLANS = {
    "P-D1": {
        "id": "P-D1",
        "kind": "decay",
        "boot_kind": "normal_restart",
        "duration_s": 900,
        "sample_interval_s": 30,
        "clients": 0,
        "snapshots_at_uptime": [60, 300, 600],
        "per_thread_samples": True,
        "runs_after": None,
        "description": "normal restart; zero clients; 15 min; 30 s samples; "
                       "snapshots at 1/5/10 min",
    },
    "P-D2": {
        "id": "P-D2",
        "kind": "decay",
        "boot_kind": "hot_deploy_restart",
        "duration_s": 900,
        "sample_interval_s": 30,
        "clients": 0,
        "snapshots_at_uptime": [60, 300, 600],
        "per_thread_samples": True,
        "runs_after": "P-D1",
        "description": "push the identical frozen bytes, restart, then exactly "
                       "P-D1; run after P-D1 and record order",
    },
    "P-W": {
        "id": "P-W",
        "kind": "windows",
        "boot_kind": "fresh_restart",
        "windows": _PW_WINDOWS,
        "runs_after": None,
        "description": "fresh restart; cold 0,2,1,0; warm 0,2,0 at uptime "
                       ">=1800 s; sensitivity window; optional leaky last",
    },
}

# PC-smoke-only scaling of the canonical plans (device runs never pass these).
_ALLOWED_OVERRIDES = {
    "duration_s": (0.01, 3600.0),
    "sample_interval_s": (0.01, 60.0),
    "snapshots_at_uptime": (0.0, 3600.0),
    "window_seconds": (0.01, 3600.0),
    "window_interval_s": (0.01, 60.0),
    "settle_s": (0.0, 3600.0),
    "warm_after_uptime_s": (0.0, 86400.0),
    "sensitivity_seconds": (0.01, 3600.0),
    "sensitivity_interval_s": (0.01, 60.0),
}


def plan_hash(plan):
    """Deterministic SHA-256 of the effective plan definition (overrides included)."""
    blob = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_plan(plan_id, overrides=None):
    """Deep-copies the canonical plan, applies validated PC-smoke overrides,
    and records what was applied. The plan hash covers the EFFECTIVE plan."""
    if plan_id not in PLANS:
        raise ValueError("unknown plan: %r" % (plan_id,))
    plan = json.loads(json.dumps(PLANS[plan_id]))        # deep copy
    applied = {}
    for key, value in (overrides or {}).items():
        if key not in _ALLOWED_OVERRIDES:
            raise ValueError("override not allowed: %r" % key)
        lo, hi = _ALLOWED_OVERRIDES[key]
        if key == "snapshots_at_uptime":
            vals = [float(v) for v in (value or [])]
            if not vals or len(vals) > 6 or not all(lo <= v <= hi for v in vals):
                raise ValueError("bad snapshots_at_uptime")
            plan["snapshots_at_uptime"] = vals
            applied[key] = vals
            continue
        num = float(value)
        if not (lo <= num <= hi):
            raise ValueError("override %s out of range" % key)
        applied[key] = num
        if plan["kind"] == "decay":
            if key == "duration_s":
                plan["duration_s"] = num
            elif key == "sample_interval_s":
                plan["sample_interval_s"] = num
        else:
            for w in plan["windows"]:
                if key == "settle_s":
                    w["settle_s"] = num
                elif key == "warm_after_uptime_s" and "after_uptime_s" in w:
                    w["after_uptime_s"] = num
                elif key == "window_seconds" and not w.get("average_only"):
                    w["seconds"] = num
                elif key == "window_interval_s" and not w.get("average_only"):
                    w["interval_s"] = num
                elif key == "sensitivity_seconds" and w.get("average_only"):
                    w["seconds"] = num
                elif key == "sensitivity_interval_s" and w.get("average_only"):
                    w["interval_s"] = num
    plan["overrides"] = applied
    return plan


# ---------------------------------------------------------------------------
# /proc readers (all routed through _PROC_ROOT so PC tests can fake the tree)
# ---------------------------------------------------------------------------

def _read_proc_text(*parts):
    try:
        with open(os.path.join(_PROC_ROOT, *parts), "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _parse_stat(raw):
    """(pid, comm, utime, stime, starttime) from a /proc/*/stat line, or None.
    comm may contain spaces/parens — split after the LAST ')' (same parsing as
    the deployed /api/v2/debug/proc-stat endpoint)."""
    if not raw or ")" not in raw:
        return None
    try:
        head, tail = raw.rsplit(")", 1)
        pid = int(head.split(" ", 1)[0])
        fields = tail.split()
        return {"pid": pid, "comm": head.split("(", 1)[1],
                "utime": int(fields[11]), "stime": int(fields[12]),
                "starttime": int(fields[19])}
    except (IndexError, ValueError):
        return None


def _clk_tck():
    try:
        return int(os.sysconf("SC_CLK_TCK")), "sysconf"
    except (AttributeError, ValueError, OSError):
        return CLK_TCK_FALLBACK, "fallback_%d" % CLK_TCK_FALLBACK


def _btime():
    raw = _read_proc_text("stat")
    if raw:
        for line in raw.splitlines():
            if line.startswith("btime "):
                try:
                    return float(line.split()[1])
                except (IndexError, ValueError):
                    break
    return None


def process_start_wall():
    """(wall_seconds, source|reason) — boot time + /proc/self/stat starttime."""
    raw = _read_proc_text("self", "stat")
    stat = _parse_stat(raw) if raw else None
    btime = _btime()
    if stat is None:
        return None, "proc self/stat unreadable"
    if btime is None:
        return None, "proc stat btime unreadable"
    hz, _ = _clk_tck()
    return btime + stat["starttime"] / float(hz), "proc_starttime_btime"


def _self_stat():
    raw = _read_proc_text("self", "stat")
    return _parse_stat(raw) if raw else None


def _task_table():
    """{tid: {"comm", "utime", "stime", "ticks"}} from /proc/self/task/*/stat."""
    root = os.path.join(_PROC_ROOT, "self", "task")
    try:
        tids = os.listdir(root)
    except OSError:
        return {}
    out = {}
    for tid in tids:
        raw = _read_proc_text("self", "task", tid, "stat")
        stat = _parse_stat(raw) if raw else None
        if stat:
            out[int(tid)] = {"comm": stat["comm"],
                             "utime": stat["utime"], "stime": stat["stime"],
                             "ticks": stat["utime"] + stat["stime"]}
    return out


def _task_ticks(tid):
    raw = _read_proc_text("self", "task", str(tid), "stat")
    stat = _parse_stat(raw) if raw else None
    return None if stat is None else stat["utime"] + stat["stime"]


# ---------------------------------------------------------------------------
# Environment / device state (spec §4 header; device-only paths NOT RUN)
# ---------------------------------------------------------------------------

def env_tag():
    if _on_android():
        return "device"
    if os.environ.get("SITECREW_TEST") == "1":
        return "pc_smoke"
    return "pc"


def _on_android():
    if not os.path.isdir("/data/data"):
        return False
    try:
        import jnius  # noqa: F401
        return True
    except Exception:
        return False


def device_state():
    """Window-boundary device state. Every field is null with a reason when
    unreadable — never guessed. The pyjnius path is device-only (spec §12:
    NOT RUN); PC returns nulls with the reason recorded."""
    fields = ("screen", "charging", "battery_saver", "doze",
              "thermal_status", "level")
    out = {k: None for k in fields}
    reasons = {}
    try:
        from jnius import autoclass                    # APK only
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        ctx = PythonActivity.mActivity.getApplicationContext()
        pm = ctx.getSystemService(ctx.POWER_SERVICE)

        def pm_call(attr, label):
            try:
                return bool(getattr(pm, attr)())
            except Exception as e:
                reasons[label] = "pyjnius %s failed: %s" % (label, str(e)[:60])
                return None

        out["screen"] = pm_call("isInteractive", "screen")
        out["doze"] = pm_call("isDeviceIdleMode", "doze")
        out["battery_saver"] = pm_call("isPowerSaveMode", "battery_saver")
        try:
            out["thermal_status"] = int(pm.getCurrentThermalStatus())
        except Exception as e:
            reasons["thermal_status"] = "getCurrentThermalStatus failed: %s" % str(e)[:60]
        try:
            BM = autoclass("android.os.BatteryManager")
            intent = ctx.registerReceiver(None, BM.ACTION_BATTERY_CHANGED)
            if intent is None:
                reasons["charging"] = reasons["level"] = "battery intent null"
            else:
                plugged = intent.getIntExtra(BM.EXTRA_PLUGGED, 0)
                out["charging"] = plugged != 0
                scale = intent.getIntExtra(BM.EXTRA_SCALE, -1)
                level = intent.getIntExtra(BM.EXTRA_LEVEL, -1)
                out["level"] = round(100.0 * level / scale, 1) if scale > 0 else None
        except Exception as e:
            reasons["charging"] = reasons["level"] = "battery read failed: %s" % str(e)[:60]
    except Exception as e:
        reason = "%s: pyjnius unavailable (%s)" % (env_tag(), str(e)[:60])
        for k in fields:
            out[k] = None
            if k not in reasons:
                reasons[k] = reason
    return dict(out, reasons=reasons)


def module_facts(base_dir, names=FROZEN_MODULES):
    """Runtime hashes + sizes + mtimes for the frozen modules and the served
    poll.js (§4). Unreadable -> nulls with a reason. Names are repo-relative
    (never absolute paths) so records are redaction-safe by construction."""
    facts = {}
    for name in names:
        path = os.path.join(base_dir, name.replace("/", os.sep))
        entry = {"sha256": None, "bytes": None, "mtime": None, "reason": None}
        try:
            with open(path, "rb") as f:
                data = f.read()
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["bytes"] = len(data)
            entry["mtime"] = int(os.path.getmtime(path))
        except OSError as e:
            entry["reason"] = "unreadable: %s" % e.__class__.__name__
        facts[name] = entry
    return facts


def _hash_drift(base_dir, header_facts):
    """Names whose runtime hash changed since the header (spec §8 rule 1 end drift)."""
    changed = []
    for name, old in (header_facts or {}).items():
        if not isinstance(old, dict):
            continue
        now = module_facts(base_dir, (name,))[name]
        if now["sha256"] != old["sha256"]:
            changed.append(name)
    return changed


# ---------------------------------------------------------------------------
# Thread registry: which OS tids belong to the diag apparatus
# ---------------------------------------------------------------------------

class ThreadRegistry:
    """native_id (= gettid = /proc tid on Linux) per diag thread, so the
    sampler can read each diag thread's ticks and subtract them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}                     # tid -> name

    def add(self, name):
        try:
            tid = threading.get_native_id()
        except Exception:
            tid = None
        if tid is not None:
            with self._lock:
                self._entries[tid] = name
        return tid

    def remove(self, tid):
        if tid is not None:
            with self._lock:
                self._entries.pop(tid, None)

    def snapshot(self):
        with self._lock:
            return sorted(self._entries.items())

    def name_for(self, tid):
        with self._lock:
            return self._entries.get(tid)


# ---------------------------------------------------------------------------
# Phone-local poll clients (spec §2)
# ---------------------------------------------------------------------------

def _error_class(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ConnectionResetError):
        return "connection-reset"
    if isinstance(exc, ConnectionRefusedError):
        return "connection-refused"
    if isinstance(exc, BrokenPipeError):
        return "broken-pipe"
    name = exc.__class__.__name__
    return {"RemoteDisconnected": "remote-disconnected",
            "BadStatusLine": "bad-status-line",
            "CannotSendRequest": "bad-state"}.get(name, "other:" + name[:40])


def _status_class(status):
    return "%dxx" % (status // 100) if 100 <= status < 600 else "other"


class ClientGroup:
    """N phone-local diagclient threads against the app's loopback port.

    Each client: random cid, http.client GET /api/v2/poll?streams=<s>:<head>
    &wait=<DIAG_WAIT_S>&cid=<cid>, reads the body, closes the response,
    updates its cursor from the response heads, and re-polls immediately.
    Errors sleep CLIENT_ERROR_BACKOFF_S (never spin). Counters are shared with
    the sampler under a lock. The labeled leaky arm skips resp.read()/close()
    (worst-case legacy shape) and is informational only."""

    def __init__(self, run, count, leaky=False, cids=None):
        self.run = run
        self.count = int(count)
        self.leaky = bool(leaky)
        self.stop = threading.Event()
        self._lock = threading.Lock()
        cids = list(cids or [])
        self.clients = [{"index": i, "name": "diagclient-%d" % i,
                         "cid": cids[i] if i < len(cids) else uuid.uuid4().hex,
                         "cursor": None,
                         "started": 0, "returned": 0, "errors": 0,
                         "unread_responses": 0, "close_errors": 0,
                         "inflight": False,
                         "wake_reasons": {}, "response_classes": {},
                         "last_wake_reason": None, "last_error_class": None,
                         "last_error_at": None}
                        for i in range(self.count)]
        self.threads = []

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        for i in range(self.count):
            t = threading.Thread(target=self._client_loop, args=(i,),
                                 name="diagclient-%d" % i, daemon=True)
            t.start()
            self.threads.append(t)

    def stop_and_join(self, timeout=DIAG_HTTP_TIMEOUT_S + 10):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=timeout)
        return [t.name for t in self.threads if t.is_alive()]

    # -- sampling view -----------------------------------------------------
    def parked_diag(self):
        with self._lock:
            return sum(1 for c in self.clients if c["inflight"])

    def aggregates(self):
        with self._lock:
            wakes, classes = {}, {}
            started = returned = errors = unread = close_err = 0
            last_err_class = None
            for c in self.clients:
                started += c["started"]
                returned += c["returned"]
                errors += c["errors"]
                unread += c["unread_responses"]
                close_err += c["close_errors"]
                if c["last_error_class"]:
                    last_err_class = c["last_error_class"]
                for k, v in c["wake_reasons"].items():
                    wakes[k] = wakes.get(k, 0) + v
                for k, v in c["response_classes"].items():
                    classes[k] = classes.get(k, 0) + v
            return {"client_started": started, "client_returned": returned,
                    "client_errors": errors, "client_unread_responses": unread,
                    "client_close_errors": close_err,
                    "last_error_class": last_err_class,
                    "wake_reasons": dict(wakes), "response_classes": dict(classes)}

    def client_stats(self):
        with self._lock:
            return [{"index": c["index"], "name": c["name"], "cid": c["cid"],
                     "started": c["started"], "returned": c["returned"],
                     "errors": c["errors"],
                     "unread_responses": c["unread_responses"],
                     "close_errors": c["close_errors"],
                     "wake_reasons": dict(c["wake_reasons"]),
                     "response_classes": dict(c["response_classes"]),
                     "last_wake_reason": c["last_wake_reason"],
                     "last_error_class": c["last_error_class"]}
                    for c in self.clients]

    # -- the client loop ---------------------------------------------------
    def _client_loop(self, i):
        c = self.clients[i]
        tid = self.run.registry.add(c["name"])
        try:
            while not self.stop.is_set():
                if c["cursor"] is None:
                    c["cursor"] = self.run.initial_cursor()
                    if c["cursor"] is None:            # review #5: never cursor-0 fallback
                        with self._lock:
                            c["errors"] += 1
                            c["last_error_class"] = "head-fetch-failed"
                        if self.stop.wait(CLIENT_ERROR_BACKOFF_S):
                            break
                        continue
                self._one_poll(c)
        finally:
            self.run.registry.remove(tid)

    def _one_poll(self, c):
        conn = http.client.HTTPConnection("127.0.0.1", self.run.port,
                                          timeout=DIAG_HTTP_TIMEOUT_S)
        c["inflight"] = True
        t0 = time.monotonic()
        try:
            path = "/api/v2/poll?streams=%s:%d&wait=%s&cid=%s" % (
                CLIENT_STREAM, c["cursor"], DIAG_WAIT_S, c["cid"])
            with self._lock:
                c["started"] += 1               # counted when ISSUED
            conn.request("GET", path)
            resp = conn.getresponse()
            reason = resp.getheader("X-Wake-Reason") or "absent"
            status, cls = resp.status, _status_class(resp.status)
            with self._lock:
                c["response_classes"][cls] = c["response_classes"].get(cls, 0) + 1
            if self.leaky:
                # Informational-only arm (spec §2): deliberately abandon the
                # response unread/unclosed — worst-case legacy shape. Never
                # counts toward any pass.
                return
            try:
                body = resp.read()                      # ALWAYS read
            except Exception:
                with self._lock:
                    c["unread_responses"] += 1
                raise
            try:
                resp.close()                            # ALWAYS close
            except Exception:
                with self._lock:
                    c["close_errors"] += 1
            if status != 200:
                with self._lock:
                    c["errors"] += 1
                    c["last_error_class"] = "http-%d" % status
                    c["last_error_at"] = round(time.monotonic() - t0, 3)
                if self.stop.wait(CLIENT_ERROR_BACKOFF_S):
                    return
                return
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
                heads = parsed.get("heads") or {}
                if CLIENT_STREAM in heads:
                    c["cursor"] = int(heads[CLIENT_STREAM])
            except Exception:
                with self._lock:
                    c["unread_responses"] += 1
                    c["last_error_class"] = "bad-body"
            with self._lock:
                c["returned"] += 1
                c["wake_reasons"][reason] = c["wake_reasons"].get(reason, 0) + 1
                c["last_wake_reason"] = reason
            # immediate re-poll: the loop continues without sleeping
        except Exception as e:
            with self._lock:
                c["errors"] += 1
                c["last_error_class"] = _error_class(e)
                c["last_error_at"] = round(time.monotonic() - t0, 3)
            if self.stop.wait(CLIENT_ERROR_BACKOFF_S):
                return
        finally:
            c["inflight"] = False
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# JSONL writer (flush + fsync per record — survives adb loss / SIGKILL)
# ---------------------------------------------------------------------------

class JsonlWriter:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()      # footer + snapshot threads may race
        self.path = path
        self.count = 0
        self.bytes = 0

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self._f.write(line)
            self._f.flush()
            os.fsync(self._f.fileno())                 # durability per record
            self.count += 1
            self.bytes += len(line.encode("utf-8"))

    def close(self):
        with self._lock:
            try:
                self._f.flush()
                os.fsync(self._f.fileno())
            finally:
                self._f.close()


# ---------------------------------------------------------------------------
# One diagnostic run (plan -> one process lifetime)
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, plan_id, effective_plan, base_dir, port, source,
                 start_after_uptime_s=0.0, boot_pickup_note=None,
                 leaky=False):
        self.plan_id = plan_id
        self.plan = effective_plan
        self.base_dir = base_dir
        self.port = int(port)
        self.source = source
        self.start_after_uptime_s = float(start_after_uptime_s or 0.0)
        self.boot_pickup_note = boot_pickup_note or {}
        self.leaky = leaky
        self.registry = ThreadRegistry()
        self.stop = threading.Event()
        self.run_id = uuid.uuid4().hex
        self.state = "running"
        self.finished = False
        self.error_detail = None
        self.window = None
        self.window_index = -1
        self.last_sample_mono = None
        self.records_path = os.path.join(base_dir, RUNS_DIR_NAME,
                                         self.run_id + ".jsonl")
        self.writer = None
        self.thread = None
        self._snap_thread = None
        self._group = None
        self._gate = {"status": "not_started"}
        self._start_wall, self._uptime_source = process_start_wall()
        self._header_facts = None
        self._head_lock = threading.Lock()
        self._head_cache = None
        self._head_fetch = {"ok": None, "via": None, "head": None}
        self.lingering_clients = []
        # client cids are generated ONCE per run and recorded in the header,
        # so the offline analyzer can detect a stray cid in any window (§8 r6)
        self.client_registry = []
        for w in self._windows():
            if w["clients"] > 0 and not w.get("leaky"):
                for i in range(w["clients"]):
                    self.client_registry.append(
                        {"window": w["name"], "index": i, "cid": uuid.uuid4().hex})

    # -- helpers -----------------------------------------------------------
    def uptime_s(self):
        if self._start_wall is None:
            return None
        return round(time.time() - self._start_wall, 3)

    def initial_cursor(self):
        """Real head for the client stream (cached per run). Primary: the
        debug store-state endpoint. Fallback (minimal test/PC apps without
        it): a wait=0 poll whose response heads carry the true head — never a
        cursor-0 fallback (review #5 lesson)."""
        with self._head_lock:
            if self._head_cache is not None:
                return self._head_cache
            head = self._fetch_head()
            self._head_cache = head
            return head

    def _fetch_head(self):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", "/api/v2/debug/store-state?db=main")
                resp = conn.getresponse()
                body = resp.read()
                resp.close()
                if resp.status == 200:
                    head = int(json.loads(body.decode("utf-8", "replace")).get("head", 0))
                    self._head_fetch = {"ok": True, "via": "store-state", "head": head}
                    return head
            finally:
                conn.close()
        except Exception:
            pass
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", "/api/v2/poll?streams=%s:0&wait=0&cid=%s"
                             % (CLIENT_STREAM, uuid.uuid4().hex))
                resp = conn.getresponse()
                body = resp.read()
                resp.close()
                if resp.status == 200:
                    heads = json.loads(body.decode("utf-8", "replace")).get("heads") or {}
                    head = int(heads.get(CLIENT_STREAM, -1))
                    if head >= 0:
                        self._head_fetch = {"ok": True, "via": "poll_wait0", "head": head}
                        return head
            finally:
                conn.close()
        except Exception:
            pass
        self._head_fetch = {"ok": False, "via": None, "reason": "both head fetches failed"}
        return None

    def _diag_ticks(self):
        buckets = {"sampler": 0, "clients": 0, "other": 0}
        live = 0
        for tid, name in self.registry.snapshot():
            ticks = _task_ticks(tid)
            if ticks is None:
                continue
            live += 1
            if name.startswith("diagclient"):
                buckets["clients"] += ticks
            elif name == "diag-sampler":
                buckets["sampler"] += ticks
            else:
                buckets["other"] += ticks
        return buckets, live

    def _server_poll_threads(self):
        """Threads currently inside THIS app's api_v2 poll route (raw count).

        sys._current_frames() is process-global: when several app servers
        share one process (PC smoke only) a frame's werkzeug handler exposes
        its own listening port via frame locals, and foreign-app parks are
        excluded. On the device there is one app, so every park belongs here;
        when the port cannot be determined the park is counted (conservative,
        device behavior unchanged)."""
        n = 0
        try:
            frames = _current_frames()
        except Exception:
            return None
        for fr in frames.values():
            f = fr
            hops = 0
            while f is not None and hops < 64:
                try:
                    if (f.f_code.co_name == "poll"
                            and f.f_code.co_filename.replace("\\", "/").endswith(
                                "sitecrew/api_v2.py")):
                        port = _frame_handler_port(f)
                        # a real executing route frame always has a caller;
                        # a caller-less frame is a suspended/dead-frame
                        # artifact (e.g. generator frames) — never counted
                        if port == self.port or (port is None
                                                 and getattr(f, "f_back", None) is not None):
                            n += 1
                        break
                    f = getattr(f, "f_back", None)
                    hops += 1
                except Exception:
                    break
        return n

    # -- records -----------------------------------------------------------
    def _write_header(self):
        clk, clk_src = _clk_tck()
        facts = module_facts(self.base_dir)
        self._header_facts = facts
        gate = self._hash_gate(facts)
        # The pid that owns the sampled ticks is /proc/self/stat's pid — the
        # same as os.getpid() on the device; if /proc is unreadable (pc), fall
        # back and record why (samples carry the same pid consistently).
        st = _self_stat()
        pid = st["pid"] if st else os.getpid()
        header = {
            "type": "header",
            "schema": SCHEMA_ID,
            "environment": env_tag(),
            "run_id": self.run_id,
            "pid": pid,
            "pid_source": ("proc_self_stat" if st else
                           "os_getpid (proc self/stat unreadable)"),
            "plan_id": self.plan_id,
            "plan_hash": plan_hash(self.plan),
            "plan_overrides": self.plan.get("overrides"),
            "boot_kind_declared": self.plan.get("boot_kind"),
            "boot_plan_pickup": self.boot_pickup_note,
            "run_source": self.source,
            "process_start": {
                "wall": round(self._start_wall, 3) if self._start_wall else None,
                "source": self._uptime_source if self._start_wall else None,
                "reason": None if self._start_wall else self._uptime_source,
            },
            "launch_gap_s": (round(time.time() - self._start_wall, 3)
                             if self._start_wall else None),
            "launch_gap_reason": None if self._start_wall else "process start unknown",
            "start_after_uptime_s": self.start_after_uptime_s,
            "clk_tck": clk,
            "clk_tck_source": clk_src,
            "cpu_count": os.cpu_count(),
            "python_version": _platform.python_version(),
            "python_full": "%d.%d.%d" % sys.version_info[:3],
            "sqlite_version": sqlite3.sqlite_version,
            "modules": facts,
            "expected_hashes": self.plan.get("expected_hashes"),
            "hash_gate": gate,
            "device_state": device_state(),
            "webview": {"expected": "about:blank in diag mode",
                        "verified": None, "reason": WEBVIEW_REASON},
            "diag_wait_s": DIAG_WAIT_S,
            "client_stream": CLIENT_STREAM,
            "client_registry": [dict(e) for e in self.client_registry],
            "leaky_legacy": self.leaky,
            "records_dir_name": RUNS_DIR_NAME.replace(os.sep, "/"),
        }
        self.writer.write(header)
        return gate

    def _hash_gate(self, facts):
        """Declared-vs-runtime hash gate (spec §8 rule 1). A mismatch aborts
        the run before any sample is taken."""
        expected = self.plan.get("expected_hashes")
        frozen_ok = sorted(facts) == sorted(FROZEN_MODULES)
        if not expected:
            return {"status": "not_declared", "mismatches": {},
                    "frozen_list_ok": frozen_ok}
        if not isinstance(expected, dict):
            return {"status": "mismatch",
                    "mismatches": {"__list__": "expected_hashes must be an object"},
                    "frozen_list_ok": frozen_ok}
        mismatches = {}
        for name, want in expected.items():
            got = facts.get(name, {}).get("sha256")
            if got != want:
                mismatches[name] = {"expected": want, "runtime": got}
        missing = [n for n in FROZEN_MODULES if n not in expected]
        if missing:
            mismatches["__frozen_list__"] = {"missing_from_expected": missing}
        return {"status": "mismatch" if mismatches else "ok",
                "mismatches": mismatches,
                "frozen_list_ok": frozen_ok}

    def _write_sample(self, window, window_index, phase, seq, interval_s):
        now_wall = time.time()
        now_mono = time.monotonic()
        st = _self_stat()
        buckets, live = self._diag_ticks()
        group = self._group
        parked_diag = group.parked_diag() if group else 0
        agg = group.aggregates() if group else {
            "client_started": 0, "client_returned": 0, "client_errors": 0,
            "client_unread_responses": 0, "client_close_errors": 0,
            "last_error_class": None, "wake_reasons": {}, "response_classes": {}}
        poll_threads = self._server_poll_threads()
        record = {
            "type": "sample",
            "ts": round(now_wall, 3),
            "mono": round(now_mono, 3),
            "uptime_s": self.uptime_s(),
            "window": window,
            "window_index": window_index,
            "phase": phase,
            "seq": seq,
            "interval_s": interval_s,
            "dt_s": (round(now_mono - self.last_sample_mono, 3)
                     if self.last_sample_mono is not None else None),
            "pid": st["pid"] if st else None,
            "utime": st["utime"] if st else None,
            "stime": st["stime"] if st else None,
            "ticks": (st["utime"] + st["stime"]) if st else None,
            "ticks_reason": None if st else "proc self/stat unreadable",
            "sampler_ticks": buckets["sampler"],
            "client_ticks": buckets["clients"],
            "diag_ticks_other": buckets["other"],
            "diag_thread_count": live,
            "server_poll_threads": poll_threads,
            "parked_diag": parked_diag,
            "parked_other": (max(0, poll_threads - parked_diag)
                             if poll_threads is not None else None),
            "wake_reasons": agg["wake_reasons"],
            "response_classes": agg["response_classes"],
            "client_started": agg["client_started"],
            "client_returned": agg["client_returned"],
            "client_errors": agg["client_errors"],
            "client_unread_responses": agg["client_unread_responses"],
            "client_close_errors": agg["client_close_errors"],
            "last_error_class": agg["last_error_class"],
        }
        if self.plan.get("per_thread_samples"):
            table = _task_table()
            record["threads"] = [{"tid": tid, "comm": v["comm"],
                                  "ticks": v["ticks"],
                                  "name": self.registry.name_for(tid)
                                  or _python_thread_name(tid)}
                                 for tid, v in sorted(table.items())]
        self.writer.write(record)
        self.last_sample_mono = now_mono

    def _write_window_marker(self, boundary, w, index, extra=None):
        record = {
            "type": "window_marker",
            "boundary": boundary,
            "window": w["name"],
            "window_index": index,
            "clients": w["clients"],
            "leaky": bool(w.get("leaky")),
            "settle_s": w.get("settle_s", 0),
            "interval_s": w.get("interval_s", 5),
            "planned_seconds": w["seconds"],
            "average_only": bool(w.get("average_only")),
            "informational_only": bool(w.get("informational_only")),
            "after_uptime_s": w.get("after_uptime_s"),
            "ts": round(time.time(), 3),
            "mono": round(time.monotonic(), 3),
            "uptime_s": self.uptime_s(),
            "device_state": device_state(),
        }
        if extra:
            record.update(extra)
        self.writer.write(record)

    def _write_footer(self, reason, detail=None):
        record = {
            "type": "footer",
            "reason": reason,
            "detail": detail,
            "ts": round(time.time(), 3),
            "mono": round(time.monotonic(), 3),
            "uptime_s": self.uptime_s(),
            # total records in the file INCLUDING this footer
            "records_written": (self.writer.count + 1) if self.writer else 1,
            "hash_drift": _hash_drift(self.base_dir, self._header_facts),
            "device_state": device_state(),
        }
        self.writer.write(record)

    # -- snapshot (decay runs only; spec §5) --------------------------------
    def _snapshot_loop(self):
        for target in self.plan.get("snapshots_at_uptime") or []:
            while not self.stop.is_set():
                up = self.uptime_s()
                if up is None:
                    self._write_snapshot(target, missed=True,
                                         reason="process uptime unknown")
                    break
                wait = target - up
                if wait <= 0:
                    if abs(up - target) > SNAPSHOT_TOLERANCE_S:
                        self._write_snapshot(target, missed=True,
                                             reason="uptime target missed "
                                                    "(%.1fs vs %.1fs)" % (up, target))
                    else:
                        self._write_snapshot(target)
                    break
                self.stop.wait(min(wait, 1.0))
            if self.stop.is_set():
                break

    def _write_snapshot(self, target, missed=False, reason=None):
        tid_self = self.registry.add("diag-snapshot")
        try:
            t0 = _task_table()
            self.stop.wait(SNAPSHOT_GAP_S)
            t1 = _task_table()
            deltas = []
            for tid, v1 in t1.items():
                v0 = t0.get(tid)
                if v0:
                    deltas.append((tid, v1["comm"], v0["ticks"], v1["ticks"],
                                   v1["ticks"] - v0["ticks"]))
            deltas.sort(key=lambda d: d[4], reverse=True)
            ranked = []
            for rank, (tid, comm, ticks0, ticks1, delta) in enumerate(deltas[:TOP_N], 1):
                frames, mapped, py_name = self._frames_for(tid)
                ranked.append({
                    "rank": rank, "tid": tid, "comm": comm,
                    "ticks_start": ticks0, "ticks_end": ticks1,
                    "tick_delta": delta,
                    "mapped": mapped,
                    "python_name": py_name,
                    "display_name": (py_name if mapped else "native:%s" % comm),
                    "frames_samples": frames,
                    "in_park_path": any(
                        (f or "").split("@")[0] == "poll" and "api_v2" in (f or "")
                        for sample in frames for f in sample),
                })
            self.writer.write({
                "type": "snapshot",
                "ts": round(time.time(), 3),
                "mono": round(time.monotonic(), 3),
                "uptime_s": self.uptime_s(),
                "window": self.window,
                "target_uptime_s": target,
                "missed": bool(missed),
                "missed_reason": reason,
                "self_tid": tid_self,
                "task_count": len(t1),
                "gap_s": SNAPSHOT_GAP_S,
                "frames_note": "func@basename:line only, max %d frames; five "
                               "samples %.0f ms apart" % (MAX_FRAMES, FRAME_GAP_S * 1000),
                "ranked": ranked,
            })
        finally:
            self.registry.remove(tid_self)

    def _frames_for(self, tid):
        """Five sys._current_frames() samples 100 ms apart for ONE tid, mapped
        through threading.enumerate(). Unmapped TIDs get no guessed frames."""
        by_native = {}
        py_name = None
        for t in threading.enumerate():
            nid = getattr(t, "native_id", None)
            if nid is not None:
                by_native[nid] = t
        t = by_native.get(tid)
        if t is not None:
            py_name = t.name
        samples = []
        for _ in range(FRAME_SAMPLES):
            if self.stop.is_set():
                break
            try:
                cur = _current_frames()
                if t is not None and t.ident in cur:
                    samples.append(_render_frames(cur[t.ident], MAX_FRAMES))
                else:
                    samples.append([])
            except Exception:
                samples.append([])
            self.stop.wait(FRAME_GAP_S)
        return samples, t is not None, py_name

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        """Writes the header SYNCHRONOUSLY (so the plan POST response can cite
        the hash gate), then starts the diag-sampler thread. On a hash-gate
        fault the run ends immediately and no thread is started."""
        self.writer = JsonlWriter(self.records_path)
        self._tid = self.registry.add("diag-sampler")
        gate = self._write_header()
        self._gate = gate
        if gate["status"] == "mismatch":
            self._finish("hash_gate_fault",
                         detail=json.dumps(gate["mismatches"])[:400])
            return
        self.thread = threading.Thread(target=self._sampler_loop,
                                       name="diag-sampler", daemon=True)
        self.thread.start()

    def abort(self):
        """Cooperative stop: the sampler wakes at its next stop.wait and
        writes the footer; clients see the same event between polls."""
        self.stop.set()

    def _sampler_loop(self):
        try:
            if self.start_after_uptime_s > 0:
                # deliberate defer (recorded in the header); launch-gap rule
                # still measured header-write vs process start
                while not self.stop.is_set():
                    up = self.uptime_s()
                    if up is not None and up >= self.start_after_uptime_s:
                        break
                    self.stop.wait(0.1)
                if self.stop.is_set():
                    self._finish("aborted")
                    return
            snap = None
            if self.plan["kind"] == "decay":
                snap = threading.Thread(target=self._snapshot_loop,
                                        name="diag-snapshot", daemon=True)
                self._snap_thread = snap
                snap.start()
            for index, w in enumerate(self._windows()):
                if self.stop.is_set() or self.finished:
                    break
                self._run_window(w, index)
            self._finish("aborted" if self.stop.is_set() else "complete")
        except Exception as e:                         # never die silently
            try:
                self._finish("error",
                             detail=("%s: %s" % (e.__class__.__name__, str(e)))[:300])
            except Exception:
                self.state = "error"

    def _windows(self):
        if self.plan["kind"] != "windows":
            return [{
                "name": "decay", "clients": self.plan["clients"],
                "seconds": self.plan["duration_s"],
                "interval_s": self.plan["sample_interval_s"],
                "settle_s": 0,
            }]
        # the optional leaky_legacy arm runs ONLY when explicitly requested
        return [w for w in self.plan["windows"]
                if not (w.get("optional") and not self.leaky)]

    def _run_window(self, w, index):
        self.window = w["name"]
        self.window_index = index
        if w.get("after_uptime_s"):
            while not self.stop.is_set():
                up = self.uptime_s()
                if up is not None and up >= w["after_uptime_s"]:
                    break
                self.stop.wait(0.5)
        self._write_window_marker("start", w, index)
        group = None
        lingering = []
        head_fetch = dict(self._head_fetch)
        measure_sample_count = 0
        settle_sample_count = 0
        window_start_mono = time.monotonic()
        try:
            if w["clients"] > 0:
                head = self.initial_cursor()
                head_fetch = dict(self._head_fetch)
                if not head_fetch.get("ok"):
                    # review #5: a head fetch failure aborts the RUN
                    self._write_window_marker("end", w, index, {
                        "actual_seconds": round(time.monotonic() - window_start_mono, 3),
                        "expected_samples": 0,
                        "measure_sample_count": 0, "settle_sample_count": 0,
                        "head_fetch": head_fetch, "client_stats": [],
                        "client_totals": {"started": 0, "returned": 0, "errors": 0},
                        "lingering_clients": [],
                        "invalid_reason": "head_fetch_failed"})
                    self._finish("error", detail="head_fetch_failed")
                    return
                group = ClientGroup(self, w["clients"], leaky=bool(w.get("leaky")),
                                    cids=self._cids_for(w["name"]))
                self._group = group
                group.start()
            try:
                # settle phase: recorded, excluded by the analyzer later
                if w.get("settle_s"):
                    settle_sample_count = self._sample_phase(
                        w, index, "settle", w["settle_s"], w.get("interval_s", 5))
                measure_sample_count = self._sample_phase(
                    w, index, "measure", w["seconds"], w.get("interval_s", 5))
            finally:
                if group is not None:
                    lingering = group.stop_and_join()
                    self.lingering_clients = lingering
                    self._group = None
            stats = group.client_stats() if group else []
            self._write_window_marker("end", w, index, {
                "actual_seconds": round(time.monotonic() - window_start_mono, 3),
                "expected_samples": _expected_samples(w["seconds"],
                                                      w.get("interval_s", 5)),
                "measure_sample_count": measure_sample_count,
                "settle_sample_count": settle_sample_count,
                "head_fetch": head_fetch,
                "client_stats": stats,
                "client_totals": {"started": sum(s["started"] for s in stats),
                                  "returned": sum(s["returned"] for s in stats),
                                  "errors": sum(s["errors"] for s in stats)},
                "lingering_clients": lingering,
            })
        except Exception as e:
            self._finish("error", detail="window %s: %s" % (w["name"], str(e)[:200]))

    def _cids_for(self, window_name):
        return [e["cid"] for e in self.client_registry
                if e["window"] == window_name]

    def _sample_phase(self, w, index, phase, seconds, interval):
        """Drift-free boundary sampling; returns the number of samples written."""
        count = 0
        started = time.monotonic()
        end = started + seconds
        k = 0
        while not self.stop.is_set():
            target = started + k * interval
            now = time.monotonic()
            if target >= end:
                break
            if target > now:
                self.stop.wait(target - now)
            if self.stop.is_set():
                break
            self._write_sample(w["name"], index, phase, k, interval)
            count += 1
            k += 1
        return count

    def _finish(self, reason, detail=None):
        if self.finished:
            return
        self.finished = True
        self.stop.set()
        if self._group is not None:
            self.lingering_clients = self._group.stop_and_join()
            self._group = None
        if self._snap_thread is not None:
            # the footer must be the LAST record: join the snapshot thread
            # before writing it (the stop event interrupts its waits)
            self._snap_thread.join(timeout=SNAPSHOT_GAP_S + 2)
            self._snap_thread = None
        try:
            self._write_footer(reason, detail)
        except Exception:
            pass
        if self.writer is not None:
            self.writer.close()
        self.state = {"complete": "complete", "aborted": "aborted"}.get(reason, "error")
        self.error_detail = detail
        self.registry.remove(getattr(self, "_tid", None))


def _expected_samples(seconds, interval):
    """Boundary targets in [0, seconds): ceil(seconds/interval) (float-safe)."""
    return int(seconds / interval + 0.999999)


def _frame_handler_port(frame, max_hops=48):
    """Best-effort listening port of the werkzeug handler owning this frame
    (the handler's ``self.request`` socket appears in an ancestor frame's
    locals). None = not determinable."""
    f = frame
    hops = 0
    while f is not None and hops < max_hops:
        try:
            obj = (getattr(f, "f_locals", None) or {}).get("self")
            sock = getattr(obj, "request", None)
            if sock is not None:
                return sock.getsockname()[1]
            f = getattr(f, "f_back", None)
        except Exception:
            return None
        hops += 1
    return None


def _render_frames(frame, max_frames=MAX_FRAMES):
    """Stack as ['func@basename:line', ...] — basename+line only, never
    locals/arguments (spec §10)."""
    out = []
    f = frame
    hops = 0
    while f is not None and hops < max_frames:
        try:
            code = f.f_code
            out.append("%s@%s:%d" % (code.co_name,
                                     os.path.basename(code.co_filename),
                                     f.f_lineno))
            f = getattr(f, "f_back", None)
        except Exception:
            break
        hops += 1
    return out


def _python_thread_name(tid):
    """Best-effort Python-side thread name for a tid (decay per-thread table)."""
    for t in threading.enumerate():
        if getattr(t, "native_id", None) == tid:
            return t.name
    return None


# ---------------------------------------------------------------------------
# Offline validity classification helpers (spec §8) — codes only, NEVER a pass
# ---------------------------------------------------------------------------

INVALID_CODES = (
    "INVALID_HASH",             # rule 1: runtime hash mismatch / end drift / frozen-list mismatch
    "INVALID_PID_DRIFT",        # rule 1 start/end drift sub-case: pid changed mid-run
    "INVALID_TRUNCATED",        # rule 1/2: footer missing (records truncated)
    "INVALID_SAMPLING",         # rule 2: gap >2.5x interval within a phase, <90% expected samples
    "INVALID_CLIENT",           # rule 3: any client error/timeout/unread/unclosed in the window
    "INVALID_PARK",             # rule 4: parked_diag != N in >5% post-settle samples, or any parked_other
    "INVALID_STATE_COMPARE",    # rule 5: screen/charging/battery-saver/Doze mismatch between compared windows
    "INVALID_FOREIGN_TRAFFIC",  # rule 6: poll-path activity not explained by diag clients (>5% of samples)
    "INVALID_LAUNCH_GAP",       # rule 6: launch gap >15 s (every plan restarts fresh)
    "INVALID_STRAY_CID",        # rule 6: window client cid not in the header registry
)


def classify(records):
    """Offline validity classification helpers over parsed JSONL records.

    Returns machine facts + INVALID_* codes ONLY — never a pass/fail claim
    (§4: samples carry raw ticks; the offline analyzer owns §9). Each code's
    rule mapping is documented in INVALID_CODES. Cross-window sample gaps are
    deliberately NOT flagged: the recorded client join at each window end
    (window_marker.actual_seconds) explains them; per-phase gaps and
    expected-sample counts catch a stalled sampler.
    """
    codes = []
    details = {}

    def add(code, detail):
        if code not in codes:
            codes.append(code)
        details.setdefault(code, []).append(detail)

    header = next((r for r in records if r.get("type") == "header"), None)
    footer = next((r for r in reversed(records) if r.get("type") == "footer"), None)
    if header is None:
        return {"codes": ["INVALID_TRUNCATED"],
                "rule_details": {"INVALID_TRUNCATED": ["no header record"]},
                "windows": {}, "state_map": {},
                "notes": ["codes only — the offline analyzer owns §9"]}
    notes = ["codes only — the offline analyzer owns the §9 ruling"]

    # rule 1: hash gate + end drift + frozen list
    gate = header.get("hash_gate") or {}
    if gate.get("status") == "mismatch":
        add("INVALID_HASH", "header hash_gate mismatch: %s"
            % sorted((gate.get("mismatches") or {}).keys()))
    if sorted((header.get("modules") or {}).keys()) != sorted(FROZEN_MODULES):
        add("INVALID_HASH", "frozen module list mismatch")
    if footer is None:
        add("INVALID_TRUNCATED", "no footer record (run truncated)")
    elif footer.get("hash_drift"):
        add("INVALID_HASH", "end hash drift: %s" % footer["hash_drift"])

    # rule 1 sub-case: pid drift
    samples = [r for r in records if r.get("type") == "sample"]
    header_pid = header.get("pid")
    for s in samples:
        if header_pid is not None and s.get("pid") not in (None, header_pid):
            add("INVALID_PID_DRIFT", "sample seq %s pid %s != header pid %s"
                % (s.get("seq"), s.get("pid"), header_pid))
            break

    # rule 2: per-phase sampling gaps + expected counts
    windows = {}
    for r in records:
        if r.get("type") == "window_marker" and r.get("boundary") == "end":
            windows[r["window"]] = r
    last_by_phase = {}
    for s in samples:
        mono, interval = s.get("mono"), s.get("interval_s") or 0
        key = (s.get("window"), s.get("phase"))
        prev = last_by_phase.get(key)
        if prev is not None and mono is not None:
            gap = mono - prev
            if gap > 2.5 * interval:
                add("INVALID_SAMPLING",
                    "gap %.2fs > 2.5x interval %.2fs (window %s phase %s seq %s)"
                    % (gap, interval, key[0], key[1], s.get("seq")))
        last_by_phase[key] = mono if mono is not None else prev
    for name, marker in windows.items():
        expected = marker.get("expected_samples") or 0
        actual = marker.get("measure_sample_count") or 0
        if expected and actual < 0.9 * expected:
            add("INVALID_SAMPLING", "window %s: %d/%d expected samples (<90%%)"
                % (name, actual, expected))

    # rule 3: client errors per window
    for name, marker in windows.items():
        totals = marker.get("client_totals") or {}
        if totals.get("errors", 0) or marker.get("invalid_reason"):
            add("INVALID_CLIENT", "window %s: client errors=%s invalid_reason=%s"
                % (name, totals.get("errors", 0), marker.get("invalid_reason")))

    # rule 4: park counters on post-settle (measure-phase) samples
    per_window_samples = {}
    for s in samples:
        if s.get("phase") == "measure":
            per_window_samples.setdefault(s.get("window"), []).append(s)
    for name, ss in per_window_samples.items():
        marker = windows.get(name) or {}
        n_clients = marker.get("clients", 0)
        bad_park = [s for s in ss if s.get("parked_diag") is not None
                    and s.get("parked_diag") != n_clients]
        if ss and len(bad_park) > 0.05 * len(ss):
            add("INVALID_PARK", "window %s: parked_diag != %d in %d/%d samples"
                % (name, n_clients, len(bad_park), len(ss)))
        other = [s for s in ss if (s.get("parked_other") or 0) > 0]
        if other:
            add("INVALID_PARK", "window %s: parked_other > 0 in %d samples"
                % (name, len(other)))

    # rule 5: device state across compared windows (level deliberately not gated)
    state_map = {name: {k: (marker.get("device_state") or {}).get(k)
                        for k in ("screen", "charging", "battery_saver", "doze")}
                 for name, marker in windows.items()}
    groups = {}
    for name in state_map:
        groups.setdefault(name.split("-")[0], []).append(name)
    for group, names in sorted(groups.items()):
        base = state_map[names[0]]
        for other in names[1:]:
            if state_map[other] != base:
                add("INVALID_STATE_COMPARE",
                    "window %s state differs from %s within group %r"
                    % (other, names[0], group))
                break

    # rule 6: foreign poll traffic + launch gap + stray cid
    for name, ss in per_window_samples.items():
        marker = windows.get(name) or {}
        n_clients = marker.get("clients", 0)
        foreign = [s for s in ss
                   if s.get("server_poll_threads") is not None
                   and s.get("server_poll_threads", 0) > (s.get("parked_diag") or 0)
                   and s.get("parked_diag", 0) >= n_clients]
        if ss and len(foreign) > 0.05 * len(ss):
            add("INVALID_FOREIGN_TRAFFIC",
                "window %s: unexplained poll-path activity in %d/%d samples"
                % (name, len(foreign), len(ss)))
    launch = header.get("launch_gap_s")
    if launch is not None and launch > LAUNCH_GAP_MAX_S:
        add("INVALID_LAUNCH_GAP", "launch gap %.1fs > %.0fs"
            % (launch, LAUNCH_GAP_MAX_S))
    registry_cids = {c.get("cid") for c in header.get("client_registry") or []}
    for name, marker in windows.items():
        if marker.get("leaky") or marker.get("informational_only"):
            continue                     # the leaky arm rotates cids by design
        for c in marker.get("client_stats") or []:
            if c.get("cid") is not None and c.get("cid") not in registry_cids:
                add("INVALID_STRAY_CID", "window %s: client cid not in registry" % name)
                break

    return {"codes": codes, "rule_details": details, "windows": windows,
            "state_map": state_map, "notes": notes}


# ---------------------------------------------------------------------------
# Public-redaction helpers (spec §10; the full redaction script comes later)
# ---------------------------------------------------------------------------

_REDACTION_PATTERNS = (
    ("windows-drive-path", r"[A-Za-z]:\\"),
    ("unix-owner-path", r"/(?:home|Users|root|data/data)/"),
    ("host-port", r"(?:127\.0\.0\.1|localhost):\d+"),
    ("ipv4", r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("nvidia-key", r"nvapi-"),
    ("generic-secret", r"\bsk-[A-Za-z0-9]{8,}"),
)


def redaction_scan(text):
    """Deterministic scan for secrets/paths/ports/IPs in records text (§10
    pre-publish check). Returns {'clean': bool, 'hits': [...]} — kinds and
    offsets only, never the matched content."""
    hits = []
    for kind, pattern in _REDACTION_PATTERNS:
        for m in re.finditer(pattern, text):
            hits.append({"kind": kind, "pos": m.start()})
    return {"clean": not hits, "hits": hits}


def short_cid(cid, salt=""):
    """Public redaction of a client cid: first 8 hex of its salted SHA-256
    (the salt stays private to the redaction environment, never in records)."""
    return hashlib.sha256((salt or "").encode("utf-8")
                          + str(cid).encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Flask registration (minimal, flag-gated; called from server/app.py)
# ---------------------------------------------------------------------------

def register_diag(app, base_dir, port=None):
    """Register /api/v2/debug/diag/* on the app. Gated by the compile-time
    build_flags.DEBUG_ENDPOINTS constant: when False this registers NOTHING
    (routes absent -> 404; cpu_diag stays unloaded in flag-off builds)."""
    import build_flags as bf
    if not getattr(bf, "DEBUG_ENDPOINTS", False):
        return False
    if getattr(app, "extensions", {}).get("cpu_diag"):
        return True                                  # idempotent
    base_dir = os.path.abspath(base_dir)
    runs_dir = os.path.join(base_dir, RUNS_DIR_NAME)
    # The routes exist ONLY because the compile-time constant was True at
    # registration (i.e. in this build); that fact is captured here and never
    # re-read per request (the constant cannot change in a running process).
    state = {"base_dir": base_dir, "runs_dir": runs_dir,
             "port": port, "flag_on": True,   # resolved lazily per run
             "current": None, "last": None, "last_status": None,
             "last_status_ts": 0.0, "lock": threading.Lock()}
    app.extensions["cpu_diag"] = state
    os.makedirs(runs_dir, exist_ok=True)

    from flask import jsonify, request, Response

    def _enabled():
        return bool(state.get("flag_on"))

    @app.post("/api/v2/debug/diag/plan")
    def diag_plan_post():
        if not _enabled():
            return jsonify({"error": "diag flag off"}), 404
        body = request.get_json(silent=True) or {}
        try:
            plan = build_plan(str(body.get("plan", "")), body.get("overrides"))
        except ValueError as e:
            return jsonify({"error": "bad_plan", "detail": str(e)[:200]}), 400
        try:
            defer = max(0.0, min(float(body.get("start_after_uptime_s") or 0), 86400.0))
        except (TypeError, ValueError):
            return jsonify({"error": "bad_start_after_uptime_s"}), 400
        leaky = bool(body.get("leaky_legacy"))
        if leaky and plan["id"] != "P-W":
            return jsonify({"error": "leaky_legacy only applies to P-W"}), 400
        expected = body.get("expected_hashes")
        if expected is not None:
            if not isinstance(expected, dict):
                return jsonify({"error": "bad_expected_hashes"}), 400
            plan["expected_hashes"] = {str(k): str(v) for k, v in expected.items()}
        with state["lock"]:
            if state["current"] is not None and not state["current"].finished:
                return jsonify({"error": "run_already_active",
                                "run_id": state["current"].run_id}), 409
            if body.get("boot"):
                _stage_boot_plan(base_dir, plan, defer, leaky)
                return jsonify({"ok": True, "staged": True, "file": BOOT_PLAN_FILE,
                                "plan": plan["id"], "plan_hash": plan_hash(plan)})
            run = _start_run(plan, plan["id"], base_dir,
                             _resolve_port(state["port"]),
                             source="endpoint", defer=defer, leaky=leaky,
                             boot_pickup_note={"found": False, "adopted": False,
                                               "reason": "started via endpoint"})
            state["current"] = run
            state["last"] = run
        return jsonify({"ok": True, "run_id": run.run_id, "state": run.state,
                        "plan": plan["id"], "plan_hash": plan_hash(plan),
                        "hash_gate": run._gate})

    @app.get("/api/v2/debug/diag/status")
    def diag_status():
        if not _enabled():
            return jsonify({"error": "diag flag off"}), 404
        with state["lock"]:
            run = state["current"]
            now = time.monotonic()
            active = run is not None and not run.finished
            if active and state["last_status"] is not None \
                    and now - state["last_status_ts"] < STATUS_CACHE_S:
                # spec §7: during a run, status recompute at most once per 60 s
                payload = dict(state["last_status"])
                payload["cached"] = True
                payload["cache_age_s"] = round(now - state["last_status_ts"], 3)
                return jsonify(payload)
            payload = _status_payload(run, state)
            if active:
                state["last_status"] = dict(payload)
                state["last_status_ts"] = now
        return jsonify(payload)

    @app.get("/api/v2/debug/diag/result")
    def diag_result():
        if not _enabled():
            return jsonify({"error": "diag flag off"}), 404
        run_id = request.args.get("run_id") or (state["last"].run_id if state["last"] else None)
        if not run_id or not _RUN_ID_OK.match(run_id):
            return jsonify({"error": "bad_run_id"}), 404
        runs_abs = os.path.abspath(state["runs_dir"])
        path = os.path.abspath(os.path.join(runs_abs, run_id + ".jsonl"))
        if not path.startswith(runs_abs + os.sep) or not os.path.exists(path):
            return jsonify({"error": "unknown_run_id"}), 404

        def stream():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        return Response(stream(), mimetype="application/x-ndjson",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/v2/debug/diag/abort")
    def diag_abort():
        if not _enabled():
            return jsonify({"error": "diag flag off"}), 404
        with state["lock"]:
            run = state["current"]
            if run is None or run.finished:
                return jsonify({"error": "no_active_run"}), 409
            run.abort()
        return jsonify({"ok": True, "run_id": run.run_id, "state": "aborting"})

    # boot plan pickup (spec §7; device-only behavior — PC-tested with a fake file)
    _adopt_boot_plan(base_dir, state)
    return True


def _resolve_port(port):
    """The app's own loopback port, resolved when a run STARTS (registration
    happens before app.run binds). APK = 8080; PC dev = SITECREW_PORT."""
    if port:
        return int(port)
    env = os.environ.get("SITECREW_PORT")
    if env and env.isdigit():
        return int(env)
    return 8080                                  # APK loopback port


def _status_payload(run, state):
    if run is None:
        return {"state": "idle", "run_id": None, "plan_id": None,
                "plan_hash": None, "window": None, "window_index": -1,
                "uptime_s": None, "uptime_source": None,
                "sampler_heartbeat_s": None, "records_written": 0,
                "bytes_written": 0, "hashes": {}, "pid": os.getpid(),
                "ts": round(time.time(), 3), "error_detail": None,
                "runs_dir_name": RUNS_DIR_NAME.replace(os.sep, "/")}
    return {
        "state": run.state,
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "plan_hash": plan_hash(run.plan),
        "window": run.window,
        "window_index": run.window_index,
        "uptime_s": run.uptime_s(),
        "uptime_source": run._uptime_source if run._start_wall else None,
        "sampler_heartbeat_s": (round(time.monotonic() - run.last_sample_mono, 3)
                                if run.last_sample_mono is not None else None),
        "records_written": run.writer.count if run.writer else 0,
        "bytes_written": run.writer.bytes if run.writer else 0,
        "hashes": {name: (fact or {}).get("sha256")
                   for name, fact in (run._header_facts or {}).items()},
        "pid": os.getpid(),
        "ts": round(time.time(), 3),
        "error_detail": run.error_detail,
        "runs_dir_name": RUNS_DIR_NAME.replace(os.sep, "/"),
    }


def _stage_boot_plan(base_dir, plan, defer, leaky):
    """Write the boot plan file (picked up at next process start). Atomic
    replace; the file contains the normalized plan only."""
    payload = {"v": 1, "plan_id": plan["id"], "plan": plan,
               "start_after_uptime_s": defer, "leaky_legacy": leaky,
               "staged_at": round(time.time(), 3)}
    path = os.path.join(base_dir, BOOT_PLAN_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _adopt_boot_plan(base_dir, state):
    """Boot plan pickup: runs at registration (process start on the device).
    A valid file is adopted and renamed applied-*; an unreadable one is
    renamed invalid-*; when a run is already active it is skipped-*."""
    path = os.path.join(base_dir, BOOT_PLAN_FILE)
    if not os.path.exists(path):
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    applied_dir = os.path.join(base_dir, RUNS_DIR_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        plan_id = payload.get("plan_id")
        plan = payload.get("plan")
        if plan_id not in PLANS or not isinstance(plan, dict):
            raise ValueError("unknown plan in boot file")
        defer = float(payload.get("start_after_uptime_s") or 0)
        leaky = bool(payload.get("leaky_legacy"))
        with state["lock"]:
            if state["current"] is not None and not state["current"].finished:
                os.replace(path, os.path.join(applied_dir,
                                              "diag_plan.skipped-%s.json" % stamp))
                return
            run = _start_run(plan, plan_id, base_dir,
                             _resolve_port(state["port"]),
                             source="boot_plan", defer=defer, leaky=leaky,
                             boot_pickup_note={"found": True, "adopted": True,
                                               "reason": "boot plan file adopted"})
            state["current"] = run
            state["last"] = run
        os.replace(path, os.path.join(applied_dir,
                                      "diag_plan.applied-%s.json" % stamp))
    except Exception:
        try:
            os.replace(path, os.path.join(applied_dir,
                                          "diag_plan.invalid-%s.json" % stamp))
        except OSError:
            pass


def _start_run(plan, plan_id, base_dir, port, source, defer, leaky,
               boot_pickup_note):
    run = Run(plan_id, plan, base_dir, port, source,
              start_after_uptime_s=defer, boot_pickup_note=boot_pickup_note,
              leaky=leaky)
    run.start()
    return run
