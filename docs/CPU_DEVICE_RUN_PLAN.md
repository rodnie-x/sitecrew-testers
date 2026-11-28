# CPU diagnostic device run plan

Status: **PLAN ONLY / DEVICE NOT RUN**. This records Chrome-1's post-fix order on
Nov 28, 2026. No CPU result, device-half pass, or G1 release pass follows from
this document. The scoring device run may start only after the FIX FIRST items
land, PC tests pass, the frozen set is re-hashed, and Chrome-1 says SAFE TO RUN.

## 0. Preconditions

- Phone: TECNO LH7n, Android 14 / SDK 34, USB powered, screen on, stable cable/port.
- Store/debug markers remain app-private; no production endpoint is enabled without
  `build_flags.DEBUG_ENDPOINTS=True` plus the existing env/marker condition.
- Runtime probe facts already recorded on this phone: SQLite 3.35.5, JSON functions
  present, FTS5 absent → device search backend is `brain_terms`.
- Readiness precheck: `tests/evidence/device_cpu_precheck.json` records screen,
  USB power, battery, Doze and thermal status. Thermal status 3 was observed; any
  compared windows must match thermal state.

## 1. Freeze and deploy

- Finish CPU-diag FIX FIRST items, run PC smoke, then freeze the exact set:
  `cpu_diag.py`, `build_flags.py`, `server/app.py`, `sitecrew/api_v2.py`,
  `server/static/poll.js`, `sitecrew/store/db.py`, `sitecrew/store/events.py`.
- Deploy the **True-variant** `build_flags.py` for the diagnostic build. The
  committed release default remains False. Record both False and True hashes.
- Use `scripts/deploy_hot.py --backup --restart` so every changed file is pushed,
  hash-verified on-device, and the restart is proven by a changed PID set.
- Verify deploy through idle `/api/v2/debug/diag/status` module facts/hashes before
  any run.

## 2. Non-scoring wiring check

Run a short, explicitly overridden, non-scoring sequence:

- P-W with PC-style overrides, about 20 s per window, no leaky arm.
- One P-D1 run scaled to about 2 minutes with one snapshot at 20 s.

Before any scoring run, confirm:

- `device_state` fields are present and non-null.
- Per-route request counters are present.
- `parked_diag` equals `server_poll_threads` in client windows.
- TID-to-Python mapping and a footer are present.
- Result pull works over adb forward.
- Header labels the run as overridden; it is never scored.

Chrome-1 reads the raw JSONL before scoring runs.

## 3. Link soak

- 30 minutes, heartbeat only, zero drops required.
- Record cable, port, USB power and any adb loss. adb loss alone does not invalidate
  app-local sampling, but it must be recorded.

## 4. Scoring order

1. **P-D1 normal restart:** boot-staged plan, then force-stop/relaunch. Screen on
   and USB charging; keep device state identical across compared windows. No adb
   traffic except status at most once per 60 s. Pull the JSONL afterward.
2. **P-D2 hot-deploy restart:** hot-deploy identical bytes, restart, then exactly
   P-D1. Record order.
3. **P-W windows:** full cold/warm 0/2/1/0 plus 0/2/0 sequence and sensitivity
   window; omit the leaky arm.

If P-D1 shows a zero-client burn with the UI foreground, run a UI-quiesced repeat
with the page blanked or the app backgrounded. The new per-route counters will show
whether `/api/state` polling explains the burn.

## 5. Evidence and redaction

- Raw JSONL stays private and encrypted; public evidence lists raw and
  redaction-script SHA-256s.
- Hash the serial; strip owner paths, app-data paths, ports, IPs, bodies,
  environment, keys and Brain/DB content.
- A deterministic seeded secret/path scan must be clean before publishing.
- The offline analyzer decides validity. Samples never self-certify a pass.
- Device half, final CPU verdict and G1 release sample remain **NOT RUN** until the
  boss rules on this evidence.
