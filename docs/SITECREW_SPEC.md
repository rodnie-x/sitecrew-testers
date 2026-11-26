# SiteCrew AI — Master Build Specification

*Design direction · feature strategy · agent architecture · test plan · autonomous build prompt — v1.0 · 25 November 2026*

## 0. How to read this document

Four labels mark how much to trust each claim:

- **[YOU]** — stated in your brief.
- **[SDK]** — checked against the claude-agent-sdk-python repository and Anthropic’s SDK docs while writing this.
- **[ASSUME]** — my assumption about your codebase. I can’t see your code, so every file path, table name and route below is a *target contract*. Phase 0 of the build confirms or corrects each assumption and records the deviation.
- **[DECISION]** — an opinionated call made for you, so there is no menu of alternatives.

**The bar for “done”:** on a real Android phone the app behaves correctly under streaming, cancellation, queueing, steering, airplane mode, app kill and approval flows — with evidence — and the interface is calm, fast and unmistakably SiteCrew. A prettier mockup over today’s engine fails this bar. So does a reliable engine behind today’s polish.

**Order:** §1 settles the foundation. §2–§5 are product and design. §6–§7 are engineering. §8–§10 are proof and sequencing. §11 is the prompt to paste into your coding agent.

## 1. Foundation verdict: the SDK, ported honestly

### 1.1 What the SDK actually is [SDK]

- A Python (3.10+) wrapper that launches the **Claude Code CLI as a subprocess** — bundled inside platform wheels built for macOS, Linux and Windows — and talks to it over a bidirectional control protocol.
- The agent loop, permission evaluation, session persistence and model calls run **inside that CLI**. The Python side supplies callbacks (hooks, `can_use_tool`, in-process tools) and consumes a message stream.
- It is bound to Claude models and Claude Code’s own toolset (Read, Write, Edit, Bash…).

### 1.2 Verdict [DECISION]

The SDK **cannot be the runtime** inside Flask on Android:

1. It needs a native CLI subprocess that is not shipped for Android.
2. It speaks to Claude; you are keeping NVIDIA models.
3. The loop lives in the CLI, so you cannot swap the model backend without a translating proxy — more moving parts on a low-spec phone.
4. Its sessions are CLI-owned and its approval callback is an in-memory await. Neither survives process death, which is your hardest reliability requirement.
5. Its README ties SDK use to Anthropic’s commercial terms. A semantics-only port (no copied source) avoids the question. Don’t vendor SDK code.

So SiteCrew gets **`sitecrew.agent`**: a pure-Python, in-process runtime that keeps the SDK’s *vocabulary and semantics* — option names, hook events, hook-output JSON, permission order, message/block shapes, interrupt behaviour — and swaps the engine underneath for an NVIDIA chat-completions client plus SQLite durability. The SDK docs stay your mental model; the code is yours.

**[ASSUME]** NVIDIA’s hosted API is OpenAI-compatible (`/v1/chat/completions`, SSE streaming, `tool_calls`). Tool-calling quality varies by model, so each model gets capability flags in config: `supports_tools`, `supports_vision`, `context_window`, `max_output`.

### 1.3 Exact mapping: SDK → SiteCrew adapter

| # | SDK semantics [SDK] | SiteCrew adapter | Deviation and why |
| --- | --- | --- | --- |
| 1 | `ClaudeSDKClient`: connect → `query()` → `receive_response()` → disconnect | `AgentSession` per chat: `submit()`, `events(after_seq)`, `cancel()`, `close()` | The session *is* the chat; state lives in SQLite, not in a subprocess |
| 2 | `query()` one-shot | `AgentSession.run_once()` for internal jobs (title, summary) | Same loop, tools off by default |
| 3 | `AssistantMessage`, `UserMessage`, `SystemMessage`, `ResultMessage` | Events `message.completed(role)`, `turn.started`, `turn.completed` carrying `terminal_reason` | Events are persisted and replayable by `seq`; the SDK stream is ephemeral |
| 4 | Blocks: `TextBlock`, `ToolUseBlock`, `ToolResultBlock` | Same shapes in `messages.content_json` | Normalised from OpenAI-style `tool_calls` |
| 5 | `include_partial_messages` → `StreamEvent` | `message.delta` events; DB snapshot coalesced every ~150 ms | Avoids write amplification on weak storage |
| 6 | `ClaudeAgentOptions` (`allowed_tools`, `disallowed_tools`, `permission_mode`, `max_turns`, `cwd`, `hooks`, `can_use_tool`) | `AgentOptions` dataclass with the same names, plus `ask_tools`, `max_tokens_budget`, `scope_roots` (for `cwd`) | `permission_mode` is only `default` or `readonly`. **No bypass mode exists** |
| 7 | `@tool` + `create_sdk_mcp_server` (in-process tools) | `@tool(name, description, schema, risk, …)` + `ToolRegistry` | Same in-process spirit without the MCP wire protocol. Names are `domain.verb` |
| 8 | Hook events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `PermissionRequest`, `Notification`, `SessionStart/End`, `PreCompact` | `HookRegistry` supports the same names; matcher = regex on tool name; callbacks `(input, tool_use_id, ctx) -> dict` | `tool_use_id` correlation kept. Subagent hooks omitted — there are no subagents |
| 9 | Hook output: `hookSpecificOutput.permissionDecision` (`allow`/`deny`/`ask`), `updatedInput`, `additionalContext`, `updatedToolOutput`, `continue_`, `systemMessage` | Identical keys | A hook `allow` never skips deny/ask rules (documented SDK behaviour) |
| 10 | Permission order: **hooks → deny rules → ask rules → permission mode → allow rules → `can_use_tool`** | `PermissionEngine.evaluate(call)`, same order. Fall-through default: read → allow, write → ask, sensitive → ask | Doc versions word the steps slightly differently. Port the order and *test it with a table* |
| 11 | `can_use_tool` awaits the user | `ApprovalBroker`: persist `approvals` row → emit `approval.requested` → suspend the turn (no thread blocked) → resolve by HTTP POST | **Durable.** Survives restart, is bound to an args hash, and expires |
| 12 | `interrupt()`: streaming only; buffer *not* cleared; a `ResultMessage` still arrives; reasons `aborted_streaming` / `aborted_tools` | `AgentSession.cancel(turn_id)` with a cooperative `CancelToken`; always ends in `turn.cancelled` with the same two reasons; partial text kept and flagged | Exact semantic parity |
| 13 | Streaming input: send another message while a turn runs | **Queue vs Steer** (§6.6) | The SDK defines no steer. This is a SiteCrew extension, labelled as such |
| 14 | Sessions: `resume`, `fork_session`, `resume_session_at` | `chat_id` = session. `recover()` = resume. Branch = fork at `message_id`. Edit/regenerate = resume-at | SDK sessions are CLI files; yours are SQLite rows |
| 15 | `max_turns`, `max_budget_usd` | `max_steps`, `max_tokens_budget` | NVIDIA usage is not metered in USD |
| 16 | `ResultError(subtype, terminal_reason, api_error_status)` | `TerminalReason` enum + `error_code` | Adds `verify_failed`, `outcome_unknown`, `interrupted_by_death` |
| 17 | File checkpoints / `rewind_files` | **Inverse-operation Undo** (§6.9) | SDK rewinds source files; SiteCrew tools are device effects, so each tool declares its own inverse |

### 1.4 Deliberately not ported [DECISION]

Subagents, external MCP servers, the Claude Code toolset, settings sources and `CLAUDE.md`, plugins, prompt caching, `bypassPermissions`, `acceptEdits`. If an NVIDIA reasoning model streams `reasoning_content`, store it for debugging but never render it as a “Used” item and never feed it back as an instruction. SiteCrew has no Edit or Shell tool, so none can ever appear in the UI.

### 1.5 Where parity breaks — said plainly

- **Durability goes beyond the SDK.** Write-ahead intent records and restart recovery have no SDK equivalent. They are the main value of the adapter.
- **NVIDIA-hosted open models call tools less reliably.** The adapter therefore validates every call against JSON Schema, runs a bounded repair loop, and detects repeated identical calls.
- **Hooks run on a weak CPU.** Each hook gets a 250 ms budget. A timeout fails *closed*: deny for write and sensitive tools, skip for read tools.
- **One process.** A hung tool can wedge a worker, so every tool runs under a watchdog and a hard timeout (§6.7).

## 2. Candid gap analysis

I only know what you described. “Probable gap” items are hypotheses for Phase 0 to confirm or kill.

| Area | What you have [YOU] | Probable gap [ASSUME] | Severity |
| --- | --- | --- | --- |
| Generation control | Queue/Steer | Stop must close the HTTP stream, keep the partial text and leave tools in a known state | P0 |
| Turn durability | Persistent queued messages | No durable turn/tool record, so app death mid-turn loses work and can re-run or duplicate an action | P0 |
| Duplicate protection | Reconnect flush | Without client-generated idempotency keys, a half-successful flush duplicates messages | P0 |
| Tool contract | Real tools, approvals | Needs schema validation, declared risk, postcondition verify and a declared inverse. Hallucinated arguments are the dominant failure mode | P0 |
| Used chips | Real chips | Must derive *only* from committed events written in the same DB transaction as the result. A failed or cancelled call must be unable to render a success chip | P0 |
| Undo | Transcript Undo | Removes text, not effects. Needs per-tool inverse operations and an honest “not reversible” state | P0 |
| Approvals | Gated | Probably in-memory and blocking. Must be durable, bound to the exact arguments, expiring, and accessible without a hold gesture | P0 |
| Localhost security | In-process Flask | Any app on the phone can reach `127.0.0.1:port`. Needs a per-launch token and Host/Origin checks | P0 |
| Android lifecycle | WebView + pyjnius | Backgrounding, Doze and process kill stop Flask mid-turn. Needs a foreground service while work is active, and SSE replay when the WebView returns | P0 |
| Context | Restorable chats | No token-budgeted context builder, rolling summary or visible meter, so truncation is silent | P0 |
| Edit / regenerate / branch | — | Table stakes in 2026. Needs a message tree (`parent_id`), not a flat list | P0 linear, P1 branch UI |
| Search / organise | Recents | Full-text search, pin, rename, delete with undo | P0 / P1 |
| Markdown / code | Unknown | Streaming-safe rendering, copy, scrollable tables, zero layout jump | P0 |
| Voice, reminders, share-in, usage | — | The Android differentiators — §5 | P1 |
| Visual craft | A direction | A direction is not a system. Tokens, type, state coverage, motion and accessibility must be specified *and enforced by tests* | P0 |

**Preserve, do not rewrite:** NVIDIA model access, upload/extraction, exports, recents and restore, every real Android tool, deterministic offline device answers, the approval requirement for SMS/calls/uninstall/settings, queued-message persistence, Queue/Steer, transcript Undo. Each becomes a regression gate in §10.

## 3. One product and design direction

### 3.1 Concept — “The Logbook” [DECISION]

SiteCrew is not a chat toy that sometimes touches your phone. It is a **field-operations logbook with a conversational front**: every action is a dated line item with a source, a result, a verification and — when physically possible — a return path. Other assistants perform; SiteCrew *records*. The design’s whole job is to make that truth legible without noise.

### 3.2 Five principles

1. **Evidence over theatre.** Nothing animates to imply work that isn’t happening. Status comes from committed events only.
2. **Provenance on every device fact.** `LIVE`, `LOCAL`, or `CACHED·3m` — the user can always tell where a number came from.
3. **One accent, used like a signal lamp.** Terracotta marks *what needs you or is happening now*. Never decoration.
4. **Squared, ruled, quiet.** Hairlines and tonal steps instead of cards, shadows, gradients or glass.
5. **Reversibility is visible.** Before acting, the UI says whether the action can be undone. After acting, it shows how.

### 3.3 Six signature details

1. **The Ledger Rail.** A 2 px vertical rail runs down the left edge of each assistant turn. Graphite when idle, terracotta while the turn runs, solid when complete, hatched when failed, broken when cancelled. Every committed tool call adds a small square tick on the rail. Ticks appear *only* on `tool.committed`, so the rail is the proof that chips are real.
2. **Receipts, not bubbles, for tools.** A tool result renders as one monospaced line — `BATTERY 62% discharging 14:07:22 LIVE` — tabular numerals, a right-aligned provenance tag, and a tap target that opens the real arguments, result, duration and verification status.
3. **The Work Order.** Sensitive actions appear as a ruled form with crop-mark corner ticks: the exact recipient or package, the exact body, a reversibility line (`CANNOT BE UNDONE`), and two squared buttons — **DECLINE** and **SIGN**. Sensitive signing is press-and-hold (segmented fill, 600 ms, haptic tick) with a visible *Confirm* button alternative for accessibility.
4. **The Signal Strip.** A 3 px full-width strip under the status bar: solid terracotta = live, dashed graphite = offline, with `Q·2` numerals when messages are queued. It is the *only* connectivity indicator, and it never lies — it reflects real request outcomes, not just the OS flag.
5. **Stamps.** State words rendered as small ruled stamps — `SENT`, `QUEUED`, `CANCELLED`, `REVERTED 14:09`, `VERIFIED`. Reverted items get a hairline strike-through with the stamp. Stamps are the only decoration in the app, and each one is a real state.
6. **Mechanical motion and a one-square wordmark.** Progress advances in discrete `steps()`, like a gauge, never springy. The wordmark is `SITECREW` in condensed caps followed by a single terracotta square, which doubles as the loader (the square steps through four positions).

### 3.4 Design tokens

**Colour** — computed contrast is noted; the agent must verify all pairs with a script and fail the build below thresholds.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `bg.0` canvas | `#121314` | `#EFEDE8` | App background (graphite / concrete) |
| `bg.1` raised | `#181A1B` | `#F6F4F0` | Sheets, composer |
| `bg.2` inset | `#1F2224` | `#E6E3DD` | User blocks, code, selected row |
| `line.1` | `#2B2F32` | `#D3CFC7` | Hairlines |
| `line.2` | `#3C4145` | `#B9B4AA` | Strong rules, control outlines |
| `text.1` | `#ECE9E3` | `#1C1B19` | Body |
| `text.2` | `#A8A69F` | `#55524C` | Secondary |
| `text.3` | `#8A8882` | `#68645D` | Meta, ≥ 12 px only (≥ 4.5:1 both themes) |
| `accent` | `#D97757` | `#C96442` | Fills, rails, focus rings |
| `accent.ink` | `#1B120E` | `#1B120E` | Text on accent fills (≈ 6:1 dark, ≈ 4.7:1 light; white on `#C96442` is only ≈ 3.9:1, so never use it) |
| `accent.text` | `#D97757` | `#A94F31` | Small accent text (light needs the darker value: `#C96442` on `bg.0` is only ≈ 3.3:1) |
| `accent.wash` | accent @ 10% | accent @ 10% | Selected/active row tint |
| `ok` | `#86A982` | `#3F6B3B` | Verified |
| `warn` | `#D6A23E` | `#8A5F0A` | Offline, expiring |
| `danger` | `#E8625C` | `#B3261E` | Failure. *Always* paired with a glyph and a word — it sits close to terracotta in hue |

**Type** — all fonts bundled locally (WOFF2, Latin subset, `font-display: swap`; no network fonts, the app works offline). Families: **IBM Plex Sans** 400/600, **IBM Plex Sans Condensed** 600, **IBM Plex Mono** 400/500. Total budget ≤ 150 KB. Fallback stack `system-ui, Roboto, sans-serif`. Industrial, legible, and not the default AI look.

| Token | Size / line | Face | Use |
| --- | --- | --- | --- |
| `t.label` | 11 / 14, +0.08em, caps | Plex Sans Condensed 600 | Section labels, stamps, state tags |
| `t.meta` | 12 / 16 | Plex Sans 400 | Timestamps, counts |
| `t.mono` | 13 / 20 | Plex Mono 400 | Receipts, code, tabular data |
| `t.body-s` | 14 / 20 | Plex Sans 400 | Secondary UI text |
| `t.body` | 16 / 26 | Plex Sans 400 | Chat prose (≤ 66ch on wide screens) |
| `t.title` | 18 / 24 | Plex Sans 600 | Sheet and card titles |
| `t.head` | 24 / 28, caps | Plex Sans Condensed 600 | Screen titles, empty-state headlines |

A user font-size setting scales the root (90 / 100 / 115 / 130 %). Layouts must still hold at 200 % system font scale. Use `font-variant-numeric: tabular-nums` for every number that changes.

**Space, shape, depth.** 4 px base: 4, 8, 12, 16, 24, 32, 48. Gutter 16 (24 at ≥ 600 px). Radius 0 everywhere — squared controls are the identity. No shadows: depth comes from the `bg` steps, 1 px hairlines and a `rgba(0,0,0,.55)` scrim behind sheets. Minimum touch target 48 × 48 dp (visual size may be smaller with padded hit area).

**Motion tokens.** `m.fast` 120 ms, `m.base` 180 ms, `m.slow` 260 ms. Enter `cubic-bezier(.2,0,0,1)`, exit `cubic-bezier(.4,0,1,1)`, progress `steps(6, end)`. Animate only `transform` and `opacity`. At most one continuously animating element per screen region. Streaming text is never animated per character.

## 4. UI and component specification

### 4.1 Screen map

Chat (home) · Recents (drawer on phones, persistent rail from 600 px) · Search · Settings (Models, Permissions, Memory, Privacy, Usage, Appearance, Data) · Undo sheet · Context sheet. Approvals are *inline* Work Orders in the transcript, never a modal over unrelated content. The one exception is the `alertdialog` fallback when the app returns from the background with an approval pending.

### 4.2 Chat screen anatomy, top to bottom

1. **Signal Strip** — 3 px.
2. **App bar** — 56 dp: recents · chat title (`t.title`, one line, tap to rename) · model tag (`t.label`) · overflow. Hairline below.
3. **Transcript** — follows the bottom only while the user is within 48 px of it. Otherwise a `↓ NEW` stamp appears. Uses `overflow-anchor` so streaming never shifts what you are reading.
4. **Queue tray** — appears above the composer only when items are queued: one-line preview, `QUEUED · n` stamp, remove ✕, tap to edit. Shows three, then `+n MORE`.
5. **Composer.**

### 4.3 Message components

- **User block.** `bg.2`, right-aligned, max 88 % width, no tail, no rounding. Attachment tiles sit above the text. A stamp row below shows `SENT`, `QUEUED` or `FAILED` (tap to retry). Long-press or overflow offers *Edit*, which creates a branch.
- **Assistant turn.** Ledger Rail plus prose in `t.body`. Beneath it: a meta row (time · model · `STOPPED` stamp when cancelled) and an action row — Copy, Regenerate, Read aloud, Branch `‹ 2/3 ›`, Undo. Actions always show on the latest turn and on tap for earlier ones, so the page stays quiet.
- **Receipt (tool line)** — states are exhaustive and each is a real event:

| State | Source event | Rendering |
| --- | --- | --- |
| Working | `tool.started` | Stepped square + `WORKING · battery.read`. **Not a chip** |
| Committed | `tool.committed` | Tick on rail + receipt line + `VERIFIED` stamp where a verify step exists |
| Failed | `tool.failed` | Danger glyph + `FAILED` + mapped plain-language reason |
| Unverified | `tool.verify_failed` | Warn stamp `UNVERIFIED` — ran, but the post-check did not confirm |
| Declined | `approval.resolved(deny)` | `DECLINED` stamp |
| Unknown outcome | `turn.recovered(outcome_unknown)` | Warn stamp `UNKNOWN OUTCOME` + where to check |

- **“Used” chip row** per turn collapses receipts into `USED · battery.read · files.list ×2`. Built only from `tool.committed`; expands on tap. Cancelled, denied and failed calls never contribute.
- **Code block.** Header: language (`t.label`) left, `COPY` right (48 dp). `t.mono`, horizontal scroll with an edge fade, wrap toggle, 40 vh cap with `EXPAND`. Lightweight regex highlighter for about eight languages; plain fallback. No heavy library.
- **Tables.** Own `overflow-x` container, sticky first column, 40 % `bg.2` banding.
- **Images.** Aspect ratio reserved from metadata so nothing jumps. Tap opens a full-screen pinch-zoom viewer.
- **Streaming Markdown.** Incremental parser re-renders only the open block. An unterminated code fence renders as a code block with a `STREAMING` label. Raw markup never flashes.

### 4.4 Composer

Squared auto-growing textarea (1–6 lines, then scroll) on `bg.1` with a 1 px `line.2` border. Focus is a 2 px accent outline. **Enter inserts a newline**; the button sends. Hardware keyboards: Ctrl+Enter sends. Drafts autosave per chat (400 ms debounce) and restore. Attach (+) opens Camera / Photo / File. A 2 px **context meter** sits on the composer’s top edge from 60 % usage; tapping it opens the Context sheet.

The primary 48 dp button is a state machine with visible labels, not icon-only:

| Turn state | Input empty | Input has text |
| --- | --- | --- |
| Idle | Mic (P1; hidden until built) | `SEND` |
| Running | `STOP` | Segmented `QUEUE` \| `STEER`; **Queue is the default** |
| Offline | Mic | `QUEUE` only (Steer hidden: a turn cannot progress offline) |
| Awaiting approval | `STOP` | `QUEUE` only; helper text “Sign or decline the work order first” |

### 4.5 Recents and search

- Rows are 64 dp: title (`t.body` 600, one line), snippet (`t.body-s`, `text.2`), relative time right. Active row gets `accent.wash` plus a 2 px accent left rule. Group labels: `PINNED`, `TODAY`, `YESTERDAY`, `THIS WEEK`, `EARLIER`.
- Swipe-left deletes, with a 6-second squared Undo bar at the bottom. Long-press starts multi-select: pin, delete, export.
- **Search:** field at the top of the drawer, 150 ms debounce, matches highlighted with a background (not colour alone). Tapping a result jumps to the message and outlines it for 1.2 s (static under reduced motion).
- Empty: `t.head` “NO LOGS YET”, one line of text, one `NEW CHAT` button.

### 4.6 Approvals — the Work Order

Source

auto

```
┌ WORK ORDER · SEND SMS ───────────── 14:07 ┐   corner ticks drawn in CSS
 TO       +63 917 123 4567 (Maria)       name only if the device resolved it
 MESSAGE  “Running late, 10 min.”              exact body, never paraphrased
 EFFECT   Sends a real SMS from this phone.
 REVERT   CANNOT BE UNDONE
 REF 7F3A21                      expires 14:17  first 6 hex of the args hash
 [ DECLINE ]                  [ HOLD TO SIGN ]
```

- The card renders the **resolved real arguments** from the approval record, never model prose. Content always wraps fully; it is never truncated at any font scale.
- **Three tiers** [DECISION]: *Read* → auto. *Reversible write* (reminder, clipboard, file create) → asks the first time per chat (`ALLOW IN THIS CHAT` / `DECLINE`). *Sensitive* (SMS, calls, uninstall, settings changes, delete, overwrite) → asks **every time**, cannot be pre-allowed, shows its reversibility line, and uses hold-to-sign.
- After resolution the card collapses into a receipt line with `SIGNED 14:07` or `DECLINED`.
- If the app returns from the background with an approval pending, a top banner and the focused card appear. In P1 a notification does the same.

### 4.7 Queue, connectivity and offline

| Signal Strip state | Look | Label |
| --- | --- | --- |
| Live | Solid accent | none |
| Connecting | Accent in stepped segments | `CONNECTING` |
| Offline | Dashed `text.3` | `OFFLINE · LOCAL TOOLS ONLY` for 4 s, then only `Q·n` |
| Degraded (model unreachable) | Dashed `warn` | `MODEL UNREACHABLE` |

A 1.5 s hysteresis prevents flicker. Queued messages show `QUEUED · SENDS WHEN ONLINE`. On reconnect, stamps flip `SENDING` → `SENT` in queue order, which is itself the visible proof of ordering.

### 4.8 Settings

Ruled 56 dp rows with `t.label` section headers:

- **Models** — default and per-chat override; capability tags `TOOLS`, `VISION` read from config.
- **Permissions** — every tool with its risk tag and mode (Ask / Allow in chat / Always ask). Sensitive rows show a lock and cannot be loosened. “Reset all”.
- **Memory** — kept items (edit, delete), a toggle for suggestions, “Clear memory”.
- **Privacy** — plain statement of what is stored on-device and what goes to NVIDIA; clear chats; export all; redact logs (on); app lock (P1).
- **Usage** — today’s requests, tokens in/out, last rate-limit event.
- **Appearance** — theme (System / Dark / Light), text size, reduce motion (System / On), haptics, “Tap to confirm instead of hold”.
- **Data** — storage used, run-report export (redacted).

### 4.9 Empty, error and loading states

| State | Rendering | Copy |
| --- | --- | --- |
| New chat | Wordmark square, `t.head`, three *real* playbook rows | “READY.” · “Phone health check” · “What is using my storage?” · “Summarise a file” |
| Chat list loading | Five static 64 dp skeleton rows (one 1.2 s sweep, twice, only if motion allowed) | — |
| Model latency > 600 ms | Rail steps | `WAITING FOR MODEL` |
| 429 | Stamp with live countdown, auto-retry, Cancel | `RATE LIMITED · RETRY 0:12` |
| Model failed after retries | Error row | “The model didn’t answer. Your message is saved.” `RETRY` `SWITCH MODEL` |
| Android permission missing | Inline card | `NEEDS PERMISSION · CONTACTS` `OPEN SETTINGS` |
| Tool failed | `FAILED` receipt | “Couldn’t read battery: the system returned no value.” |
| Verify failed | `UNVERIFIED` receipt | “The action ran but the check didn’t confirm it. Check the Clock app.” |
| Recovered after kill | System line | `SITECREW RESTARTED · RESUMED 14:07` + the honest outcome |

**Copy rules:** terse and declarative. Sentence case for messages, caps only for stamps. No exclamation marks, no “Oops”, no emoji. UI chrome is impersonal; the assistant may say “I”. Never write copy that claims an action happened before its committed event.

### 4.10 Motion and feedback

| Event | Motion | Reduced motion |
| --- | --- | --- |
| Message enters | translateY 8→0 + fade, `m.base` | Instant |
| Tick added to rail | Scale .6→1, `m.fast`, light haptic if enabled | Instant |
| Rail running | 4-step marching segments, 600 ms loop, only while running and visible | Static solid accent |
| Streaming text | None | None |
| Sheet open/close | Slide `m.slow` / `m.base` | 120 ms fade |
| Hold to sign | Segmented fill over 600 ms, haptic on completion | Tap SIGN, then tap CONFIRM within 5 s |
| Undo applied | Strike-through draws in 180 ms | Instant |

Reduced motion follows the OS setting *and* an in-app override. Haptics use native vibration through pyjnius (WebView `navigator.vibrate` is unreliable) and are optional.

### 4.11 Accessibility — each line is a test

- Transcript is `role=log`, `aria-busy=true` while streaming, and announces once at completion (“Reply complete”) instead of per token.
- On `approval.requested`, focus moves to the Work Order heading, which is `role=alertdialog` for sensitive actions and reads “Work order: send SMS to Maria. Cannot be undone.” Focus returns to the composer afterwards.
- Hold-to-sign always has a visible *Confirm* alternative, and the alternative **becomes the default** when TalkBack or Switch Access is on (detect with AccessibilityManager through pyjnius) or the setting is enabled.
- Contrast ≥ 4.5:1 for text and ≥ 3:1 for UI, verified by script in both themes. No state is carried by colour alone: every state has a glyph and a word.
- Targets ≥ 48 dp with ≥ 8 dp spacing. Focus ring 2 px accent, offset 2. No keyboard traps; Esc closes sheets.
- Layout holds at 200 % system font scale with no clipped controls.
- Approvals expire after 10 minutes (adjustable to 60), and expiry is announced. Nothing ever auto-approves.

### 4.12 Responsive behaviour and performance budget

| Width | Layout |
| --- | --- |
| ≤ 359 px | Gutter 12, model tag hidden |
| 360–599 px | Phone layout above |
| 600–839 px | Persistent recents rail 280, transcript centred ≤ 640 |
| ≥ 840 px | Rail 320 + transcript 720 + optional Details inspector pane |

Landscape phone: app bar 48, composer ≤ 3 lines. Keyboard handling uses `visualViewport`; the transcript’s bottom padding equals the composer height. Use `100dvh`, never `100vh`.

**Premium feel on low-spec hardware is a measured budget:** warm start to interactive ≤ 1.5 s on the target device; 60 fps scroll through a 500-message chat by windowing (last ~60 rendered, rest virtualised); streaming DOM updates batched to ≤ 1 per 50 ms inside `requestAnimationFrame`; JS ≤ 200 KB gzipped; no `backdrop-filter`, no box-shadows, no large raster assets.

## 5. Feature strategy

**Rule:** rank by *user value × (1 − implementation risk)*, with reliability foundations first. **P0** ships in this build. **P1** comes next, and the P0 schema already has the hooks. **P2** waits until P0 and P1 gates are green.

### 5.1 Matrix

| ID | Feature | Pri | Why it matters | Risk and mitigation |
| --- | --- | --- | --- | --- |
| F01 | Durable turn engine: turn/job records, per-chat serialisation, structured events | P0 | Everything else depends on it | Medium — SQLite WAL + strict transitions + recovery tests |
| F02 | Streaming with real Stop | P0 | Basic trust; saves tokens and time | Low — close the HTTP stream, keep partial text |
| F03 | Queue and Steer with defined semantics; idempotent client sends | P0 | Already shipped, so it must be *correct* | Medium — `client_msg_id` + single-flight lock |
| F04 | Tool contract: schema validation, PreToolUse gate, execution log, postcondition verify, bounded retries | P0 | Stops hallucinated arguments and false “done” | Medium — per-tool `verify` required |
| F05 | Durable approvals + Work Order + hold/confirm | P0 | Safety on SMS, calls, uninstall, settings | Low-medium — args-hash binding |
| F06 | Used chips, Ledger Rail and Receipts from committed events only | P0 | Core honesty promise | Low |
| F07 | Kill/crash recovery, SSE replay, foreground service | P0 | A phone app *will* be killed | High — test on real devices, not emulators only |
| F08 | Inverse-operation Undo with typed reversibility | P0 | Turns “undo” from cosmetic to real | Medium — only where physically possible |
| F09 | Edit last message and Regenerate (tree stored, linear UI) | P0 | Table stakes in 2026 | Low |
| F10 | Context builder, rolling summary, context meter | P0 | Dependable per-chat context | Medium — summaries are additive; raw messages are never deleted |
| F11 | Rich Markdown and code UX | P0 | Daily-use quality | Low |
| F12 | Rename, pin, delete-with-undo, basic search | P0 | Recents stop scaling at ~30 chats | Low (`LIKE`; FTS5 in P1) |
| F13 | Security baseline: localhost token, key in Android Keystore, redacted logs, per-tool permissions screen | P0 | Without it any app can drive the agent | Low-medium |
| F14 | Design system, themes, a11y, reduced motion | P0 | Goal 1 | Low |
| F15 | Offline hardening: local-router parity events, true connectivity, ordered flush | P0 | Existing promise | Medium |
| F20 | Branch navigation UI (`‹ 2/3 ›`) | P1 | Exploit the tree already stored | Low |
| F21 | User-controlled memory: model *proposes*, user taps Keep, viewer/editor | P1 | Useful without creeping | Low — nothing auto-commits |
| F22 | Image attach (camera/gallery) for models flagged `supports_vision` | P1 | Common mobile task | Medium — model support varies; hide when unsupported |
| F23 | Voice input (`SpeechRecognizer`) and read-aloud (`TextToSpeech`) | P1 | Mobile-native; works hands-busy | Medium — pyjnius threading and permissions |
| F24 | Share-out (Android share sheet) and **share-in** (“Process text” / `ACTION_SEND`) | P1 | Puts SiteCrew inside every other app | Medium — manifest and shell change |
| F25 | Playbooks: saved prompts with variables and a tool scope, `/` picker | P1 | Repeatable field routines | Low |
| F26 | Reminders and alarms (real `AlarmManager` + notification), verified by read-back, Undo = cancel | P1 | Reversible, real, high Android value | Medium — exact-alarm permission rules; use inexact where acceptable |
| F27 | Notifications for turn complete / approval needed while backgrounded | P1 | Makes the foreground service worth it | Low-medium |
| F28 | Usage visibility and model fallback | P1 | Free-tier rate limits are the real constraint | Low |
| F29 | App lock (biometric) | P1 | Privacy | Low |
| F30 | FTS5 search and in-chat search | P1 | Speed at scale | Low — fall back to `LIKE` if FTS5 is missing |
| F40 | Folders and tags | P2 | Only needed at large chat counts | Low |
| F41 | Home-screen widget, Quick Settings tile, app shortcut | P2 | Faster entry | Medium |
| F42 | Web-search tool with citations | P2 | Needs a provider and a citation UX | Medium |
| F43 | Scheduled or recurring playbooks | P2 | Needs reliable background execution | High |
| F44 | Automatic local backup to a user-chosen folder | P2 | Data-loss insurance (manual export exists) | Medium |

### 5.2 The few features that make SiteCrew *notably* better than a generic chatbot

1. **Do it, prove it, undo it.** Device actions carry a receipt, a verification and a declared return path (F04, F06, F08). No mainstream chatbot can offer this on a phone.
2. **Honest offline.** Device questions are answered deterministically with `LOCAL` provenance, even in airplane mode (F15).
3. **Reminders that actually fire**, verified by reading the alarm back, and cancellable with one tap (F26).
4. **SiteCrew inside every app** through the share sheet and “Process text” (F24).
5. **Field playbooks** — Phone health check, Storage cleanup (moves to trash, with Undo), Battery-drain investigation, Pre-shift check — all built from real tools (F25).
6. **It survives being killed.** Foreground service, resume and replay mean a long turn finishes or says exactly why it didn’t (F07).

### 5.3 Explicit rejections and deferrals [DECISION]

- **Rejected:** subagents and multi-agent swarms; MCP or plugin marketplace; accounts and cloud sync; image generation; personas and characters; social or feed sharing; UI automation through `AccessibilityService` (policy and safety risk); autonomous background actions with no user present; auto-sending SMS or placing calls; automatic “learn everything” memory; analytics or telemetry; carrier free-data tunnelling; full-duplex realtime voice; a code-execution sandbox; a browsing agent.
- **Deferred:** everything in P2, and anything needing a new backend service.

## 6. Agent architecture (per chat)

### 6.1 Six invariants

1. **One writer per chat.** A `ChatActor` serialises every mutation for its chat.
2. **Intent before effect.** A write-ahead record is on disk *before* any device action runs.
3. **Events are the only source of UI truth.** The UI renders what the event log says, never what it expects.
4. **Every effect is idempotent, reconcilable, or honestly flagged `outcome_unknown`.**
5. **Nothing is claimed before its commit.** No chip, tick or “done” text is produced ahead of `tool.committed`.
6. **Cancel is cooperative and always ends in a recorded terminal state.**

### 6.2 Components and data flow [ASSUME names, DECISION structure]

Source

auto

```
WebView UI ── HTTP + SSE (127.0.0.1, per-launch token) ──► Flask routes (thin)
                                                       │
                                             ChatActor registry (1 per chat, single-flight)
                                                       │
         ┌──────────────────────────────┼───────────────────────────┐
   TurnRunner                         ApprovalBroker                 UndoManager
 (model → gate → exec loop)         (durable approvals)            (inverse ops)
    │        │         │
 NvidiaClient PermissionEngine ToolExecutor (watchdog + pyjnius bridge)
    │            │              │
    └────────────┴─────────────────► EventStore (SQLite WAL, per-chat seq) ◄── LocalRouter (offline, read-only)
```

**Threading [ASSUME]:** Flask runs threaded behind a pure-Python server (`waitress`) — never the debug reloader. Each `ChatActor` owns one lazily created worker thread. A global semaphore allows 2 concurrent model streams. `ToolExecutor` has 2 threads; every thread that touches pyjnius calls `jnius.detach()` in a `finally`, and UI-thread work goes through the existing `run_on_ui_thread` pattern. SSE handlers wait on a per-chat `threading.Condition` instead of polling.

### 6.3 SQLite schema (target; migrate in place after a backup copy, versioned by `PRAGMA user_version`)

Source

SQL

```
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;
CREATE TABLE chats(id TEXT PRIMARY KEY, title TEXT, pinned INT DEFAULT 0, model TEXT,
  mem_enabled INT DEFAULT 1, head_message_id TEXT, created_at INT, updated_at INT, deleted_at INT);
CREATE TABLE messages(id TEXT PRIMARY KEY, chat_id TEXT, parent_id TEXT, role TEXT,
  kind TEXT,            -- user|steer|assistant|system|local
  content_json TEXT, status TEXT,   -- final|interrupted|tombstoned
  client_msg_id TEXT, turn_id TEXT, created_at INT, UNIQUE(chat_id, client_msg_id));
CREATE TABLE turns(id TEXT PRIMARY KEY, chat_id TEXT, user_message_id TEXT, state TEXT,
  step INT DEFAULT 0, attempt INT DEFAULT 0, source TEXT DEFAULT 'model',   -- model|local
  terminal_reason TEXT, error_code TEXT, started_at INT, ended_at INT);
CREATE TABLE events(chat_id TEXT, seq INTEGER, id TEXT, turn_id TEXT, type TEXT,
  data_json TEXT, ts INT, PRIMARY KEY(chat_id, seq));
CREATE TABLE tool_calls(id TEXT PRIMARY KEY, turn_id TEXT, tool_use_id TEXT, name TEXT,
  args_json TEXT, args_hash TEXT, risk TEXT,
  status TEXT,  -- proposed|invalid|denied|awaiting_approval|intent|executed|verified|committed|failed|unverified|outcome_unknown|cancelled
  result_json TEXT, verify_json TEXT, error_code TEXT, attempt INT DEFAULT 0,
  started_at INT, ended_at INT, committed_seq INT);
CREATE TABLE approvals(id TEXT PRIMARY KEY, tool_call_id TEXT, args_hash TEXT,
  status TEXT,  -- pending|approved|denied|expired
  expires_at INT, decided_at INT);
CREATE TABLE queue(id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT,
  state TEXT,   -- pending|held|sending|done|removed
  position INT);
CREATE TABLE undo_ops(id TEXT PRIMARY KEY, tool_call_id TEXT, plan_json TEXT,
  status TEXT,  -- registered|applied|failed|expired|not_reversible
  applied_at INT);
CREATE TABLE summaries(chat_id TEXT, version INT, covers_to_message_id TEXT, text TEXT,
  created_at INT, PRIMARY KEY(chat_id, version));
CREATE TABLE memory_items(id TEXT PRIMARY KEY, text TEXT, source_chat_id TEXT,
  created_at INT, deleted_at INT);
CREATE TABLE usage(day TEXT PRIMARY KEY, requests INT, tokens_in INT, tokens_out INT);
```

`seq` is allocated inside the same `BEGIN IMMEDIATE` transaction as the state change it describes. That one rule makes “event exists ⇔ state committed” true by construction.

### 6.4 Turn state machine

| # | From | Trigger | To | Guard and side-effects |
| --- | --- | --- | --- | --- |
| 1 | — | Send accepted (idempotent insert) | `QUEUED` → `PREPARING` | `turn.queued`; if actor is idle, `turn.started` immediately |
| 2 | `QUEUED` | Previous turn `COMPLETED` and actor free | `PREPARING` | FIFO pop; **only after COMPLETED** (§6.6 Q3) |
| 3 | `PREPARING` | Context built (§6.10) and `UserPromptSubmit` hook allows | `MODEL_STREAMING` | `model.request`. A hook with `continue_=false` ends as `COMPLETED(blocked_by_hook)` |
| 4 | `MODEL_STREAMING` | Text delta | same | `message.delta` (DB snapshot coalesced ≈ 150 ms) |
| 5 | `MODEL_STREAMING` | Stream ends, no tool calls | `FINALIZING` | Persist final assistant message |
| 6 | `MODEL_STREAMING` | Stream ends with `tool_calls` | `TOOL_PROPOSED` | Persist assistant message with `tool_use` blocks |
| 7 | `TOOL_PROPOSED` | Schema valid | `GATING` | `tool.proposed`, `tool.validated`. Invalid → error `tool_result` to the model, ≤ 2 repairs, then `FAILED(tool_args_invalid)` |
| 8 | `GATING` | Engine returns **deny** | `MODEL_STREAMING` | `gate.decision(deny)`; error `tool_result` to the model; no chip |
| 9 | `GATING` | Engine returns **ask** | `AWAITING_APPROVAL` | `approvals` row + `approval.requested`; **no thread blocks** |
| 10 | `GATING` | Engine returns **allow** | `EXECUTING` | Write `tool_calls.status=intent` to disk *first*; `tool.started` |
| 11 | `AWAITING_APPROVAL` | Approve, args hash matches, not expired | `EXECUTING` | `approval.resolved(approve)` |
| 12 | `AWAITING_APPROVAL` | Deny or expiry | `MODEL_STREAMING` | approval.resolved(deny or expired); model told plainly |
| 13 | `EXECUTING` | Tool returns | `VERIFYING` | Timeout or exception → retry rule (§6.7 E6) or `tool.failed` |
| 14 | `VERIFYING` | `verify` ok | commit → next tool, else `MODEL_STREAMING` | **One transaction:** `tool.committed` + `undo.registered`. Only now may a chip or tick render |
| 15 | `VERIFYING` | `verify` not ok after one re-check | `MODEL_STREAMING` | `tool.verify_failed`; result goes to the model flagged unverified; no chip |
| 16 | `MODEL_STREAMING` | Tool results returned, `step += 1` | same | `step > max_steps` → `FINALIZING(max_steps)` |
| 17 | `FINALIZING` | Final message saved, `Stop` hook run | `COMPLETED` | May enqueue a background summary job (§6.10) |
| 18 | any active | `cancel` | `CANCELLING` → `CANCELLED` | §6.6 Q7; reason `aborted_streaming` or `aborted_tools` |
| 19 | any active | Retryable model error | same state, `attempt += 1` | Backoff 1 / 3 / 9 s ± jitter, honour `Retry-After` (cap 60 s). Exhausted → `FAILED(model_unavailable)` |
| 20 | `PREPARING` / `MODEL_STREAMING` | Connectivity lost | `PAUSED_OFFLINE` | Partial text marked interrupted; resumes at `MODEL_STREAMING` on reconnect |
| 21 | any active | Process death | `INTERRUPTED` at next boot | Recovery algorithm §6.8 |

**Terminal reasons:** `completed`, `max_steps`, `token_budget`, `aborted_streaming`, `aborted_tools`, `blocked_by_hook`, `tool_args_invalid`, `model_unavailable`. `interrupted_by_death` is never terminal — it always resolves through recovery.

### 6.5 Event schema

Envelope (every event):

Source

JSON

```
{"chat_id":"c_81f2","seq":42,"id":"e_7c1a","turn_id":"t_19aa","type":"tool.committed",
 "ts":1795567627123,"v":1,
 "data":{"tool_use_id":"tu_3","name":"battery.read","args":{},
         "result":{"percent":62,"charging":false},"provenance":"LIVE",
         "verified":"n/a","duration_ms":41,"undo":null}}
```

| Family | Types | UI consumer |
| --- | --- | --- |
| turn | `turn.queued`, `turn.started`, `turn.steered`, `turn.cancel_requested`, `turn.cancelled`, `turn.completed`, `turn.failed`, `turn.recovered` | Rail state, composer button state, system lines |
| message | `message.delta`, `message.completed` | Streaming text; final text |
| model | `model.request`, `model.retry`, `model.error` | `WAITING FOR MODEL`, rate-limit stamp |
| tool | `tool.proposed`, `tool.validated`, `tool.invalid`, `tool.started`, `tool.output`, `tool.verified`, `tool.verify_failed`, `tool.committed`, `tool.failed` | Working line, receipts, **Used chips (`tool.committed` only)** |
| gate | `gate.decision` | Details drawer |
| approval | `approval.requested`, `approval.resolved` | Work Order |
| undo | `undo.registered`, `undo.applied`, `undo.failed` | Undo sheet, `REVERTED` stamp |
| memory | `memory.proposed`, `memory.committed`, `memory.deleted` | Memory cards |
| system | `connectivity.changed`, `queue.changed`, `context.summarised` | Signal Strip, queue tray, context meter |

**Replay rule:** reconnecting clients send `Last-Event-ID` (the last `seq`) and receive every later event in order. Rendering the same event log twice must produce an identical transcript — this is the idempotency test for the front end.

### 6.6 Queue versus Steer — the exact rules [SiteCrew extension]

The SDK allows streaming input but defines no steer. These rules are the spec:

- **Q1 Idempotent sends.** The client generates `client_msg_id` (UUIDv4) when the user taps send and persists it locally *before* any network call. The server inserts with `UNIQUE(chat_id, client_msg_id)` and returns a `disposition`: `started`, `queued`, `steered`, or `duplicate`. A duplicate never creates a message or a turn.
- **Q2 Mode.** Idle: the send starts a turn. Running: `mode` is required, `queue` or `steer`, and the UI defaults to `queue`.
- **Q3 Queue.** Durable per-chat FIFO ordered by server accept time, ties broken by client sequence. Each item becomes its own turn. The queue **auto-continues only after `COMPLETED`**. After a user Stop or a `FAILED` turn it **pauses** (`held`) with a tray offering `RESUME QUEUE` and `CLEAR`, because a user who pressed Stop usually has changed their mind. `PAUSED_OFFLINE` resumes automatically.
- **Q4 Editable until sent.** Queued items can be edited or removed until their state is `sending`.
- **Q5 Steer.** Valid while the turn is in `MODEL_STREAMING`, `TOOL_PROPOSED`, `GATING`, `EXECUTING` or `VERIFYING`. Procedure: persist the steer message (`kind=steer`) and emit `turn.steered`, then act at the next *safe boundary*:
- **Q6 Edge cases.** Steer during `AWAITING_APPROVAL` is refused (409; the composer explains why and offers Queue). Steer while offline converts to Queue with a visible notice. Steer during `FINALIZING` converts to Queue.
- **Q7 Stop.** Cancels the *current* turn only. Streaming: close the HTTP stream, keep the partial text flagged `interrupted`, reason `aborted_streaming`. Tool phase: a cancellable read tool is signalled; a write tool is allowed to finish and verify, so the state is never unknown; reason `aborted_tools`. Both always emit `turn.cancelled`. This mirrors `interrupt()`: buffered output is kept and a terminal record still arrives.
- **Q8 Ordering invariants (testable).** For N queued messages: visible order = turn start order = event order; zero duplicates; exactly one assistant reply per user message.
- **Q9 Local router.** Offline, read-only device questions are answered at once as `source=local` turns even when AI messages are queued ahead. The transcript follows send order, and when the AI queue later flushes, its context includes every message in send order. Anything that is not a read-only device fact is queued.

### 6.7 Tool contract, gate and executor

Source

Python

```
@tool(name='reminder.create', description='Create a reminder notification.',
      schema={'type': 'object', 'additionalProperties': False,
              'required': ['title', 'at_epoch_ms'],
              'properties': {'title': {'type': 'string', 'maxLength': 120},
                             'at_epoch_ms': {'type': 'integer', 'minimum': 0}}},
      risk='write',            # read | write | sensitive
      idempotent=False, reversible='yes',   # yes | partial | no
      timeout_s=8, offline_ok=True, cancellable=False)
def reminder_create(args, ctx): ...             # performs the effect, returns result dict

@reminder_create.verify
def _(args, result, ctx): ...                    # read the state back; return VerifyResult(ok, evidence)

@reminder_create.inverse
def _(args, result, ctx): ...                    # return UndoPlan(kind, params) or None

@reminder_create.reconcile
def _(args, ctx): ...                            # after a crash: did the effect happen? yes | no | unknown
```

**Gate order** — a faithful port of the SDK sequence, with SiteCrew’s hard rules inside it:

Source

Python

```
def evaluate(call, opts, hooks, rules, mode, chat_grants):
    h = hooks.pre_tool_use(call)                        # 1 hooks: 250 ms budget; fail closed for write/sensitive
    if h.decision == 'deny': return Deny(h.reason)
    call = call.with_input(h.updated_input or call.input)
    if rules.deny.matches(call): return Deny('blocked by rule')       # 2 deny rules
    if rules.ask.matches(call) or h.decision == 'ask' or call.tool.risk == 'sensitive':
        return Ask(call)                                              # 3 ask rules; sensitive is pinned here
    if mode == 'readonly' and call.tool.risk != 'read':
        return Deny('read-only mode')                                 # 4 permission mode
    if call.tool.risk == 'read' or rules.allow.matches(call) or chat_grants.has(call.tool.name):
        return Allow()                                                # 5 allow rules
    return Ask(call)                                                  # 6 fall-through → ApprovalBroker
```

A hook that returns `allow` does **not** skip steps 2–3 (documented SDK behaviour). A unit test must cover every cell of this table.

**Executor rules**

- **E1 Validate.** JSON Schema with `additionalProperties:false`. Unknown tool names are rejected. Only safe coercions (`"5"` → `5` when the schema says integer).
- **E2 Idempotency key** is `tool_use_id`.
- **E3 Watchdog.** Every tool runs in the executor pool under `timeout_s` (default 10, max 60). A timed-out *read* is `failed(timeout)`. A timed-out *write* becomes `outcome_unknown` unless `reconcile` resolves it.
- **E4 Intent first.** The `intent` row is committed to disk before the effect starts.
- **E5 Verify.** Required for `risk ≥ write`. One re-check after 500 ms for eventual consistency. Evidence is stored.
- **E6 Bounded retries.** Model call 3 attempts. Schema repair ≤ 2. A tool retries once, only if `idempotent` and the error is transient. The same `(name, args_hash)` more than twice in one turn is denied as a loop. `max_steps` defaults to 8 (playbooks may raise it to 15).
- **E7 Results to the model** are capped at 4 KB (full result stays local) and stripped of secrets.
- **E8 Everything logs.** Each phase updates `tool_calls` and emits an event. The Details drawer reads these rows; nothing in it is synthesised.

**Default tool classification** [ASSUME the current tool names; Phase 0 maps the real ones]

| Tool family | Risk | Reversible | Verify | Inverse | Crash reconcile |
| --- | --- | --- | --- | --- | --- |
| Battery, device, apps list, settings read, files list/read | read | n/a | none | none | re-run |
| Clipboard set | write | partial | read back | restore previous (foreground only) | read clipboard |
| File create / write | write | yes | exists and hash | delete | check path |
| File overwrite | sensitive | yes | hash | restore shadow copy | compare hash |
| File delete | sensitive | yes (7 days) | not at path, present in trash | restore from app trash | check both |
| Settings write | sensitive | yes | read back | restore captured prior value | read back |
| Reminder create (P1) | write | yes | alarm read-back | cancel alarm | query alarm |
| SMS send / call place | sensitive | **no** | none possible | none | **unknown** → never auto-resend |
| App uninstall | sensitive | **no** | package absent | none | package check |

### 6.8 Checkpoint and resume after process death

On every boot, `recover()` runs before the server accepts requests:

1. Open the DB and run `PRAGMA quick_check` (log only).
2. Find every turn whose state is not terminal. Mark it `INTERRUPTED` and emit `turn.recovered(cause=process_death)`.
3. For each interrupted turn, classify its `tool_calls` by status:
4. Mark any partial assistant message `interrupted`.
5. **Continuation.** If the turn had no ambiguous tool and is ≤ 15 minutes old and the network is up, resume at `MODEL_STREAMING` with a fresh model call whose context includes the interrupted partial and the committed tool results. Otherwise end it as `CANCELLED(aborted_streaming)` with a system line and a `CONTINUE` button that starts a new turn.
6. Queue: `sending` items are reconciled by `client_msg_id` (message exists → `done`; missing → `pending`). The queue stays held until the interrupted turn resolves, then follows Q3.
7. Emit a `turn.recovered` event listing the per-tool outcome. The UI shows one system line.
8. **`recover()` is idempotent:** running it twice produces identical state. This is a required test.

### 6.9 Undo — real inverse operations

**Rule:** every tool declares `reversible: yes | partial | no` *before* it runs, and the Work Order and Details drawer show it. Undo never pretends.

**Procedure.** Tapping Undo on a turn opens the Undo sheet with one line per effect, built from `undo_ops`:

| Mark | Meaning | Example |
| --- | --- | --- |
| ✓ | Will be reverted | Reminder cancelled; file restored from trash |
| ◐ | Partially reversible | Clipboard restored only while the app is in the foreground |
| ✗ | Cannot be undone | SMS sent; app uninstalled |
| ⏱ | Expired | Trash retention (7 days) elapsed |
| ≠ | Changed since | The setting was changed again after SiteCrew set it, so reverting would overwrite a newer value — ask first |

Two toggles, both on by default: **Revert device effects (N of M possible)** and **Remove turn from chat**. Turning the first off leaves exactly today’s behaviour — transcript-only Undo is preserved.

**Execution.** The `UndoManager` runs inverse ops in *reverse commit order*. Each inverse is itself a tool-like operation with intent, verify and commit events (`undo.applied` or `undo.failed`) and is idempotent. Because settings changes need confirmation, reverting settings shows one Work Order titled `REVERT SETTINGS`. Removing from chat **tombstones** messages (`status=tombstoned`, never deletes), moves `chats.head_message_id` to the parent, deletes memory items committed in that turn, and invalidates summaries that covered it. Redo is out of scope for P0.

**Retention:** file trash is app-private with 7-day retention. Settings snapshots are stored with the value SiteCrew wrote, which is how the `≠` check works.

### 6.10 Context and memory

**Three layers.** L1 chat context — automatic, per chat (P0). L2 pinned notes per chat (P1). L3 global memory — user-controlled (P1).

**Context builder** (deterministic, unit-tested):

Source

auto

```
budget = context_window - max_output - 10% safety - tool_schema_tokens
assemble in order:
  1. system prompt   (identity, honesty rules, tool rules, local time and locale)
  2. memory block    (P1; <= 500 tokens, newest first)
  3. latest summary  (if any)
  4. recent messages walked back along parent_id from head_message_id
     until 70% of budget is used
  5. attachment extracts (each <= 2k tokens, marked truncated when cut)
token estimate = chars / 3.6 with a 15% margin, unless the model tokenizer is available
```

Walking `parent_id` from the head means **branches never leak into each other**. Nothing is trimmed silently: the meter appears at 60 %, and the Context sheet lists “n earlier messages summarised”.

**Rolling summary.** When the estimate passes 70 % at `COMPLETED`, a background `run_once` job (no tools) summarises everything older than the last 8 turns, merged with the previous summary. It commits `summaries(version+1, covers_to_message_id)` atomically with a `context.summarised` event. If the job fails, nothing changes. Raw messages are never deleted. The prompt must preserve goals, decisions, constraints, open tasks and **tool outcomes as timestamped, provenance-tagged facts** (“battery 62 % at 14:07, LIVE”). The system prompt tells the model that device facts from earlier turns are stale and must be re-read with tools. That is how “no invented device facts” survives long chats.

**Memory (P1).** A `memory.propose(text)` tool emits `memory.proposed`, shown as a card with `KEEP`, `EDIT`, `DISMISS`. Only **Keep** writes `memory_items` and emits `memory.committed`, so the model can never silently remember anything. Proposals matching secret patterns (API keys, OTPs, card numbers, passwords) are rejected at the gate. Memory never stores device facts, since they are volatile. A per-chat toggle (`mem_enabled`) disables it. Inverse = delete.

### 6.11 Offline and connectivity

- **Truth sources:** `ConnectivityManager.NetworkCallback` with the *validated* capability, passive evidence from real request outcomes, and an active probe (lightweight request to the model host, at most once per 30 s, only while idle with a non-empty queue). 1.5 s hysteresis.
- **When OFFLINE:** model turns go to `PAUSED_OFFLINE`; new sends are queued; the `LocalRouter` answers read-only device questions.
- **LocalRouter:** a deterministic intent matcher (keyword and slot rules) that calls the *same* read tools through the *same* gate and executor and emits the *same* events with `provenance=LOCAL`. It never calls the model. An unrecognised intent is queued. If Android returns nothing, it says “unavailable” — it never fills the gap.
- **Flush on reconnect:** one at a time under Q3, with 0–500 ms jitter, idempotent through `client_msg_id`.

### 6.12 Android runtime requirements [ASSUME — Phase 0 verifies against the real shell]

- **Foreground service** while a turn is running, an approval is pending, or the queue is non-empty with network. It stops after a 30 s idle grace. Choose a service type that is valid for the target SDK, and check the Android 14/15 foreground-service type and timeout rules before choosing. It is not a permanent daemon. `POST_NOTIFICATIONS` is requested at runtime on Android 13+.
- **Process model.** Document which process hosts Flask and how the service keeps it alive. If Flask lives in the UI process, killing the app kills the agent — `recover()` is the safety net, the service the prevention.
- **WebView lifecycle.** On `visibilitychange` → visible, reconnect SSE with `Last-Event-ID`. Handle `onRenderProcessGone` by recreating the WebView and replaying from the last `seq`.
- **Localhost security.** Bind `127.0.0.1` only, on a random port per launch. The native shell injects a 32-byte random token; every API and SSE request must carry it. `EventSource` cannot set headers, so use `fetch()` with a `ReadableStream` reader. Validate the `Host` header equals `127.0.0.1:<port>` (DNS-rebinding defence). Send no CORS headers.
- **Key storage.** The NVIDIA key lives in Android Keystore–backed storage through pyjnius, never in logs, events or exports. A redaction filter masks `nvapi-` patterns and bearer tokens everywhere.
- **Permissions.** Runtime permission prompts run from the native UI thread. A tool returns `needs_permission`; the UI shows the inline card. After the grant the turn is **not** auto-retried — the user taps `RETRY`.
- **Storage.** App-scoped roots plus SAF-granted trees. Trash is app-private. No `MANAGE_EXTERNAL_STORAGE`.
- **Battery.** A *Background reliability* diagnostic in Settings shows the battery-optimisation status with a deep link. Never prompt for it automatically.

### 6.13 Key flows with their proof

| Flow | Steps | Event proof |
| --- | --- | --- |
| **A. Send while idle** | Tap send → optimistic `SENT` stamp → stream → final | `turn.queued`, `turn.started`, `model.request`, `message.delta`×n, `message.completed`, `turn.completed` |
| **B. Read tool** | Model proposes `battery.read` → validate → gate allow → execute → commit → model continues | `tool.proposed`, `tool.validated`, `gate.decision(allow)`, `tool.started`, `tool.committed`, then deltas |
| **C. Sensitive action** | Proposal → gate ask → Work Order → hold-to-sign → execute → verify → commit | `gate.decision(ask)`, `approval.requested`, `approval.resolved(approve)`, `tool.started`, `tool.verified`, `tool.committed`, `undo.registered(not_reversible)` |
| **D. Steer mid-stream** | Tap STEER with text → stream aborts at next chunk boundary → steer message appended → new model call | `turn.steered`, `message.completed(interrupted)`, `model.request`, … (same `turn_id`) |
| **E. Offline queue** | Airplane mode → send → `QUEUED` → reconnect → ordered flush | `connectivity.changed(offline)`, `queue.changed`, `connectivity.changed(live)`, one `turn.started` per item in order |
| **F. Kill during write tool** | App killed after `intent` → relaunch → `recover()` | `turn.recovered` with per-tool outcome; sensitive tools never silently re-run |
| **G. Undo** | Undo → sheet → confirm → inverse ops → tombstone | `undo.applied`×n (reverse order), `queue.changed` / head moved |
| **H. Edit or regenerate** | Edit user message → new branch from its parent → new turn | New `messages` row with the same `parent_id`; `turn.started` on the new branch; old branch intact |

## 7. Server APIs, schemas and file-level change map

Everything here is a **target contract** [ASSUME]. Phase 0 maps it onto your real routes and files. Legacy routes stay alive as shims until the UI has cut over (§7.6).

### 7.1 Conventions

Base path `/api/v2`. JSON bodies. Every request carries `X-SiteCrew-Token`. Errors share one shape: `{"error":{"code":"steer_not_allowed","message":"A work order is waiting. Sign or decline it first.","retryable":false}}`. Every state-changing route is idempotent through a client-supplied id. Event envelopes carry `v` so the schema can evolve.

### 7.2 Endpoints

| Method and path | Purpose | Notes |
| --- | --- | --- |
| `POST /chats` | Create a chat | Returns `{id}` |
| `GET /chats?q=&cursor=&limit=` | List and search | Items: `id, title, snippet, pinned, updated_at`; `LIKE` in P0, FTS5 in P1 |
| `PATCH /chats/{id}` | Rename, pin, model, `mem_enabled` |  |
| `DELETE /chats/{id}` · `POST /chats/{id}/restore` | Soft delete and the Undo bar | Hard purge after 30 days or on “Clear chats” |
| `GET /chats/{id}` | **Bootstrap**: messages along the head chain, queue, pending approvals, `last_seq` | The client opens the stream *after* `last_seq` |
| `POST /chats/{id}/messages` | Send | Body below. Returns `disposition` |
| `PATCH` / `DELETE /chats/{id}/queue/{qid}` | Edit or remove a queued item | Refused once `sending` |
| `POST /chats/{id}/queue/resume` · `/queue/clear` | Release or discard a held queue |  |
| `GET /chats/{id}/events?after={seq}` | Event stream | `Last-Event-ID` honoured; heartbeat every 15 s |
| `POST /chats/{id}/cancel` | Stop | Body `{turn_id}` |
| `POST /approvals/{id}` | Sign or decline | Body `{decision, args_hash}`; `409 args_mismatch`, `410 expired` |
| `POST /chats/{id}/messages/{mid}/edit` | Edit a user message → new branch | Body `{client_msg_id, text}` |
| `POST /chats/{id}/regenerate` | Regenerate from a message | Body `{from_message_id, client_msg_id}` |
| `GET /chats/{id}/branches?at={mid}` | Siblings at a point | P1 UI; P0 stores the tree |
| `GET /chats/{id}/undo/preview?turn_id=` · `POST /chats/{id}/undo` | Undo sheet data; execute | Body `{turn_id, revert_effects, remove_turn}` |
| `GET /tool_calls/{id}` | Details drawer | Redacted real args, result, timings, verify evidence |
| `GET /chats/{id}/context` | Context meter and sheet | Used / budget, summary version, trimmed count |
| `GET` / `POST` / `DELETE /memory` | Memory (P1) |  |
| `GET /usage` · `GET`/`PUT /settings` · `GET`/`PUT /permissions` | Usage, settings, per-tool modes | Sensitive tools cannot be set below *Always ask* |
| `GET /health` | `{server, db, network, model, service}` | Used by diagnostics and tests |

### 7.3 Send: request and response

Source

JSON

```
POST /api/v2/chats/c_81f2/messages
{"client_msg_id":"7b1f2c0e-3a54-4d8e-9a10-52f1c9e0b7aa",
 "text":"What is my battery doing?",
 "attachments":[{"id":"a_12"}],
 "mode":"queue",
 "model":null}

202 Accepted
{"message_id":"m_204","turn_id":"t_19aa","disposition":"queued",
 "queue_position":2,"last_seq":41}
```

`disposition` is one of `started`, `queued`, `steered`, `duplicate`. `mode` is ignored when idle and **required** while a turn is running (`400 mode_required` otherwise).

### 7.4 Stream framing and client rule

Source

auto

```
id: 42
event: tool.committed
data: {"chat_id":"c_81f2","seq":42, ... }

: heartbeat
```

Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. The client consumes it with `fetch()` and a stream reader (so it can send the token header) and reconnects with the last `seq` on any drop, backing off 0.5 / 1 / 2 / 4 / 8 s.

**Client rule:** `state = reduce(events)`. No component mutates the transcript directly. The optimistic user block is keyed by `client_msg_id` and reconciled when the matching `message.completed` arrives. Replaying the same events twice must produce an identical DOM.

### 7.5 Approval payload

Source

JSON

```
{"type":"approval.requested","data":{
 "approval_id":"ap_9","tool_use_id":"tu_4","name":"sms.send",
 "args":{"to":"+639171234567","body":"Running late, 10 min."},
 "args_hash":"7f3a21…(full sha256)","ref":"7F3A21",
 "risk":"sensitive","reversible":"no","expires_at":1795568227123,
 "display":{"title":"SEND SMS",
   "rows":[["TO","+63 917 123 4567 (Maria)"],["MESSAGE","Running late, 10 min."]],
   "effect":"Sends a real SMS from this phone.","revert":"CANNOT BE UNDONE"}}}
```

The **server** builds `display` from resolved arguments, and the UI renders only that. Model prose never reaches a Work Order. Recipients and package names are shown **in full, never masked**. Approving sends the full `args_hash`; the executor recomputes the hash of the arguments it is about to run and refuses on mismatch.

### 7.6 File-level change map

**Assumed current layout [ASSUME — confirm in Phase 0]:** a Flask entry file with routes and inline agent logic; a static front end (`index.html`, `app.js`, `style.css`); device-tool module(s) built on pyjnius; an NVIDIA client; recents and queue stored as files or a small DB.

| Target path | Action | Content |
| --- | --- | --- |
| `sitecrew/agent/options.py` | NEW | `AgentOptions`, `TerminalReason`, `PermissionMode` |
| `sitecrew/agent/events.py` | NEW | Envelope, `EventStore`, per-chat `Condition` bus |
| `sitecrew/agent/session.py` | NEW | `AgentSession`, `ChatActor` registry, single-flight lock, queue and steer |
| `sitecrew/agent/runner.py` | NEW | `TurnRunner`: the state machine of §6.4 |
| `sitecrew/agent/nvidia.py` | REFACTOR | Wrap the existing client: streaming, cancel (close stream), retry/backoff, usage, capability flags |
| `sitecrew/agent/permissions.py` | NEW | `PermissionEngine`, rule matching, chat grants |
| `sitecrew/agent/hooks.py` | NEW | `HookRegistry` and built-in hooks: redact secrets, audit log, loop guard |
| `sitecrew/agent/tools.py` | NEW | `@tool`, `ToolRegistry`, schema validation, `ToolExecutor` + watchdog |
| `sitecrew/agent/approvals.py` | NEW | `ApprovalBroker` |
| `sitecrew/agent/recovery.py` | NEW | `recover()` of §6.8 |
| `sitecrew/agent/undo.py` | NEW | `UndoManager` and inverse registry |
| `sitecrew/agent/context.py` | NEW | Builder, estimator, summariser job |
| `sitecrew/agent/localrouter.py` | REFACTOR | Existing offline answers behind the common event path |
| `sitecrew/agent/memory.py` | NEW (P1) | Proposal and commit |
| `sitecrew/store/db.py` + `migrations/001_init.sql` | NEW | Schema of §6.3, `user_version` migrations |
| `sitecrew/store/migrate_legacy.py` | NEW | Imports existing recents, restorable chats and queued messages after a backup copy |
| `sitecrew/android/bridge.py` | REFACTOR | One pyjnius entry point; `detach()` discipline; UI-thread helper |
| `sitecrew/android/connectivity.py`, `service.py`, `keystore.py` | NEW | `NetworkCallback`, foreground service control, key storage |
| `sitecrew/android/notifications.py`, `speech.py`, `share.py` | NEW (P1) | Notifications, STT/TTS, share-in/out |
| `sitecrew/tools/*.py` | REFACTOR | Wrap each existing tool in the contract; add `verify`, `inverse`, `reconcile`; **behaviour unchanged** |
| `sitecrew/api/routes.py` | NEW | Blueprints for §7.2; legacy routes become thin shims |
| `web/css/tokens.css`, `base.css`, `components/*.css` | NEW | §3.4 tokens and §4 components |
| `web/js/{state,api,stream}.js` | NEW | Event reducer, API client, stream reader |
| `web/js/render/*.js`, `composer.js`, `approvals.js`, `recents.js`, `settings.js` | REFACTOR | Split `app.js`; render from state only |
| `web/fonts/*.woff2` | NEW | Plex Sans / Condensed / Mono subsets |
| `tests/fakes/nvidia_server.py` | NEW | Scripted OpenAI-compatible stream: tool calls, delays, 429 with `Retry-After`, mid-stream drop, malformed args |
| `tests/fakes/bridge_fake.py` | NEW | Pyjnius stand-in with scripted device state and fault injection |
| `tests/{unit,integration,ui}/` | NEW | Gates of §9–§10 |
| `scripts/android/*.sh` | NEW | `adb` helpers: install, kill, airplane mode, screenshot, UI dump, logcat capture |

### 7.7 Rollout and rollback

- Feature flag `SITECREW_ENGINE=v1|v2`. Default stays `v1` until the Phase 3 gate passes; `v2` then becomes the default while `v1` remains available for one release.
- Legacy routes are shims over v2 services, so behaviour cannot silently fork.
- Migration is additive and idempotent. The first run copies the old data aside (`*.pre-v2.bak`) and never deletes it.
- Each phase must leave the app launchable and every previous gate green.

## 8. Acceptance criteria

Every criterion is **binary and measurable**. “Device” means a physical Android phone; “PC” means the fake NVIDIA server and fake bridge with a desktop browser. A criterion is *met* only when the evidence file named in §9 exists in the gate report. Thresholds are starting values: if the agent believes one is unrealistic on the target device, it must measure, report the real figure and propose a change — never silently relax it.

### 8.1 P0

| ID | Criterion | Verified on |
| --- | --- | --- |
| **F01** | **AC-01.1** Every turn has a `turns` row and only the transitions of §6.4 occur (property test, 10 000 random event orders, 0 illegal transitions). **AC-01.2** `seq` is gapless and monotonic per chat, and killing the process at 200 random points leaves 0 states without their event and 0 events without their state. **AC-01.3** 50 parallel sends to one chat never produce two concurrent `EXECUTING` turns, and every response has a valid `disposition`. | PC + Device |
| **F02** | **AC-02.1** First model chunk to first painted text ≤ 150 ms on PC and ≤ 300 ms on device. **AC-02.2** Stop closes the HTTP stream within 500 ms (the fake server observes the disconnect), emits `turn.cancelled`, keeps the partial text with a `STOPPED` stamp, and no `message.delta` follows. **AC-02.3** 100 Stops at random moments: 0 stuck turns, 0 duplicate messages. | PC + Device |
| **F03** | **AC-03.1** The same `client_msg_id` sent 20 times concurrently yields 1 message, 1 turn and 19 `duplicate`. **AC-03.2** 10 messages queued offline and flushed: visible order = turn start order = send order, exactly 10 assistant replies, 0 duplicates (DB query *and* DOM check). **AC-03.3** Steer: the next model request starts ≤ 1 s after the safe boundary, contains the steer text (fake-server log), reuses `turn_id`, and an in-flight write tool completes and verifies. **AC-03.4** Stop holds the queue; Resume continues; editing or removing a queued item works until `sending`. **AC-03.5** Steer during an approval returns 409 and the composer explains why. | PC + Device |
| **F04** | **AC-04.1** Startup fails if any registered tool lacks schema, `risk` or `reversible`. **AC-04.2** 500 fuzzed calls (wrong types, extra keys, unknown tools): 0 executed, each yields an error `tool_result`, repairs ≤ 2. **AC-04.3** Every write and sensitive tool has `verify`; with fault injection (bridge reports success, state unchanged) the result is `tool.verify_failed` and no chip appears. **AC-04.4** Retries follow 1 / 3 / 9 s within ±20 %, honour `Retry-After`, and a third identical call in a turn is denied. **AC-04.5** A hook exceeding 250 ms denies write and sensitive tools (fail closed). **AC-04.6** The gate table test covers all six steps × three risks × hook decisions (≥ 60 cells), all passing. | PC + Device |
| **F05** | **AC-05.1** Sensitive tools run 0 times without `approval.resolved(approve)` for the same `args_hash` (fuzz and property test); a mismatched hash returns 409 and nothing runs. **AC-05.2** A pending approval survives `kill -9` and the same card (same `REF`) is visible ≤ 2 s after the UI is ready. **AC-05.3** Expired approvals cannot be signed (410). **AC-05.4** The Work Order shows the server-built `display`, wraps fully at 200 % font scale, hold-to-sign takes 600 ms ± 50 ms, and the Confirm alternative works and is the default under TalkBack. **AC-05.5** Settings cannot pre-allow a sensitive tool (UI disabled; API returns 422). | PC + Device |
| **F06** | **AC-06.1** Chip count = `tool.committed` count per turn across 200 scripted turns that include failed, denied, cancelled and killed ones (automated DOM-versus-DB diff). **AC-06.2** Unit test: no reducer path renders a success chip from `tool.proposed` or `tool.started`. **AC-06.3** Details-drawer values equal the DB row after redaction. **AC-06.4** No UI string for an Edit or Shell activity exists anywhere (`grep` gate). | PC + Device |
| **F07** | **AC-07.1** `kill -9` at each of the 12 kill points of §9.2: no duplicate message, no lost message, no repeated sensitive effect, `recover()` idempotent, an accurate system line ≤ 3 s after relaunch. **AC-07.2** Dropping the stream at 100 random points yields a DOM identical to an uninterrupted run. **AC-07.3** Backgrounding the app for 10 minutes mid-turn: the turn completes or recovers with 0 lost messages. **AC-07.4** The foreground service stops ≤ 45 s after idle and no notification is left behind. | PC (7.1, 7.2) + Device (all) |
| **F08** | **AC-08.1** Each reversible tool: perform, undo, and the prior state is restored exactly (hash or value equality), ≥ 20 runs each. **AC-08.2** Irreversible tools show ✗ before *and* after, attempt no inverse, and transcript removal still works. **AC-08.3** Undo is idempotent: a second press reports “already reverted”. **AC-08.4** With *Revert device effects* off, behaviour equals legacy transcript Undo (golden test on existing fixtures). **AC-08.5** A value changed externally after SiteCrew set it shows ≠ and asks before reverting. | PC + Device |
| **F09** | **AC-09.1** Edit creates a sibling branch; the original branch is byte-identical afterwards. **AC-09.2** The new branch’s model request contains 0 messages from the sibling branch (fake-server log). **AC-09.3** Regenerate keeps the previous answer stored. | PC |
| **F10** | **AC-10.1** Across 1 000 randomised histories the built context never exceeds budget. **AC-10.2** Killing the summary job mid-write leaves the previous summary intact. **AC-10.3** The meter appears at ≥ 60 % and matches server figures ±1 %. **AC-10.4** Eval set of 10 scripted prompts on the real model: device facts older than the current turn are re-read by tool in ≥ 9/10, and a fact seeded at turn 3 of a 150-turn chat is used correctly in ≥ 8/10. | PC (10.1–10.3) + Device (10.4) |
| **F11** | **AC-11.1** On a 20-sample corpus (tables, nested lists, fences, an unterminated fence, very long lines) cumulative layout shift during streaming ≤ 0.02. **AC-11.2** Code *Copy* is byte-equal to the source. **AC-11.3** A 500-message chat scrolls with < 5 % janky frames on device (`dumpsys gfxinfo`). | PC + Device |
| **F12** | **AC-12.1** Rename, pin and delete each take ≤ 2 taps. **AC-12.2** Delete-Undo within 6 s restores the chat with all messages. **AC-12.3** Search over 500 chats × 50 messages returns its first result ≤ 250 ms on PC and ≤ 600 ms on device. | PC + Device |
| **F13** | **AC-13.1** A request without the token returns 401, and a wrong `Host` returns 421, tested from a second process on the device. **AC-13.2** Grepping logs, a DB dump and an export for `nvapi-` returns 0 hits. **AC-13.3** The Permissions screen locks sensitive rows. | Device |
| **F14** | **AC-14.1** Every token pair passes the contrast script (≥ 4.5:1 text, ≥ 3:1 UI) in both themes. **AC-14.2** axe-core on PC shows 0 critical or serious issues; the TalkBack checklist of §9.3 passes on device. **AC-14.3** At 200 % font scale, 8 key screens show no clipped or overlapping controls. **AC-14.4** With reduced motion on, `document.getAnimations().length` is 0 during streaming. **AC-14.5** Every interactive element is ≥ 48 × 48 dp (automated sweep, 0 violations). **AC-14.6** Budgets: JS ≤ 200 KB gzipped, fonts ≤ 150 KB, warm start ≤ 1.5 s (median of 10 on device). **AC-14.7** Golden screenshots for 8 screens × 2 themes × 2 widths exist and diffs were reviewed. | PC + Device |
| **F15** | **AC-15.1** In airplane mode a device question is answered ≤ 1 s with a `LOCAL` tag and the same event types, and the fake server records 0 model requests. **AC-15.2** 20 connectivity toggles in 60 s cause 0 duplicate sends and no strip change faster than the 1.5 s hysteresis. **AC-15.3** Reconnect flush is ordered and idempotent. | Device |

### 8.2 P1

| ID | Criterion | Verified on |
| --- | --- | --- |
| **F20** | Switching branch ≤ 150 ms; the context after switching matches the selected branch (fake-server log). | PC |
| **F21** | 0 `memory.committed` events without a preceding user *Keep* action; 20 secret-pattern proposals rejected; deleting an item removes it from the next request (fake-server log); the viewer lists every stored item. | PC |
| **F22** | Offered only when `supports_vision`; images downscaled to ≤ 1 600 px before upload; ≥ 95 % success over 20 camera and gallery attaches. | Device |
| **F23** | Dictated text appears in the composer ≤ 2 s after speech ends; read-aloud starts ≤ 1 s and Stop works; denied microphone permission shows the inline card; no crash on rotation mid-dictation. | Device |
| **F24** | Share-in from Chrome text selection and from Files opens SiteCrew with the content attached ≤ 2 s; resending the same intent creates no duplicate; share-out produces the correct MIME type. | Device |
| **F25** | The 4 built-in playbooks each pass a scripted run on the fake bridge; create, edit and delete work; a tool outside the playbook’s scope is denied at the gate. | PC + Device |
| **F26** | Alarm fires within ± 1 min in 10 trials, including 10 minutes after screen-off under forced idle; Undo cancels it (`dumpsys alarm` confirms); the reminder is re-registered after reboot. | Device |
| **F27** | A notification appears ≤ 3 s after an approval is needed or a turn completes while backgrounded; tapping deep-links to that chat; denied permission is handled. | Device |
| **F28** | Usage counters equal the fake-server request log exactly; the 429 countdown matches `Retry-After` ± 1 s; fallback model engages after the configured failures and is labelled. | PC |
| **F29** | Biometric prompt gates entry; the recents-screen thumbnail is blank when lock is on (`FLAG_SECURE`). | Device |
| **F30** | FTS5 search over 10 000 messages ≤ 100 ms on PC; the `LIKE` fallback path is tested with FTS5 disabled. | PC |

## 9. PC and real-Android test matrix

**What PC cannot prove:** pyjnius behaviour, the foreground service, Doze, TalkBack, real rate limits and real device effects. Those are device-only gates. An emulator is fine for development loops but never closes a gate.

### 9.1 Matrix

| # | Scenario | PC procedure (fake NVIDIA server + fake bridge) | Real-Android procedure | Pass condition |
| --- | --- | --- | --- | --- |
| 1 | Streaming | Scripted 400-token stream with varied chunk sizes; measure paint latency and layout shift | Same prompt against the real model on the phone; screen-record | AC-02.1, AC-11.1 |
| 2 | Cancellation | Stop at 5 phases: before first token, mid-text, during tool, during approval, during verify | Same on device with a real read tool and a real write tool | AC-02.2, AC-02.3 |
| 3 | Queue order | Send 10 messages while a slow turn runs; assert order in DB and DOM | Same with airplane mode, then reconnect | AC-03.1, AC-03.2, AC-03.4 |
| 4 | Steering | Steer during stream, during a slow write tool, during approval, while offline, during `FINALIZING` | Steer during a real streamed answer and a real file operation | AC-03.3, AC-03.5 |
| 5 | Reconnect | Drop the SSE connection at 100 random points; toggle the fake server up and down | Toggle Wi-Fi and data via `adb`; flap 20 times in 60 s | AC-07.2, AC-15.2, AC-15.3 |
| 6 | Process restart | Hard-kill the server process at each kill point K1–K12 and restart | `adb shell am force-stop` and `kill -9` at K1–K12; also `am send-trim-memory` | AC-07.1 |
| 7 | Approvals | Approve, decline, expire, hash mismatch, kill while pending, double-tap approve | Same for SMS (to a number the tester controls), call, uninstall (a throwaway APK) and one settings change | AC-05.1–5.5 |
| 8 | No duplicates, no loss | Randomised send/kill/reconnect fuzz for 30 min; compare sent set to stored set | 20-minute scripted soak with random kills and network flaps | 0 duplicates, 0 missing, 0 orphan states |
| 9 | Accessibility | axe-core, keyboard-only walk, 200 % zoom | TalkBack checklist §9.3, Switch Access walk, `font_scale 2.0`, display size max | AC-14.2, AC-14.3 |
| 10 | Offline tools | LocalRouter against the fake bridge with scripted values and nulls | Airplane mode: battery, apps, storage, device info, files list | AC-15.1 |
| 11 | Android runtime | n/a | Permission denial and grant, rotation, split-screen, 10-minute background, forced Doze, WebView render-process-gone, low-memory kill | AC-07.3, AC-07.4 |
| 12 | Themes and motion | Golden screenshots 8 × 2 × 2; `getAnimations()` under reduced motion | Same on device under system Dark and Light; “Remove animations” on | AC-14.1, AC-14.4, AC-14.7 |
| 13 | Performance | Streaming CPU profile; 500-message scroll trace | Cold and warm start (10 runs, median), `gfxinfo` jank, battery delta over a 10-minute chat | AC-11.3, AC-14.6 |
| 14 | Security | Token and Host checks with `curl` | `adb shell curl` from a second process; secret grep over logs, DB, exports | AC-13.1, AC-13.2 |
| 15 | Undo | Inverse ops against the fake bridge, including ≠ and expired cases | Each reversible tool × 20 on the phone | AC-08.1–8.5 |
| 16 | Migration | Import fixture copies of legacy recents, restorable chats and queued messages | Install the new build **over** the current build on a phone with real data (after backup) | 0 lost chats, 0 lost queued messages, `.pre-v2.bak` present |

### 9.2 The twelve kill points

1. During `MODEL_STREAMING`, mid-delta.
2. After `tool.proposed`, before the gate.
3. During `AWAITING_APPROVAL`.
4. After approval resolved, before the `intent` write.
5. After the `intent` write, before the effect.
6. After the effect, before `verify`.
7. After `verify`, before the commit transaction.
8. After commit, before the next model call.
9. During queue flush, with a message in `sending`.
10. During Undo, between two inverse operations.
11. During the summary job’s commit.
12. During `recover()` itself (a second crash while recovering).

For each: assert the five properties of AC-07.1 and capture the event log before the kill, after relaunch and after the user-visible resolution.

### 9.3 TalkBack checklist (device)

1. Launch focus lands on the composer or latest message with a meaningful label.
2. Every control has an accessible name — no “unlabelled button” anywhere.
3. Composer state changes (Send, Queue, Steer, Stop) are announced.
4. Streaming produces one “Reply complete” announcement, not a token stream.
5. An approval takes focus, reads in full, Decline and Confirm are reachable, and focus returns afterwards.
6. Hold-to-sign is replaced by Confirm automatically.
7. Receipts and stamps have text equivalents (“Verified”, “Queued”, “Reverted”).
8. Recents rows read title and time; swipe-to-delete has an overflow-menu alternative.
9. Signal Strip changes are announced politely (“Offline. 2 messages queued.”).
10. Switches in Settings expose on/off state.
11. Code blocks have a labelled Copy button and readable content.
12. Back closes sheets in order; no focus trap anywhere. Switch Access can reach every action.

### 9.4 Device harness (the agent writes these into `scripts/android/`)

Source

Bash

```
adb devices -l                                   # confirm a physical device is attached
adb exec-out screencap -p > screens/<name>.png   # screenshot
adb shell uiautomator dump /sdcard/ui.xml        # UI hierarchy for assertions
adb logcat -d -v time > logs/logcat.txt          # evidence
adb shell screenrecord --time-limit 60 /sdcard/flow.mp4
adb shell am force-stop <pkg>                    # clean stop
adb shell kill -9 <pid>                          # hard kill (find pid with pidof)
adb shell am send-trim-memory <pid> RUNNING_CRITICAL
adb shell svc wifi disable; adb shell svc data disable      # offline (cmd connectivity airplane-mode where supported)
adb shell dumpsys deviceidle force-idle          # Doze
adb shell dumpsys alarm | grep <pkg>             # reminder evidence
adb shell settings put system font_scale 2.0     # 200 % text
adb shell settings put global animator_duration_scale 0      # remove animations
adb shell dumpsys gfxinfo <pkg> framestats       # jank
```

Command availability varies by Android version and vendor. Where a command is unavailable the agent must say so, use the closest alternative, and flag any step that needs a human (for example toggling airplane mode) in the report. **Verify that WebView actually maps the OS setting to `prefers-reduced-motion`; if it does not, the in-app override is the tested path.**

### 9.5 Evidence rules

Each gate produces `reports/gate-N/` containing `summary.md`, `logs/` (test output, logcat), `screens/` (`<screen>-<theme>-<width>-<state>.png`), `ui/` (hierarchy dumps), `db/` (SQLite snapshots and event-log exports), `video/` and `traces/`. Each acceptance criterion in `summary.md` cites a file path, the measured value, the date, device model, Android version and app build hash. **A criterion that did not run is `NOT RUN` — never `PASS`.** A failing criterion is never edited into a pass.

## 10. Phased implementation with dependencies and regression gates

### 10.1 Phases

| Phase | Scope | Depends on | Exit gate |
| --- | --- | --- | --- |
| **0 — Audit and baseline** | Map the real architecture against this spec. Write `ASSUMPTIONS.md` (each assumption confirmed, corrected or unknown). Build characterization tests for every preserved feature. Build the fakes, `scripts/android/*`, and a one-command gate runner. **No behaviour change.** | — | **G0:** all characterization tests pass on the *unmodified* app, on PC and device; baseline screenshots stored |
| **1 — Store and events** | SQLite schema, migrations, `EventStore`, legacy import with backups, legacy routes as shims over the store | 0 | **G1:** migration test on fixtures and on a real-data copy; all G0 tests still green |
| **2 — Engine core** | `AgentSession`, `TurnRunner`, NVIDIA wrapper with cancel and retry, `PermissionEngine`, hooks, tool contract wrapped around existing tools, Stop. Behind `SITECREW_ENGINE=v2` | 1 | **G2:** F01, F02, F04 PC criteria; gate-table and fuzz tests; v1 untouched |
| **3 — Durability and safety** | Idempotent queue and Steer, `ApprovalBroker`, `recover()`, inverse Undo, LocalRouter parity, localhost token, key storage, foreground service | 2 | **G3 (the big one):** F03, F05, F07, F08, F13, F15 on PC *and* device, including all 12 kill points. `v2` becomes the default only after G3 |
| **4 — UI foundation** | Tokens, fonts, base components, reducer-driven rendering, Ledger Rail, receipts, Signal Strip, composer states, Work Order, recents, settings, themes, a11y, reduced motion | 2 (event contract); can overlap Phase 3 | **G4:** F06 (UI), F14 on device, golden screenshots, budgets |
| **5 — Remaining P0 UX** | Edit and regenerate, context builder, summary and meter, rename/pin/delete/search, Markdown and code polish | 3, 4 | **G5:** F09, F10, F11, F12 |
| **6 — P1, in value order** | F26 reminders → F24 share-in/out → F23 voice and read-aloud → F25 playbooks → F21 memory → F20 branch UI → F27 notifications → F28 usage and fallback → F22 images → F30 FTS5 → F29 app lock | 5 | **G6.x:** one sub-gate per feature, run with every earlier gate |
| **7 — Hardening and release candidate** | Full matrix on device, 2-hour soak, memory and DB-size checks, regression of every preserved feature, `ARCHITECTURE.md`, `TESTING.md`, `CHANGELOG.md` | 6 | **G7:** all P0 and P1 criteria `PASS` with evidence; no `NOT RUN` |

**Why this P1 order:** reminders and share-in are the Android differentiators and reuse the tool contract and Undo. Voice is next because it is device-specific and high value. Playbooks reuse existing tools. Memory needs the context builder. Notifications need the foreground service from Phase 3. Images depend on model support, so they come late. FTS5 and app lock are low risk and low urgency.

### 10.2 Standing regression gates — run at the end of every phase

| Preserved behaviour [YOU] | Characterization test |
| --- | --- |
| NVIDIA model chat | Real model, one short prompt, streamed reply; plus the fake-server suite |
| Uploads and text extraction | One fixture each for PDF, DOCX, TXT; extracted text matches the golden file |
| Exports | Each existing format produces a file equal to the golden output |
| Auto-saved recents and restorable chats | Create, kill, relaunch, restore: transcript identical |
| Android tools: battery, apps, settings, device, files | Each tool run on the real device; values compared with `adb shell` ground truth |
| Approval-gated actions (SMS, calls, uninstall, settings) | Each refuses to run without confirmation; each runs after it |
| Deterministic offline device answers | Airplane mode: same answers as before |
| Persistent queued messages and reconnect flush | Queue offline, kill, relaunch, reconnect: all delivered once |
| Used chips are real | Chip count equals real tool executions |
| Queue / Steer | Existing behaviours reproduced, then tightened by §6.6 |
| Transcript Undo | Golden comparison with the legacy behaviour |
| Identity | Terracotta `#D97757` dark / `#C96442` light and squared controls present in both themes |
| Forbidden behaviours | `grep` gates: no Edit/Shell activity strings, no invented-fact fallbacks, no tunnelling code paths |

### 10.3 Definition of done

Every selected P0 and P1 criterion is `PASS` with evidence in `reports/gate-7/`; every standing regression gate is green; `ASSUMPTIONS.md` has no *unknown* left unresolved or explicitly accepted; and nothing has been committed or pushed unless the owner asked.

## 11. MASTER PROMPT for the coding agent

### 11.1 Before you paste it

1. Export this document as Markdown and save it in your repo as `docs/SITECREW_SPEC.md`. The prompt treats that file as the single source of truth, so you do not have to paste the spec itself.
2. Create `docs/TEST_TARGETS.md` with throwaway targets the agent may use for *real* sensitive actions on your phone. Without it the agent must test those paths with fakes only and mark the device-effect criteria `NOT RUN`:

Source

auto

```
sms_test_number: <a number you control>
call_test_number: <a number you control>
uninstall_test_package: <a throwaway APK you installed for this>
settings_test: brightness only
```

1. Give the agent shell access, and `adb` access to a physical phone if you can (USB or wireless). Paste the prompt below.

### 11.2 The prompt

Source

auto

```
ROLE
You are the lead engineer on SiteCrew AI, an Android app (Python/Flask in an Android WebView, native access through pyjnius, NVIDIA-hosted models). Implement the specification in docs/SITECREW_SPEC.md end to end and autonomously: every P0 and P1 feature, the design system, the agent architecture, and every acceptance criterion in section 8. The spec is the contract. Re-read the relevant sections at the start of every phase.

YOU ARE NOT DONE UNTIL
Every P0 and P1 acceptance criterion is PASS with evidence in reports/gate-7/, every standing regression gate in spec section 10.2 is green, and you have inspected the real UI on a real Android device. A plan, a first draft, a green unit-test run, or a working emulator is NOT completion. Do not stop to ask whether to continue. Do not hand back a plan.

NON-NEGOTIABLE CONSTRAINTS
1. Keep NVIDIA models, Python/Flask, Android WebView and pyjnius. Never switch to the Claude API. Do NOT add claude-agent-sdk as a dependency: implement the in-process adapter in spec section 1 (package sitecrew.agent) with the exact semantic mapping given there. Do not copy SDK source code.
2. Preserve every existing behaviour listed in spec section 10.2. Write characterization tests for each BEFORE changing code.
3. Sensitive actions (SMS, calls, uninstall, settings changes, file delete/overwrite) always require confirmation, bound to the exact arguments by hash. Never auto-run, auto-retry or pre-allow them.
4. No decorative or fake activity: no Edit/Shell strings, no Used chip without a committed tool.committed event, no invented device facts (an unavailable value is reported as unavailable), and no carrier free-data tunnelling code paths.
5. Minimal, maintainable changes. Prefer the standard library and pure-Python packages that run on Android. Justify every new dependency in reports/DEPENDENCIES.md (size, license, why the stdlib is not enough). Keep the front end framework-free unless the project already uses a framework. No gradients, glass, shadows or rounded corners anywhere in the UI.
6. GIT: never commit, push, tag, stash, reset, rebase or rewrite history unless the owner explicitly asks. Read-only git commands (status, diff, log) are fine. Before Phase 1, save the current tree as a patch/tarball under reports/backup/ so rollback is possible. Back up user data as *.pre-v2.bak before any migration and never delete the backup.
7. SECRETS: never print, log, commit or export the NVIDIA key. Redact it everywhere (spec 6.12).
8. REAL-DEVICE SAFETY: execute real sensitive actions on the phone ONLY against the throwaway targets in docs/TEST_TARGETS.md. If a target is missing, test that path with the fake bridge, verify that the gate and Work Order behave correctly, and record the device-effect criterion as NOT RUN with the reason.

OPERATING LOOP (repeat for every phase in spec section 10.1)
1. Restate the phase's acceptance criteria as executable tests first, then implement the smallest change that can satisfy them.
2. Run the PC gate (unit, integration with tests/fakes, UI checks). Then build and install on the phone and run the device checks of spec section 9. Capture evidence exactly as spec 9.5 requires.
3. LOOK AT THE UI. Take screenshots with adb, open and examine the images yourself, dump the UI hierarchy, and compare against the spec: tokens, type scale, spacing, states, copy, contrast, touch targets, motion, dark and light, 200 percent font scale. For each screen write what you checked and what you changed in reports/UI_REVIEW.md. Fix what is wrong and re-capture. Do not claim a screen is correct without having inspected a screenshot of it.
4. FAILURE PROTOCOL: when anything fails, find the root cause, fix it, and re-run the WHOLE gate, not only the failing test. Make at most 5 attempts per failure, each with a new hypothesis recorded in reports/PROGRESS.md. If still failing, write reports/BLOCKERS.md (what you tried, evidence, options) and continue with unblocked work. NEVER weaken a test, loosen a threshold, delete an assertion or mark a failing criterion PASS to get past a gate. If you believe a threshold is unrealistic, measure the real value, report it, and propose a change in reports/DEVIATIONS.md.
5. After each step update reports/PROGRESS.md: current phase, a table of every criterion with PASS / FAIL / NOT RUN and an evidence path, open blockers, and deviations. This file is your memory across a long run.
6. Ask the owner a question only when it blocks all progress (physical device access, throwaway targets, NVIDIA key availability). Batch questions and keep working on everything else meanwhile.

PHASE 0 FIRST (no behaviour changes)
- Map the repository: entry points, routes, storage, tool modules, front-end structure, how the APK is built and installed, how the Python process runs on the device, which process hosts Flask, how the WebView talks to it. Write ARCHITECTURE_AS_IS.md.
- Write ASSUMPTIONS.md: every [ASSUME] in the spec marked confirmed, corrected or unknown, with the evidence (file and line). Adapt the file-level change map (spec 7.6) to the real layout and record each difference.
- Detect the environment: run adb devices. If no physical device is reachable, say so prominently, finish everything verifiable on PC, generate reports/DEVICE_CHECKLIST.md as a precise human-executable script for every device-only criterion, mark those criteria NOT RUN, and never present emulator or PC evidence as device evidence.
- Build tests/fakes (scripted NVIDIA server, fake bridge with fault injection, injectable clock and ids), scripts/android/*, and one command that runs a gate and writes reports/gate-N/ (spec 9.5).
- Write characterization tests for every preserved behaviour and pass G0 on the unmodified app before proceeding.

DESIGN RULES (spec sections 3 and 4)
- Implement the tokens exactly. Add a script that verifies contrast for every token pair in both themes and fails the build below 4.5:1 text or 3:1 UI. Bundle IBM Plex Sans, Plex Sans Condensed and Plex Mono locally as WOFF2 subsets; no network fonts.
- Build a debug-only state gallery page that renders every state in spec 4 deterministically (empty, loading, streaming, tool working, committed, failed, unverified, declined, unknown outcome, approval, expired approval, queued, offline, degraded, rate limited, permission needed, recovered, undone) so each can be screenshotted on the device without engineering it. It must be absent from release builds.
- Implement all six signature details of spec 3.3 or document precisely why one cannot be done.
- The UI renders ONLY from the event stream through a reducer. No component mutates the transcript directly and no chip is ever created optimistically.
- Reduced motion, 48 dp targets, TalkBack behaviour and the Confirm alternative to hold-to-sign are tested, not assumed.

ENGINEERING RULES (spec sections 6 and 7)
- Contract first: event schema, then store, then engine behind SITECREW_ENGINE=v2, then shims, then UI. v1 keeps working until gate G3 passes.
- Every tool is wrapped in the tool contract with risk, reversible, verify, inverse and reconcile where required; startup fails on a missing declaration.
- Intent before effect; one writer per chat; seq allocated in the same transaction as the state change; hash-bound approvals; sensitive tools are never re-run after a crash without a fresh approval; unreconcilable effects are reported as outcome_unknown.
- Inject time, randomness and ids so tests are deterministic. Every loop, wait and retry has a bound and a timeout.
- Respect Android platform rules over the spec where they conflict (foreground-service types and timeouts, exact-alarm restrictions, scoped storage, background limits). Implement the closest compliant behaviour and document it in reports/DEVIATIONS.md.
- The target hardware is low-spec: measure memory, CPU, start time and jank on the device and keep the budgets of AC-14.6 and AC-11.3.

EVIDENCE AND HONESTY
- Every PASS cites a file path, a measured value, the date, device model, Android version and app build hash. A criterion that did not run is NOT RUN, never PASS. Never fabricate a measurement or a screenshot.
- If the spec is wrong or infeasible anywhere, do not hide it: implement the closest correct alternative, explain why in reports/DEVIATIONS.md, and keep going.
- Keep diffs small and reviewable, one concern per change, with docs updated in the same change (ARCHITECTURE.md, TESTING.md, CHANGELOG.md).

FINAL HAND-OFF (only when the definition of done is met)
Reply with: (1) the full criteria table, PASS / NOT RUN / FAIL, each with an evidence path; (2) the list of deviations and known limitations; (3) git diff --stat; (4) the exact commands to re-run every gate; (5) the sentence 'No commits were made.' If anything is FAIL or NOT RUN, say so first and do not describe the work as complete.

BEGIN NOW with Phase 0. Do not ask me to confirm the plan.
```

### 11.3 Four calls to veto before you run it

These are the opinionated choices most likely to differ from your taste. Each is a one-line change in §6.6 or §4.6 if you disagree.

1. **Stop pauses the queue** instead of letting queued messages run on. (Safer; one extra tap.)
2. **Steer is refused during an approval.** The user must sign or decline first.
3. **Hold-to-sign** for sensitive actions, with a visible Confirm alternative that becomes the default under TalkBack.
4. **IBM Plex** as the type family, bundled locally. It suits the field-operations identity and works offline.

Three things I could not know and Phase 0 must answer: how your APK is built and deployed, which Android versions you must support, and whether Flask runs in the same process as the UI.

---

## 12. CONTINUATION SLOT — reserved for Claude's continued work

> **STATUS (updated 2026-11-25 23:4x PHT):** Sections 0–11.3 above were generated and
> finished by Claude (Sonnet 5.5 Extra) as **v1.0** and exported verbatim into this file.
> The free-usage pause resolved at 23:40; **Continue** was clicked and Claude finished
> the continuation at ~23:45, confirming the document "is ready" and covers all ten
> deliverables — a byte-identical re-export (100,160 chars) confirmed **no content
> changed**. This slot remains reserved for future Claude revisions/additions.

**How to resume when usage resets**

1. Open the paused chat: <https://claude.ai/chat/ccbc799a-59ae-413b-ad85-a8fa2c16fc0b>
2. Click **Continue** on the paused message (or send):
   *"Continue the SiteCrew AI — Master Build Specification exactly where you paused.
   Finish anything incomplete and output any missing prompt text in full."*
3. Export the new/updated content and paste it between the markers below, replacing the
   placeholder. Do not edit anything above this section.

<!-- ===== CLAUDE-CONTINUATION START — paste continued spec/prompt text below this line ===== -->

[PENDING — empty until Claude adds future revisions/additions. RESOLVED 2026-11-25:
the first continuation (clicked after the usage reset) completed and CONFIRMED v1.0
complete — no content changes; slot re-opened for the next phase of Claude work: the
Real Office blueprint (see RESUME.md standing directive). If Claude's continuation
only *revises* earlier sections, note the changed section numbers here and apply them
above; if it adds new content, paste it in full between these markers.]

<!-- ===== CLAUDE-CONTINUATION END — do not paste below this line ===== -->

*End of continuation slot. Sections 0–11.3 are the authoritative v1.0 generated content.*
