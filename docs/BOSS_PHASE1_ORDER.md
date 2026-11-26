# BOSS PHASE 1 ORDER — verbatim ruling (Laa account, chat 9c316b33-9c81-4ea2-a0f8-cb67ec479d7e, Nov 26 2026)

Received after all five blueprint sections were pasted (Phase 1 scope, v1.0 DDL,
migration 002 storage DDL, event envelope v2, Brain DDL). This is the binding
execution order for Phase 1. Where it conflicts with earlier text, THIS wins.

## Sequencing (approved with corrections)

- Dual-write behind `SITECREW_STORE=1` with legacy routes live is the right shape.
- [BOSS CALL] The flag defaults OFF, and with it off the store modules are never
  imported (lazy import inside the flag check). A bad sqlite build, missing FTS5, or
  broken migration must not touch the shipped app or the 38 tests.
- [BOSS CALL] The suite runs in BOTH modes. Flag off must show 38/39 unchanged. Flag on
  must show the same 38 plus the new store tests.
- Legacy stays the source of truth while mirroring: write legacy first, then mirror.
  Mirror failures are caught (never break a request) but counted in `mirror_errors`,
  and tests assert it is 0. Mirror from the single function where legacy appends to
  queue.json and the transcript, not from scattered call sites.
- The import is idempotent and records a marker row in the migrations table. Backups
  are copies, never moves, named `*.pre-v2.bak`, so turning the flag off is a full
  rollback. Checkpoint the WAL (TRUNCATE) before copying the DB, and copy -wal and
  -shm with it. Add `*.db`, `*-wal`, `*-shm` and `*.pre-v2.bak` to .gitignore.
- [BOSS CALL] The flip is the LAST task of Phase 1, not a follow-up. Order: mirror,
  then a parity script (legacy vs store, 0 diffs across the 38-test run and the device
  tour), then flip, then G1.
- `/api/v2/poll` is purely additive, ships in the mirror stage, reads from the store
  from day one. threading.Condition notify on append plus a 2 s safety re-check of max
  seq. Add a test where a parked poll and a normal request run concurrently.

## P1.2 kill test (approved with additions)

- One DB across all cycles: the writer reopens, resumes, gets killed. At the end seq
  must be exactly 1..N, no gaps or dupes across all 200 kills.
- Writer sends ACK seq to the parent over a pipe only after commit returns. After each
  reopen: `last_acked <= max(seq) <= last_acked + 1`.
- Join states to events in both directions; 0 orphans each way. integrity_check ok;
  foreign_key_check empty.
- Seeded RNG (seed logged), microsecond delays, writer flat out, Popen.kill().
- [BOSS CALL] Deterministic injection via `SITECREW_FAULT=after_state|after_event|
  before_commit|after_commit_before_ack` (writer SIGKILLs itself at that point), 25
  cycles per point. Report must show the in-flight transaction rolled back in the
  first three modes.
- [BOSS CALL] WAL with synchronous=NORMAL, busy_timeout set, BEGIN IMMEDIATE under one
  process-wide writer lock. Power loss is out of scope and the report says so.
- Atomicity claim covers only the DB's own states+events (one transaction). Legacy
  queue.json is not in that transaction in mirror mode — parity script owns that.
- [BOSS CALL] Device leg P1.2-D: 20 cycles on the phone. Debug append loop, logcat ACK
  marker (event-driven), `adb shell kill -9`, relaunch, same oracle. No screenshots.
  DB in app-private internal storage only.
- Evidence: JSON report under tests/evidence/ (seed, per-mode kill counts, every check
  result). PASS = report exists and every check is 0 or ok.

## Blueprint transport ruling

- §2.2 Brain DDL was sent and ruled on. §5 envelope sent and ruled on.

## FTS5 / search

- FTS5 probe on the PHONE's own SQLite: record sqlite version, PRAGMA journal_mode=WAL
  returns wal on device, FTS5 compiled in. If missing → brain_terms fallback, G1 not
  blocked.
- P1.4: fresh tests, identical fixtures, both backends. "Byte-compared" = each result
  set serialized canonically and sorted by entry id. Ranked order compared only on
  fixtures with one unambiguous winner.

## Failed G0 device test triage

- Capture the real error class (DNS/timeout/HTTP) from logcat or server log, not
  screenshots. Confirm the app showed its plain-language no-internet error. Re-run with
  a connectivity preflight recorded in the evidence. G0 stays 38/39 + 1 open until the
  re-run. Phase 1 proceeds (failure unrelated) but the item closes before G1 signs.

## Rulings from the schema + envelope sections

- [BOSS CALL] One connect() factory sets foreign_keys=ON, synchronous=NORMAL,
  busy_timeout on EVERY connection (poll threads, importer, kill harness, tests). A
  test asserts the pragmas on every connection.
- [BOSS CALL] Add `meta(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)` for import
  markers and source sha256s. user_version 1 after 001, 2 after 002.
- Before touching any route: capture golden responses from every legacy GET on the
  fixtures; byte-compare after the flip (real P1.5 evidence).
- P1.2 oracle: message scenario = one tx inserts messages row id k-i + message.created
  event with events.id k-i. Tool scenario = one tx inserts tool_calls row with
  committed_seq = allocated seq + its event. Check both directions per chat; seq
  contiguous per chat.
- Migration 002 tables created EMPTY; new columns NULL in every row; no seeding until
  the roster phase; accidental shim write fails the build.
- Invariants: cases.turn_id and turns.case_id must agree wherever both are set.
- [BOSS CALL] Office stream is literal 'office'; retract `$office`. new_id('c') is the
  only source of chat ids. Chat creation and the importer reject 'office' and any id
  containing ':' or ','. check_invariants() asserts no chats row has id 'office'.
  Legacy ids outside ^[A-Za-z0-9_-]{1,64}$ remapped deterministically, mapping in meta,
  applied to posted recents.
- [BOSS CALL] Import events in system.* family: system.import.chat per chat stream
  (message_count, queue_count, source_sha256); one system.import.completed in the
  office stream (totals, per-source sha256). Yield to the v1.0 catalog if equivalent.
- [BOSS CALL] One EVENT_TYPES registry: explicit names for the three new families,
  prefixes for v1.0 ones. SITECREW_STRICT=1 (set in every test run) raises on unknown;
  production logs, counts, writes. Phase 1 declares office/brain/site families but
  emits none; after the 38 tests and import, assert every event type is a v1.0 family
  or system.import.*.
- Envelope mapping: v is a constant 2 on output (no column). data → data_json through
  one dumps(); one to_envelope(row). Test: append the §5.3 example verbatim, read back
  equal. tool.committed must match tool_calls.committed_seq via turn→chat — invariants
  checker. Duplicated paths/tier/grant_id in data are a snapshot; tool_calls row is
  truth.
- [BOSS CALL] IDs: new_id(prefix) = prefix + 16 random hex chars. Add
  `CREATE UNIQUE INDEX idx_events_id ON events(id)`.
- Poll contract: `GET /api/v2/poll?streams=c_x:318,office:12&wait=25` (cursor = last
  applied seq; wait capped 25) → `{"events":[envelopes],"heads":{...},"more":false,
  "reset":[]}`. Grouped by stream in request order, ascending seq, ≤200 per response;
  more:true → client re-polls immediately. Any stream past cursor returns immediately;
  else park up to wait; timeout → empty events + heads. Cursor above head → reset;
  client reloads that stream from head. Client applies idempotently by (stream, seq),
  re-polls from last contiguous seq on gap.
- [BOSS CALL] Phase 1 ships poll.js (vanilla) + flag-gated test page rendering a
  transcript purely from events (P1.3 DOM comparison there). Production chat UI does
  NOT switch to poll in Phase 1 — its only change is the one-time recents POST.
- Outstanding paste: v1.0 event catalog (concrete types inside turn.*/message.*/model.*/
  tool.*/gate.decision/approval.*/undo.*/system.*). Until it arrives, build the registry
  with those families as prefixes.

## Build order (begin now)

0. Parallel prep: real-data backup + RAM transcript dump, on-device probe, failing
   test's ID.
1. Foundation: connect() factory, new_id, dumps, migrations 001+002,
   ensure_search_backend. Evidence: migration round trip + 50 kill cycles.
2. EventStore: head cache per stream, writer lock, check_invariants(), P1.2 harness —
   4 scenarios, 200 random + 100 injected kills, 20-cycle device leg.
3. Poll: endpoint, poll.js, test page, P1.3 latency + reconnect runs.
4. Search: SearchIndex both backends, P1.4 suite, reports/fts5_probe.json.
5. Import: importer, then P1.1 on all three corpora.
6. Mirror, then flip: mirror behind flag → parity script + golden legacy GETs → shims
   with queue.json write-through. P1.5 in all three configurations.
7. G1 pack: one report per P1.x in reports/, failing test triaged, nothing committed.

**After steps 2 (EventStore/kill), 3 (poll), and 6/7 (mirror/flip): send the report
summary — not the code — and the boss rules PASS or FAIL on the evidence.**
