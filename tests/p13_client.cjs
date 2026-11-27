/* P1.3 PC client (node): latency + forced-reconnect runs against the REAL
 * integrated server/static/poll.js and the real /api/v2/poll endpoint.
 *
 *   node p13_client.cjs <poll.js path> <base> <mode> <seed> <stream>
 *
 *   mode=latency    one parked client; 200 samples of append -> client-arrival
 *                   latency (ms), p50/p95/max in the JSON verdict
 *   mode=reconnect  phase A: both clients apply a stream of appends (ref =
 *                   uninterrupted run); phase B: appends STOP and the chaos
 *                   client suffers aborts until >= 100 FORCED reconnects
 *                   (parked polls return nothing, so aborts dominate);
 *                   phase C: drain. Verdict: identical DOM state hashes.
 */
"use strict";
const [, , jsPath, base, mode, seedArg, streamArg] = process.argv;
require(jsPath);
const P = globalThis.SiteCrewPoll;
if (!P) { console.error("SiteCrewPoll not exported"); process.exit(2); }

const stream = streamArg || "c_debug";
const seed = parseInt(seedArg, 10) >>> 0;
let s = seed || 1;
function rng() {            // deterministic mulberry32
  s |= 0; s = (s + 0x6D2B79F5) | 0;
  let t = Math.imul(s ^ (s >>> 15), 1 | s);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body) {
  const r = await fetch(base + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error("HTTP " + r.status + " for " + path);
  return r.json();
}

async function latency() {
  const st = await api("/api/v2/debug/store-state?db=main");
  const head = st.head || 0;
  const samples = 200;
  const arrival = new Map();          // seq -> Date.now() at onEvents
  const latencies = [];
  let misses = 0;
  const client = P.create({
    baseUrl: base,                       // node fetch cannot do relative URLs
    streams: { [stream]: head },
    wait: 25,
    onEvents: (sName, evs) => {
      for (const e of evs) if (!arrival.has(e.seq)) arrival.set(e.seq, Date.now());
    },
  });
  client.start();
  await sleep(300);                    // let the first parked poll settle
  for (let i = 0; i < samples; i++) {
    await sleep(30 + Math.floor(rng() * 220));
    const t0 = Date.now();
    let ack = null;
    try {
      const r = await api("/api/v2/debug/append", { cycles: 1, db: "main" });
      ack = (r.acks || [])[r.acks.length - 1] || null;
    } catch (e) { misses++; continue; }
    if (ack == null) { misses++; continue; }
    const deadline = t0 + 5000;
    while (!arrival.has(ack) && Date.now() < deadline) await sleep(2);
    if (arrival.has(ack)) latencies.push(arrival.get(ack) - t0);
    else misses++;
  }
  client.stop();
  const sorted = latencies.slice().sort((a, b) => a - b);
  const pick = (q) => sorted.length
    ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : null;
  const out = {
    mode: "latency", seed, stream, start_cursor: head, samples,
    received: latencies.length, misses,
    p50_ms: pick(0.5), p95_ms: pick(0.95),
    max_ms: sorted.length ? sorted[sorted.length - 1] : null,
    mean_ms: sorted.length
      ? Math.round(sorted.reduce((a, b) => a + b, 0) / sorted.length) : null,
    latency_definition: "t0 = just before the append POST is sent; t1 = the " +
      "poll client's onEvents fired for that seq. Includes append RTT + " +
      "parking wakeup + poll response RTT (slightly conservative).",
  };
  console.log(JSON.stringify(out));
  process.exit(out.p95_ms != null && out.p95_ms <= 300 ? 0 : 1);
}

async function reconnect() {
  const st = await api("/api/v2/debug/store-state?db=main");
  const cursor0 = st.head || 0;
  const refState = { streams: {} }, chaosState = { streams: {} };
  const ref = P.create({
    baseUrl: base,                       // node fetch cannot do relative URLs
    streams: { [stream]: cursor0 }, wait: 2,
    onEvents: (sName, evs) => evs.forEach((e) => P.reduceEvent(refState, e)),
  });
  const chaos = P.create({
    baseUrl: base,                       // node fetch cannot do relative URLs
    streams: { [stream]: cursor0 }, wait: 2,
    chaos: { p: 0.7, maxMs: 250 },
    onEvents: (sName, evs) => evs.forEach((e) => P.reduceEvent(chaosState, e)),
  });
  ref.start(); chaos.start();
  await sleep(300);

  // phase A: appends while both clients run (ref = uninterrupted run)
  const aT0 = Date.now();
  let events = 0, posts = 0;
  while (events < 120 && Date.now() - aT0 < 120000) {
    const n = 1 + Math.floor(rng() * 5);
    try {
      const r = await api("/api/v2/debug/append", { cycles: n, db: "main" });
      events += (r.acks || []).length;
    } catch (e) { /* blip: keep going */ }
    posts++;
    await sleep(20 + Math.floor(rng() * 120));
  }
  // phase B: STOP appending; aborts now dominate -> forced reconnects climb
  const bT0 = Date.now();
  while (chaos.stats.forced < 100 && Date.now() - bT0 < 180000) await sleep(50);
  // phase C: drain both clients to the final head
  const final = await api("/api/v2/debug/store-state?db=main");
  const head = final.head || 0;
  const cT0 = Date.now();
  while (Date.now() - cT0 < 60000) {
    const rs = refState.streams[stream], cs = chaosState.streams[stream];
    if (ref.cursors()[stream] === head && chaos.cursors()[stream] === head &&
        rs && cs && rs.seq === head && cs.seq === head) break;
    await sleep(50);
  }
  ref.stop(); chaos.stop();
  const rs = refState.streams[stream] || { seq: 0, items: [] };
  const cs = chaosState.streams[stream] || { seq: 0, items: [] };
  const refHash = P.hash(JSON.stringify(rs));
  const chaosHash = P.hash(JSON.stringify(cs));
  const out = {
    mode: "reconnect", seed, stream, start_cursor: cursor0, final_head: head,
    events_appended: events, append_posts: posts,
    forced_reconnects: chaos.stats.forced,
    chaos_polls: chaos.stats.polls, chaos_errors: chaos.stats.errors,
    chaos_dupes: chaos.stats.dupes, chaos_gaps: chaos.stats.gaps,
    ref_items: rs.items.length, chaos_items: cs.items.length,
    ref_hash: refHash, chaos_hash: chaosHash,
    hashes_equal: refHash === chaosHash,
    cursors_equal: ref.cursors()[stream] === head &&
                   chaos.cursors()[stream] === head,
    wait_used: 2,
  };
  out.pass = out.hashes_equal && out.cursors_equal &&
             rs.items.length === head - cursor0 && chaos.stats.forced >= 100 &&
             chaos.stats.gaps === 0;
  console.log(JSON.stringify(out));
  process.exit(out.pass ? 0 : 1);
}

(async () => {
  try {
    if (mode === "latency") await latency();
    else if (mode === "reconnect") await reconnect();
    else { console.error("unknown mode " + mode); process.exit(2); }
  } catch (e) {
    console.error("FATAL", e && e.message);
    process.exit(3);
  }
})();
