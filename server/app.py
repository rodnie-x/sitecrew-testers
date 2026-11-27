"""Flask server: serves the chat UI and the crew API.

Runs on the phone inside the APK (port 8080, reached by the WebView shell)
or on the PC via run_server.py. Supports first-run key entry: until an NVIDIA
key exists, the UI shows a setup screen and /api/chat explains what to do.
"""
import json
import os
import re
import socket
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, send_from_directory

from crew.site import SiteCrew
from crew.approvals import ServerApprovals
from crew.models import Event
from tools import reminders
from tools.platform import IS_TERMOX, IS_ANDROID
from .attachments import AttachmentError, prepare_attachments

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE_DIR, ".env")
QUEUE_FILE = os.path.join(BASE_DIR, "logs", "queue.json")


def _load_queue():
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            q = json.load(f)
            return q if isinstance(q, list) else []
    except Exception:
        return []


def _save_queue():
    try:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE["queue"], f, ensure_ascii=False)
    except Exception:
        pass


STATE = {
    "messages": [],
    "busy": False,
    "busy_since": None,
    "activity": deque(maxlen=150),
    "approvals": {},
    "queue": _load_queue(),
    "tool_counts": {},
}
_lock = threading.Lock()

crew = None
app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
            static_url_path="")
# Base64 adds roughly 33% to the 20 MB raw attachment limit.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "Upload is too large. Attach up to 20 MB at once."}), 413


def _on_event(e):
    with _lock:
        STATE["activity"].append(e.to_dict())


def _reminder_notify(text):
    try:
        tool = crew.registry.get("notify")
        if tool and tool.enabled:
            tool.run({"title": "SiteCrew reminder", "text": text})
    except Exception:
        pass
    _on_event(Event("reminder", "—", "reminder-fired", text, level="warn"))


def get_key():
    """Key from env, or from the .env file next to the crew code."""
    k = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    if k:
        return k
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("NVIDIA_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def build_crew():
    """Construct the crew if a key exists. Returns True when ready."""
    global crew
    if crew is not None:
        return True
    if not get_key():
        return False
    crew = SiteCrew(approval=ServerApprovals(STATE), on_event=_on_event)
    reminders.start_loop(notify=_reminder_notify)
    _wrap_tool_counts(crew)
    return True


def _wrap_tool_counts(crew_obj):
    """Count real tool calls per job so replies can show 'Used 2 · battery, ...'."""
    for tool in crew_obj.registry.tools.values():
        original = tool.run

        def wrapped(args, _orig=original, _tid=tool.id):
            with _lock:
                STATE["tool_counts"][_tid] = STATE["tool_counts"].get(_tid, 0) + 1
            return _orig(args)

        tool.run = wrapped


def start_crew():
    """PC entry (run_server.py): build or fail with a clear message."""
    if not build_crew():
        raise RuntimeError(
            "NVIDIA_API_KEY is missing. Copy .env.example to .env and paste "
            "your free key from https://build.nvidia.com")


# ---------------- pages ----------------

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


# ---------------- API ----------------

_NET = {"ok": None, "ts": 0.0}


def _net_ok(max_age=10.0):
    """Cached connectivity probe to the NVIDIA API host (cheap TCP connect)."""
    now = time.time()
    if _NET["ok"] is not None and now - _NET["ts"] < max_age:
        return _NET["ok"]
    ok = False
    try:
        s = socket.create_connection(("integrate.api.nvidia.com", 443), timeout=4)
        s.close()
        ok = True
    except OSError:
        ok = False
    _NET["ok"] = ok
    _NET["ts"] = now
    return ok


_LOCAL = {"registry": None}


def _local_registry():
    """Standalone tool registry for on-device answers (works with no key, no internet)."""
    if _LOCAL["registry"] is None:
        from crew.config import CrewConfig
        from tools.build import build_registry
        _LOCAL["registry"] = build_registry(CrewConfig())
    return _LOCAL["registry"]


# Message patterns that a local tool can answer with zero data usage.
_OFFLINE_INTENTS = (
    (re.compile(r"\bbattery\b", re.I), "battery", lambda m: {}),
    (re.compile(r"\b(?:device|phone)\s+info\b|\bhow much (?:storage|ram)|\bfree (?:storage|space|ram)|\bstorage\b|\bram\b", re.I),
     "device_info", lambda m: {}),
    (re.compile(r"\b(?:list|show|find)\s+(?:my\s+|the\s+|installed\s+)?apps\b|\binstalled\s+apps\b", re.I),
     "list_apps", lambda m: {}),
    (re.compile(r"\bapp\s+info(?:\s+(?:for|about)\s+)?\s*(\S+)?", re.I), "app_info",
     lambda m: {"package": m.group(1)} if m.group(1) else {}),
    (re.compile(r"\bopen\s+(?:the\s+)?app\s+(\S+)", re.I), "open_app", lambda m: {"app": m.group(1)}),
    (re.compile(r"\bsettings\b|\bbrightness\b|\bwi-?fi\b|\bbluetooth\b|\bvolume\b|\bairplane\b", re.I),
     "get_settings", lambda m: {}),
    (re.compile(r"\bclipboard\b", re.I), "read_clipboard", lambda m: {}),
    (re.compile(r"\bvibrate\b", re.I), "vibrate", lambda m: {}),
    (re.compile(r"remind\s+me\s+(.+?)\s+in\s+(\d+)\s*minutes?", re.I), "add_reminder",
     lambda m: {"text": m.group(1), "in_minutes": m.group(2)}),
    (re.compile(r"\bfind\s+(?:the\s+)?files?\s+(?:named\s+|with\s+|containing\s+)?(.+)", re.I),
     "find_files", lambda m: {"query": m.group(1)}),
    (re.compile(r"\blist\s+files?\b(?:\s+in\s+(\S+))?", re.I), "list_files",
     lambda m: {"path": m.group(1)} if m.group(1) else {}),
)


# "What phone tools can you use?" — the tool inventory is a fact of the registry,
# so answer it deterministically: no model call, works offline, and a small laborer
# model can never claim the crew has no tools. Phrasings that ask what the crew
# itself can run match; questions that merely mention tools ("list the tools you
# need to build a fence") still reach the crew normally.
_TOOLS_TOUR = re.compile(
    r"\b(?:what|which)\s+(?:phone\s+)?tools?\s+(?:can|do|have|are|is)\b"
    r"|\btools?\s+can\s+you\s+use\b"
    r"|\b(?:list|show|name|tour)\b[^.?!]{0,24}\b(?:your|phone)\s+tools?\b"
    r"|\bwhat\s+can\s+you\s+do\b", re.I)


def _tools_tour():
    """Plain-language tour of the crew's phone tools, straight from the registry."""
    all_tools = _local_registry().enabled_map()
    enabled = [t for t in all_tools if t["enabled"]]
    off = len(all_tools) - len(enabled)
    lines = [f"✅ The crew can use {len(enabled)} phone tools right now:"]
    for t in enabled:
        lines.append(f"• {t['id']} — {t['description']}"
                     + (" · 🚦 asks your OK first" if t["sensitive"] else ""))
    lines.append("Sensitive tools always show a work permit in the chat before anything runs.")
    if off:
        lines.append(f"{off} more are turned off in Settings → Tools.")
    lines.append('Try one: "check my battery", "list installed apps", "what\'s my device info".')
    return "\n".join(lines)


def _match_offline_intent(msg):
    for rx, tool_id, build in _OFFLINE_INTENTS:
        m = rx.search(msg)
        if m:
            return tool_id, build(m)
    return None


def _run_offline_intent(tool_id, args):
    reg = _local_registry()
    tool = reg.get(tool_id)
    if tool is None:
        return None
    if not tool.enabled:
        return "'" + tool_id + "' is turned off — enable it in Settings → Tools."
    with _lock:
        STATE["tool_counts"][tool_id] = STATE["tool_counts"].get(tool_id, 0) + 1
    try:
        return tool.run(args or {})
    except Exception as e:
        return "error: " + str(e)[:200]


def _queue_message(msg, attachments, file_context):
    entry = {"role": "user", "content": msg,
             "model_content": msg + ("\n\n" + file_context if file_context else "")}
    if attachments:
        entry["attachments"] = attachments
    with _lock:
        STATE["messages"].append(entry)
        STATE["queue"].append({"message": msg, "file_context": file_context})
        _save_queue()


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    msg = str(data.get("message", "")).strip()
    raw_attachments = data.get("attachments") or []
    if not msg and not raw_attachments:
        return jsonify({"error": "Write a message or attach a file."}), 400
    try:
        attachments, file_context = prepare_attachments(raw_attachments)
    except AttachmentError as e:
        return jsonify({"error": str(e)}), 400
    if not msg:
        msg = "Please read and summarize the attached file(s)."

    # 0. Tool-inventory questions answer straight from the registry — deterministic,
    #    zero data, works offline, even while the crew is busy.
    if _TOOLS_TOUR.search(msg):
        started = time.time()
        output = _tools_tour()
        with _lock:
            entry = {"role": "user", "content": msg}
            if attachments:
                entry["attachments"] = attachments
            STATE["messages"].append(entry)
            STATE["messages"].append({
                "role": "assistant", "content": output,
                "job": sum(1 for m in STATE["messages"] if m.get("role") == "user"),
                "elapsed_s": round(time.time() - started, 1),
                "workers": ["foreman"]})
        _on_event(Event("foreman", "registry", "tools-tour",
                        "answered from the registry — no model call", level="success"))
        return jsonify({"ok": True, "local": True})

    # 1. On-device tools answer with zero data usage — they work offline too.
    #    Online, only short commands go local; conversational phrasing reaches
    #    the crew so it can add real advice on top of the tool data.
    intent = _match_offline_intent(msg)
    if intent is not None and _net_ok() and len(msg) > 45:
        intent = None
    if intent is not None:
        tool_id, args = intent
        offline_started = time.time()
        output = _run_offline_intent(tool_id, args)
        if output is not None:
            with _lock:
                counts = dict(STATE["tool_counts"])
                STATE["tool_counts"] = {}
                entry = {"role": "user", "content": msg}
                if attachments:
                    entry["attachments"] = attachments
                STATE["messages"].append(entry)
                last = {"role": "assistant", "content": "⚡ Answered on-device — no data used.\n\n" + str(output)}
                if counts:
                    last["tools_used"] = counts
                last["job"] = sum(1 for m in STATE["messages"] if m.get("role") == "user")
                last["elapsed_s"] = round(time.time() - offline_started, 1)
                last["workers"] = ["operator"]
                STATE["messages"].append(last)
            _on_event(Event("site", "local", "offline-tool", f"{tool_id} — no internet needed", level="success"))
            return jsonify({"ok": True, "local": True})

    # 2. Crew busy → queue the message (it runs right after the current job and steers it).
    if STATE["busy"]:
        _queue_message(msg, attachments, file_context)
        _on_event(Event("site", "—", "queued", msg[:60], level="info"))
        return jsonify({"ok": True, "queued": True})

    # 3. No internet → queue it; it sends automatically when data works again.
    if not _net_ok():
        _queue_message(msg, attachments, file_context)
        _on_event(Event("site", "—", "queued-offline", msg[:60], level="warn"))
        return jsonify({"ok": True, "queued": True, "offline": True})

    if crew is None and not build_crew():
        return jsonify({"error": "No NVIDIA key configured yet — add it in Settings first."}), 400
    STATE["busy"] = True
    STATE["busy_since"] = time.time()
    threading.Thread(target=_run_job, args=(msg, attachments, file_context), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/undo")
def undo():
    """Remove the last exchange (assistant reply + the user message before it)."""
    if STATE["busy"]:
        return jsonify({"error": "the crew is still working — undo after it finishes"}), 429
    removed = 0
    with _lock:
        msgs = STATE["messages"]
        while msgs and msgs[-1].get("role") == "assistant":
            msgs.pop()
            removed += 1
        if msgs and msgs[-1].get("role") == "user":
            msgs.pop()
            removed += 1
    if removed:
        _on_event(Event("site", "—", "undo", f"removed last exchange ({removed} messages)", level="warn"))
    return jsonify({"ok": True, "removed": removed})


def _user_message(msg, attachments, file_context):
    model_content = msg + ("\n\n" + file_context if file_context else "")
    message = {"role": "user", "content": msg, "model_content": model_content}
    if attachments:
        message["attachments"] = attachments
    return message


def _job_extra(ev_start, elapsed):
    """Ticket metadata for the reply header: job number, wall time, roles that worked."""
    with _lock:
        job = sum(1 for m in STATE["messages"] if m.get("role") == "user")
        workers = sorted({e.get("worker") for e in list(STATE["activity"])[ev_start:]
                          if e.get("worker") not in (None, "", "—", "-", "site", "local", "reminder")})
    return {"job": job, "elapsed_s": elapsed, "workers": workers}


def _run_job(msg, attachments=None, file_context="", user_appended=False):
    attachments = attachments or []
    user_message = None if user_appended else _user_message(msg, attachments, file_context)
    instruction = msg + ("\n\n" + file_context if file_context else "")
    started = time.time()
    ev_start = len(STATE["activity"])
    try:
        with _lock:
            history = [dict(m) for m in STATE["messages"]]
            if user_appended and history and history[-1].get("role") == "user" \
                    and history[-1].get("content") == msg:
                history = history[:-1]  # the queued message is already in the transcript
        reply = crew.handle(instruction, history)
        elapsed = round(time.time() - started, 1)
        extra = _job_extra(ev_start, elapsed)  # takes _lock; never call it inside the blocks below
        with _lock:
            if user_message is not None:
                STATE["messages"].append(user_message)
            STATE["messages"].append(dict({"role": "assistant", "content": reply}, **extra))
        compressed, did = crew.storekeeper.compress(
            STATE["messages"], max_tokens=crew.config.limit("max_context_tokens") or 2800)
        if did:
            with _lock:
                STATE["messages"] = compressed
    except Exception as e:
        elapsed = round(time.time() - started, 1)
        extra = _job_extra(ev_start, elapsed)  # same lock ordering as the success path
        with _lock:
            if user_message is not None:
                STATE["messages"].append(user_message)
            STATE["messages"].append(dict({"role": "assistant", "content": f"Crew error: {e}"}, **extra))
    finally:
        STATE["busy"] = False
        STATE["busy_since"] = None
        with _lock:
            counts = dict(STATE["tool_counts"])
            STATE["tool_counts"] = {}
            if counts and STATE["messages"] and STATE["messages"][-1].get("role") == "assistant":
                STATE["messages"][-1]["tools_used"] = counts


def _queue_worker():
    """Sends queued (busy/offline) messages when the crew is idle and internet works."""
    while True:
        time.sleep(10)
        try:
            if not STATE["queue"] or STATE["busy"]:
                continue
            if not _net_ok():
                continue
            if crew is None and not build_crew():
                continue
            with _lock:
                if not STATE["queue"] or STATE["busy"]:
                    continue
                item = STATE["queue"].pop(0)
                _save_queue()
                STATE["busy"] = True
                STATE["busy_since"] = time.time()
            _on_event(Event("site", "—", "queue-flush", str(item.get("message", ""))[:60], level="info"))
            try:
                threading.Thread(
                    target=_run_job,
                    args=(item.get("message", ""), None, item.get("file_context", "") or "", True),
                    daemon=True).start()
            except Exception:
                STATE["busy"] = False
        except Exception:
            pass


threading.Thread(target=_queue_worker, daemon=True).start()


_RESTORE_MAX_MESSAGES = 120
_RESTORE_MAX_CHARS = 8000
_RESTORE_ATTACHMENT_KEYS = ("name", "size", "type", "characters", "truncated")


def _restore_messages(raw):
    """Sanitize a client-supplied conversation before it becomes live state.

    Accepts only user/assistant text plus attachment metadata — never raw
    file data or model-only fields.
    """
    cleaned = []
    if not isinstance(raw, list):
        return cleaned
    for m in raw[:_RESTORE_MAX_MESSAGES]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        entry = {"role": role, "content": str(m.get("content", ""))[:_RESTORE_MAX_CHARS]}
        atts = m.get("attachments")
        if role == "user" and isinstance(atts, list):
            meta = [{k: a[k] for k in _RESTORE_ATTACHMENT_KEYS if k in a}
                    for a in atts[:8] if isinstance(a, dict)]
            if meta:
                entry["attachments"] = meta
        cleaned.append(entry)
    return cleaned


@app.post("/api/restore")
def restore():
    """Replace the live conversation with a saved one so the crew keeps its context."""
    data = request.get_json(silent=True) or {}
    if STATE["busy"]:
        return jsonify({"error": "the crew is already working on a job"}), 429
    cleaned = _restore_messages(data.get("messages"))
    with _lock:
        STATE["messages"] = cleaned
    return jsonify({"ok": True, "count": len(cleaned)})


@app.get("/api/state")
def state():
    with _lock:
        # Extracted file contents stay server-side; polling returns only small metadata.
        messages = [{k: v for k, v in m.items() if k != "model_content"}
                    for m in STATE["messages"]]
        activity = list(STATE["activity"])[-100:]
        approvals = [{k: v[k] for k in ("id", "tool", "summary", "time")}
                     for v in STATE["approvals"].values()]
    return jsonify({
        "messages": messages[-60:],
        "busy": STATE["busy"],
        "busy_since": STATE.get("busy_since"),
        "approvals": approvals,
        "activity": activity,
        "usage": crew.usage.to_dict() if crew else {},
        "tools": crew.registry.enabled_map() if crew else [],
        "models": crew.config.resolved if crew else {},
        "has_key": bool(crew or get_key()),
        "phone_mode": IS_TERMOX or IS_ANDROID,
        "android": IS_ANDROID,
        "queued": len(STATE["queue"]),
        "online": _net_ok(),
    })


@app.post("/api/key")
def set_key():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key", "")).strip()
    if not key.startswith("nvapi-") or len(key) < 20:
        return jsonify({"error": "That does not look like an NVIDIA key (should start with nvapi-)"}), 400
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("NVIDIA_API_KEY=" + key + "\n")
    except OSError as e:
        return jsonify({"error": f"could not save key: {e}"}), 500
    os.environ["NVIDIA_API_KEY"] = key
    global crew
    try:
        if crew is not None:  # key changed → rebuild the crew with it
            crew = None
        if not build_crew():
            return jsonify({"error": "key saved, but the crew failed to start (network?)"}), 500
    except Exception as e:
        return jsonify({"error": f"key saved, but the crew failed to start: {str(e)[:200]}"}), 500
    return jsonify({"ok": True, "models": crew.config.resolved})


@app.post("/api/approval")
def approval():
    data = request.get_json(silent=True) or {}
    if crew:
        crew.approvals.resolve(str(data.get("id", "")), bool(data.get("approved")))
    return jsonify({"ok": True})


@app.post("/api/tools")
def toggle_tool():
    data = request.get_json(silent=True) or {}
    if crew:
        crew.registry.set_enabled(str(data.get("id", "")), bool(data.get("enabled")))
    return jsonify({"ok": True})


@app.post("/api/clear")
def clear():
    with _lock:
        STATE["messages"] = []
    return jsonify({"ok": True})


# ---------------- Phase 1 store (additive, flag-gated; docs/BOSS_PHASE1_ORDER.md) ----
# Enabled by env SITECREW_STORE=1 OR a SITECREW_STORE marker file in BASE_DIR.
# With the flag off these endpoints 404 and the store modules are never imported
# (boss ruling: a bad sqlite build must not touch the shipped app).

def _store_enabled():
    return (os.environ.get("SITECREW_STORE") == "1"
            or os.path.exists(os.path.join(BASE_DIR, "SITECREW_STORE")))


def _debug_enabled():
    """Debug surfaces (/api/v2/debug/*) are off unless explicitly enabled on
    THIS install: SITECREW_DEBUG=1 env or a SITECREW_DEBUG marker file in
    BASE_DIR. A release APK ships neither — boss ruling: debug endpoints must
    never be reachable in production."""
    return (os.environ.get("SITECREW_DEBUG") == "1"
            or os.path.exists(os.path.join(BASE_DIR, "SITECREW_DEBUG")))


if _store_enabled():
    from sitecrew.api_v2 import register_v2          # lazy: nothing imported when the flag is off
    register_v2(app, lambda: _store("main"))         # get_store() -> the process-wide EventStore


_STORE = {"events": None, "error": None, "kill": None}


def _store(name="main"):
    """EventStore per named DB: main = sitecrew.db, kill = killtest.db (device kill
    leg uses its OWN DB file per the boss ruling — never sitecrew.db)."""
    if not _store_enabled():
        return None
    if _STORE["error"]:
        return None
    if _STORE.get(name) is None:
        try:
            from sitecrew.store.events import EventStore
            fname = "sitecrew.db" if name == "main" else "killtest.db"
            _STORE[name] = EventStore(os.path.join(BASE_DIR, fname))
        except Exception as e:
            _STORE["error"] = str(e)[:200]
    return _STORE.get(name)


@app.post("/api/v2/debug/append")
def debug_append():
    """Debug append loop for the P1.2-D device kill leg (event-driven via logcat).

    Body: {"cycles": N} batch mode; {"flat": true} starts a background thread that
    writes state+event pairs flat out (SC_STORE_ACK per commit) until {"stop": true};
    {"db": "kill"} targets killtest.db (the kill leg's own DB file — boss ruling).
    """
    if not _store_enabled() or not _debug_enabled():
        return jsonify({"error": "store/debug flag off"}), 404
    data = request.get_json(silent=True) or {}
    dbname = "kill" if data.get("db") == "kill" else "main"
    st = _store(dbname)
    if st is None:
        return jsonify({"error": "store unavailable", "detail": _STORE["error"]}), 503
    stream = "c_kill" if dbname == "kill" else "c_debug"

    if data.get("stop"):
        _STORE["flat_stop"] = True
        return jsonify({"ok": True, "stopped": True})

    if data.get("flat"):
        if _STORE.get("flat_running"):
            return jsonify({"ok": True, "already_running": True})
        _STORE["flat_running"] = True
        _STORE["flat_stop"] = False

        def flat_loop():
            i = 0
            while not _STORE.get("flat_stop"):
                i += 1
                cycle = st.head(stream) + 1
                mid = "k_%d" % cycle

                def msw(conn, seq, _mid=mid, _s=stream, _i=i):
                    conn.execute(
                        "INSERT OR IGNORE INTO chats(id, title, created_at,"
                        " updated_at) VALUES(?,?,1,1)", (_s, "debug"))
                    conn.execute(
                        "INSERT INTO messages(id, chat_id, role, kind, content_json,"
                        " status, created_at) VALUES(?,?,?,?,?,?,?)",
                        (_mid, _s, "user", "user",
                         json.dumps({"p": "F" * 2048, "i": _i}), "final", 1))
                    return _mid

                try:
                    env = st.append(stream, "message.created",
                                    {"p": "F" * 2048, "i": i},
                                    state_writer=msw, strict=True)
                    print("SC_STORE_ACK %s %d" % (stream, env["seq"]), flush=True)
                except Exception as e:
                    print("SC_STORE_ERR %s" % str(e)[:120], flush=True)
                    break
            _STORE["flat_running"] = False
            print("SC_STORE_FLAT_DONE", flush=True)

        threading.Thread(target=flat_loop, daemon=True).start()
        return jsonify({"ok": True, "flat": True, "db": dbname})

    cycles = max(1, min(int(data.get("cycles", 1)), 50))
    acks = []
    for i in range(cycles):
        cycle = st.head(stream) + 1
        if cycle % 2 == 1:
            mid = "k_%d" % cycle

            def msw(conn, seq, _mid=mid, _s=stream, _i=i):
                conn.execute(
                    "INSERT OR IGNORE INTO chats(id, title, created_at, updated_at)"
                    " VALUES(?,?,1,1)", (_s, "debug"))
                conn.execute(
                    "INSERT INTO messages(id, chat_id, role, kind, content_json,"
                    " status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (_mid, _s, "user", "user", json.dumps({"i": _i}),
                     "final", 1))
                return _mid

            env = st.append(stream, "message.created", {"i": i},
                            state_writer=msw, strict=True)
        else:
            tc, tu, tid = "tc_%d" % cycle, "tu_%d" % cycle, "t_%d" % cycle

            def tsw(conn, seq, _tc=tc, _tu=tu, _tid=tid, _s=stream):
                conn.execute(
                    "INSERT INTO turns(id, chat_id, state, started_at) VALUES(?,?,?,1)",
                    (_tid, _s, "running"))
                conn.execute(
                    "INSERT INTO tool_calls(id, turn_id, tool_use_id, name, status,"
                    " committed_seq) VALUES(?,?,?,?,?,?)",
                    (_tc, _tid, _tu, "fs.read", "committed", seq))

            env = st.append(stream, "tool.committed",
                            {"tool_use_id": tu, "name": "fs.read", "verified": True},
                            turn_id=tid, agent="debug", site="phone",
                            state_writer=tsw, strict=True)
        acks.append(env["seq"])
        print("SC_STORE_ACK %s %d" % (stream, env["seq"]), flush=True)  # logcat ACK
    return jsonify({"ok": True, "acks": acks})


@app.get("/api/v2/debug/store-state")
def debug_store_state():
    if not _store_enabled() or not _debug_enabled():
        return jsonify({"error": "store/debug flag off"}), 404
    dbname = request.args.get("db") or "main"
    st = _store("kill" if dbname == "kill" else "main")
    if st is None:
        return jsonify({"error": "store unavailable", "detail": _STORE["error"]}), 503
    stream = "c_kill" if dbname == "kill" else "c_debug"

    # Device facts for the P1.2-D report (boss fix #4): all computed on THIS
    # device against THIS DB — never reported from assumptions.
    import hashlib
    import sqlite3
    facts = {}
    try:
        uri = "file:%s?mode=ro" % st.path.replace(os.sep, "/")
        ro = sqlite3.connect(uri, uri=True)
        facts["journal_mode"] = ro.execute("PRAGMA journal_mode").fetchone()[0]
        facts["user_version"] = ro.execute("PRAGMA user_version").fetchone()[0]
        master = ro.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        ro.close()
        h = hashlib.sha256()
        for row in master:
            h.update(("%s|%s|%s\n" % row).encode("utf-8", "replace"))
        facts["schema_sha256"] = h.hexdigest()
        facts["sqlite_version"] = sqlite3.sqlite_version
    except Exception as e:
        facts["facts_error"] = str(e)[:200]
    wal = st.path + "-wal"
    facts["wal_bytes"] = os.path.getsize(wal) if os.path.exists(wal) else 0
    facts["store_flag"] = {
        "env": os.environ.get("SITECREW_STORE"),
        "marker": os.path.exists(os.path.join(BASE_DIR, "SITECREW_STORE")),
        "debug_env": os.environ.get("SITECREW_DEBUG"),
        "debug_marker": os.path.exists(os.path.join(BASE_DIR, "SITECREW_DEBUG")),
    }
    mods = {}
    for name in ("sitecrew/store/db.py", "sitecrew/store/events.py",
                 "sitecrew/api_v2.py"):
        p = os.path.join(BASE_DIR, name.replace("/", os.sep))
        try:
            with open(p, "rb") as f:
                mods[name] = hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            mods[name] = "error: %s" % str(e)[:100]
    facts["store_modules_sha256"] = mods

    return jsonify({"db": dbname, "head": st.head(stream),
                    "invariants": st.check_invariants(),
                    "error": _STORE["error"], **facts})


@app.get("/api/v2/debug/fts5-probe")
def debug_fts5_probe():
    """FTS5 probe inside the app's own Python (blueprint §2.2, boss order step 4)."""
    if not _store_enabled() or not _debug_enabled():
        return jsonify({"error": "store/debug flag off"}), 404
    try:
        import sqlite3
        r = {"platform": "android" if IS_ANDROID else "other",
             "sqlite_version": sqlite3.sqlite_version}
        conn = sqlite3.connect(":memory:")
        r["journal_mode_wal"] = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        opts = [row[0] for row in conn.execute("PRAGMA compile_options")]
        r["fts5_in_compile_options"] = any(o == "ENABLE_FTS5" for o in opts)
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(title, body)")
            conn.execute("INSERT INTO t(rowid, title, body) VALUES(1, 'hello', 'world')")
            r["fts5_create_and_match"] = len(
                conn.execute("SELECT rowid FROM t WHERE t MATCH 'hello'").fetchall()) == 1
        except Exception as e:
            r["fts5_create_and_match"] = False
            r["fts5_error"] = str(e)[:200]
        r["backend"] = "fts5" if r["fts5_create_and_match"] else "brain_terms"
        return jsonify(r)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
