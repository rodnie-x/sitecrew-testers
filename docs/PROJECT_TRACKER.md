# PROJECT TRACKER — SiteCrew AI "Real Office" build
Updated: Nov 28, 2026. Phone is unplugged: every phone-only item is NOT RUN.
All three Chrome bosses are connected. Chrome-2 ruled the c627d50 tooling
SAFE TO RUN for PC/tooling scope (device still NOT RUN); two final robustness
nits were applied locally. Chrome-1 delivered the revised phone-local CPU diag
spec (docs/DEVICE_CPU_DIAG_SPEC.md). Chrome-3 delivered the Phase 2/S0 draft;
owner decisions remain OPEN. In-app Claude remains logged out (owner action).

## ✅ ACCOMPLISHED (evidence-backed, boss-ruled)

### Phase 0 — audit
- 38/39 PC tests + device checks; ARCHITECTURE_AS_IS / ASSUMPTIONS / TEST_TARGETS written.

### P1.2 — store, events, kill-safety  ✅ CLOSED (boss-signed)
- SQLite WAL store, migrations 001+002, EventStore (seq + state in ONE
  BEGIN IMMEDIATE, failpoint hooks, query_only reads, invariants, poll
  contract), ProcLock. 25/25 store+events tests.
- PC leg SIGNED: 300 kills, 25,294 events, identity holds, 99% in-flight,
  75/75 rollback asserts, WAL 4.2 MB.
- Migration kills 50/50 (45 random consistent + 5 deterministic after-commit
  at v2 — clean v2 state survives a kill).
- Device leg 30/30 PASS on the physical TECNO LH7n (Android 14) — sign-off
  eligible: run-as-kill-9 ×30, ACK side-file oracle (logcat tail-deltas {0,1}),
  all 12 named checks green, APK pulled + matched, single 127.0.0.1:8080
  listener verified via /proc/net/tcp+tcp6.

### P1.3 PC half — ✅ PASS (multiple reruns)
- /api/v2/poll to the boss contract: wait≤25, ≤200 events + more, reset[],
  cid supersede, 1s park slices, client liveness (select+MSG_PEEK),
  X-Liveness-Path header. Slot-freed test. Storm at wait=25: http_503 = 0.
- Latency: p50 9–15 ms, p95 13–131 ms (gate ≤300 ms), 200/200, 0 misses.
- Reconnect: 100–103 forced at wait=25, render-state hashes identical to the
  uninterrupted reference, 0 dupes, 0 gaps.
- All 4 boss-required tests exist (parked+concurrent, gap re-poll, idempotent
  apply, reset).

### Privacy + integrity (boss hold — executed)
- Public repo cleaned: step-1 backup content removed from tree AND history
  (filter-repo ×2 — first purge left untracked files, caught + re-purged).
- Real backup in private encrypted custody (Fernet AES; password with owner).
- Secret scan clean (no nvapi- hits — no rotation). Device serial hashed.
- D7 CLOSED (G0 reconcile green on the phone) + D8 CLOSED (backup hashes).

### Tooling built this round
- In-page WebView probe (debug-gated page: real poll.js, page-fetched poll.js
  SHA-256, in-page X-Liveness-Path, 200 performance.now samples, results
  endpoint) — PC-smoked.
- CDP harness (scripts/webview_cdp.py): devtools socket discovery, forward,
  attach, navigate/evaluate/reload, in-page transcript SHA-256.
- Idle-park CPU sampler v3 (in-app /proc/self/stat via debug endpoint —
  run-as /proc reads are denied on Android 14).
- SQLite floor 3.35.5 lint (no STRICT/->/->>/unixepoch) + JSON probe in facts.
- Deploy verification chain: pre/post hashes on every hot-deploy.

## 🔄 IN FLIGHT / WAITING
- Idle-park CPU: **NOT RUN — phone unplugged**. The last run stopped after the
  decay window on a sampler IndexError; it produced no final CPU verdict. The
  sampler now keeps full 4-tuples, saves decay samples incrementally, samples
  p95 every 5 s, checks pid/errors/negative deltas, gates every parker, rereads
  head before parking, and restores USB stay-awake in `finally`.
- Code-review boss: **SAFE TO RUN (PC/tooling scope) at c627d50**. Its final
  ticks1 guard and optional-websocket portability nits are applied locally;
  physical-device checks remain NOT RUN. Next review: P1.1 importer + P1.4 search.
- Phase-2 planning boss: execution order and risks received (including token
  contrast failures and the Stop-abort feasibility spike). Archive the full
  order when Chrome-3 reconnects, then review S0 before implementation.
- Chrome-1 device ruling + Chrome-3 follow-up: **WAITING — browsers
  disconnected**. Do not describe either as actively working until verified.

## ❌ MISSING / TODO (in order)

1. **Idle-park CPU evidence** — NOT RUN until the phone reconnects on a stable
   cable. Required: 12-minute post-restart decay, zero-client baseline, two-client
   park, per-wake/per-interval figures and parker validity. Earlier 67% readings
   are invalid because the sampler could refetch from cursor 0 and spin on errors;
   that defect is a credible explanation, not a measured root-cause observation.
   No CPU improvement or slice verdict may be claimed yet.
2. **1468ms PC outlier**: diagnostic rerun latency was clean (200/200, p50 7ms,
   p95 13ms, max 25ms, all wake reasons=events), so the old outlier did not reproduce
   and is not root-caused. That combined diagnostic run FAILED reconnect count
   (21 <100; DOM/cursors/loss passed) and is preserved separately, not as a new PASS.
   sample (missed-notify → 1s-belt wake is the standing hypothesis).
3. **P1.3 device half** (GO given; the gate):
   - hot-deploy verification (last device-verified trio: api_v2 ccec4f56,
     app.py 2a99374e, poll.js 0b8554f5; app.py now has a newer local run-id
     fix — redeploy and reverify the FINAL tree at run time),
   - in-page probe run (200 samples, in-page liveness, page-fetched poll.js
     hash; one run DETACHED from CDP),
   - CDP transcript hash: uninterrupted vs chaos-100 + ≥10 reloads at wait=25,
   - reconciled counts (requests = 2xx + superseded + forced + in-flight),
   - device report → boss ruling → P1.3 device-half PASS.
4. **G1 pack** (design received; execute after the device half): release APK
   from the final tree, leg rerun on it (covers the am force-stop path),
   release checks (probe + proc-stat 404, flag off), evidence format.
5. **Phase 1 leftovers**: P1.4 PC increment implemented (SearchIndex FTS5 +
   forced brain_terms; 8/8 search tests and 37/37 related tests PASS;
   `reports/p14_search_pc.json`; boss review still required). P1.1 legacy chat-id
   mapping now commits atomically with import state+event; synthetic smoke PASS
   12/12 chats, 41/41 messages, 12/12 queued including rerun/partial recovery
   (`reports/p11_importer_smoke.json`). The three real corpora and device import
   remain NOT RUN. Review boss then ruled FIX FIRST: Unicode/BM25 parity and backend
   rebuild fixes are in progress; imported-message invariants/ID mapping and an
   overlap policy are also being resolved before real corpora. P1.5 architecture is
   ruled (docs/P15_DESIGN_RULING.md); implementation remains TODO pending these
   prerequisites. Earlier 50/50 PASS is historical for that revision, not current
   review sign-off.
6. **Phase 2** (order being drafted by the planning boss): engine core,
   ModelGateway (NVIDIA-only allowlist), tool contract + tiers + approval
   gates, Calm iOS shell — then the Phase 2 gate run.
7. **Phases 3–8**: durability/service/queue/steer/undo (P3), Office hats/
   Cases/tasks (P4), Brain (P5), device control v2 (P6), HQ laptop (P7),
   polish/RC (P8).
8. **Small items**: idle_park_cpu.py into the frozen hash set (edited after
   its run); the in-app claude.ai session needs the owner to log back in;
   hot-deploy lesson recorded — push EVERY changed file and hash-verify the
   whole set.

## 🔑 KEY FACTS
- Phone: TECNO LH7n, Android 14 (SDK 34), SQLite 3.35.5 (no FTS5), serial
  hashed in reports. adb serial 103953738Q003608.
- Frozen hashes live in tests/evidence/p13_sha256s.txt (refreshed to the
  working tree at every deploy; device verified against it).
- Evidence repo: github.com/rodnie-x/sitecrew-testers (PUBLIC — no raw device
  data, no secrets; raw device data only in private encrypted custody).
- Boss channels: Chrome-1 chat 4e7af78f (device half), Chrome-2 (review),
  Chrome-3 (Phase 2), in-app Kerwin Noha (logged out; wall was 1:20 AM).
