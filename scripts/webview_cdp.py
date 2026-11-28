"""WebView CDP harness (boss P1.3-device spec) — review-hardened.

Attaches to the app's real WebView over adb forward + the devtools abstract
socket, and provides navigate / evaluate / reload / transcript-hash. Used for:
  - driving + reading the in-page probe (server/app.py /api/v2/debug/probe-page)
  - the transcript-hash comparison (uninterrupted vs chaos + reloads)
  - X-Liveness-Path + poll.js hash read IN-PAGE (the WebView path is the one
    that matters, per the boss)

Review fixes applied: adb_helpers reuse (no local _adb), suppress_origin=True
(Chromium 111+ rejects foreign Origins with 403), Page.enable + load-event
waits + errorText checks on navigate/reload, send() timeout actually applied
(ws.settimeout before each recv), exceptionDetails checked, STRICT target and
transcript-scope selection (no fail-open fallbacks), forward return code
checked with stderr surfaced.
"""
import collections
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import websocket                                    # pip install websocket-client

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from scripts.android import adb_helpers  # noqa: E402

PKG = "com.sitecrew.ai"
CDP_PORT = 9222


def find_devtools_socket():
    """The WebView devtools abstract socket for OUR app's pid. STRICT: raises
    when none matches (no silent fallback to another app's WebView)."""
    out, rc = adb_helpers.adb("shell", "cat", "/proc/net/unix", timeout=20)
    if rc != 0:
        raise RuntimeError("/proc/net/unix read failed (rc=%s): %s"
                           % (rc, out[:200]))
    names = [l.split()[-1].lstrip("@") for l in out.splitlines()
             if "webview_devtools_remote" in l]
    if not names:
        raise RuntimeError("no webview_devtools_remote socket (WebView "
                           "debugging disabled or the app is not running)")
    pids, _ = adb_helpers.adb("shell", "pidof", PKG, timeout=15)
    pids = [p for p in pids.split() if p.isdigit()]
    for n in names:
        suffix = n.rsplit("_", 1)[-1]
        if suffix.isdigit() and suffix in pids:
            return n
    raise RuntimeError("no devtools socket matches the app pid %s; seen: %r"
                       % (pids, names))


class CDP:
    """Minimal CDP over one websocket connection to a page target."""

    def __init__(self, port=CDP_PORT, timeout=30):
        self.port = port
        self.timeout = timeout
        self.ws = None
        self._id = 0
        self.target_url = None
        self._events = collections.deque(maxlen=256)

    def attach_to_page(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % self.port, timeout=10) as r:
            targets = json.loads(r.read().decode("utf-8"))
        pages = []
        for target in targets:
            url = target.get("url") or ""
            parsed = urllib.parse.urlparse(url)
            if (target.get("type") == "page" and parsed.port == 8080
                    and parsed.hostname in ("127.0.0.1", "localhost")):
                pages.append(target)
        if not pages:
            raise RuntimeError("no app page target on 127.0.0.1/localhost:8080; "
                               "targets: %s"
                               % [t.get("url") for t in targets][:5])
        page = pages[0]
        self.target_url = page.get("url")
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=self.timeout,
            suppress_origin=True)                      # review fix #3
        self.send("Page.enable")                       # review fix #4
        return self.target_url

    def send(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        deadline = time.time() + (timeout or self.timeout)
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("%s timed out" % method)
            self.ws.settimeout(remaining)              # review #12
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("method"):
                self._events.append(msg)               # preserve load events
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s failed: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def evaluate(self, expression, await_promise=False, timeout=None):
        res = self.send("Runtime.evaluate",
                        {"expression": expression,
                         "awaitPromise": await_promise,
                         "returnByValue": True}, timeout=timeout)
        r = res.get("result", {})
        if res.get("exceptionDetails"):                # review LOW: thrown non-Errors
            raise RuntimeError("evaluate threw: %s"
                               % json.dumps(res["exceptionDetails"])[:300])
        if r.get("subtype") == "error":
            raise RuntimeError("evaluate failed: %s" % r.get("description"))
        return r.get("value")

    def _wait_load(self, timeout=30):
        """Read frames until Page.loadEventFired, including buffered events."""
        deadline = time.time() + timeout
        while True:
            while self._events:
                msg = self._events.popleft()
                if msg.get("method") == "Page.loadEventFired":
                    return True
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.ws.settimeout(remaining)
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Page.loadEventFired":
                return True
            if msg.get("method"):
                self._events.append(msg)
        raise TimeoutError("Page.loadEventFired not seen in %ss" % timeout)

    def _settle(self, url_prefix=None, timeout=30, ready_js=None):
        """Wait for the requested document, with a page-specific readiness
        expression (the debug probe has #out, not the app's #transcript)."""
        if url_prefix is None:
            target = urllib.parse.urlparse(self.target_url or "")
            url_prefix = urllib.parse.urlunparse(
                (target.scheme, target.netloc, "/", "", "", ""))
        ready_js = ready_js or ("document.querySelector('textarea, "
                                "div[contenteditable], #transcript, main')")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = self.evaluate(
                    "JSON.stringify({ rs: document.readyState,"
                    " href: location.href, hasApp: !!(" + ready_js + ") })",
                    timeout=5)
                d = json.loads(st) if isinstance(st, str) else st
                if (d.get("rs") == "complete"
                        and d.get("href", "").startswith(url_prefix)
                        and d.get("hasApp")):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("page did not settle on %s within %ss"
                           % (url_prefix, timeout))

    def navigate(self, url, settle=True, ready_js=None):
        self._events.clear()      # never consume a previous page's load event
        res = self.send("Page.navigate", {"url": url})
        if res.get("errorText"):                       # review fix #4
            raise RuntimeError("navigate failed: %s" % res["errorText"])
        # Even a non-settling away navigation must consume ITS load event;
        # otherwise about:blank can satisfy the next page's _wait_load().
        self._wait_load()
        if settle:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("settle=True requires an absolute HTTP(S) URL")
            prefix = urllib.parse.urlunparse(
                (parsed.scheme, parsed.netloc, "/", "", "", ""))
            self._settle(url_prefix=prefix, ready_js=ready_js)
        return res

    def reload(self, settle=True, ready_js=None):
        self._events.clear()      # never consume a previous page's load event
        res = self.send("Page.reload", {"ignoreCache": False})
        if res.get("errorText"):
            raise RuntimeError("reload failed: %s" % res["errorText"])
        self._wait_load()
        if settle:
            self._settle(ready_js=ready_js)
        return res

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def connect(timeout=30):
    """Full attach: find the devtools socket, forward it (return code
    checked), return a CDP bound to the app's page target."""
    sock = find_devtools_socket()
    out, rc = adb_helpers.adb("forward", "tcp:%d" % CDP_PORT,
                              "localabstract:%s" % sock, timeout=20)
    if rc != 0:
        raise RuntimeError("adb forward failed (rc=%s): %s" % (rc, out[:200]))
    deadline = time.time() + 20
    last = None
    while time.time() < deadline:
        try:
            cdp = CDP(port=CDP_PORT, timeout=timeout)
            cdp.attach_to_page()
            return cdp
        except (websocket.WebSocketException, OSError,
                RuntimeError, ValueError) as e:
            last = e
            time.sleep(1)
    raise RuntimeError("CDP attach failed: %s" % last)


def transcript_hash(cdp):
    """The transcript hash IN THE REAL DOM (boss: a real WebView/DOM check).
    STRICT (review LOW): #transcript must exist — no silent body fallback —
    and the scope + size are returned so compared runs must match."""
    js = (
        "(async () => {"
        "  const el = document.querySelector('#transcript');"
        "  if (!el) return JSON.stringify({ error: 'no #transcript element' });"
        "  const text = el.innerText || '';"
        "  const buf = await crypto.subtle.digest('SHA-256',"
        "       new TextEncoder().encode(text));"
        "  const hex = [...new Uint8Array(buf)]"
        "      .map(b => b.toString(16).padStart(2, '0')).join('');"
        "  return JSON.stringify({ sha256: hex, chars: text.length,"
        "                          scope: el.tagName + '#' + el.id });"
        "})()"
    )
    val = cdp.evaluate(js, await_promise=True, timeout=20)
    if val is None:
        raise RuntimeError("transcript hash evaluate returned None")
    out = json.loads(val) if isinstance(val, str) else val
    if out.get("error"):
        raise RuntimeError("transcript hash failed: %s" % out["error"])
    if not out.get("chars"):
        raise RuntimeError("transcript hash: empty transcript")
    return out


if __name__ == "__main__":
    cdp = connect()
    try:
        print(json.dumps({"target": cdp.target_url,
                          "transcript": transcript_hash(cdp)}, indent=2))
    finally:
        cdp.close()
