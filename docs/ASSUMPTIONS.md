# ASSUMPTIONS.md — Phase 0 resolution of every [ASSUME] in the spec

Statuses: **CONFIRMED** / **CORRECTED** / **UNKNOWN**. Evidence = `file:line` in the
unmodified tree. This file is the contract the spec's Phase 0 exit gate (G0) requires.

## A. The three things Claude could not know (spec §11.3)

| # | Assumption | Status | Evidence |
| --- | --- | --- | --- |
| A1 | APK is built by CI with p4a and deployed by `adb push` + `run-as cp` hot-deploy | **CONFIRMED** | `.github/workflows/build-apk.yml` — p4a `--bootstrap=webview --package=com.sitecrew.ai --port=8080 --private=app`; hot-deploy procedure recorded in `RESUME.md` |
| A2 | Supported Android versions: floor API 24, CI target API 33, physical device = TECNO LH7n on Android 14 (API 34) | **CONFIRMED** | p4a args `--minsdk=24 --ndk-api=24 --android-api=33`; device serial `sha256:41f18c1143227955` |
| A3 | Flask runs in the **same process** as the UI (webview bootstrap, port 8080); killing the app kills the agent | **CONFIRMED** | `apk_main.py:19,40-58` — `app.run(host="127.0.0.1", port=8080, threaded=True)` inside the p4a Python process; WebView polls localhost:8080 |

## B. Spec §1.3 [ASSUME] rows

| Assumption | Status | Evidence / correction |
| --- | --- | --- |
| NVIDIA hosted API is OpenAI-compatible (`/v1/chat/completions`) | **CONFIRMED** | `crew/nvidia.py:10` (`NVIDIA_BASE_URL = ".../v1"`), `:64-82` POST `/chat/completions` |
| …supports SSE streaming | **CORRECTED** | Current client is **non-streaming** `requests` JSON (`crew/nvidia.py:39-58`). Streaming is a Phase 2 build item, not an existing capability. No `tool_calls` handling exists today — tools run through the Operator's JSON-protocol loop, not OpenAI tool-call format |
| …tool_calls support varies by model → capability flags | **CONFIRMED as gap** | No capability flags exist; `crew/config.py` resolves roles from `available_models()`. Phase 2 adds flags |

## C. Spec §2 gap-table [ASSUME] rows

| Gap hypothesis | Status | Evidence |
| --- | --- | --- |
| Approvals in-memory + blocking, not durable/hash-bound | **CONFIRMED** | `crew/approvals.py:20-38` — dict + `Event.wait(180)`; no args hash, no persistence; worker thread blocks |
| No durable turn/tool record (app death mid-turn loses work) | **CONFIRMED** | transcript only in RAM (`server/app.py:47-55`); only `logs/queue.json` + append-only `logs/activity.jsonl` exist |
| No client idempotency keys (reconnect can duplicate) | **CONFIRMED** | `/api/chat` has no `client_msg_id` (`server/app.py:260-338`) |
| No tool schema validation / postcondition verify / inverse declarations | **CONFIRMED** | `tools/base.py:4-11` — only `params` descriptions + `sensitive` bool |
| Used chips derive from real events | **CONFIRMED (already true)** | `server/app.py:113-123,408-415` — counts from patched `Tool.run`, attached only after job ends; failed jobs still attach counts, so "commit-only" tightening is Phase 2/3 work |
| Undo is transcript-only | **CONFIRMED** | `server/app.py:341-357` |
| Localhost reachable by any app; no token, no Host check | **CONFIRMED** | binds 127.0.0.1 (`apk_main.py:55`, `run_server.py:41`) but no auth token/Host validation anywhere in `server/app.py` |
| Context: no token-budgeted builder/visible meter; Storekeeper compresses | **CONFIRMED** | `crew/storekeeper.py:16-26`, max 2800 tokens, no meter |
| Edit/regenerate/branch absent | **CONFIRMED** | no such routes/UI |
| Search/organise absent (recents only) | **CONFIRMED** | `index.html:444-513` localStorage list |
| Markdown/code quality unknown | **CONFIRMED partial** | renderer has tables/blockquote/hr handling (RESUME premium pass); streaming-safe rendering n/a (no streaming) |
| Visual craft: direction exists, not a token system | **CONFIRMED** | single `sitecrew.css`, no `tokens.css`/component split |

## D. Spec §6.12 runtime [ASSUME]s

| Assumption | Status | Evidence |
| --- | --- | --- |
| Foreground service exists | **CORRECTED — absent** | no service anywhere in `apk_main.py`/manifest perms; Phase 3 item |
| WebView SSE replay | **CORRECTED — absent** | UI polls `/api/state` (`index.html` refresh loop); no SSE |
| Key stored in plaintext `.env` (needs Keystore) | **CONFIRMED** | `server/app.py:85-97,520-540` |
| Redaction filter absent | **CONFIRMED** | `crew/models.py:56-65` writes events verbatim |
| Runtime permission prompts exist (from Python) | **CONFIRMED** | `apk_main.py:22-37`; needs_permission→RETRY flow is a Phase 2/3 addition |
| Storage: app roots + SAF; no MANAGE_EXTERNAL_STORAGE | **CONFIRMED** | `tools/platform.py:19-31`; `storage_help` tool opens All-Files-access settings instead of requesting the permission |
| Battery-optimisation diagnostic | **UNKNOWN → deferred P1** | not present |

## E. Spec §7.6 layout [ASSUME]

| Assumption | Status | Evidence |
| --- | --- | --- |
| "Flask entry with routes and inline agent logic" | **CONFIRMED** | `server/app.py` holds routing + queue + offline intents; agent logic in `crew/` |
| "static front end (`index.html`, `app.js`, `style.css`)" | **CORRECTED** | single `index.html` with inline JS + `sitecrew.css`; no `app.js` |
| "device-tool module(s) built on pyjnius" | **CONFIRMED** | `tools/android_impl.py` |
| "recents and queue stored as files or small DB" | **CORRECTED** | recents live in **client localStorage**, not files; queue in `logs/queue.json` |
| 26 tools / registry shape | **CONFIRMED** | `tools/build.py` registers 26; `tools/base.py:14-40` |

## F. Open unknowns for later phases

1. Whether pypdf builds are byte-identical across CI runs (only matters if we vendor-diff) — **accepted, not blocking**.
2. `QUERY_ALL_PACKAGES` fix is still parked (needs CI rebuild) — tracked in `RESUME.md`.
3. Device sensitive-effect tests require `docs/TEST_TARGETS.md` values only the owner can supply → those criteria stay **NOT RUN** until then.

## G. Findings discovered during characterization (Phase 0, no fixes applied)

| Finding | Evidence | Disposition |
| --- | --- | --- |
| **Registry divergence**: `/api/tools` toggles `crew.registry`, but the offline local path consults a separate `_LOCAL` registry — disabling a tool in Settings does NOT disable it in the offline local path | `server/app.py:163-172,234-246` (local) vs `server/app.py:551-556` (toggle on crew) | Phase 2: unify behind `LocalRouter` / shared registry; characterized by `test_disabled_tool_refused_locally` |
| **Failed jobs still get Used chips**: `_run_job`'s `finally` attaches `tool_counts` even when the reply is `Crew error: …` | `server/app.py:401-415` | Phase 2/3: attach counts only on committed turns (spec F06) |
| Queue persists to `logs/queue.json` outside any locking discipline across processes (single-process app, acceptable today) | `server/app.py:38-44` | Phase 1: replaced by SQLite store |
