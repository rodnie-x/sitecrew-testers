"""PC-only tests for the phone-local CPU diagnostic (docs/DEVICE_CPU_DIAG_SPEC.md).

Scope (spec §11 PC smoke, boss-approved implementation order): plan state,
sampler records, client counters, redaction-safe fields, 404-when-off, and the
hash gate fault — plus the offline validity classification helpers. Windows
has no /proc, so every test points ``cpu_diag._PROC_ROOT`` at a fake /proc
tree (spec: "if Windows cannot run /proc path, make tests mock it") and fake
frames come from suspended generator frames.

These tests make NO physical-device claim: everything here is pc_smoke
(``environment: pc_smoke`` in produced headers) and stays outside gate
evidence. The real device half (real CPU, pyjnius state, boot plan pickup on
Android, about:blank diag mode) is NOT RUN — spec §12.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("SITECREW_TEST", "1")        # header environment: pc_smoke

import cpu_diag                                    # noqa: E402
import build_flags                                 # noqa: E402

from werkzeug.serving import make_server           # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeProc:
    """A minimal fake /proc tree: self/stat, self/task/<tid>/stat, stat btime."""

    def __init__(self, start_uptime=0.0):
        self.root = tempfile.mkdtemp(prefix="sc_cpudiag_proc_")
        os.makedirs(os.path.join(self.root, "self", "task"), exist_ok=True)
        # process start = now - start_uptime: btime + starttime/CLK_TCK
        now = time.time()
        self.write_btime(now - start_uptime)
        self.write_self_stat(pid=4242, utime=100, stime=50, starttime=0)

    @staticmethod
    def _stat_line(pid, comm, utime, stime, starttime):
        # after the ')': index 0 = state, 11 = utime, 12 = stime, 19 = starttime
        tail = ["0"] * 24
        tail[0] = "S"
        tail[11] = str(utime)
        tail[12] = str(stime)
        tail[19] = str(starttime)
        return "%d (%s) %s" % (pid, comm, " ".join(tail))

    @staticmethod
    def _atomic_write(path, text):
        # readers poll these files concurrently (snapshot vs bump thread):
        # open("w") truncates and a concurrent read sees an empty file, so
        # write to a temp file and os.replace; on Windows a concurrent reader
        # can briefly hold the target -> retry the replace
        tmp = path + ".tmp.%d" % threading.get_ident()
        with open(tmp, "w") as f:
            f.write(text)
        for attempt in range(60):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.002)
        os.replace(tmp, path)                      # last try: let it raise

    def write_btime(self, value):
        self._atomic_write(os.path.join(self.root, "stat"),
                           "btime %d\ncpu  0 0 0 0 0 0 0 0 0 0 0\n" % int(value))

    def write_self_stat(self, pid, utime, stime, starttime=0, comm="python"):
        self._atomic_write(os.path.join(self.root, "self", "stat"),
                           self._stat_line(pid, comm, utime, stime, starttime))

    def write_task(self, tid, comm, utime, stime, starttime=0):
        path = os.path.join(self.root, "self", "task", str(tid))
        os.makedirs(path, exist_ok=True)
        self._atomic_write(os.path.join(path, "stat"),
                           self._stat_line(int(tid), comm, utime, stime, starttime))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


_FAKE_GEN_KEEPALIVE = []          # a suspended frame dies with its generator


def fake_frame(func_name, filename, lineno):
    """A REAL suspended frame with a controlled co_name/co_filename/lineno
    (types.FrameType cannot be instantiated from Python). next() advances the
    generator to its yield so f_lineno == lineno."""
    src = "\n" * (lineno - 2) + "def %s():\n    yield 1\n" % func_name
    ns = {}
    exec(compile(src, filename, "exec"), ns)
    gen = ns[func_name]()
    next(gen, None)
    _FAKE_GEN_KEEPALIVE.append(gen)
    return gen.gi_frame


class FakeStore:
    """EventStore stand-in for the real /api/v2/poll blueprint (test_api_v2_smoke
    style): parks `wait` seconds, never returns events, stable heads."""

    def __init__(self, head=5, fail_after=None):
        self.head = head
        self.fail_after = fail_after
        self.calls = 0

    def poll(self, cursors, wait=0):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("boom")
        if wait > 0:
            time.sleep(min(wait, 0.5))          # a real parked long-poll
        return {"events": [], "heads": {s: self.head for s in cursors},
                "more": False, "reset": []}


class LiveServer:
    """Real werkzeug server on an ephemeral loopback port (threaded)."""

    def __init__(self, app):
        self.srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.srv.server_port

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=10)


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------

class DiagTest(unittest.TestCase):
    """Common setup: fake /proc, patched timings, fresh tmp base_dir."""

    def setUp(self):
        self.proc = FakeProc()
        self._old_proc_root = cpu_diag._PROC_ROOT
        cpu_diag._PROC_ROOT = self.proc.root
        self._patches = []
        for name, value in (("SNAPSHOT_GAP_S", 0.3), ("FRAME_GAP_S", 0.01),
                            ("CLIENT_ERROR_BACKOFF_S", 0.05),
                            ("DIAG_WAIT_S", 0.5), ("DIAG_HTTP_TIMEOUT_S", 5.0)):
            self._patches.append((name, getattr(cpu_diag, name)))
            setattr(cpu_diag, name, value)
        self.tmp = tempfile.mkdtemp(prefix="sc_cpudiag_base_")
        self._freeze_tree(self.tmp)

    def tearDown(self):
        cpu_diag._PROC_ROOT = self._old_proc_root
        for name, value in self._patches:
            setattr(cpu_diag, name, value)
        self.proc.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _freeze_tree(base_dir):
        """Copy the five frozen modules into the fake base_dir so the hash gate
        and module facts hash real bytes (the device hashes the same relative
        paths under its app-private dir)."""
        for name in cpu_diag.FROZEN_MODULES:
            src = os.path.join(REPO, name.replace("/", os.sep))
            dst = os.path.join(base_dir, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)

    def wait_finished(self, run, timeout=30):
        deadline = time.time() + timeout
        while not run.finished and time.time() < deadline:
            time.sleep(0.02)
        if run.thread is not None:
            run.thread.join(timeout=10)
        return run.finished

    @staticmethod
    def read_records(path):
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def make_app(self, store=None, live=True):
        """Fresh Flask app: real api_v2 poll blueprint (when a store is given)
        + flag-on diag registration against the fake base_dir, wired to a real
        loopback werkzeug server (clients and head fetches hit its port)."""
        from flask import Flask
        app = Flask(__name__)
        if store is not None:
            from sitecrew.api_v2 import register_v2
            register_v2(app, lambda: store, max_parked=8)
        server = LiveServer(app) if live else None
        if server is not None:
            self.addCleanup(server.stop)
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", True):
            self.assertTrue(cpu_diag.register_diag(
                app, self.tmp, port=server.port if server else 1))
        self.server = server
        return app, store


# ---------------------------------------------------------------------------
# plan state (spec §6)
# ---------------------------------------------------------------------------

class TestPlans(unittest.TestCase):
    def test_canonical_plans_match_spec(self):
        d1 = cpu_diag.PLANS["P-D1"]
        self.assertEqual((d1["clients"], d1["duration_s"], d1["sample_interval_s"]),
                         (0, 900, 30))
        self.assertEqual(d1["snapshots_at_uptime"], [60, 300, 600])
        self.assertEqual(d1["boot_kind"], "normal_restart")
        d2 = cpu_diag.PLANS["P-D2"]
        self.assertEqual(d2["boot_kind"], "hot_deploy_restart")
        self.assertEqual(d2["runs_after"], "P-D1")
        pw = cpu_diag.PLANS["P-W"]
        names = [w["name"] for w in pw["windows"]]
        self.assertEqual(names[:4], ["cold-0", "cold-2", "cold-1", "cold-0b"])
        self.assertEqual([w["clients"] for w in pw["windows"][:4]], [0, 2, 1, 0])
        self.assertEqual([w["seconds"] for w in pw["windows"][:4]],
                         [300, 600, 600, 300])
        self.assertEqual([w["settle_s"] for w in pw["windows"][:4]], [60] * 4)
        warm = [w for w in pw["windows"] if w["name"].startswith("warm-")]
        self.assertEqual([w["clients"] for w in warm], [0, 2, 0])
        self.assertTrue(all(w["after_uptime_s"] == 1800 for w in warm))
        sens = pw["windows"][-2]
        self.assertEqual((sens["clients"], sens["interval_s"]), (2, 60))
        self.assertTrue(sens.get("average_only"))
        leaky = pw["windows"][-1]
        self.assertTrue(leaky.get("optional") and leaky.get("leaky")
                        and leaky.get("informational_only"))

    def test_plan_hash_deterministic_and_covers_overrides(self):
        a, b = cpu_diag.build_plan("P-D1"), cpu_diag.build_plan("P-D1")
        self.assertEqual(cpu_diag.plan_hash(a), cpu_diag.plan_hash(b))
        self.assertEqual(a["overrides"], {})
        c = cpu_diag.build_plan("P-D1", {"duration_s": 1.0})
        self.assertNotEqual(cpu_diag.plan_hash(a), cpu_diag.plan_hash(c))
        self.assertEqual(c["overrides"], {"duration_s": 1.0})
        self.assertEqual(c["duration_s"], 1.0)

    def test_override_validation(self):
        with self.assertRaises(ValueError):
            cpu_diag.build_plan("NOPE")
        with self.assertRaises(ValueError):
            cpu_diag.build_plan("P-D1", {"not_an_override": 1})
        with self.assertRaises(ValueError):
            cpu_diag.build_plan("P-D1", {"duration_s": 99999})
        with self.assertRaises(ValueError):
            cpu_diag.build_plan("P-W", {"window_seconds": -1})


class TestProcParsers(unittest.TestCase):
    def test_parse_stat_handles_spaces_and_parens_in_comm(self):
        raw = "42 (Thread-2 (recv)) S 0 0 0 0 0 0 0 0 0 0 7 9 0 0 0 0 0 0 55 0 0"
        st = cpu_diag._parse_stat(raw)
        self.assertEqual((st["pid"], st["comm"], st["utime"], st["stime"],
                          st["starttime"]), (42, "Thread-2 (recv)", 7, 9, 55))

    def test_process_start_from_fake_proc(self):
        proc = FakeProc(start_uptime=120.0)
        try:
            old = cpu_diag._PROC_ROOT
            cpu_diag._PROC_ROOT = proc.root
            wall, source = cpu_diag.process_start_wall()
            self.assertEqual(source, "proc_starttime_btime")
            self.assertAlmostEqual(time.time() - wall, 120.0, delta=2.0)
        finally:
            cpu_diag._PROC_ROOT = old
            proc.cleanup()

    def test_process_start_reason_when_unreadable(self):
        old = cpu_diag._PROC_ROOT
        cpu_diag._PROC_ROOT = os.path.join(tempfile.gettempdir(), "definitely-missing-proc")
        try:
            wall, reason = cpu_diag.process_start_wall()
            self.assertIsNone(wall)
            self.assertTrue(reason)
        finally:
            cpu_diag._PROC_ROOT = old


# ---------------------------------------------------------------------------
# full tiny P-D1 run: sampler records + snapshots + redaction-safe fields
# ---------------------------------------------------------------------------

class TestSamplerRecords(DiagTest):
    def test_decay_run_records_end_to_end(self):
        app, _store = self.make_app(FakeStore(head=5))
        # a real parked thread the fake /proc ranks #1, with fake frames
        sentinel = self._park_sentinel()
        client = app.test_client()
        r = client.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1",
            "overrides": {"duration_s": 1.0, "sample_interval_s": 0.05,
                          "snapshots_at_uptime": [0.3, 0.7]}})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body["hash_gate"]["status"], "not_declared")
        state = app.extensions["cpu_diag"]
        run = state["current"]
        self.assertEqual(run.state, "running")
        path = run.records_path
        # mid-run: header already flushed (+ samples) — flush+fsync per record
        time.sleep(0.45)
        partial = self.read_records(path)
        self.assertEqual(partial[0]["type"], "header")
        self.assertGreaterEqual(sum(1 for x in partial if x["type"] == "sample"), 1)
        self.assertTrue(self.wait_finished(run, 30))

        records = self.read_records(path)
        header = records[0]
        self._assert_header(header, body)
        samples = [x for x in records if x["type"] == "sample"]
        markers = [x for x in records if x["type"] == "window_marker"]
        snaps = [x for x in records if x["type"] == "snapshot"]
        footers = [x for x in records if x["type"] == "footer"]
        self.assertEqual(len(markers), 2)                 # start + end
        self.assertEqual(markers[0]["boundary"], "start")
        self.assertEqual(markers[1]["boundary"], "end")
        self.assertEqual(markers[1]["expected_samples"], 20)
        self.assertGreaterEqual(markers[1]["measure_sample_count"], 18)
        self.assertEqual(len(snaps), 2)                   # 0.3 s and 0.7 s
        self.assertTrue(all(not s["missed"] for s in snaps))
        self.assertTrue(all(len(s["ranked"]) >= 1 for s in snaps))
        top = snaps[0]["ranked"][0]
        self.assertEqual(top["tid"], sentinel["tid"])
        self.assertTrue(top["mapped"])
        self.assertEqual(top["python_name"], "parked-test")
        self.assertEqual(top["display_name"], "parked-test")
        self.assertTrue(top["in_park_path"])
        self.assertEqual(top["frames_samples"],
                         [["poll@api_v2.py:157"]] * 5)
        self.assertGreater(top["tick_delta"], 0)
        unmapped = [e for s in snaps for e in s["ranked"] if not e["mapped"]]
        self.assertTrue(unmapped)
        self.assertTrue(all(e["display_name"].startswith("native:")
                            for e in unmapped))
        # a run can end before/inside a capture (stop interrupts the 5-frame
        # sweep) — truncation is recorded honestly; unmapped TIDs still never
        # get guessed Python frames
        self.assertTrue(all(len(e["frames_samples"]) <= 5
                            and all(s == [] for s in e["frames_samples"])
                            for e in unmapped), unmapped)
        self.assertEqual(footers[-1]["reason"], "complete")
        self.assertEqual(footers[-1]["hash_drift"], [])
        self.assertEqual(footers[-1]["records_written"], len(records))
        # sample fields: raw ticks, uptime, park counters, wake reasons, counts
        s = samples[-1]
        for field in ("ts", "mono", "uptime_s", "seq", "phase", "window",
                      "interval_s", "pid", "utime", "stime", "ticks",
                      "sampler_ticks", "client_ticks", "diag_thread_count",
                      "server_poll_threads", "parked_diag", "parked_other",
                      "wake_reasons", "response_classes", "client_started",
                      "client_returned", "client_errors"):
            self.assertIn(field, s, field)
        self.assertEqual((s["utime"], s["stime"]), (100, 50))
        self.assertEqual(s["ticks"], 150)
        self.assertEqual(s["parked_diag"], 0)             # zero-client decay run
        self.assertEqual(s["parked_other"], 0)
        self.assertEqual(s["phase"], "measure")
        # decay samples carry the per-thread TID/name/ticks table
        self.assertTrue(all("threads" in x for x in samples))
        tids = {t["tid"] for t in samples[-1]["threads"]}
        self.assertIn(sentinel["tid"], tids)
        # offline classification of this clean run: no codes, and NEVER a pass
        verdict = cpu_diag.classify(records)
        self.assertEqual(verdict["codes"], [])
        self.assertNotIn("pass", json.dumps(verdict).lower())

    def test_records_are_redaction_safe(self):
        app, _store = self.make_app(FakeStore(head=5))
        client = app.test_client()
        r = client.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1",
            "overrides": {"duration_s": 0.6, "sample_interval_s": 0.05,
                          "snapshots_at_uptime": [0.3]}})
        run = app.extensions["cpu_diag"]["current"]
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(self.wait_finished(run, 30))
        with open(run.records_path, encoding="utf-8") as f:
            text = f.read()
        scan = cpu_diag.redaction_scan(text)
        self.assertEqual(scan["hits"], [], scan)
        records = self.read_records(run.records_path)
        for rec in records:
            if rec["type"] == "snapshot":
                for entry in rec["ranked"]:
                    for sample in entry["frames_samples"]:
                        for fr in sample:
                            self.assertRegex(fr, r"^[A-Za-z_][\w]*@[\w.]+:\d+$")
                        self.assertLessEqual(len(sample), cpu_diag.MAX_FRAMES)

    def _park_sentinel(self):
        release = threading.Event()
        started = threading.Event()
        holder = {}

        def park():
            holder["tid"] = threading.get_native_id()
            started.set()
            release.wait(15)

        t = threading.Thread(target=park, name="parked-test", daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        self.proc.write_task(holder["tid"], "parked-test", utime=10, stime=5)
        # a second task the diag does NOT own -> exercises native:<comm> mapping
        self.proc.write_task(424242, "roid-worker", utime=1, stime=1)
        # the fake busy thread burns ticks CONTINUOUSLY so any snapshot window
        # (two reads SNAPSHOT_GAP_S apart) sees a growing delta -> ranks #1
        def bump():
            ticks = 10
            while not release.is_set():
                ticks += 250
                self.proc.write_task(holder["tid"], "parked-test",
                                     utime=ticks, stime=5)
                time.sleep(0.05)
        threading.Thread(target=bump, daemon=True).start()
        old = cpu_diag._current_frames
        cpu_diag._current_frames = lambda: {t.ident: fake_frame(
            "poll", "sitecrew/api_v2.py", 157)}
        self.addCleanup(lambda: (setattr(cpu_diag, "_current_frames", old),
                                 release.set()))
        self.addCleanup(release.set)
        return {"tid": holder["tid"], "thread": t}

    def _assert_header(self, header, post_body):
        self.assertEqual(header["schema"], cpu_diag.SCHEMA_ID)
        self.assertEqual(header["environment"], "pc_smoke")
        self.assertEqual(header["run_id"], post_body["run_id"])
        self.assertEqual(header["plan_hash"], post_body["plan_hash"])
        self.assertEqual(header["plan_id"], "P-D1")
        self.assertEqual(header["boot_kind_declared"], "normal_restart")
        self.assertIsNotNone(header["process_start"]["wall"])
        self.assertIsNotNone(header["launch_gap_s"])
        self.assertLessEqual(header["launch_gap_s"], 15.0)
        self.assertIsInstance(header["clk_tck"], int)
        self.assertIsInstance(header["cpu_count"], int)
        self.assertTrue(header["python_version"])
        self.assertTrue(header["sqlite_version"])
        self.assertEqual(sorted(header["modules"]), sorted(cpu_diag.FROZEN_MODULES))
        for name, fact in header["modules"].items():
            self.assertTrue(fact["sha256"], name)
            self.assertIsInstance(fact["bytes"], int)
            self.assertIsInstance(fact["mtime"], int)
        self.assertIsNone(header["device_state"]["screen"])
        self.assertTrue(header["device_state"]["reasons"])
        self.assertEqual(header["webview"]["expected"], "about:blank in diag mode")
        self.assertIsNone(header["webview"]["verified"])
        self.assertEqual(header["client_registry"], [])
        self.assertEqual(header["records_dir_name"], "logs/diag")


# ---------------------------------------------------------------------------
# client counters (spec §2) — phone-local diagclient threads
# ---------------------------------------------------------------------------

class TestClientCounters(DiagTest):
    PW_OVERRIDES = {"window_seconds": 0.6, "window_interval_s": 0.05,
                    "settle_s": 0.2, "warm_after_uptime_s": 0.0,
                    "sensitivity_seconds": 0.1, "sensitivity_interval_s": 0.05}

    def test_clients_park_and_count_clean(self):
        store = FakeStore(head=5)
        app, _ = self.make_app(store)
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-W", "overrides": dict(self.PW_OVERRIDES)})
        self.assertEqual(r.status_code, 200, r.get_json())
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 60))
        records = self.read_records(run.records_path)
        markers = {x["window"]: x for x in records
                   if x["type"] == "window_marker" and x["boundary"] == "end"}
        # optional leaky arm was NOT requested -> not run
        self.assertNotIn("leaky-legacy", markers)
        cold2 = markers["cold-2"]
        stats = cold2["client_stats"]
        self.assertEqual([s["name"] for s in stats], ["diagclient-0", "diagclient-1"])
        self.assertNotEqual(stats[0]["cid"], stats[1]["cid"])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", s["cid"]) for s in stats))
        self.assertGreaterEqual(cold2["client_totals"]["started"], 2)
        self.assertEqual(cold2["client_totals"]["returned"],
                         cold2["client_totals"]["started"])
        self.assertEqual(cold2["client_totals"]["errors"], 0)
        self.assertTrue(all(s["unread_responses"] == 0 and s["close_errors"] == 0
                            for s in stats))
        # head came from the wait=0 poll fallback (no store-state endpoint here)
        self.assertEqual(cold2["head_fetch"]["via"], "poll_wait0")
        self.assertEqual(cold2["head_fetch"]["head"], 5)
        # post-settle samples: both clients parked, nothing else, timeout wakes
        samples = [x for x in records if x["type"] == "sample"]
        cold2_measure = [x for x in samples
                         if x["window"] == "cold-2" and x["phase"] == "measure"]
        self.assertTrue(cold2_measure)
        parked2 = sum(1 for x in cold2_measure if x["parked_diag"] == 2)
        self.assertGreaterEqual(parked2 / len(cold2_measure), 0.8)
        self.assertTrue(all(x["parked_other"] == 0 for x in cold2_measure))
        last = cold2_measure[-1]
        self.assertGreaterEqual(last["wake_reasons"].get("timeout", 0), 2)
        # headers counted as 2xx can briefly lead full read+close (returned)
        self.assertGreaterEqual(last["response_classes"].get("2xx", 0),
                                last["client_returned"])
        self.assertGreater(last["client_returned"], 0)
        # zero-client windows saw zero parked requests of any kind
        for name in ("cold-0", "cold-0b"):
            zs = [x for x in samples if x["window"] == name and x["phase"] == "measure"]
            self.assertTrue(zs)
            self.assertTrue(all(x["parked_diag"] == 0 and x["parked_other"] == 0
                                for x in zs))
        verdict = cpu_diag.classify(records)
        self.assertEqual(verdict["codes"], [], verdict["rule_details"])
        # settle phase was recorded but is excluded from measure analysis
        settles = [x for x in samples if x["window"] == "cold-2"
                   and x["phase"] == "settle"]
        self.assertTrue(settles)
        # nothing lingers after the run
        self.assertEqual(self._diag_threads(), [])

    def test_client_errors_are_counted_not_hidden(self):
        store = FakeStore(head=5, fail_after=1)   # head fetch ok, parks fail
        app, _ = self.make_app(store)
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-W", "overrides": dict(self.PW_OVERRIDES)})
        self.assertEqual(r.status_code, 200)
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 60))
        records = self.read_records(run.records_path)
        marker = [x for x in records if x["type"] == "window_marker"
                  and x["boundary"] == "end" and x["window"] == "cold-2"][0]
        self.assertGreater(marker["client_totals"]["errors"], 0)
        self.assertTrue(all(s["last_error_class"] == "http-500"
                            for s in marker["client_stats"]))
        verdict = cpu_diag.classify(records)
        self.assertIn("INVALID_CLIENT", verdict["codes"])

    def test_leaky_arm_informational_only(self):
        store = FakeStore(head=5)
        app, _ = self.make_app(store)
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-W", "leaky_legacy": True,
            "overrides": dict(self.PW_OVERRIDES)})
        self.assertEqual(r.status_code, 200)
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 90))
        records = self.read_records(run.records_path)
        marker = [x for x in records if x["type"] == "window_marker"
                  and x["boundary"] == "end" and x["window"] == "leaky-legacy"][0]
        self.assertTrue(marker["leaky"] and marker["informational_only"])
        self.assertGreater(marker["client_totals"]["started"], 0)
        self.assertLess(marker["client_totals"]["returned"],
                        marker["client_totals"]["started"])
        self.assertEqual(self._diag_threads(), [])

    @staticmethod
    def _diag_threads():
        return [t.name for t in threading.enumerate()
                if t.name.startswith(("diagclient", "diag-sampler",
                                      "diag-snapshot"))]


# ---------------------------------------------------------------------------
# hash gate fault (spec §8 rule 1)
# ---------------------------------------------------------------------------

class TestHashGate(DiagTest):
    def _module_hashes(self):
        return {name: fact["sha256"]
                for name, fact in cpu_diag.module_facts(self.tmp).items()}

    def test_hash_mismatch_voids_run_before_sampling(self):
        app, _ = self.make_app()
        bad = dict(self._module_hashes())
        bad["server/app.py"] = "0" * 64
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "expected_hashes": bad,
            "overrides": {"duration_s": 0.5, "sample_interval_s": 0.05}})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["state"], "error")
        self.assertEqual(body["hash_gate"]["status"], "mismatch")
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(run.finished)
        records = self.read_records(run.records_path)
        self.assertEqual([x["type"] for x in records], ["header", "footer"])
        self.assertEqual(records[1]["reason"], "hash_gate_fault")
        self.assertFalse(any(x["type"] == "sample" for x in records))
        verdict = cpu_diag.classify(records)
        self.assertIn("INVALID_HASH", verdict["codes"])

    def test_frozen_list_mismatch_voids_run(self):
        app, _ = self.make_app()
        bad = {n: h for n, h in self._module_hashes().items()
               if n != "cpu_diag.py"}
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "expected_hashes": bad,
            "overrides": {"duration_s": 0.5, "sample_interval_s": 0.05}})
        body = r.get_json()
        self.assertEqual(body["hash_gate"]["status"], "mismatch")
        self.assertIn("__frozen_list__", body["hash_gate"]["mismatches"])

    def test_matching_hashes_pass_the_gate_and_run(self):
        app, _ = self.make_app()
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "expected_hashes": self._module_hashes(),
            "overrides": {"duration_s": 0.6, "sample_interval_s": 0.05,
                          "snapshots_at_uptime": [0.3]}})
        body = r.get_json()
        self.assertEqual(body["hash_gate"]["status"], "ok")
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 30))
        records = self.read_records(run.records_path)
        self.assertEqual(records[-1]["reason"], "complete")
        self.assertEqual(records[0]["hash_gate"]["status"], "ok")
        self.assertTrue(any(x["type"] == "sample" for x in records))


# ---------------------------------------------------------------------------
# 404-when-off (compile-time flag) + flag-on endpoints
# ---------------------------------------------------------------------------

class TestFlagOff(unittest.TestCase):
    DIAG_ROUTES = (("post", "/api/v2/debug/diag/plan"),
                   ("get", "/api/v2/debug/diag/status"),
                   ("get", "/api/v2/debug/diag/result"),
                   ("post", "/api/v2/debug/diag/abort"))

    def _fresh_app(self):
        from flask import Flask
        return Flask(__name__)

    def test_flag_off_registers_nothing(self):
        app = self._fresh_app()
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", False):
            self.assertFalse(cpu_diag.register_diag(app, tempfile.gettempdir()))
        client = app.test_client()
        for method, path in self.DIAG_ROUTES:
            self.assertEqual(client.open(path, method=method).status_code, 404, path)

    def test_flag_off_does_not_pick_up_boot_plan(self):
        base = tempfile.mkdtemp(prefix="sc_cpudiag_off_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        plan_file = os.path.join(base, cpu_diag.BOOT_PLAN_FILE)
        with open(plan_file, "w") as f:
            json.dump({"plan_id": "P-D1", "plan": cpu_diag.build_plan("P-D1")}, f)
        app = self._fresh_app()
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", False):
            cpu_diag.register_diag(app, base)
        self.assertTrue(os.path.exists(plan_file))        # untouched, not adopted
        self.assertIsNone(app.extensions.get("cpu_diag"))

    def test_flag_on_serves_status(self):
        app = self._fresh_app()
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", True):
            self.assertTrue(cpu_diag.register_diag(app, tempfile.mkdtemp(
                prefix="sc_cpudiag_on_")))
        payload = app.test_client().get("/api/v2/debug/diag/status").get_json()
        self.assertEqual(payload["state"], "idle")

    @unittest.skipUnless(importlib.util.find_spec("crew") is not None,
                         "full product modules are not in the public subset")
    def test_real_server_app_is_inert_by_default(self):
        # the shipped build_flags.DEBUG_ENDPOINTS is False: the real app never
        # registers the diag routes (inert by construction). NOTE: server_app
        # serves static files at the site root, so unknown POST paths answer
        # 405 (static rule is GET-only) — both 404 and 405 mean "no endpoint".
        self.assertFalse(build_flags.DEBUG_ENDPOINTS)
        from server.app import app as server_app
        rules = {str(r) for r in server_app.url_map.iter_rules()}
        self.assertEqual([r for r in rules if "debug/diag" in r], [])
        client = server_app.test_client()
        for method, path in self.DIAG_ROUTES:
            resp = client.open(path, method=method,
                               json={} if method == "post" else None)
            self.assertIn(resp.status_code, (404, 405), path)


# ---------------------------------------------------------------------------
# boot plan pickup + start_after_uptime_s (spec §7)
# ---------------------------------------------------------------------------

class TestBootPlanAndDefer(DiagTest):
    def _boot_payload(self, overrides):
        return {"v": 1, "plan_id": "P-D1",
                "plan": cpu_diag.build_plan("P-D1", overrides),
                "start_after_uptime_s": 0, "leaky_legacy": False}

    def test_boot_plan_file_is_adopted_at_registration(self):
        with open(os.path.join(self.tmp, cpu_diag.BOOT_PLAN_FILE), "w") as f:
            json.dump(self._boot_payload({"duration_s": 0.8,
                                          "sample_interval_s": 0.05,
                                          "snapshots_at_uptime": [0.4]}), f)
        from flask import Flask
        app = Flask(__name__)
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", True):
            cpu_diag.register_diag(app, self.tmp, port=1)   # port unused: no clients
        run = app.extensions["cpu_diag"]["current"]
        self.assertIsNotNone(run)
        self.assertEqual(run.source, "boot_plan")
        self.assertTrue(self.wait_finished(run, 30))
        records = self.read_records(run.records_path)
        self.assertEqual(records[0]["run_source"], "boot_plan")
        self.assertTrue(records[0]["boot_plan_pickup"]["found"])
        self.assertTrue(records[0]["boot_plan_pickup"]["adopted"])
        self.assertEqual(records[-1]["reason"], "complete")
        applied = [n for n in os.listdir(os.path.join(self.tmp, "logs", "diag"))
                   if n.startswith("diag_plan.applied-")]
        self.assertEqual(len(applied), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, cpu_diag.BOOT_PLAN_FILE)))

    def test_invalid_boot_plan_is_quarantined(self):
        with open(os.path.join(self.tmp, cpu_diag.BOOT_PLAN_FILE), "w") as f:
            f.write("{not json")
        from flask import Flask
        app = Flask(__name__)
        with unittest.mock.patch.object(build_flags, "DEBUG_ENDPOINTS", True):
            cpu_diag.register_diag(app, self.tmp, port=1)
        self.assertEqual(app.extensions["cpu_diag"]["current"], None)
        quarantined = [n for n in os.listdir(os.path.join(self.tmp, "logs", "diag"))
                       if n.startswith("diag_plan.invalid-")]
        self.assertEqual(len(quarantined), 1)

    def test_start_after_uptime_s_defers_sampling_not_the_header(self):
        app, _ = self.make_app(FakeStore(head=5))
        client = app.test_client()
        r = client.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "start_after_uptime_s": 0.4,
            "overrides": {"duration_s": 0.4, "sample_interval_s": 0.05,
                          "snapshots_at_uptime": [0.6]}})
        self.assertEqual(r.status_code, 200)
        run = app.extensions["cpu_diag"]["current"]
        # the header (and its launch gap) exist immediately, before the defer
        self.assertGreaterEqual(run.writer.count, 1)
        records = self.read_records(run.records_path)
        self.assertEqual(records[0]["start_after_uptime_s"], 0.4)
        self.assertTrue(self.wait_finished(run, 30))
        records = self.read_records(run.records_path)
        samples = [x for x in records if x["type"] == "sample"]
        self.assertTrue(samples)
        self.assertGreaterEqual(samples[0]["uptime_s"], 0.35)
        self.assertEqual(records[-1]["reason"], "complete")


# ---------------------------------------------------------------------------
# abort + thread hygiene (spec §11 "lingering threads")
# ---------------------------------------------------------------------------

class TestAbortAndThreads(DiagTest):
    def test_abort_stops_run_and_leaves_no_lingering_threads(self):
        app, _ = self.make_app(FakeStore(head=5))
        client = app.test_client()
        r = client.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1",
            "overrides": {"duration_s": 6.0, "sample_interval_s": 0.05,
                          "snapshots_at_uptime": [3.0]}})
        self.assertEqual(r.status_code, 200)
        time.sleep(0.6)
        r = client.post("/api/v2/debug/diag/abort")
        self.assertEqual(r.status_code, 200)
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 30))
        self.assertEqual(run.state, "aborted")
        records = self.read_records(run.records_path)
        self.assertEqual(records[-1]["reason"], "aborted")
        # status reflects the terminal state
        status = client.get("/api/v2/debug/diag/status").get_json()
        self.assertEqual(status["state"], "aborted")
        # no diag threads survive the run
        alive = [t.name for t in threading.enumerate()
                 if t.name.startswith(("diagclient", "diag-sampler",
                                       "diag-snapshot"))]
        self.assertEqual(alive, [])
        # aborting again (no active run) is a 409, not a crash
        self.assertEqual(client.post("/api/v2/debug/diag/abort").status_code, 409)


# ---------------------------------------------------------------------------
# endpoints: validation, result streaming, status cache
# ---------------------------------------------------------------------------

class TestEndpoints(DiagTest):
    def test_plan_validation_errors(self):
        app, _ = self.make_app(FakeStore(head=5))
        c = app.test_client()
        self.assertEqual(c.post("/api/v2/debug/diag/plan",
                                json={"plan": "NOPE"}).status_code, 400)
        self.assertEqual(c.post("/api/v2/debug/diag/plan",
                                json={"plan": "P-D1",
                                      "overrides": {"nope": 1}}).status_code, 400)
        self.assertEqual(c.post("/api/v2/debug/diag/plan",
                                json={"plan": "P-D1",
                                      "start_after_uptime_s": "zzz"}).status_code, 400)
        self.assertEqual(c.post("/api/v2/debug/diag/plan",
                                json={"plan": "P-D1",
                                      "leaky_legacy": True}).status_code, 400)
        self.assertEqual(c.post("/api/v2/debug/diag/abort").status_code, 409)

    def test_second_plan_while_running_is_409(self):
        app, _ = self.make_app(FakeStore(head=5))
        c = app.test_client()
        r = c.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "overrides": {"duration_s": 1.5,
                                          "sample_interval_s": 0.05,
                                          "snapshots_at_uptime": [0.5]}})
        self.assertEqual(r.status_code, 200)
        r2 = c.post("/api/v2/debug/diag/plan", json={"plan": "P-D1"})
        self.assertEqual(r2.status_code, 409)
        self.assertTrue(self.wait_finished(app.extensions["cpu_diag"]["current"], 30))

    def test_boot_staging_writes_plan_file_only(self):
        app, _ = self.make_app(FakeStore(head=5))
        r = app.test_client().post("/api/v2/debug/diag/plan", json={
            "plan": "P-W", "boot": True,
            "overrides": {"window_seconds": 0.5}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["staged"])
        with open(os.path.join(self.tmp, cpu_diag.BOOT_PLAN_FILE)) as f:
            staged = json.load(f)
        self.assertEqual(staged["plan_id"], "P-W")
        self.assertEqual(staged["plan"]["overrides"], {"window_seconds": 0.5})

    def test_result_streams_exact_bytes_and_404s(self):
        app, _ = self.make_app(FakeStore(head=5))
        c = app.test_client()
        run_id = c.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "overrides": {"duration_s": 0.5,
                                          "sample_interval_s": 0.05,
                                          "snapshots_at_uptime": [0.25]}}).get_json()["run_id"]
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 30))
        with open(run.records_path, "rb") as f:
            expected = f.read()
        got = c.get("/api/v2/debug/diag/result?run_id=" + run_id)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.headers["Content-Type"].startswith("application/x-ndjson"), True)
        self.assertEqual(got.data, expected)
        # default = last run
        self.assertEqual(c.get("/api/v2/debug/diag/result").data, expected)
        self.assertEqual(c.get("/api/v2/debug/diag/result?run_id=missing").status_code, 404)
        self.assertEqual(c.get("/api/v2/debug/diag/result?run_id=..").status_code, 404)
        self.assertEqual(c.get("/api/v2/debug/diag/result?run_id=../secret").status_code, 404)

    def test_status_is_cached_during_a_run(self):
        app, _ = self.make_app(FakeStore(head=5))
        c = app.test_client()
        c.post("/api/v2/debug/diag/plan", json={
            "plan": "P-D1", "overrides": {"duration_s": 1.2,
                                          "sample_interval_s": 0.05,
                                          "snapshots_at_uptime": [0.5]}})
        first = c.get("/api/v2/debug/diag/status").get_json()
        second = c.get("/api/v2/debug/diag/status").get_json()
        self.assertFalse(first.get("cached"))
        self.assertTrue(second.get("cached"))             # spec §7: 60 s cache
        run = app.extensions["cpu_diag"]["current"]
        self.assertTrue(self.wait_finished(run, 30))
        after = c.get("/api/v2/debug/diag/status").get_json()
        self.assertEqual(after["state"], "complete")
        self.assertFalse(after.get("cached"))


# ---------------------------------------------------------------------------
# frames + native-id mapping (spec §5)
# ---------------------------------------------------------------------------

class TestFramesAndMapping(unittest.TestCase):
    def test_render_frames_format(self):
        fr = fake_frame("poll", "sitecrew/api_v2.py", 157)
        self.assertEqual(cpu_diag._render_frames(fr), ["poll@api_v2.py:157"])

    def test_render_frames_truncates_to_six(self):
        acc, release, seen = [], threading.Event(), threading.Event()

        def deep(n):
            if n == 0:
                acc.append(sys._current_frames()[threading.get_ident()])
                seen.set()
                release.wait(10)
                return
            deep(n - 1)

        t = threading.Thread(target=deep, args=(9,), daemon=True)
        t.start()
        self.assertTrue(seen.wait(5))
        try:
            frames = cpu_diag._render_frames(acc[0], cpu_diag.MAX_FRAMES)
            self.assertEqual(len(frames), 6)
            for fr in frames:
                self.assertRegex(fr, r"^[A-Za-z_][\w]*@[\w.]+:\d+$")
        finally:
            release.set()

    def test_snapshot_maps_native_ids_and_marks_unmapped(self):
        proc = FakeProc()
        old_root = cpu_diag._PROC_ROOT
        cpu_diag._PROC_ROOT = proc.root
        old_frames = cpu_diag._current_frames
        release = threading.Event()
        started = threading.Event()
        holder = {}

        def park():
            holder["tid"] = threading.get_native_id()
            started.set()
            release.wait(15)

        try:
            t = threading.Thread(target=park, name="parked-test", daemon=True)
            t.start()
            self.assertTrue(started.wait(5))
            proc.write_task(holder["tid"], "parked-test", utime=10, stime=5)
            proc.write_task(424242, "roid-worker", utime=1, stime=1)
            # the busy thread gains ticks mid-snapshot -> ranks first
            def bump():
                time.sleep(0.1)
                proc.write_task(holder["tid"], "parked-test", utime=500, stime=5)
            threading.Thread(target=bump, daemon=True).start()
            cpu_diag._current_frames = lambda: {t.ident: fake_frame(
                "poll", "sitecrew/api_v2.py", 200)}
            base = tempfile.mkdtemp(prefix="sc_cpudiag_snap_")
            run = cpu_diag.Run("P-D1", cpu_diag.build_plan("P-D1"), base, 1, "test")
            run.writer = cpu_diag.JsonlWriter(run.records_path)
            run._write_snapshot(0.0)
            records = TestSamplerRecords.read_records(run.records_path)
            snap = [r for r in records if r["type"] == "snapshot"][-1]
            self.assertFalse(snap["missed"])
            top = snap["ranked"][0]
            self.assertEqual(top["tid"], holder["tid"])
            self.assertTrue(top["mapped"])
            self.assertEqual(top["python_name"], "parked-test")
            self.assertEqual(top["display_name"], "parked-test")
            self.assertEqual(top["frames_samples"], [["poll@api_v2.py:200"]] * 5)
            self.assertTrue(top["in_park_path"])
            unmapped = snap["ranked"][-1]
            self.assertEqual(unmapped["tid"], 424242)
            self.assertFalse(unmapped["mapped"])
            self.assertEqual(unmapped["display_name"], "native:roid-worker")
            self.assertEqual(unmapped["frames_samples"], [[]] * 5)
            self.assertFalse(unmapped["in_park_path"])
            shutil.rmtree(base, ignore_errors=True)
        finally:
            release.set()
            cpu_diag._PROC_ROOT = old_root
            cpu_diag._current_frames = old_frames
            proc.cleanup()


# ---------------------------------------------------------------------------
# offline validity classification helpers (spec §8) — synthetic records
# ---------------------------------------------------------------------------

def hdr(**kw):
    base = {"type": "header", "pid": 100, "launch_gap_s": 1.0,
            "hash_gate": {"status": "ok", "mismatches": {}},
            "modules": {n: {"sha256": "x"} for n in cpu_diag.FROZEN_MODULES},
            "client_registry": [{"window": "cold-2", "index": 0, "cid": "aaa"},
                                {"window": "cold-2", "index": 1, "cid": "bb"}]}
    base.update(kw)
    return base


def samp(window="cold-2", phase="measure", seq=0, interval=5.0, mono=0.0,
         parked=None, other=0, spt=None, pid=100, **kw):
    clients = 0 if window.endswith("0") else 2
    if parked is None:
        parked = clients
    if spt is None:
        spt = parked
    base = {"type": "sample", "window": window, "phase": phase, "seq": seq,
            "interval_s": interval, "mono": mono, "pid": pid,
            "parked_diag": parked, "parked_other": other,
            "server_poll_threads": spt}
    base.update(kw)
    return base


def marker(window="cold-2", clients=2, expected=10, actual=10, stats=None,
           totals=None, state=None, **kw):
    if stats is None:
        stats = [{"cid": "aaa", "errors": 0}, {"cid": "bb", "errors": 0}]
    if totals is None:
        totals = {"started": 10, "returned": 10, "errors": 0}
    base = {"type": "window_marker", "boundary": "end", "window": window,
            "clients": clients, "expected_samples": expected,
            "measure_sample_count": actual, "client_stats": stats,
            "client_totals": totals,
            "device_state": state or {"screen": True, "charging": True,
                                      "battery_saver": False, "doze": False}}
    base.update(kw)
    return base


def footer(**kw):
    base = {"type": "footer", "reason": "complete", "hash_drift": []}
    base.update(kw)
    return base


class TestClassify(unittest.TestCase):
    def result(self, records):
        return cpu_diag.classify(records)

    def test_clean_records_produce_no_codes_and_never_a_pass(self):
        records = [hdr(), footer()]
        for w, client in (("cold-0", 0), ("cold-2", 2)):
            records.append(marker(window=w, clients=client))
            for i in range(10):
                records.append(samp(window=w, seq=i, mono=i * 5.0,
                                    parked=client, spt=client))
        r = self.result(records)
        self.assertEqual(r["codes"], [], r["rule_details"])
        self.assertNotIn("pass", json.dumps(r).lower())

    def test_rule1_hash_gate_mismatch(self):
        r = self.result([hdr(hash_gate={"status": "mismatch", "mismatches": {"a": {}}}),
                         footer()])
        self.assertIn("INVALID_HASH", r["codes"])

    def test_rule1_end_hash_drift(self):
        r = self.result([hdr(), footer(hash_drift=["server/app.py"])])
        self.assertIn("INVALID_HASH", r["codes"])

    def test_rule1_frozen_list_mismatch(self):
        h = hdr()
        h["modules"] = {k: v for k, v in h["modules"].items() if k != "build_flags.py"}
        r = self.result([h, footer()])
        self.assertIn("INVALID_HASH", r["codes"])

    def test_truncated_run_has_no_footer(self):
        r = self.result([hdr(), samp()])
        self.assertIn("INVALID_TRUNCATED", r["codes"])

    def test_rule1_pid_drift(self):
        r = self.result([hdr(), samp(pid=101), footer()])
        self.assertIn("INVALID_PID_DRIFT", r["codes"])

    def test_rule2_gap_over_2_5x_interval(self):
        records = [hdr(), marker(), samp(mono=0.0), samp(seq=1, mono=16.0), footer()]
        r = self.result(records)
        self.assertIn("INVALID_SAMPLING", r["codes"])

    def test_rule2_under_90pct_expected_samples(self):
        records = [hdr(), marker(expected=10, actual=8), footer()]
        r = self.result(records)
        self.assertIn("INVALID_SAMPLING", r["codes"])

    def test_rule2_cross_window_gaps_are_not_flagged(self):
        # the recorded client join between windows explains the gap
        records = [hdr(), marker(window="cold-0", clients=0),
                   samp(window="cold-0", parked=0, spt=0, mono=0.0),
                   marker(),
                   samp(seq=0, mono=60.0), footer()]
        r = self.result(records)
        self.assertNotIn("INVALID_SAMPLING", r["codes"])

    def test_rule3_client_error(self):
        records = [hdr(), marker(totals={"started": 10, "returned": 9, "errors": 1}),
                   footer()]
        r = self.result(records)
        self.assertIn("INVALID_CLIENT", r["codes"])

    def test_rule4_parked_diag_differs_from_n(self):
        records = [hdr(), marker()]
        for i in range(10):
            parked = 1 if i in (0, 1) else 2      # 20% bad > 5%
            records.append(samp(seq=i, mono=i * 5.0, parked=parked, spt=parked))
        records.append(footer())
        r = self.result(records)
        self.assertIn("INVALID_PARK", r["codes"])

    def test_rule4_any_parked_other(self):
        records = [hdr(), marker(), samp(seq=0, mono=0.0, other=1, spt=3), footer()]
        r = self.result(records)
        self.assertIn("INVALID_PARK", r["codes"])

    def test_rule5_state_mismatch_between_compared_windows(self):
        on = {"screen": True, "charging": True, "battery_saver": False, "doze": False}
        off = dict(on, charging=False)
        records = [hdr(),
                   marker(window="cold-0", clients=0, state=on),
                   marker(window="cold-2", state=off), footer()]
        r = self.result(records)
        self.assertIn("INVALID_STATE_COMPARE", r["codes"])

    def test_rule6_foreign_traffic(self):
        records = [hdr(), marker()]
        for i in range(2):
            records.append(samp(seq=i, mono=i * 5.0, parked=2, spt=7))
        records.append(footer())
        r = self.result(records)
        self.assertIn("INVALID_FOREIGN_TRAFFIC", r["codes"])

    def test_rule6_launch_gap(self):
        r = self.result([hdr(launch_gap_s=20.0), footer()])
        self.assertIn("INVALID_LAUNCH_GAP", r["codes"])

    def test_rule6_stray_cid(self):
        stats = [{"cid": "aaa", "errors": 0}, {"cid": "intruder", "errors": 0}]
        records = [hdr(), marker(stats=stats), footer()]
        r = self.result(records)
        self.assertIn("INVALID_STRAY_CID", r["codes"])

    def test_leaky_window_is_exempt_from_stray_cid(self):
        stats = [{"cid": "rotating", "errors": 0}]
        records = [hdr(), marker(window="leaky-legacy", leaky=True,
                                 informational_only=True, stats=stats), footer()]
        r = self.result(records)
        self.assertNotIn("INVALID_STRAY_CID", r["codes"])


# ---------------------------------------------------------------------------
# redaction helpers (spec §10)
# ---------------------------------------------------------------------------

class TestRedactionHelpers(unittest.TestCase):
    def test_scan_detects_paths_ports_ips_keys(self):
        text = ("see C:\\Users\\x and /home/y and /data/data/z, hit "
                "127.0.0.1:8080 and 10.0.0.1, key nvapi-abc, sk-abcdefgh12")
        scan = cpu_diag.redaction_scan(text)
        kinds = {h["kind"] for h in scan["hits"]}
        self.assertIn("windows-drive-path", kinds)
        self.assertIn("unix-owner-path", kinds)
        self.assertIn("host-port", kinds)
        self.assertIn("ipv4", kinds)
        self.assertIn("nvidia-key", kinds)
        self.assertIn("generic-secret", kinds)
        self.assertFalse(scan["clean"])

    def test_scan_clean_on_redaction_safe_records(self):
        clean = json.dumps({"frames": ["poll@api_v2.py:157"],
                            "modules": {"sitecrew/api_v2.py": {"sha256": "ab"}},
                            "records_dir_name": "logs/diag"})
        self.assertTrue(cpu_diag.redaction_scan(clean)["clean"])

    def test_short_cid(self):
        cid = uuid.uuid4().hex
        out = cpu_diag.short_cid(cid, salt="pepper")
        self.assertRegex(out, r"^[0-9a-f]{8}$")
        self.assertEqual(out, cpu_diag.short_cid(cid, salt="pepper"))
        self.assertNotEqual(out, cpu_diag.short_cid(cid, salt="other"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
