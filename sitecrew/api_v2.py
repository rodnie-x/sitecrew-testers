""" /api/v2 routes (flag-gated, additive). Wire-up in the legacy app:

    if os.environ.get("SITECREW_STORE") == "1":
        from sitecrew.api_v2 import register_v2          # lazy: nothing imported when the flag is off
        register_v2(app, get_store)                      # get_store() -> the process-wide EventStore

Run the server threaded (app.run(threaded=True)): a parked long-poll holds one thread for <= 25 s.

Contract (blueprint 5.3 + the poll ruling):
    GET /api/v2/poll?streams=c_81f2:318,office:12&wait=25
    -> {"events":[envelopes], "heads":{"c_81f2":320,"office":12}, "more":false, "reset":[]}

ASSUMPTION (I could not open events.py): EventStore.poll(cursors: dict[str,int], wait: float) -> dict
with exactly that body. If your signature differs, change the ONE line marked ADAPT.
"""
import logging
import math
import re
import socket
import threading
import time

log = logging.getLogger("sitecrew.api_v2")

_STREAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")      # 'office' matches; ':' and ',' can never appear
MAX_STREAMS = 32
MAX_WAIT = 25.0
PARK_SLICE_S = 1.0                                     # boss ruling after the phone CPU measurement: 0.25 s slices burned ~64% of a core on the device -> 1 s slices + cid supersede


def parse_streams(raw):
    """'c_81f2:318,office:12' -> {'c_81f2': 318, 'office': 12}. ValueError on any malformed input."""
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()

        if not part:
            continue
        sid, sep, seq = part.partition(":")
        if not sep or not _STREAM_ID.match(sid) or not seq.isdigit() or len(seq) > 15:
            raise ValueError("bad stream spec: %r" % part[:80])
        if sid in out:
            raise ValueError("duplicate stream: %s" % sid)
        out[sid] = int(seq)
        if len(out) > MAX_STREAMS:
            raise ValueError("too many streams (max %d)" % MAX_STREAMS)
    if not out:
        raise ValueError("streams is required")
    return out


def parse_wait(raw):
    try:
        w = float(raw) if raw not in (None, "") else 0.0
    except ValueError:
        raise ValueError("wait must be a number")
    if not math.isfinite(w):
        raise ValueError("wait must be finite")
    return min(max(w, 0.0), MAX_WAIT)


def _client_gone(request):
    """Feature-detect a client disconnect between park slices (boss P1.3
    ruling: a disconnected client must give its parked slot back promptly,
    or a phone coming back from a network drop sees 503s for up to 25 s).

    Cross-platform probe (no MSG_DONTWAIT — Windows lacks it): select() the
    request socket with a 0 timeout, then MSG_PEEK. Not readable = alive and
    idle; readable + b'' = clean EOF; readable + data = alive (never consumed).
    Returns (gone: bool, path: str); path is "none" when no liveness mechanism
    is available (feature-detected)."""
    sock = request.environ.get("werkzeug.socket")
    if sock is None:
        return False, "none"
    try:
        import select
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False, "werkzeug-socket-peek"     # idle: alive
        data = sock.recv(1, socket.MSG_PEEK)
        return (data == b""), "werkzeug-socket-peek"  # b'' = clean EOF
    except (ConnectionResetError, BrokenPipeError):
        return True, "werkzeug-socket-peek"
    except (OSError, ValueError):
        return False, "werkzeug-socket-peek"          # unexpected: assume alive
    except Exception:
        return False, "none"


def make_blueprint(get_store, max_parked=8):
    from flask import Blueprint, jsonify, request       # lazy import (flag-off path never loads this)

    bp = Blueprint("sitecrew_v2", __name__, url_prefix="/api/v2")
    slots = threading.BoundedSemaphore(max_parked)      # parked long-polls can't exhaust the thread pool
    test_report = {}

    # cid supersede (boss design): a reconnecting client presents the same cid;
    # its NEW poll bumps the generation, and the OLD parked poll returns at its
    # next slice boundary — the slot frees within ~1 slice without waiting for
    # the full park. Cheap: a dict lookup per slice, no per-slice syscalls.
    _supersede = {}
    _sup_lock = threading.Lock()
    _SUP_MAX = 256                                      # bound the registry (LRU-ish by insertion)


    def reply(obj, status=200, retry_after=None, liveness_path=None):
        resp = jsonify(obj)
        resp.status_code = status
        resp.headers["Cache-Control"] = "no-store"
        if liveness_path:
            resp.headers["X-Liveness-Path"] = liveness_path
        if retry_after:
            resp.headers["Retry-After"] = str(retry_after)
        return resp

    @bp.get("/poll")
    def poll():
        try:
            cursors = parse_streams(request.args.get("streams"))
            wait = parse_wait(request.args.get("wait"))
        except ValueError as e:
            return reply({"error": "bad_request", "detail": str(e)}, 400)
        if not slots.acquire(blocking=False):
            return reply({"error": "busy"}, 503, retry_after=1)
        # cid supersede: bump my generation BEFORE parking so any older poll
        # with the same cid returns at its next slice boundary
        cid = request.args.get("cid") or ""
        my_gen = None
        if cid and _STREAM_ID.match(cid):
            with _sup_lock:
                my_gen = _supersede.get(cid, 0) + 1
                _supersede[cid] = my_gen
                if len(_supersede) > _SUP_MAX:
                    for k in list(_supersede)[:len(_supersede) - _SUP_MAX]:
                        _supersede.pop(k, None)
        try:
            # Review fix #11: detect the liveness mechanism ONCE and report it
            # on EVERY reply (a wait=0 reply returns before _client_gone runs,
            # so the in-page probe could otherwise never see 'none').
            live_path = ("werkzeug-socket-peek"
                         if request.environ.get("werkzeug.socket") is not None
                         else "none")
            # Boss P1.3 ruling: hold the slot across PARK_SLICE_S slices and
            # check client liveness / supersede between slices. A client that
            # disconnects or reconnects frees its slot within ~1 slice instead
            # of holding it for the full wait. The response body is identical
            # to a single long park: the last store.poll result, ≤200 events,
            # more, reset.
            store = get_store()
            deadline = time.monotonic() + wait
            result = None
            while True:
                wait_left = deadline - time.monotonic()
                slice_wait = max(0.0, min(PARK_SLICE_S, wait_left))
                result = store.poll(cursors, wait=slice_wait)
                events = result.get("events") or []
                if events or result.get("more"):
                    return reply(result, liveness_path=live_path)
                if wait_left <= 0:
                    return reply(result, liveness_path=live_path)
                if my_gen is not None:
                    with _sup_lock:
                        if _supersede.get(cid) != my_gen:
                            log.debug("poll superseded by a newer cid request")
                            return reply(result, liveness_path="cid-superseded")
                gone, path = _client_gone(request)
                if gone:
                    log.debug("poll client disconnected; freeing slot early")
                    return reply(result, liveness_path=path)
        except Exception:
            log.exception("poll failed")
            return reply({"error": "internal"}, 500)
        finally:
            slots.release()

    import os
    if os.environ.get("SITECREW_TEST") == "1":           # P1.3 harness hooks, never in production
        @bp.post("/_test/report")
        def post_report():
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return reply({"error": "bad_request"}, 400)

            test_report.clear()
            test_report.update(body)
            return reply({"ok": True})

        @bp.get("/_test/report")
        def get_report():
            return reply(test_report or {"empty": True})

    return bp


def register_v2(app, get_store, **kw):
    app.register_blueprint(make_blueprint(get_store, **kw))
