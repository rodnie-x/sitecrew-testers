# P1.5 mirror/flip design ruling

Source: Chrome-1 device boss, Nov 28 2026. This is an architecture ruling, not a
P1.5 PASS. Physical device and device tour: **NOT RUN** (phone unplugged).

## Approved shape

- **Owner decision (Nov 28):** overlapping legacy chat IDs merge across ALL import
  sources and live data. Preserve every source row (no content dedupe), source
  provenance, deterministic idempotence, one coherent append chain, and globally
  contiguous queue positions. Reruns must not duplicate messages or queue entries.
- One legacy mutation boundary in `server/app.py`; one store adapter. A static test
  enumerates legacy queue/transcript writes and rejects mutation sites outside it.
- Unify legacy chat-ID remapping first: persisted, deterministic and bijective.
- Capture raw legacy GET goldens after that fix and before route/shim changes.
  Declare and hash timestamp/generated-ID normalization before capture; never tune it
  afterward to obtain parity.
- Imported messages receive deterministic, idempotent `message.created` events with
  `origin=import`; existing events are never edited.

## Snapshot in, diff to events out

After each legacy mutation, diff the canonical legacy snapshot against the current
store projection, keyed by per-row content hash. Emit only the difference through the
frozen EventStore API. Blind replace-all and scattered event writes are rejected.

Each mutation is one event batch with an idempotency key derived from mutation seq and
snapshot hash. Boot compares legacy/projection hashes and reconciles any crash gap.
Mirror errors never break the legacy response; they are logged/counted and any nonzero
count fails parity. Preserve public and internal fields; compare canonical JSON at
every mutation. O(n) device cost is unmeasured.

## Removal and mutation history

No physical deletion of message history:

- `message.tombstoned(reason, seq)`; `message.restored` compensates undo.
- Clear uses `clear.applied` with a watermark or a tombstone batch sharing `clear_id`.
- Compression uses `compression.applied {covers[], summary_id}`; originals remain but
  are hidden from the active projection.
- `tools_used` uses `message.annotated`; never mutate the original created event.
- Backup restore uses `restore.applied` with the snapshot hash.

The active projection is a versioned function of events. Non-created events must refer
to existing `message.created` events. Privacy erasure is an OPEN OWNER DECISION:
tombstones retain content; a separate redaction/crypto-shred design is required before
FLIP ships if clear/delete means erasure.

## Flip ordering and hold

Flip implementation may start after PC mirror zero-diff over the full corpus, every
mutation kind, imported rows, seeded fuzz and kill-during-mutation. Flip acceptance and
P1.5 still require the device tour. Flip defaults OFF.

Store commits first, then `queue.json` write-through. Boot repairs a crash between them
by regenerating queue.json from the store, never vice versa. Rollback FLIP→OFF→FLIP
must be lossless. Recents POST has an idempotency marker event; production continues to
use `/api/state`. Since app.py changes, hold device half/G1 until the P1.5 tree is
frozen once. G1 verifies every packaged `sitecrew/` file and hash in the extracted APK.

## Required three-config evidence

All configurations use identical goldens, seeded mutation script, recorded commit and
runtime hashes. Device tour remains NOT RUN in every report.

1. **OFF:** responses equal goldens after declared normalization; store never opened,
   zero events; store modules remain lazily imported.
2. **MIRROR:** responses byte-equal OFF each step; internal/public projection diff zero
   after every mutation; divergence count zero. Faults: kill between legacy write and
   mirror reconciles at boot; injected mirror exception leaves legacy response intact.
   Imported rows have message.created per row; ID map bijective.
3. **FLIP:** responses equal goldens; queue.json equals legacy shape after every
   mutation; rollback OFF lossless; events-only rebuild equals live projection; undo,
   clear, restore, compression and tools_used round-trip; invariants pass.

Cross-config per-step response hashes must match modulo declared normalization.
EventStore source hashes unchanged, prior suite counts do not fall, SQLite-floor lint
passes, and no gate is weakened.
