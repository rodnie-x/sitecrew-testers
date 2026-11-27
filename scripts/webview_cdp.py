"""WebView CDP harness (boss P1.3-device spec).

Attaches to the app's real WebView over adb forward + the devtools abstract
socket, and provides navigate / evaluate / reload / transcript-hash. Used for:
  - driving + reading the in-page probe (server/app.py /api/v2/debug/probe-page)
  - the transcript-hash comparison (uninterrupted vs chaos + reloads)
  - X-Liveness-Path + poll.js hash read IN-PAGE (the WebView path is the one
    that matters, per the boss)

CDP attachment must be NOTED in any timing run (the boss: one probe run
detached from CDP to show the 300 ms gate doesn't depend on the observer).
"""
import json
import os
import time
import urllib.request

import websocket                                    # pip install websocket-client

ADB = os.environ.get(
    "SC_ADB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".tooling", "android-sdk", "platform-tools", "adb.exe"))
PKG = "com.sitecrew.ai"
CDP_PORT = 9222


def _adb(*args, timeout=30):
    out = subprocess.run([ADB, *args], capture_output=True, text=True,
                         timeout=timeout, encoding="utf-8", errors="replace")
    return out.stdout.strip(), out.returncode


def find_devtools_socket():
    """The WebView devtools abstract socket for OUR app's pid."""
    out, _ = _adb("shell", "cat", "/proc/net/unix", timeout=20)
    names = [l.split()[-1].lstrip("@") for l in out.splitlines()
             if "webview_devtools_remote" in l]
    if not names:
        return None, []
    app_pid = None
    pids, _ = _adb("shell", "pidof", PKG, timeout=15)
    pids = [p for p in pids.split() if p.isdigit()]
    for n in names:
        suffix = n.rsplit("_", 1)[-1]
        if suffix.isdigit() and suffix in pids:
            return n, names
    return (names[0] if names else None), names


class CDP:
    """Minimal CDP over one websocket connection to a page target."""

    def __init__(self, port=CDP_PORT, timeout=30):
        self.port = port
        self.timeout = timeout
        self.ws = None
        self._id = 0

    def attach_to_page(self, url_prefix="http://127.0.0.1:8080"):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % self.port, timeout=10) as r:
            targets = json.loads(r.read().decode("utf-8"))
        page = None
        for t in targets:
            if t.get("type") == "page" and (t.get("url") or "").startswith(
                    url_prefix):
                page = t
                break
        if page is None and targets:
            page = targets[0]
        if page is None:
            raise RuntimeError("no CDP page target found")
        self.target_url = page.get("url")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                              timeout=self.timeout)
        return self.target_url

    def send(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while True:
            remaining = max(1, int((deadline - time.time()) * 1000))
            raw = self.ws.recv()
            msg = json.loads(raw)
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
        if r.get("subtype") == "error":
            raise RuntimeError("evaluate failed: %s" % r.get("description"))
        return r.get("value")

    def navigate(self, url):
        return self.send("Page.navigate", {"url": url})

    def reload(self):
        return self.send("Page.reload", {"ignoreCache": False})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def connect(timeout=30):
    """Full attach: find the devtools socket, forward it, return a CDP bound
    to the app's page target."""
    sock, names = find_devtools_socket()
    if not sock:
        raise RuntimeError("no webview_devtools_remote socket (WebView "
                           "debugging disabled or the app is not running); "
                           "sockets seen: %r" % names)
    _adb("forward", "tcp:%d" % CDP_PORT, "localabstract:%s" % sock, timeout=20)
    # the devtools endpoint needs a moment after the forward
    deadline = time.time() + 20
    last = None
    while time.time() < deadline:
        try:
            cdp = CDP(port=CDP_PORT, timeout=timeout)
            cdp.attach_to_page()
            return cdp
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError("CDP attach failed: %s" % last)


def transcript_hash(cdp):
    """The transcript hash IN THE REAL DOM (boss: a real WebView/DOM check).
    SHA-256 of the transcript-bearing element's innerText, computed in-page."""
    js = (
        "(async () => {"
        "  const el = document.querySelector('#transcript') ||"
        "             document.querySelector('main') || document.body;"
        "  const text = (el.innerText || el.textContent || '');"
        "  const buf = await crypto.subtle.digest('SHA-256',"
        "       new TextEncoder().encode(text));"
        "  const hex = [...new Uint8Array(buf)]"
        "      .map(b => b.toString(16).padStart(2, '0')).join('');"
        "  return JSON.stringify({ sha256: hex, chars: text.length,"
        "                          scope: el.tagName + '#' + (el.id || '') });"
        "})()"
    )
    return json.loads(cdp.evaluate(js, await_promise=True, timeout=20))


if __name__ == "__main__":
    cdp = connect()
    try:
        print(json.dumps({"target": cdp.target_url,
                          "transcript": transcript_hash(cdp)}, indent=2))
    finally:
        cdp.close()
