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
const waitOverride = parseInt(process.argv[8] || "0", 10) || 0;   // boss: reconnect at wait=25
const seed = parseInt(seedArg, 10) >>> 0;
const livenessSeen = new Set();     // X-Liveness-Path values from poll responses
const wakeReasons = {};             // X-Wake-Reason values from poll responses
let s = seed || 1;
function rng() {            // deterministic mulberry32
  s |= 0; s = (s + 0x6D2B79F5) | 0;
  let t = Math.imul(s ^ (s >>> 15), 1 | s);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// global fetch wrapper: capture X-Liveness-Path for every poll response
// (boss: record liveness_path in the reports); in storm mode it also counts
// POLL responses only (the appender's POSTs previously inflated the counts —
// boss reconciliation note: forced + returned + errored must equal polls)
let COUNT_FETCH = false;
const fetchCount = { p2xx: 0, p503: 0, pOther4xx5xx: 0 };
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  const r = await realFetch(url, opts);
  const u = String(url);
  if (u.includes("/api/v2/poll")) {
    const lp = r.headers && r.headers.get ? r.headers.get("X-Liveness-Path") : null;
    if (lp) livenessSeen.add(lp);
    const wr = r.headers && r.headers.get ? r.headers.get("X-Wake-Reason") : null;
    if (wr) wakeReasons[wr] = (wakeReasons[wr] || 0) + 1;
    if (COUNT_FETCH) {
      if (r.status === 503) fetchCount.p503++;
      else if (r.status >= 400) fetchCount.pOther4xx5xx++;
      else fetchCount.p2xx++;
    }
  }
  return r;
};

async function api(path, body) {
  const r = await fetch(base + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (path.startsWith("/api/v2/poll")) {
    const lp = r.headers.get("X-Liveness-Path");
    if (lp) livenessSeen.add(lp);      // boss: record liveness_path in reports
  }
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
    liveness_path: Array.from(livenessSeen),
    wake_reasons: wakeReasons,
    latencies_ms: latencies,
  };
  console.log(JSON.stringify(out));
  process.exit(out.p95_ms != null && out.p95_ms <= 300 ? 0 : 1);
}

async function reconnect() {
  const st = await api("/api/v2/debug/store-state?db=main");
  const cursor0 = st.head || 0;
  const refState = { streams: {} }, chaosState = { streams: {} };
  const waitS = waitOverride || 2;       // boss: run 100+ reconnects at wait=25
  const ref = P.create({
    baseUrl: base,                       // node fetch cannot do relative URLs
    streams: { [stream]: cursor0 }, wait: waitS,
    onEvents: (sName, evs) => evs.forEach((e) => P.reduceEvent(refState, e)),
  });
  const chaos = P.create({
    baseUrl: base,                       // node fetch cannot do relative URLs
    streams: { [stream]: cursor0 }, wait: waitS,
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
    wait_used: waitS,
    liveness_path: Array.from(livenessSeen),
  };
  out.pass = out.hashes_equal && out.cursors_equal &&
             rs.items.length === head - cursor0 && chaos.stats.forced >= 100 &&
             chaos.stats.gaps === 0;
  console.log(JSON.stringify(out));
  process.exit(out.pass ? 0 : 1);
}

async function storm() {
  // Boss P1.3-device ruling, tested on PC FIRST: run the forced-reconnect
  // storm at PRODUCTION wait=25. Risk: an aborted parked request keeps its
  // parked slot until the park expires (up to 25 s), so a client coming back
  // from a network drop could see 503 busy for up to 25 s — blowing the
  // latency gate exactly when it matters. Metrics: 503 counts (POLL responses
  // only — the appender's POSTs are not polls), longest 503 window, and
  // time-to-first-event after each reconnect (event cadence — NOT cited as
  // reconnect cost).
  COUNT_FETCH = true;
  const st = await api("/api/v2/debug/store-state?db=main");
  const cursor0 = st.head || 0;
  const chaosState = { streams: {} };
  let lastForced = 0, lastAbortAt = null, last503at = null;
  let longest503run = 0, cur503run = 0;
  const ttfs = [];
  let appliedTotal = 0;
  const chaos = P.create({
    baseUrl: base,
    streams: { [stream]: cursor0 }, wait: 25,
    chaos: { p: 0.95, maxMs: 400 },
    onEvents: (sName, evs) => {
      evs.forEach((e) => P.reduceEvent(chaosState, e));
      appliedTotal += evs.length;
      if (lastAbortAt != null) { ttfs.push(Date.now() - lastAbortAt); lastAbortAt = null; }
    },
  });
  chaos.start();

  const t0 = Date.now();
  const DURATION = 90000;
  let appended = 0;
  const t503 = setInterval(() => {
    // sample the forced-abort counter to timestamp reconnects
    if (chaos.stats.forced > lastForced) { lastForced = chaos.stats.forced; lastAbortAt = Date.now(); }
    if (last503at != null && Date.now() - last503at > 1500) {
      if (cur503run > longest503run) longest503run = cur503run;
      cur503run = 0; last503at = null;
    }
    if (Date.now() - t0 > DURATION) {
      clearInterval(t503);
      chaos.stop();
      const sorted = ttfs.slice().sort((a, b) => a - b);
      const out = {
        mode: "storm", wait_used: 25, duration_s: Math.round((Date.now() - t0) / 1000),
        forced_reconnects: chaos.stats.forced,
        polls: chaos.stats.polls, errors: chaos.stats.errors,
        http_503: fetchCount.p503, http_other_4xx5xx: fetchCount.pOther4xx5xx,
        http_2xx_polls: fetchCount.p2xx,
        reconciliation: "polls == forced + 2xx_polls + other_4xx5xx + in-flight-at-exit (the appender's POSTs are no longer counted)",
        events_appended: appended, events_applied: appliedTotal,
        longest_consecutive_503: longest503run || cur503run,
        liveness_path: Array.from(livenessSeen),
        time_to_first_event_after_reconnect_ms: {
          n: ttfs.length,
          p50: sorted.length ? sorted[Math.floor(sorted.length * 0.5)] : null,
          max: sorted.length ? sorted[sorted.length - 1] : null,
          note: "event cadence (~2 s append interval) — NOT reconnect cost; the device half uses the catch-up metric",
        },
        verdict_note: "slots_free_promptly = http_503 == 0.",
      };
      console.log(JSON.stringify(out));
      process.exit(0);
    }
  }, 50);

  while (Date.now() - t0 < DURATION - 2000) {
    try {
      const r = await api("/api/v2/debug/append", { cycles: 1, db: "main" });
      appended += (r.acks || []).length;
    } catch (e) { /* keep the storm going */ }
    await sleep(2000);
  }
}

(async () => {
  try {
    if (mode === "latency") await latency();
    else if (mode === "reconnect") await reconnect();
    else if (mode === "storm") await storm();
    else { console.error("unknown mode " + mode); process.exit(2); }
  } catch (e) {
    console.error("FATAL", e && e.message);
    process.exit(3);
  }
})();
