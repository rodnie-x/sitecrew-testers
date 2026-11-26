# ARCHITECTURE AS-IS — SiteCrew AI (Phase 0 audit, 2026-11-25)

Ground truth for `docs/SITECREW_SPEC.md` §7.6. Every claim cites `file:line` in the
unmodified working tree. **No behavior changed during Phase 0.**

## 1. Runtime topologies

| Aspect | Reality | Evidence |
| --- | --- | --- |
| PC entry | `run_server.py` → `app.run(host="127.0.0.1", port=8000)` after `start_crew()` | `run_server.py:35-41` |
| Android entry | `apk_main.py` (p4a webview bootstrap) → `app.run(host="127.0.0.1", port=8080, threaded=True)` | `apk_main.py:19,40-58` |
| Process model | **One process**: Flask + crew + WebView all live inside the Android app process. Killing the app kills the agent; there is no foreground service. | `apk_main.py:40-58`; spec §6.12 gap |
| Permissions | All 8 runtime permissions requested at startup from Python via `android.permissions` | `apk_main.py:22-37` |
| Resilience | `build_crew()` failure never kills the app; crew rebuilds lazily on first chat | `apk_main.py:46-54`, `server/app.py:333` |
| CI build | p4a `--bootstrap=webview --minsdk=24 --ndk-api=24 --android-api=33 --arch=arm64-v8a --port=8080 --private=app` + 8 `--permission=` args | `.github/workflows/build-apk.yml` (p4a apk block) |
| Hot deploy | `adb push` → `run-as com.sitecrew.ai cp … files/app/…`; Python changes need force-stop/relaunch; static files need a reload only | `RESUME.md` deployment notes |

## 2. Server (`server/app.py`, 563 lines)

### State (all in-memory except the queue)
- `STATE` dict: `messages`, `busy`, `busy_since`, `activity` (deque 150), `approvals`, `queue`, `tool_counts` — `server/app.py:47-55`
- Single `threading.Lock` guards transcript/activity mutations — `server/app.py:56`
- **Queue persistence**: `logs/queue.json`, loaded at import (`_load_queue`), saved on every enqueue/flush — `server/app.py:26,29-44`
- Key: env var or `BASE_DIR/.env` plaintext — `server/app.py:85-97`

### Routes
| Route | Method | Behavior | Evidence |
| --- | --- | --- | --- |
| `/` | GET | serves `static/index.html` | `server/app.py:136-138` |
| `/api/chat` | POST | 0) tools-tour regex → registry answer; 1) offline-intent regex → local tool (short commands only when online, ≤45 chars); 2) busy → queue; 3) offline → queue; else start `_run_job` thread | `server/app.py:260-338` |
| `/api/undo` | POST | pops trailing assistant message(s) + preceding user message; refuses while busy (429). Transcript-only — no tool inverses | `server/app.py:341-357` |
| `/api/restore` | POST | sanitized conversation replace (max 120 msgs / 8000 chars / 8 attachment metas) | `server/app.py:451-491` |
| `/api/state` | GET | transcript (−`model_content`), activity, approvals, usage, tools, models, `online` probe, `queued` count | `server/app.py:494-517` |
| `/api/key` | POST | validates `nvapi-` prefix, writes `.env`, rebuilds crew | `server/app.py:520-540` |
| `/api/approval` | POST | `crew.approvals.resolve(id, approved)` | `server/app.py:543-548` |
| `/api/tools` | POST | toggle tool enabled | `server/app.py:551-556` |
| `/api/clear` | POST | clears transcript | `server/app.py:559-563` |
| 413 handler | — | friendly upload-too-large JSON | `server/app.py:65-67` |

### Job execution
- `_run_job` runs on a daemon thread; appends user+assistant rows, Storekeeper compresses over 2800 tokens; on exception appends `Crew error: …`; `finally` attaches `tools_used` counts to the last assistant row — `server/app.py:377-415`
- `_wrap_tool_counts` monkey-patches every `Tool.run` to count calls per job — `server/app.py:113-123`
- `_queue_worker` daemon thread polls every 10 s; flushes FIFO when idle + online; single item per pass — `server/app.py:418-448`
- Connectivity: cached 10 s TCP probe to `integrate.api.nvidia.com:443` — `server/app.py:143-160`

### Offline local answers
- `_OFFLINE_INTENTS`: 11 regex→tool mappings (battery, device_info, list_apps, app_info, open_app, get_settings, clipboard, vibrate, reminders, find/list files) — `server/app.py:176-195`
- `_TOOLS_TOUR`: deterministic registry tour — `server/app.py:203-223`
- Local tools run through the same registry (counts toward Used chips) — `server/app.py:234-246`

## 3. Crew (`crew/`)

| Module | Role | Notes |
| --- | --- | --- |
| `site.py` | Orchestrator: express lane (≤60 chars w/o tool hints), Foreman classify + keyword safety net, simple→Laborer or Operator+Inspector, complex→Engineer plan→steps→assembly→Inspector rework ladder | `crew/site.py:19-43,85-193` |
| `nvidia.py` | `requests` POST `/chat/completions`, **non-streaming**, 1 retry on Connection/Timeout, `<think>` strip, usage metering | `crew/nvidia.py:20-82` |
| `foreman.py` | Classifier → JSON route; hardened prompt + non-dict guard | (per RESUME) |
| `engineer.py` | Complex plans | |
| `laborer.py` | Worker pool with escalation ladder; empty-reply retry | |
| `inspector.py` | Review/rework verdicts | |
| `operator.py` | Tool loop: model proposes tools, executes, `_clean_answer` strips invented "Tools used" lines | `crew/operator.py` |
| `storekeeper.py` | Token-budget compression; keeps last 6 turns verbatim (tools_used survives) | `crew/storekeeper.py:16-26` |
| `approvals.py` | `ServerApprovals`: in-memory pending card + `threading.Event.wait(180)`; **not durable, not bound to argument hashes, blocks the worker thread** | `crew/approvals.py:17-38` |
| `models.py` | `Event`, `Usage`, `log_jsonl` (append-only `logs/activity.jsonl`, no redaction) | `crew/models.py:12-65` |
| `config.py` | Role→model resolution against `available_models()`; limits | |

## 4. Tools (`tools/`)

- `base.py`: `Tool(id, desc, run, params, sensitive, enabled)` + `ToolRegistry` (describe/enabled_map/set_enabled) — `tools/base.py:4-40`. **No JSON-schema validation, no risk taxonomy beyond `sensitive`, no verify/inverse/reconcile.**
- `build.py`: registers 26 tools — send_sms, make_call, read_sms, list_contacts, take_photo, get_location, read_clipboard, write_clipboard, notify, speak, open_path, battery, vibrate, add_reminder, list_files, read_file, write_file, find_files, storage_help, list_apps, app_info, open_app, uninstall_app, device_info, get_settings, set_setting. Sensitive: sms/call/write_clipboard/notify/open_path/write_file/uninstall/set_setting (per flags).
- `platform.py`: `IS_TERMOX` (env PREFIX), `IS_ANDROID` (jnius importable + `/data/data`), `storage_root()` fallbacks — `tools/platform.py:4-31`. On PC everything simulates safely.
- `android_impl.py`: pyjnius implementations (battery via `IntentFilter` fix, apps, settings via `Settings.Global`, etc.).
- `filesys.py`, `reminders.py` (persistent JSON reminders + background loop).

## 5. Front end (`server/static/`)

- Single `index.html` (~52 KB, inline JS) + `sitecrew.css` (~27 KB). No framework, no build step.
- Recents: `localStorage` keys (`sc_live_id`, `sc_recents`, …) auto-saved; restore posts `/api/restore` — `index.html:295-296,444-457,811-834`
- Export: client-side JSON download of the current/archive transcript — `index.html:1124-1135`
- Activity: polls `/api/state`; Used chips render from `tools_used` counts; undo button on last reply.
- Identity: graphite/concrete surfaces, squared controls, terracotta `#D97757` dark / `#C96442` light (tokens in CSS custom properties).

## 6. Uploads (`server/attachments.py`, 304 lines)

- Limits: 5 files, 8 MB/file, 20 MB total, 12k chars/file, 40k total; zip-bomb guards — `attachments.py:14-20`
- Extraction: TXT (utf-8/cp1252/utf-16), PDF (pypdf, vendored in APK at `apk/app/libs/pypdf`), DOCX/XLSX/PPTX/ODT/ODS/ODP (stdlib zip+xml), RTF — `attachments.py:211-226`
- File contents stay server-side: `/api/state` returns metadata only (`text` stripped, `model_content` stripped) — `server/app.py:497-499`, `attachments.py:303`

## 7. What exists vs. spec §7.6 target layout

| Spec target | Current state |
| --- | --- |
| `sitecrew/agent/*` engine | **does not exist** — logic lives in `server/app.py` + `crew/*` |
| `sitecrew/store/db.py` | **does not exist** — transcript in RAM, queue in `logs/queue.json`, recents in client `localStorage` |
| `sitecrew/android/*` | **does not exist** — direct pyjnius calls inside `tools/android_impl.py`; no service/keystore/connectivity module |
| `sitecrew/api/routes.py` | **does not exist** — routes are functions on the single `app.py` module |
| `web/*` split tokens/components | **does not exist** — one `sitecrew.css` |
| Legacy import path | n/a yet — queue.json + localStorage are the legacy sources for Phase 1 migration |

## 8. Preserved-behavior inventory (characterization targets, spec §10.2)

1. NVIDIA chat via crew (fake-able at `crew` seam; live path needs key+network)
2. Uploads & extraction (docx/pdf/txt/rtf + all limit rejections)
3. Exports (client-side JSON; HTML presence test)
4. Auto-saved recents + restorable chats (`/api/restore` sanitize + localStorage keys)
5. Android tools battery/apps/settings/device/files (PC-simulated; real values on device)
6. Approval-gated sensitive actions (ServerApprovals + `/api/approval`)
7. Deterministic offline device answers (`_OFFLINE_INTENTS`, `_TOOLS_TOUR`)
8. Persistent queued messages + reconnect flush (`logs/queue.json`, `_queue_worker`)
9. Used chips from real tool counts (`_wrap_tool_counts`)
10. Queue/Steer semantics (busy → queued, transcript append, run-after)
11. Transcript Undo (`/api/undo`)
12. Identity tokens in CSS (`#D97757`, `#C96442`, squared controls)
13. Forbidden: no Edit/Shell activity strings, no fake chips, no invented facts (grep gates)
