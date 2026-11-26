#!/usr/bin/env bash
# P1.2-D device kill leg on the CI emulator — single script so variables persist
# (the emulator-runner executes each script line as a separate `sh -c`).
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

# boot the app once so p4a unpacks the payload, then enable the store flag
adb shell am start -n com.sitecrew.ai/org.kivy.android.PythonActivity || true
sleep 25
adb shell "echo 1 > /data/data/com.sitecrew.ai/files/app/SITECREW_STORE"
adb shell am force-stop com.sitecrew.ai
adb forward tcp:18110 tcp:8080

export SC_ADB=adb
export SC_CYCLES="$CYCLES"
export SC_BOOT_TIMEOUT=240
python tests/device_kill_leg.py
