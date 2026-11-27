/* Idempotent-apply + hole-guard check against the REAL integrated poll.js
 * client (node, stubbed transport — no network).
 *
 * The client's guarantees (docstring): an event is applied at most once per
 * (stream, seq); a hole in seq never advances the cursor (next poll re-asks
 * from the last contiguous seq); replay after reconnect cannot duplicate.
 * reduceEvent (the DOM reducer) adds the second layer: seq <= cursor is a
 * no-op, so re-delivery after a reset stays idempotent.
 *
 * Prints a one-line JSON verdict; nonzero exit on any assertion failure.
 */
"use strict";
const path = process.argv[2];
require(path);
const P = globalThis.SiteCrewPoll;
if (!P) { console.error("SiteCrewPoll not exported"); process.exit(2); }

const STREAM = "c_t";
function ev(seq) {
  return { chat_id: STREAM, seq: seq, type: "message.created", id: "m" + seq,
           data: { i: seq } };
}

// Scripted poll responses, served in order; then idle empty responses.
const bodies = [
  { events: [ev(1), ev(2)], heads: { [STREAM]: 2 }, more: false, reset: [] },  // apply 1,2
  { events: [ev(4)], heads: { [STREAM]: 4 }, more: false, reset: [] },         // HOLE: 3 missing
  { events: [ev(3), ev(4)], heads: { [STREAM]: 4 }, more: false, reset: [] },  // fill
  { events: [ev(1), ev(2), ev(3), ev(4)], heads: { [STREAM]: 4 }, more: false, reset: [] }, // replay
  { events: [], heads: { [STREAM]: 2 }, more: false, reset: [STREAM] },        // cursor above head -> reset
  { events: [ev(3), ev(4)], heads: { [STREAM]: 4 }, more: false, reset: [] },  // re-delivery after reset
];
let bodyIdx = 0;
let saw4Before3 = false;
let saw3 = false;
globalThis.fetch = async (url) => {
  const body = bodyIdx < bodies.length ? bodies[bodyIdx++] :
    { events: [], heads: { [STREAM]: 4 }, more: false, reset: [] };
  if (bodyIdx === 2) {                      // right after the hole response
    // (checked below via onEvents ordering, not here)
  }
  return { ok: true, json: async () => body };
};

const applied = [];
const state = { streams: {} };
let resetSeen = false;
const client = P.create({
  streams: { [STREAM]: 0 },
  wait: 1,
  onEvents: (s, evs) => {
    for (const e of evs) {
      if (e.seq === 3) saw3 = true;
      if (e.seq === 4 && !saw3) saw4Before3 = true;   // hole must suppress 4
      applied.push(e.seq);
      P.reduceEvent(state, e);                        // DOM reducer
    }
  },
  onReset: () => { resetSeen = true; },
});
client.start();

const t0 = Date.now();
(function waitDone() {
  const done = bodyIdx >= bodies.length + 2 && applied.length >= 8;
  if (!done && Date.now() - t0 < 10000) return setTimeout(waitDone, 25);
  client.stop();

  const cur = client.cursors()[STREAM];
  const st = state.streams[STREAM] || { seq: 0, items: [] };
  const checks = {
    // the hole (seq 4 while cursor 2) was suppressed: 4 never applied before 3
    hole_suppressed: !saw4Before3,
    // the fill applied 3 then 4: applied order contains 1,2 then 3,4
    fill_in_order: JSON.stringify(applied.slice(0, 4)) === "[1,2,3,4]",
    // replay of 1..4 was fully deduped by (stream, seq): dupes counted, no
    // extra onEvents items beyond the 4 + 2 post-reset re-deliveries
    replay_deduped: client.stats.dupes >= 4,
    // reset fired when the cursor was above the server head
    reset_fired: resetSeen,
    // DOM reducer state: exactly one item per seq 1..4, seq == 4
    dom_exact_once: st.items.length === 4 &&
      JSON.stringify(st.items.map((x) => x.seq)) === "[1,2,3,4]",
    cursor_final: cur === 4,
    gaps_seen: client.stats.gaps >= 1,
  };
  checks.ok = checks.hole_suppressed && checks.fill_in_order &&
              checks.replay_deduped && checks.reset_fired &&
              checks.dom_exact_once && checks.cursor_final && checks.gaps_seen;
  console.log(JSON.stringify(Object.assign({ stats: client.stats }, checks)));
  process.exit(checks.ok ? 0 : 1);
})();
