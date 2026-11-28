# SiteCrew AI — Phase 2 Execution Order

**Source of truth:** `docs/SITECREW_OFFICE_BLUEPRINT.md` v2.0, §6 Phase 2 (gates **P2.1–P2.8**), with §1.4, §3.5, §4, §5 and §6.1 as referenced.
**Document date:** 2026-11-28 · **Status:** DRAFT for S0 review · **Owner rulings in force:** see §0.

> **Phone status: NOT RUN.** The phone is disconnected. Every phone-dependent criterion below is `NOT RUN — phone disconnected` until a device run produces evidence. Nothing in this document claims a device result.

Markers used below:

- **†** = specificity added by this plan (counts, run numbers). The blueprint's headline threshold is the gate; † only makes it measurable.
- **[ADDED]** = an extra acceptance check proposed by this plan. It does not replace or weaken any P2.x gate.
- **PC** = runnable without the phone. **PHONE** = needs the device.
- Evidence rule (blueprint §6): every criterion is `PASS` with a file path and a measured value, or `NOT RUN` with a reason. Evidence lives under `reports/p2/`.

---

## 0. Standing rulings and boundaries

| # | Ruling | Consequence in Phase 2 |
|---|---|---|
| R-1 | **Store, events and poll are FROZEN** (P1.2 closed; P1.3 PC half passed with 1 s slices and `cid` supersede) | Phase 2 builds **on** them. No schema change, no new migration, no change to `EventStore`, `seq` semantics, `/api/v2/poll` or the legacy shims. If a Phase 2 step needs one, stop and file a deviation (`reports/DEVIATIONS.md`) for owner sign-off. Adding rows and event types through the existing API is not a change. |
| R-2 | **NVIDIA models only; never the Claude API in the product** | Gateway host allowlist plus a grep gate (S4). |
| R-3 | **Python/Flask backend; pyjnius on the device** | No new runtimes. |
| R-4 | **Preserve:** uploads and exports, recents/restore, offline device answers, queue/steer/undo, approval gates | S6 and S8 regression coverage. |
| R-5 | **BOSS CALL #5 is NOT approved** (SMS reads, contacts, location, photos become ask-once) | **OPEN / UNAPPROVED.** Current sensitive-action gates are preserved exactly. See §8 (S0 package) and call 5. |
| R-6 | **Calm iOS supersedes the old squared-only rule** | S7; the old UI stays behind a flag until cut-over; the two looks are never mixed (§7 rule 5 of the blueprint). |
| R-7 | **No commit, push, tag or history rewrite unless asked** | Applies to every step. |
| R-8 | **Real sensitive actions only against `docs/TEST_TARGETS.md` throwaways; never print or log the NVIDIA key** | S8. |

**Frozen-layer inventory (confirm in S0):** SQLite schema v1.0 tables (`chats`, `messages`, `turns`, `events`, `tool_calls`, `approvals`, `queue`, `undo_ops`, `summaries`, `usage`) plus migration 002 (the extra columns and the empty office and Brain tables); `EventStore` (`seq` in the same transaction); `/api/v2/poll`; legacy-route shims; the one-time import.

---

## 1. Entry conditions (Gate G1)

Blueprint §6: the next phase does not start until the gate passes, and §6.2 allows only **UI foundation work** to overlap the back half of Phase 1.

| Criterion | Known status | Notes |
|---|---|---|
| P1.1 import loses 0 chats / 0 queued (incl. real device data backup) | **UNKNOWN to this plan** | The real-device-data half needs the phone → `NOT RUN` while disconnected. |
| P1.2 `seq` gapless; 200 kill points | **CLOSED** (owner) | |
| P1.3 ≤ 300 ms PC / ≤ 600 ms device; 100 forced reconnects identical DOM | **PC half PASSED** (owner). **Device half NOT RUN** | Phone disconnected. |
| P1.4 FTS5 result recorded; fallback passes | **UNKNOWN to this plan** | The FTS5 probe is on the real device build → `NOT RUN` while disconnected. |
| P1.5 all 38 passing G0 tests still pass | **UNKNOWN to this plan** | The 39th G0 test must have a recorded fix or reason (P1 triage); P2.8 needs that record. |

**Owner ruling E-1 (needed):** G1 cannot formally close while its device criteria are `NOT RUN`. Choose one:

- **E-1a** — Hold S1–S6 until the phone is back and G1 closes. Only S0 and S7a–b proceed now.
- **E-1b** — Start S1–S6 now, with G1's device items carried as recorded `NOT RUN` debt and re-run in S8 before G2.

**Default if no ruling:** E-1a (blueprint-literal). S0 and S7a–b are allowed under both.

---

## 2. Ordered build steps at a glance

| Step | Produces | Gate(s) | Where | Starts when | Status |
|---|---|---|---|---|---|
| **S0** | Pre-flight package (inventory, tiers, five calls, token fixes) | — (feeds P2.1, P2.2, P2.7) | PC | now | NOT STARTED |
| **S1** | Tool contract + registry | **P2.1** | PC | S0 closed + E-1 | NOT STARTED |
| **S2** | PermissionEngine, tiers, hooks | **P2.2** | PC | S1 | NOT STARTED |
| **S3** | Durable approvals + grants | **P2.3** | PC (+ PHONE part) | S2 | NOT STARTED |
| **S4** | ModelGateway + reusable fake server | **P2.4** | PC | S0 + E-1 (parallel with S1) | NOT STARTED |
| **S5** | AgentSession + TurnRunner + Stop | **P2.5, P2.6** | PC | S1–S4 | NOT STARTED |
| **S6** | Integration behind the v2 flag | **P2.8** (PC half) | PC | S5 | NOT STARTED |
| **S7a–d** | Calm iOS foundation | **P2.7** | PC (+ PHONE review) | S7a–b now; S7c after S3 + S5 | NOT STARTED |
| **S8** | Device pass | P2.3 / P2.5 / P2.7 / P2.8 device parts | **PHONE** | S6 + S7 + phone connected | **NOT RUN — phone disconnected** |
| **S9** | Gate G2 run | P2.1–P2.8 + §6.1 + G0/G1 re-run | Both | S8 | NOT RUN |

```
S0 ─► S1 ─► S2 ─► S3 ─┐
 │                    ├─► S5 ─► S6 ─┐
 └─► S4 (parallel) ───┘             ├─► S8 (phone) ─► S9 (G2)
S7a ─► S7b ─► S7c (needs S3 + S5) ─► S7d ┘
```

---

## 3. Step detail and acceptance criteria

### S0 — Pre-flight package (PC, no product code)

**Produces:** `reports/p2/S0_package.md` (the content of §8 of this document, completed with real extraction), entries in `reports/DEVIATIONS.md`, owner answers to calls 1–5 and ruling E-1.

**Acceptance**
- Inventory has **26 rows, 0 `UNKNOWN`**, each citing the code location it was read from.
- **[ADDED] P2.2-A:** the effective-tier table equals the extracted current gate for every one of the 26 tools (diff empty).
- Calls 1–5 each have an owner answer or a recorded default; call 5 stays OPEN/UNAPPROVED unless the owner rules otherwise.
- Token changes (§8.4) are logged as deviations and the pair list and exemptions are signed off.

---

### S1 — Tool contract and registry → **P2.1**

**Produces:** a registry wrapping all 26 tools with `schema`, `risk`, `reversible`, `verify`, `inverse`, `reconcile`; old ids kept as aliases; a startup validator; an argument validator in front of every execution; a fuzzer.

**Acceptance**
- **P2.1:** startup fails if any tool lacks `schema`, `risk` or `reversible`; a **500-call fuzz** (wrong types, extra keys, unknown tools) executes **0** invalid calls.
  - † Mutation check: removing each of the three fields from each of the 26 tools (78 cases) makes startup fail every time.
  - † Execution is counted by an instrumented executor, not by the validator's own report.
- **[ADDED] P2.1-A:** all 26 old ids resolve through aliases to the same underlying tool, and the existing tools-tour fixtures pass unchanged.
- A tool without a `verify` declares so explicitly (`verify: none`); silent absence fails the build. † (Needed because A2 forbids `tool.committed` before verification.)

**Evidence:** `reports/p2/P2.1_registry.md`, fuzz log, mutation matrix.

---

### S2 — PermissionEngine, tiers, hooks → **P2.2**

**Produces:** `tier_of` and the gate order, hooks, the NEVER rules, and the **effective-tier table** pinned to today's gates.

Gate order (blueprint §3.5; v1.0 §6.7 is the base and was not available to this plan): hooks run first; deny rules and `NEVER` next; `ASK_ALWAYS` joins the ask rules; the permission mode applies; `FREE` and an unexpired grant satisfy the allow step; anything left falls through to an approval. A hook returning `allow` cannot skip deny or ask.

**Important (call 5 hold):** the blueprint's generic rule `if t.personal: return ASK_ONCE` is **not enabled** for the four personal reads. The engine resolves each real tool from the pinned `effective` table first; the blueprint's target table is stored as `target` and is inactive.

**Acceptance**
- **P2.2:** the gate-table test (every step × risk × tier × hook decision, **≥ 60 cells**, including `NEVER`) passes.
  - † Expected values are hand-written from §3.5, not generated by the code under test.
  - † ASK_ONCE and grant cells are exercised with synthetic test tools, so no real sensitive tool changes tier.
- **[ADDED] P2.2-A:** for each of the 26 tools, effective tier == extracted current gate. The four personal reads (SMS reads, contacts, location, photos) still execute with **0 approval prompts**, exactly as the baseline run; the three ask-every-time tools (`write_clipboard`, `notify`, `open_path`) still prompt every call.

**Evidence:** `reports/p2/P2.2_gate_table.md`, `reports/p2/P2.2-A_effective_tiers.diff` (must be empty).

---

### S3 — Durable approvals and grants → **P2.3**

**Pre-check (frozen layer):** confirm the existing `approvals` table can hold `REF`, `args_hash`, expiry and state. If a column is missing, **stop and file a deviation**; do not alter the schema silently.

**Produces:** persisted approvals replacing `ServerApprovals`' in-memory `Event.wait(180)`; a stable `REF`; binding to `args_hash`; execution-time re-check; `approval.requested` / `approval.resolved` events through the existing `EventStore`; grants (scope = Case + tool family + Site + root; expire at Case close or 4 h idle; revocable). Grants are exercised only by synthetic test tools while call 5 holds all real tools at their current gates.

**Acceptance**
- **P2.3 (a):** sensitive tools run **0** times without an approval on the same `args_hash` (fuzz).
  - † ≥ 1,000 calls, including args mutated after approval, replayed approvals and expired approvals.
- **P2.3 (b):** a pending approval survives `kill -9` and reappears with the **same `REF`**.
  - † ≥ 20 kill runs at randomised points.
- Orphaned approvals (the turn died; `recover()` is Phase 3) follow call 3.
- **PHONE part:** real SMS, call, uninstall and settings gates against throwaway targets → **NOT RUN — phone disconnected** (covered in S8).

**Evidence:** `reports/p2/P2.3_approvals.md`, kill-run log.

---

### S4 — ModelGateway and reusable fake server → **P2.4** (parallel with S1)

**Produces:** token bucket per model (default **30 rpm**, concurrency **2**, both configuration); priority queue (Chief of Staff → active desk loop → Planner → Inspector → Writer → Librarian); 429/5xx handling (`Retry-After`, then 1/3/9 s with jitter, then down the ladder); circuit breaker (opens after 3 consecutive failures per model); 90 s timeout; ledger **computed from `agent_runs`**; `ratelimit.entered/cleared` events carrying a real countdown; NVIDIA host allowlist; a fake server (limit-configurable) that Phases 4 and 8 reuse.

**Acceptance**
- **P2.4:** against a fake server limited to 40 RPM, **200 requests from 4 hats** cause **0 limiter violations** and **0 lost requests**, priority order holds, and `Retry-After` is honoured **± 1 s**.
  - † Hats do not exist until Phase 4: the 4 "hats" are 4 priority classes. Violations are counted **server-side** over a rolling 60 s window.
- **[ADDED] P2.4-A:** breaker opens after exactly 3 consecutive failures; the ladder is walked in order; a request to any non-NVIDIA host is refused.
- **[ADDED] P2.4-B:** grep gate — 0 Claude API clients or hostnames in product code.
- Case caps (Quick 8, Project 24) are enforced in Phase 4 (**P4.2**), not here. The gateway only exposes the computed `calls_used`.

**Evidence:** `reports/p2/P2.4_gateway.md`, server-side request log.

---

### S5 — AgentSession, TurnRunner (non-streaming), Stop → **P2.5, P2.6**

**Produces:** the run loop on top of S1–S4; an `agent_runs` row per run with a **10 s heartbeat**; the **A2 single-transaction commit** (result, verification and undo registration together; `tool.committed` never before verification); Stop for a non-streaming model (mark cancelled, close the HTTP session from a watcher thread, discard the reply; a sent call still counts in the ledger).

**Spike first (before the full runner):** measure how fast an in-flight blocking HTTP call can be aborted from a watcher thread. Blueprint thresholds depend on it.

**Acceptance**
- **P2.5:** Stop flips state **≤ 200 ms**, closes the HTTP session **≤ 500 ms**, and **no `message.completed`** follows; **100 random Stops** leave **0** stuck turns.
  - † The fake server must observe the disconnect. Random Stops land in waiting-model, waiting-approval and mid-tool.
- **P2.6:** chip count == `tool.committed` count across **200 scripted turns**, including failed, denied, cancelled and killed ones.
  - † Compared in both the rendered DOM (via the reducer) and the DB.
- **PHONE:** Stop timings re-measured on a real NVIDIA call → **NOT RUN — phone disconnected** (recommended, not blocking).

**Evidence:** `reports/p2/P2.5_stop.md` (with spike result), `reports/p2/P2.6_chips.md`.

---

### S6 — Integration behind the v2 flag → **P2.8 (PC half)**

**Produces:** `SiteCrew.handle` and the legacy shims running over `TurnRunner` with `engine=v2` on or off; the old UI still selectable.

**Acceptance**
- **P2.8 (PC):** every G0 regression test passes with the v2 engine enabled.
  - † And with it disabled (both runs recorded).
  - † Uploads/extraction/exports match golden outputs byte for byte; express lane, offline regexes, queue/steer and transcript undo match their golden tests.
  - The G0 test that was failing at Phase 0 follows its recorded P1 triage.
- Every behaviour difference is reviewed and recorded.

**Evidence:** `reports/p2/P2.8_pc_regression.md`.

---

### S7 — Calm iOS foundation → **P2.7**

| Sub-step | Produces | Starts |
|---|---|---|
| **S7a** | Tokens (with the §8.4 corrections), Inter (local), icon sprite, the contrast script, a size tripwire (JS ≤ 250 KB gz, fonts ≤ 120 KB, sprite ≤ 20 KB) | **now** |
| **S7b** | Components (§4.2), debug-only **state gallery**, `renderCommitted()` as the only chip/activity renderer, A7 static gate | **now** |
| **S7c** | Chat tab (Lobby; Chats list with **Restore**; composer with Queue/Steer/Stop and the queue tray; typing dots only while a `model.request` is open) and the three approval sheets (Standard, Diff, Critical with slide-to-approve; plain **Approve** button under TalkBack or Tap to confirm) | after S3 + S5 |
| **S7d** | Settings basics: Company, Models and key (existing flow with test call), Appearance, Usage and budget (Case caps, RPM, Lean mode), Trust overview | after S7c |

Office, Brain and Sites tabs are **hidden in release** until built, so the UI shows nothing the events do not prove (principle 10). The old UI stays behind the flag until cut-over.

**Acceptance**
- **P2.7:** the **contrast script passes every token pair in both themes** (≥ 4.5:1 text, ≥ 3:1 meaningful UI; pair list in §8.4, committed with the script).
- **P2.7:** Chat, approval sheets and Settings match §4.1–§4.3 (and the §4.6 behaviour).
- **P2.7:** golden screenshots exist for **2 themes × 2 widths** († widths chosen and recorded, e.g. 360 and 412 px).
  - PC headless screenshots are labelled `PC-PROVISIONAL`; device screenshots are the evidence the blueprint calls for → **NOT RUN — phone disconnected**.
- **P2.7:** Reduce Motion tested (every animation becomes a ≤ 150 ms cross-fade; † automated computed-style check) and **48 px targets** tested († DOM audit, 0 violations).
- **[ADDED] P2.7-A:** replaying the same events twice yields an identical DOM (A5); a static gate fails the build if chip/activity markup exists outside `renderCommitted()` or if "Edit" / "Shell" activity strings exist (A7).

**Status of P2.7 overall:** PC portion can reach PASS; the full gate stays `NOT RUN` until device screenshots exist.

---

### S8 — Device pass (PHONE) — **NOT RUN — phone disconnected**

Everything below needs the phone, `docs/TEST_TARGETS.md` throwaways and (for the first item) the NVIDIA key. Each line has the status `NOT RUN — phone disconnected` until it has evidence.

| Item | Source | Status |
|---|---|---|
| Real NVIDIA chat prompt | §6.1 | NOT RUN |
| Uploads, extraction, exports with golden outputs | §6.1 | NOT RUN |
| Recents and restore: create, kill, relaunch, restore → identical transcript | §6.1 | NOT RUN |
| 26-tool tour vs `adb shell` ground truth (current gates; no new prompts) | §6.1 | NOT RUN |
| Approval gates for SMS, calls, uninstall, settings: refuse without approval, run after it | §6.1 / P2.3 | NOT RUN |
| Offline deterministic answers (airplane mode); queue → kill → relaunch → reconnect delivers once | §6.1 | NOT RUN |
| Used chips real; Queue and Steer; transcript Undo (golden comparison) | §6.1 | NOT RUN |
| G1 device carry-over: P1.1 real data, P1.3 device ≤ 600 ms, P1.4 FTS5 probe | G1 | NOT RUN |
| Golden screenshots on the device, TalkBack check of sheets | P2.7 | NOT RUN |
| Stop timing on a real call | P2.5 | NOT RUN |

---

### S9 — Gate G2 run

**Produces:** an evidence table for P2.1–P2.8 (every row `PASS` + path + measured value, or `NOT RUN` + reason), all §6.1 standing gates, **G0 and G1 re-run**, and the final `reports/DEVIATIONS.md`.

**Acceptance:** no row without evidence. A phase-2 gate with any phone-dependent criterion still `NOT RUN` is **not closed**; the phase does not advance.

---

## 4. PC-only vs phone

| Gate | PC | PHONE |
|---|---|---|
| P2.1 | All | — |
| P2.2 | All (incl. P2.2-A) | — |
| P2.3 | Fuzz and `kill -9` | Real SMS/call/uninstall/settings gates — **NOT RUN** |
| P2.4 | All | — |
| P2.5 | Full measurement on the fake server | Re-measure on a real call — **NOT RUN** |
| P2.6 | All | — |
| P2.7 | Contrast, token, DOM audits, reduced-motion, 48 px, provisional screenshots | Device golden screenshots and TalkBack — **NOT RUN** |
| P2.8 | The G0 tests (flag on and off) | The §6.1 device rows (§S8) — **NOT RUN** |

---

## 5. Standing regression gates (§6.1) — Phase 2 handling

| Preserved behaviour | Test | Where | Status |
|---|---|---|---|
| NVIDIA chat, uploads and extraction, exports | Real model prompt; fixtures with golden outputs | Fixtures PC; real prompt PHONE | Fixtures NOT STARTED; real prompt NOT RUN |
| Recents and restore | Create, kill, relaunch, restore: identical transcript | PC then PHONE | PC NOT STARTED; device NOT RUN |
| 26-tool tour and real device values | Each tool vs `adb shell` ground truth | PHONE | NOT RUN |
| Approval gates for SMS, calls, uninstall, settings | Refuse without approval; run after it | PC (fake) then PHONE | PC NOT STARTED; device NOT RUN |
| Offline deterministic answers; queue flush | Airplane mode; queue, kill, relaunch, reconnect: delivered once | PHONE | NOT RUN |
| Used chips real; Queue and Steer; transcript Undo | Event-to-DOM diff; golden comparisons | PC | NOT STARTED |
| Forbidden behaviours | Grep gates: no Edit/Shell activity strings, no invented-fact fallbacks, no tunnelling code, no `shell=True` | PC | NOT STARTED |
| Evidence rule / Path rules / HQ independence | Phases 5 / 6 / 7 onward | — | Not applicable in Phase 2 |

---

## 6. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Stop abort feasibility.** Aborting a blocking non-streaming HTTP call from a watcher thread inside 500 ms is not guaranteed with a plain `requests` session. | Spike first (S5); measured result in the evidence file; deviation if the threshold cannot be met. |
| 2 | **26 tool ids are not enumerable from the blueprint** (it names about 15 ids and describes several more by behaviour). | S0 extracts from code; S0 fails with any `UNKNOWN`. |
| 3 | **Personal reads stay ungated** (SMS reads, contacts, location, photos) while call 5 is open. The SMS-read → send-SMS injection chain that U1/U2 defends therefore remains as it is today. `send_sms` stays ask-always. | Owner-accepted pending a ruling; recorded in DEVIATIONS; U1/U2 stay OFF by default (call 5c). |
| 4 | **Token deviations.** The blueprint's own tokens fail its own contrast rule in several places (§8.4). | Deviation entries D-P2-01…05; owner sign-off before S7a. |
| 5 | **Frozen-layer pressure.** `approvals` columns, the A2 single-transaction commit and `agent_runs` heartbeats all touch the store. | Pre-check in S3 and S5; any need becomes a deviation, never a silent change. |
| 6 | **G1 device debt.** P1.1 (real data), P1.3 device, P1.4 FTS5 are `NOT RUN`. | Ruling E-1; carried to S8. |
| 7 | **v1.0 not available to this plan** (§6.4, §6.7, §6.8, §6.9, §9.2, §9.5, §10.2 and the 26-tool list). | S0 checklist item; steps citing v1.0 are provisional until read. |
| 8 | **Oracle drift in P2.2.** If expected cells are derived from the code under test, the test proves nothing. | Hand-written expected table, committed separately. |
| 9 | **UI budgets.** Inter ≈ 90 KB, JS ≤ 250 KB gz, sprite ≤ 20 KB. | Size tripwire from S7a. |
| 10 | **Hats do not exist until Phase 4.** P2.4's "4 hats" and the `agent_runs` ids need stand-ins. | Priority classes (S4); call 4 for ids. |

---

## 7. Open owner decisions (summary)

| ID | Decision | Status | Default if unanswered |
|---|---|---|---|
| E-1 | Start S1–S6 before G1's device criteria are closed? | OPEN | Hold (E-1a) |
| Call 1 | `write_file` tier before Workspaces exist | OPEN | Keep current gate |
| Call 2 | Synthetic Case per turn | OPEN | Yes (rows only, no Case UI) |
| Call 3 | Approvals orphaned by a kill before `recover()` | OPEN | Reappear as "interrupted"; no Approve button |
| Call 4 | `agent_runs` ids for legacy roles | OPEN | §1.3 successors |
| Call 5 | **BOSS CALL #5** (personal reads → ask-once) | **OPEN / UNAPPROVED** | **Not applied; current gates preserved** |

---

## 8. S0 PACKAGE

### 8.1 Current tool inventory

**Honest limit:** the blueprint refers to "all 26 tools" but never lists them. The rows below are the **slots the blueprint names or describes**, with the current gate **only where the blueprint states it**. Everything else says `EXTRACT`: read it from the code. The inventory is complete only when it has **26 real rows with real ids and no `EXTRACT`**.

**Columns:** *Current gate* = today's behaviour (preserved). *§3.5 target* = what the blueprint would make it (**INACTIVE in Phase 2**). *Phase 2 effective* = what the engine enforces.

| # | Slot (id as named in blueprint, else `?`) | Blueprint cite | Current gate | §3.5 target (inactive) | Phase 2 effective |
|---|---|---|---|---|---|
| 1 | `list_files` (alias → `fs.list`) | §3.2 | EXTRACT | Free in readable roots | = current |
| 2 | `read_file` (alias → `fs.read`) | §3.2 | EXTRACT | Free in readable roots | = current |
| 3 | `find_files` (alias → `fs.find`) | §3.2 | EXTRACT | Free | = current |
| 4 | `storage_help` (alias → `fs.usage`) | §3.2 | EXTRACT | Free | = current |
| 5 | `write_file` (alias → `fs.write`) | §3.2, §3.5 | EXTRACT | Ask once inside a Workspace, else ask always | = current (**call 1**) |
| 6 | `write_clipboard` | §3.5 | **Ask every time** (stated) | Ask once | **= current** (held; see call 5b) |
| 7 | `notify` | §3.5 | **Ask every time** (stated) | Ask once | **= current** (held; call 5b) |
| 8 | `open_path` | §3.5 | **Ask every time** (stated) | Ask once | **= current** (held; call 5b) |
| 9 | `open_app` (alias → `apps.open`) | §3.4 | EXTRACT | Ask once | = current |
| 10 | `send_sms` | §3.5, §4.7 G | **Ask always** (stated: "stays ask-always") | Ask always + slide to approve | = current |
| 11 | place-a-call tool, id `?` | §3.5 | **Ask always** (stated) | Ask always + slide | = current |
| 12 | uninstall tool, id `?` | §3.5 | **Ask always** (stated) | Ask always + slide | = current |
| 13 | settings-change tool(s), id `?` | §3.3, §3.5 | **Ask always** (stated) | Ask always (catalogued) | = current |
| 14 | `get_settings` | §3.3 | EXTRACT | Free | = current |
| 15 | `device_info` | §3.3 | EXTRACT | Free | = current |
| 16 | `battery` | §3.3 | EXTRACT | Free | = current |
| 17 | SMS read, id `?` | §3.5, BOSS CALL #5 | **Ungated** (stated) | Ask once | **Ungated = current** (**call 5, UNAPPROVED**) |
| 18 | contacts, id `?` | §3.5, BOSS CALL #5 | **Ungated** (stated) | Ask once | **Ungated = current** (call 5) |
| 19 | location, id `?` | §3.5, BOSS CALL #5 | **Ungated** (stated) | Ask once | **Ungated = current** (call 5) |
| 20 | photos, id `?` | BOSS CALL #5 | **Ungated** (stated) | Ask once | **Ungated = current** (call 5) |
| 21 | camera, id `?` | §3.5 | EXTRACT | Ask once | = current |
| 22 | clipboard read, id `?` | §3.5 | EXTRACT | Ask once | = current |
| 23 | `speak` | §3.5 | EXTRACT | Free | = current |
| 24 | `vibrate` | §3.5 | EXTRACT | Free | = current |
| 25 | add-reminder tool (`reminder.*`), id `?` | §1.2, §3.5 | EXTRACT | Ask once | = current |
| 26 | apps list / info tools, ids `?` (`apps.list`, `apps.info`) | §3.4 | EXTRACT | Free | = current |
| — | `export.*` (Writer scope, "existing") | §1.2 | EXTRACT | — | = current |

The 26 count is the owner's. This table has 26 slots plus `export.*`; extraction may merge or split slots (for example camera/photos, or the separate settings-change tools). **Extraction decides the real rows.**

**Not in the Phase 2 inventory unless extraction shows they exist today:** `net.download`, `apps.install` (blueprint §3.4, Phase 6) and every `fs.*` tool beyond the legacy aliases.

**Extraction checklist (S0):** (1) the tool registry module; (2) the `ServerApprovals` gating list; (3) the tools-tour fixtures; (4) any tool exposed through pyjnius to the WebView. For each row, record: real id, aliases, family, `risk`, `reversible`, personal data? (Y/N), current gate, code location (file:line), `verify`/`inverse` present (Y/N).

### 8.2 Approval-tier table (effective vs blueprint target)

Tier machinery in S2/S3 supports all four blueprint tiers. What is **active** for real tools is only what exists today.

| Action class | Current gate (preserved) | §3.5 target | Phase 2 effective | Approval status of the *change* |
|---|---|---|---|---|
| SMS read, contacts, location, photos | **Ungated** | Ask once | **Ungated** | **OPEN / UNAPPROVED (BOSS CALL #5)** |
| `write_clipboard`, `notify`, `open_path` | Ask every time | Ask once | **Ask every time** | Held with call 5 — not covered by an owner-approved call (call 5b) |
| `open_app`, add reminder, camera, clipboard read | EXTRACT | Ask once | = current | Not changed in Phase 2 |
| `write_file` | EXTRACT | Ask once inside a Workspace, else always | = current | Call 1; Workspaces arrive in Phase 6 |
| Send SMS, place a call | Ask always | Ask always + slide | = current | No change (slide-to-approve UI is S7c; it adds friction, not a tier change) |
| Uninstall | Ask always | Ask always + slide | = current | No change |
| Change a phone setting | Ask always | Ask always | = current | No change |
| Free reads (device info, battery, settings reads, app list/info, speak, vibrate) | EXTRACT / Free | Free | = current | No change |
| `fs.*` (new), `net.download`, `apps.install`, HQ | — | per §3.5 | **Not active** | Phases 6–7 |
| NEVER: secret stores, SiteCrew's own key, protected paths, other apps' data, shell/sudo, bypassing an approval | Not applicable today | NEVER | **NEVER rules loaded and tested** (P2.2 cells) | Additive; no existing tool is affected |

**Mapping rule:** today's "ask every time" is the engine's `ASK_ALWAYS` tier (each call needs its own approval, bound to the arguments hash). No real tool is in `ASK_ONCE` during Phase 2; grants are exercised only with synthetic test tools.

### 8.3 The five boss calls

**Call 1 — `write_file` tier before Workspaces exist.**
*Question:* the blueprint makes `write_file` ask-once only inside a Workspace, but Workspaces arrive in Phase 6.
*Options:* (a) keep today's gate until Phase 6; (b) introduce a stand-in Workspace now.
*Recommendation:* (a). *Status:* OPEN. *Default:* keep current gate.

**Call 2 — Case concept before Phase 4.**
*Question:* grants and the ledger need a Case; Cases arrive in Phase 4.
*Options:* (a) populate one synthetic Case row per turn (`kind` chat/quick) in the existing `cases` table and fill `case_id`; (b) leave `case_id` null.
*Recommendation:* (a), with **no Case UI** and no new schema. *Status:* OPEN. *Default:* (a).

**Call 3 — Approvals orphaned by a kill, before `recover()` (P3.2).**
*Question:* P2.3 only requires reappearance with the same `REF`. What can the owner do with it?
*Options:* (a) it reappears marked "interrupted" with **no Approve button** (Dismiss only); the sensitive call must be re-proposed; (b) Approve re-runs the call.
*Recommendation:* (a) — a sensitive effect must never be silently repeated (blueprint §4.7 H). *Status:* OPEN. *Default:* (a).

**Call 4 — `agent_runs` ids for the legacy roles.**
*Question:* the ledger is computed from `agent_runs`, but hats do not exist yet.
*Options:* (a) use the §1.3 successors (Foreman → `chief_of_staff`, Operator → a legacy desk id, Inspector → `inspector`, Laborer → `writer`, Engineer → `planner`); (b) one generic `legacy` id.
*Recommendation:* (a) so priority order and per-hat figures work from day one. *Status:* OPEN. *Default:* (a).

**Call 5 — BOSS CALL #5: SMS reads, contacts, location, photos become ask-once. OPEN / UNAPPROVED.**
*Owner ruling in force:* **not approved.** Current sensitive-action gates are preserved.
- **5a — Hold.** The four personal reads stay **ungated exactly as today**. The engine must not apply `personal → ASK_ONCE`. Test: **P2.2-A** (zero approval prompts, identical to the baseline run, before and after the engine switch).
- **5b — Related held change.** The blueprint's move of `write_clipboard`, `notify` and `open_path` from ask-every-time to ask-once is not covered by an owner-approved call either; it is held at **ask every time**. If the owner approves it separately, it becomes a one-line tier-table change plus a test update.
- **5c — U1/U2 for SMS (additive protections).** The blueprint schedules the untrusted-content rules for Phase 6. Wrapping SMS text in `<untrusted-data>` (U1) and forcing ask-always when an SMS body is derived from untrusted text (U2) would change model inputs and behaviour. **Default OFF**; pulling them forward is a separate owner decision.
- *If the owner later approves #5:* change `effective` to `target` for those four rows only, re-run P2.2-A (expected diff = exactly those four rows) and the 26-tool tour with grants pre-approved.
*Status:* **OPEN / UNAPPROVED.** *Default:* not applied.

### 8.4 Design-token fixes with recalculated contrast ratios

**Why:** P2.7 requires the contrast script to pass every token pair in both themes. Recalculating the blueprint's own §4.1 tokens (WCAG 2.x relative luminance) shows several fail **by construction**. These are deviations from the blueprint and must be logged (D-P2-01…05) before S7a.

**Method and assumptions**
- Surfaces: light — `bg.base` #FFFFFF, `bg.grouped` #F2F2F7, `bg.cell` #FFFFFF, `bg.elevated` #FFFFFF; dark — #000000, #000000, #1C1C1E, #2C2C2E.
- Composites: `fill.control` = rgba(118,118,128, .12 light / .24 dark) over each surface; tinted capsule and selected tab pill = accent at 12 % over each surface.
- Thresholds: text ≥ 4.5:1; meaningful UI ≥ 3:1. The ratios below are the **worst case across the surfaces** where the token can appear.

**A. Text tokens**

| Token | Blueprint value | Worst case before | Proposed value | Worst case after |
|---|---|---|---|---|
| Light `accent.text` | #0062CC | 4.499 on tinted capsule over `bg.grouped` (fails by 0.001) | **#0061CA** | 4.57 (same pair) |
| Light `label.2` | #6C6C70 | **4.09** on `fill.control` over `bg.grouped` | **#646468** | 4.61 (same pair); 5.89 on `bg.base`; 5.28 on `bg.grouped` |
| Dark `accent.text` | #0A84FF | **2.92** on `fill.control` over `bg.elevated` (3.82 on plain `bg.elevated`) | **#5DAEFF** | 4.54 on `fill.control` over `bg.elevated`; 5.95 on `bg.elevated`; 8.96 on `bg.base` |
| Dark `label.2` | #98989D | **3.71** on `fill.control` over `bg.elevated` | **#AAAAAE** | 4.60 (same pair); 6.02 on `bg.elevated` |
| Dark danger **text** | #FF453A | **4.09** on `bg.elevated` | **new `danger.text` dark #FF6B62** | 5.00 on `bg.elevated`; 7.53 on `bg.base` |

The blueprint's other text tokens pass as written: light `ok.text` #1F7A35, `warn.text` #9A5B00, `danger.text` #D70015 (≥ 4.83 on every surface); dark `ok` #30D158 and `warn` #FF9F0A (≥ 6.78 on every surface); light and dark `label`.

**B. Filled-button labels (white text)**

| Token | Blueprint value | Before | Proposed | After |
|---|---|---|---|---|
| Light destructive fill | `danger` #FF3B30 | white label **3.55** | **new `danger.fill` light #D70015** | 5.38 |
| Dark destructive fill | `danger` #FF453A | white label **3.41** | **new `danger.fill` dark #D70015** | 5.38 |
| Light `accent.fill` | #0062CC | 5.80 | unchanged | 5.80 |
| Dark `accent.fill` | #0A6CFF | 4.56 | unchanged (thin margin) | 4.56 |

**C. Meaningful UI colours (icons, switch track), light mode**

| Token | Blueprint value | Before (vs `bg.base`) | Proposed | After |
|---|---|---|---|---|
| Switch track / success icon (`ok`) | #34C759 | **2.22** (white knob 2.22) | **new `ok.ui` light #248A3D** | 4.40 on `bg.base`, 3.94 on `bg.grouped`; white knob 4.40 |
| Waiting / stale icon (`warn`) | #FF9500 | **2.20** | **new `warn.ui` light #C93400** | 5.28 on `bg.base`, 4.73 on `bg.grouped` |
| Accent icons | #007AFF | 4.02 | unchanged | 3.60 on `bg.grouped` (≥ 3) |
| Danger icons | #FF3B30 | 3.55 | unchanged | 3.18 on `bg.grouped` (≥ 3) |

Dark icons pass as written: accent #0A84FF ≥ 3.82, danger #FF453A ≥ 4.09, `ok`/`warn` ≥ 6.78.

**D. Agent avatar glyphs (the glyph carries meaning, so ≥ 3:1)**

| Hat | Fill | White glyph (blueprint) | Proposed glyph | After |
|---|---|---|---|---|
| Chief of Staff | indigo #5856D6 | 5.65 | white | 5.65 |
| Planner | teal #30B0C7 | **2.57** | **black #000000** | 8.16 |
| Field Lead | orange #FF9500 | **2.20** | **black** | 9.55 |
| Files Desk | blue #007AFF | 4.02 | white | 4.02 |
| Settings Desk | grey #8E8E93 | 3.26 | white | 3.26 (thin) |
| Apps Desk | brown #A2845E | 3.50 | white | 3.50 |
| Comms Desk | green #34C759 | **2.22** | **black** | 9.46 |
| Concierge | purple #AF52DE | 4.13 | white | 4.13 |
| Writer | pink #FF2D55 | 3.65 | white | 3.65 |
| Inspector | yellow #FFCC00 | 1.51 (blueprint already uses a dark glyph) | black | 13.89 |
| Librarian | mint #00C7BE | **2.12** | **black** | 9.91 |

The rule extends the blueprint's own Inspector exception: a per-hat `avatar.glyph` token; fills stay as specified.

**E. Committed pair list and exemptions (for the contrast script)**

*Checked pairs:* `label`, `label.2`, `accent.text` on all four surfaces and on `fill.control` over each; `accent.text` on the tinted capsule over each surface; `ok.text`, `warn.text`, `danger.text` on the four surfaces; white on `accent.fill` and on `danger.fill`; `accent`, `ok.ui`, `warn.ui`, `danger` icons on the four surfaces (≥ 3:1); white knob on `ok.ui` track; every avatar glyph on its fill (≥ 3:1).

*Exemptions (need owner sign-off):*

| # | Exempt | Reason | Risk |
|---|---|---|---|
| E1 | `label.3` (light #8E8E93 is 3.26:1 on `bg.base`; dark #636366 is 3.51:1 on black but 2.33:1 on `bg.elevated`) | Blueprint limits it to placeholders and disabled text | Placeholder text is below 4.5:1; restrict by lint to those two uses |
| E2 | `separator` hairlines | Decorative, not meaningful UI | None |
| E3 | Avatar fill against the page surface | Colour is decoration; the title and glyph carry the meaning (§4.1) | None |
| E4 | Filled-button fill against its surface (for example dark `danger.fill` #D70015 on `bg.elevated` is 2.59:1) | The label's contrast (≥ 4.5:1) identifies the control | Button edge is low-contrast on elevated dark surfaces; revisit if reviewers object |

**Deviation entries to log (`reports/DEVIATIONS.md`):**
- **D-P2-01** — Light `label.2` #6C6C70 → #646468 and dark `label.2` #98989D → #AAAAAE (composite surfaces failed 4.5:1).
- **D-P2-02** — Light `accent.text` #0062CC → #0061CA; dark `accent.text` #0A84FF → #5DAEFF.
- **D-P2-03** — New `danger.fill` (#D70015, both themes) and dark `danger.text` #FF6B62.
- **D-P2-04** — New light `ok.ui` #248A3D and `warn.ui` #C93400.
- **D-P2-05** — Per-hat `avatar.glyph` (black on teal, orange, green, mint, yellow).

Each entry: the reason is the failing measurement above; the evidence is the contrast script output (`reports/p2/P2.7_contrast.md`), which must exit non-zero if any committed pair falls below its threshold.

---

## 9. S0 REVIEW CHECKLIST

Mark each box with evidence (file path) before S1 or S4 starts.

**Scope and boundaries**
- [ ] Frozen-layer inventory (§0) confirmed against the repo: tables, `EventStore`, `/api/v2/poll`, shims, import. Any Phase 2 need that touches them has a deviation entry.
- [ ] v1.0 read: §6.4, §6.7, §6.8, §6.9, §9.2, §9.5, §10.2, and the location of the 26-tool list.
- [ ] Ruling **E-1** recorded (hold S1–S6, or start with G1 device debt carried).
- [ ] G1 status filled in (§1): P1.1, P1.4, P1.5 known; the 39th G0 test has a fix or recorded reason.

**Inventory and tiers**
- [ ] Inventory has **26 real rows**, real ids, **0 `EXTRACT`/`UNKNOWN`**, each with file:line.
- [ ] Effective-tier table equals the extracted current gate for all 26 (P2.2-A diff is empty).
- [ ] The four personal reads (SMS reads, contacts, location, photos) are **ungated exactly as today**, and no code path applies `ASK_ONCE` to them.
- [ ] `write_clipboard`, `notify`, `open_path` remain ask-every-time (call 5b).
- [ ] Every tool declares `verify` or `verify: none`.

**Owner decisions**
- [ ] Call 1 answered or default recorded.
- [ ] Call 2 answered or default recorded.
- [ ] Call 3 answered or default recorded.
- [ ] Call 4 answered or default recorded.
- [ ] **Call 5 recorded as OPEN / UNAPPROVED**, with 5a, 5b and 5c stated; U1/U2 default OFF.

**Design tokens**
- [ ] D-P2-01…05 logged in `reports/DEVIATIONS.md`.
- [ ] §8.4 ratios re-run with the real token file; the committed pair list matches §8.4 E.
- [ ] Exemptions E1–E4 signed off or rejected.

**Evidence and safety**
- [ ] Evidence folder `reports/p2/` and file names agreed.
- [ ] Grep gates agreed: no Claude API client or hostname, no `shell=True`, no Edit/Shell activity strings, no invented-fact fallbacks, no tunnelling code.
- [ ] `docs/TEST_TARGETS.md` lists the throwaways S8 needs (SMS recipient, call target, throwaway APK to uninstall, a settings value safe to change).
- [ ] Every phone-dependent row reads `NOT RUN — phone disconnected`; no `PASS` is claimed without a file path and a measured value.
