#!/usr/bin/env bash
# P1.2-D device kill leg on the CI emulator — single script so variables persist
# (the emulator-runner executes each script line as a separate `sh -c`).
#
# BOOT SEQUENCE (critical): p4a's FIRST run extracts the payload; killing the app
# during extraction poisons the loop (extract → killed → re-extract). So the first
# launch waits UP TO 300 s untouched for the server to answer; only then is the
# store marker written and the kill leg started.
set -e

BASE="http://127.0.0.1:18110"
PKG="com.sitecrew.ai"
CYCLES="${SC_CYCLES:-20}"

echo "== kill leg starting: $CYCLES cycles =="
adb wait-for-device

APK=$(ls apk-build/*.apk 2>/dev/null | head -n 1)
if [ -z "$APK" ]; then APK=apk-build/sitecrew-debug-0.1.apk; fi
echo "APK: $APK"
test -f "$APK"

adb install -r -t "$APK"
adb root
sleep 2
adb forward tcp:18110 tcp:8080

echo "== first launch: payload extraction + python boot (up to 300 s, untouched) =="
adb shell am start -n com.sitecrew.ai/org.kivy.android.PythonActivity || true
up=0
for i in $(seq 1 100); do
  if curl -s -m 3 "$BASE/api/state" > /dev/null 2>&1; then
    up=1
    echo "server up after ~$((i * 3)) s"
    break
  fi
  sleep 3
done
if [ "$up" != "1" ]; then
  echo "server never came up on first boot"
  adb logcat -d -v time > boot_failure_logcat.txt || true
  exit 1
fi

# server is up: now enable the store flag and restart into the kill leg
adb shell "echo 1 > /data/data/com.sitecrew.ai/files/app/SITECREW_STORE"
adb shell am force-stop com.sitecrew.ai
sleep 3
adb shell am start -n com.sitecrew.ai/org.kivy.android.PythonActivity || true
sleep 3

export SC_ADB=adb
export SC_CYCLES="$CYCLES"
export SC_BOOT_TIMEOUT=240
python tests/device_kill_leg.py
