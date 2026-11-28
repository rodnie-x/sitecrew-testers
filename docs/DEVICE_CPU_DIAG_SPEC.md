# Revised CPU diagnostic — device-boss implementation specification

Status: **DESIGN ONLY / DEVICE NOT RUN**. Received from the Chrome-1 device boss on
Nov 28, 2026. This supersedes the earlier PC-forwarded parker design. No CPU result,
root-cause claim, device-half pass, or G1 release pass follows from this document.

## 1. Frozen edit set

Before any device run, land one hash-verified edit set:

- `sitecrew/api_v2.py`: wake-reason counters and `X-Wake-Reason`.
- `server/app.py`: diagnostic registration and boot mode.
- New `cpu_diag.py`.
- `build_flags.py`: `DEBUG_ENDPOINTS` as a compile-time constant.

All join the frozen hash list. Diagnostic code and routes are inert/404 when the flag
is off. Earlier idle-park reports remain superseded errata.

## 2. Phone-local clients

`cpu_diag.py` runs N threads named `diagclient-<i>`, using `http.client` to the app's
loopback port. Each has a random cid and mirrors the real poll shape (`wait=25`),
re-polls immediately, and always reads and closes responses. No parked request crosses
adb. This does not test poll.js; the later in-page probe remains necessary.

Report total process CPU and **server-only CPU = total - diagclient-* -
diag-sampler**. The pass rule applies to server-only CPU. An optional `leaky_legacy`
arm is informational only and can never satisfy a pass.

## 3. WebView confound

The app's own poll.js page would invalidate a “zero-client” window. A boot plan must
load `about:blank` in diag mode. If the host cannot do that, the design is blocked.
Validity requires `parked_other == 0`; WebView renderer CPU is outside app-process CPU
and must be stated as such.

## 4. In-app sampler and records

A `diag-sampler` thread appends JSONL to an app-private file with flush+fsync per
record. Sampling survives adb loss. Intervals: 5 s in windows, 30 s in decay runs.
Record types: `header`, `sample`, `snapshot`, `window_marker`, `footer`.

Header: schema/run/plan hash, declared boot kind, process start, launch gap (<=15 s),
CLK_TCK, CPU count, Python/SQLite versions, runtime hashes+sizes+mtimes for the frozen
modules and served poll.js. Window-boundary device state: screen, charging, battery
saver, Doze, thermal status, level; unreadable fields are null with reasons.

Samples contain raw ticks, not self-certifying verdicts: sequence/window, monotonic
time, uptime, process utime/stime, sampler/client ticks and thread count; request and
park counters (`parked_diag`, `parked_other`); wake reasons and response classes;
client started/returned/error counts and last error class. Decay samples also carry a
per-thread TID/name/ticks table. Averages use cumulative tick deltas over the whole
span; p95 uses per-interval rates.

## 5. Thread attribution

Snapshot windows at start/middle/end and decay runs at uptime 60/300/600 s (±5 s).
Read `/proc/self/task/*/stat` twice 5 s apart and rank by tick delta. For the top 3,
capture five `sys._current_frames()` samples 100 ms apart, six frames maximum, rendered
as `func@basename:line`. Map native_id through `threading.enumerate()`; unmapped TIDs
are `native:<comm>` with no guessed Python frames. Per-wake cost is server-only ms /
slice wakes only when the top thread is in the park path; otherwise null.

## 6. Plans

- **P-D1 normal restart:** force-stop/relaunch, zero clients, 15 min, 30 s samples,
  snapshots at 1/5/10 min.
- **P-D2 hot-deploy restart:** push the identical frozen bytes, restart, then exactly
  P-D1. Run after P-D1 and record order.
- **P-W windows:** fresh restart; cold order 0,2,1,0 clients. Zero windows 300 s,
  client windows 600 s, each after a recorded/excluded 60 s settle. At uptime >=1800 s,
  warm repeat 0,2,0. Finish with a 2-client 60 s-sampling average-only sensitivity
  window; optional leaky_legacy last.

Each plan is one process lifetime with a restart before it.

## 7. Minimal flag-gated loopback endpoints

- `POST /api/v2/debug/diag/plan` (optional `start_after_uptime_s`; boot plan file too).
- `GET /api/v2/debug/diag/status` (state/window/uptime/hashes/sampler heartbeat).
- `GET /api/v2/debug/diag/result?run_id=` streaming JSONL.
- `POST /api/v2/debug/diag/abort`.

During a run, status polling is at most once per 60 s; other traffic is concurrent work.

## 8. Offline validity rules

The device records; an offline analyzer decides. Invalidate for:

1. Runtime hash mismatch, start/end drift, or frozen-list mismatch (whole run void).
2. Sample gap >2.5× interval, heartbeat loss, or <90% expected samples.
3. Any client error/timeout/unread/unclosed response in the window.
4. `parked_diag` differs from N in >5% of post-settle samples, or any `parked_other`.
5. Screen/charging/battery-saver/Doze mismatch between compared windows (comparison
   invalid, not necessarily the window).
6. Non-diag debug/API traffic, or cold/decay plan launch gap >15 s.

adb/forward loss alone does not invalidate local measurement; record it. A charging
state change still triggers rule 5.

## 9. Pass/classification rule

All are required:

- N-client server-only average delta over the mean of flanking zero windows <=1%.
- Server-only p95 delta <=3%.
- Zero-window drift <=half the threshold, else repeat once.

Classification: `ZERO_CLIENT_DECAYS` (state duration), `ZERO_CLIENT_PERSISTS`,
`CLIENT_ONLY`, `NO_BURN`, or `INCONCLUSIVE`. Provisional decay: three consecutive
30 s samples <=1%; the boss must still rule whether a launch burn is acceptable.

## 10. Public redaction

Hash serial with the private salt; shorten cid hashes to 8 hex chars. Frames contain
basename+line only, never locals/arguments. Strip owner paths, app-data paths, ports,
IPs, bodies, environment, keys, and Brain/DB content. Keep model/Android/hashes/ticks/
counters/timestamps. Raw JSONL stays privately encrypted; public evidence lists the
raw and redaction-script SHA-256s. A deterministic seeded secret/path scan must be
clean before publishing.

## 11. PC smoke scope (Linux/WSL for `/proc`)

Smoke output is `environment: pc_smoke`, outside gate evidence. Test proc/task parsing,
native-ID mapping, frames, counters, compressed plan state, local clients, lingering
threads, known busy-thread attribution, synthetic analyzer answers, redaction, and
404-when-off. Fault-inject hash mismatch, stalled sampler, client error, stray cid,
state mismatch and late start; each must produce its INVALID code.

## 12. Device-only / NOT RUN

Real CPU and decay, actual native/Python thread inventory, Android state via pyjnius,
device CLK_TCK, boot plan pickup, about:blank diag mode, restart/hot-deploy behavior,
mtime evidence, result pull, and Doze/screen effects. Device half, G1 release sample
and final CPU verdict remain held. The X-Wake-Reason PC rerun may proceed once the
header lands.
