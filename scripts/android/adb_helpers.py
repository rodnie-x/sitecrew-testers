"""Android device harness for evidence capture (spec §9.4), Windows-friendly.

Every command used by the gates lives here so reports cite one source. A step that
needs a human (airplane mode toggle on some OEMs) is reported, never faked.

CLI examples:
  python scripts/android/adb_helpers.py devices
  python scripts/android/adb_helpers.py screenshot reports/gate-0/screens/main.png
  python scripts/android/adb_helpers.py uidump reports/gate-0/ui/main.xml
  python scripts/android/adb_helpers.py logcat reports/gate-0/logs/logcat.txt
  python scripts/android/adb_helpers.py force-stop
  python scripts/android/adb_helpers.py offline on|off
  python scripts/android/adb_helpers.py forward 18110
"""
import os
import re
import subprocess
import sys
import time

PKG = "com.sitecrew.ai"
ACTIVITY = PKG + "/org.kivy.android.PythonActivity"

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# adb lives in the workspace next to the repo: <workspace>/.tooling/android-sdk/...
_DEFAULT_ADB = os.path.join(os.path.dirname(_REPO), ".tooling", "android-sdk",
                            "platform-tools", "adb.exe")


def adb_path():
    return os.environ.get("SC_ADB") or _DEFAULT_ADB


def adb(*args, timeout=30):
    out = subprocess.run([adb_path(), *args], capture_output=True, text=True,
                         timeout=timeout, encoding="utf-8", errors="replace")
    return out.stdout.strip(), out.returncode


def devices():
    out, _ = adb("devices")
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":   # state column, ignores -l suffixes
            rows.append(parts[0])
    return rows


def serial():
    """First usable device. devices() only returns rows in state "device",
    so "unauthorized" and "offline" entries are skipped by construction."""
    devs = devices()
    return devs[0] if devs else None


def screenshot(out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    raw, _ = adb("exec-out", "screencap", "-p", timeout=60)
    with open(out_path, "wb") as f:
        f.write(raw.encode("utf-8", errors="surrogateescape") if isinstance(raw, str)
                else raw)
    return os.path.getsize(out_path)


def screenshot_binary(out_path):
    """screencap via stdout can mangle bytes on some shells; pull-through-tmp fallback."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    adb("shell", "screencap", "-p", "/data/local/tmp/sc_gate.png", timeout=60)
    adb("pull", "/data/local/tmp/sc_gate.png", out_path, timeout=60)
    adb("shell", "rm", "/data/local/tmp/sc_gate.png")
    return os.path.getsize(out_path)


def uidump(out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    adb("shell", "uiautomator", "dump", "/sdcard/sc_gate.xml", timeout=60)
    adb("pull", "/sdcard/sc_gate.xml", out_path, timeout=60)
    adb("shell", "rm", "/sdcard/sc_gate.xml")
    return os.path.getsize(out_path)


def logcat(out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out, _ = adb("logcat", "-d", "-v", "time", timeout=120)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return os.path.getsize(out_path)


def force_stop():
    return adb("shell", "am", "force-stop", PKG)


def launch():
    return adb("shell", "am", "start", "-n", ACTIVITY)


def pid():
    out, _ = adb("shell", "pidof", PKG)
    return out.strip()


def pid_alive(target_pid):
    out, _ = adb("shell", "pidof", PKG, timeout=15)
    return bool(target_pid) and target_pid in out.split()


def hard_kill_pid(target_pid=None):
    """Kill the app, escalating through methods that work on a standard
    (non-rooted) device; returns (method, exit_status).

    Plain `adb shell kill -9` is usually DENIED on a non-rooted device (the
    shell uid cannot signal an app uid), so: run-as kill -9 first (works on
    debuggable builds), then am force-stop (always permitted), then plain
    kill -9 (rooted devices). The caller asserts the pid is actually gone.
    """
    p = target_pid or pid()
    if not p:
        return ("none", 0)
    out, rc = adb("shell", "run-as", PKG, "kill", "-9", p, timeout=20)
    if rc == 0 and not pid_alive(p):
        return ("run-as-kill-9", rc)
    out2, rc2 = adb("shell", "am", "force-stop", PKG, timeout=20)
    if not pid_alive(p):
        return ("am-force-stop", rc2)
    out3, rc3 = adb("shell", "kill", "-9", p, timeout=20)
    return ("kill-9", rc3)          # caller asserts death via pid_alive()


def hard_kill():
    return hard_kill_pid(pid())


def getprops():
    """Device facts for the report (recorded, never hardcoded)."""
    keys = {"ro.product.manufacturer": "manufacturer",
            "ro.product.model": "model",
            "ro.build.version.sdk": "sdk",
            "ro.build.version.release": "release"}
    props = {}
    for k, short in keys.items():
        out, _ = adb("shell", "getprop", k, timeout=15)
        props[short] = out.strip()
    return props


def package_info():
    """Installed APK versionName + path (from the device, not assumptions)."""
    info = {}
    out, _ = adb("shell", "dumpsys", "package", PKG, timeout=30)
    m = re.search(r"versionName=(\S+)", out)
    if m:
        info["versionName"] = m.group(1)
    out2, _ = adb("shell", "pm", "path", PKG, timeout=15)
    for line in out2.splitlines():
        if line.startswith("package:"):
            info["apk_path"] = line.split("package:", 1)[1].strip()
            break
    return info


def trim_memory():
    p = pid()
    return adb("shell", "am", "send-trim-memory", p, "RUNNING_CRITICAL") if p else ("", 0)


def forward(local=18110, remote=8080):
    return adb("forward", "tcp:%d" % local, "tcp:%d" % remote)


def offline(on):
    """Toggle data/wifi. Some OEMs ignore `svc`; report rather than assume."""
    action = "disable" if on else "enable"
    r1 = adb("shell", "svc", "wifi", action)
    r2 = adb("shell", "svc", "data", action)
    return r1, r2


def doze(on):
    if on:
        return adb("shell", "dumpsys", "deviceidle", "force-idle")
    return adb("shell", "dumpsys", "deviceidle", "step")


def font_scale(value):
    return adb("shell", "settings", "put", "system", "font_scale", str(value))


def animator_scale(value):
    return adb("shell", "settings", "put", "global", "animator_duration_scale", str(value))


def gfxinfo(out_path):
    out, _ = adb("shell", "dumpsys", "gfxinfo", PKG, "framestats", timeout=60)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return os.path.getsize(out_path)


def wait_server(base, timeout=90):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "devices"
    if cmd == "devices":
        print(devices() or "NO DEVICE")
    elif cmd == "screenshot":
        print("saved", screenshot_binary(sys.argv[2]), "bytes")
    elif cmd == "uidump":
        print("saved", uidump(sys.argv[2]), "bytes")
    elif cmd == "logcat":
        print("saved", logcat(sys.argv[2]), "bytes")
    elif cmd == "force-stop":
        force_stop(); print("stopped")
    elif cmd == "launch":
        launch(); print("launched")
    elif cmd == "offline":
        offline(sys.argv[2] == "on"); print("done")
    elif cmd == "forward":
        forward(int(sys.argv[2]) if len(sys.argv) > 2 else 18110); print("forwarded")
    else:
        print(__doc__)
