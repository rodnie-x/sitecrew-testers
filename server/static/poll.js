/* web/poll.js - v2 long-poll client. Vanilla, no build step, no dependencies.
 *
 *   var client = SiteCrewPoll.create({
 *     streams: { c_81f2: 0, office: 0 },    // stream -> last APPLIED seq (0 = from the beginning)
 *     baseUrl: "",                           // same origin
 *     wait: 25,                              // seconds the server may park the request
 *     onEvents: function (stream, events) {},// contiguous, ascending, never duplicated
 *     onReset:  function (stream, head) {},  // cursor was above the server head: reload that stream
 *     onState:  function (state) {},         // "polling" | "backoff" | "stopped"
 *     chaos: { p: 0.3, maxMs: 300 }          // TEST ONLY: abort a request with probability p after <= maxMs
 *   });
 *   client.start(); client.stop(); client.cursors(); client.addStream("c_x", 0); client.stats
 *
 * Guarantees: an event is applied at most once per (stream, seq); a hole in seq never advances the
 * cursor (the next poll re-asks from the last contiguous seq); any network error resumes from the
 * last applied seq, so reconnects cannot lose or duplicate events.
 */
(function (root) {
  "use strict";

  function create(opts) {
    var cur = Object.assign({}, opts.streams || {});
    var base = opts.baseUrl || "";
    var waitS = opts.wait == null ? 25 : opts.wait;
    // cid supersede (boss design): one stable id per client instance — when
    // this client reconnects, the server supersedes (frees) its older parked
    // poll within one slice instead of holding it for the full wait.
    var cid = opts.cid ||
              (Math.random().toString(36).slice(2) + Date.now().toString(36));
    var running = false, ctl = null, timer = null, failures = 0, gapRuns = 0;
    var stats = { polls: 0, events: 0, dupes: 0, gaps: 0, resets: 0, errors: 0, forced: 0 };

    function emit(state) { if (opts.onState) opts.onState(state); }

    function pollUrl() {
      var parts = Object.keys(cur).map(function (s) { return s + ":" + cur[s]; });
      return base + "/api/v2/poll?streams=" + parts.join(",") + "&wait=" + waitS + "&cid=" + cid;
    }

    // Apply one stream's events in order. Returns true if a hole was seen.
    function applyStream(stream, events) {
      var out = [], hole = false;
      events.forEach(function (e) {
        var c = cur[stream];
        if (c === undefined) return;                               // stream was removed meanwhile
        if (e.seq <= c) { stats.dupes++; return; }                 // replay after a reconnect
        if (e.seq !== c + 1) { stats.gaps++; hole = true; return; }// hole: do not advance
        cur[stream] = e.seq;
        out.push(e);
      });
      if (out.length) {
        stats.events += out.length;
        if (opts.onEvents) opts.onEvents(stream, out);
      }
      return hole;
    }

    // Returns true if any stream had a hole.
    function handle(body) {
      var hole = false;
      (body.reset || []).forEach(function (r) {
        var s = typeof r === "string" ? r : r && r.stream;
        if (!s || cur[s] === undefined) return;
        var head = body.heads && body.heads[s] != null ? body.heads[s] : 0;
        cur[s] = head;
        stats.resets++;
        if (opts.onReset) opts.onReset(s, head);
      });
      var byStream = {};
      (body.events || []).forEach(function (e) {
        var s = e.chat_id;
        (byStream[s] = byStream[s] || []).push(e);
      });
      Object.keys(byStream).forEach(function (s) {
        if (applyStream(s, byStream[s])) hole = true;
      });
      return hole;
    }

    function schedule(ms) {
      clearTimeout(timer);
      timer = setTimeout(loop, ms);
    }

    function backoffMs(retryAfter) {
      var ra = parseFloat(retryAfter);
      if (isFinite(ra) && ra > 0) return Math.min(ra, 30) * 1000;
      var base = Math.min(5000, 250 * Math.pow(2, Math.max(0, failures - 1)));
      return base * (0.5 + Math.random() / 2);                       // jitter
    }

    function loop() {
      if (!running) return;
      var started = Date.now();
      var c = new AbortController();
      ctl = c;
      var done = false;
      var hardStop = setTimeout(function () { c.abort(); }, (waitS + 10) * 1000);
      if (opts.chaos && Math.random() < opts.chaos.p) {
        setTimeout(function () {
          if (!done) { stats.forced++; c.abort(); }                       // only count aborts that really cut a request
        }, Math.random() * (opts.chaos.maxMs || 300));
      }
      stats.polls++;
      emit("polling");
      fetch(pollUrl(), { signal: c.signal, cache: "no-store" })
        .then(function (r) {
          if (!r.ok) {
            var err = new Error("HTTP " + r.status);
            err.retryAfter = r.headers.get("Retry-After");
            throw err;
          }
          return r.json();
        })
        .then(function (body) {
          done = true;
          clearTimeout(hardStop);
          failures = 0;
          var hole = handle(body);
          gapRuns = hole ? gapRuns + 1 : 0;
          var idle = !(body.events && body.events.length) && !body.more;
          if (gapRuns >= 3) { emit("backoff"); schedule(1000); }          // persistent hole: do not spin
          else if (idle && Date.now() - started < 50) schedule(250);      // server answered instantly with nothing
          else schedule(hole ? 50 : 0);
        })
        .catch(function (e) {
          done = true;
          clearTimeout(hardStop);
          if (!running) return;
          var forced = e && e.name === "AbortError" && opts.chaos;
          if (!forced) { stats.errors++; failures++; emit("backoff"); }
          schedule(forced ? 0 : backoffMs(e && e.retryAfter));
        });
    }

    return {
      start: function () { if (running) return; running = true; failures = 0; loop(); },
      stop: function () { running = false; clearTimeout(timer); if (ctl) ctl.abort(); emit("stopped"); },
      cursors: function () { return Object.assign({}, cur); },
      addStream: function (id, seq) { if (cur[id] === undefined) cur[id] = seq || 0; },
      stats: stats
    };
  }

  // Pure reducer: state = { streams: { id: { seq, items: [...] } } }. Same events in -> same state out.
  function reduceEvent(state, e) {
    var s = state.streams[e.chat_id] || (state.streams[e.chat_id] = { seq: 0, items: [] });
    if (e.seq <= s.seq) return state;                                // idempotent
    s.seq = e.seq;
    s.items.push({ seq: e.seq, type: e.type, id: e.id, text: JSON.stringify(e.data || {}).slice(0, 160) });
    return state;
  }

  // 32-bit FNV-1a x2 over a string -> 16 hex chars (equality checks only, not security).
  function hash(str) {
    var h1 = 0x811c9dc5, h2 = 0x01000193;
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
      h2 = Math.imul(h2 ^ c, 0x85ebca6b) >>> 0;
    }
    return ("00000000" + h1.toString(16)).slice(-8) + ("00000000" + h2.toString(16)).slice(-8);
  }

  root.SiteCrewPoll = { create: create, reduceEvent: reduceEvent, hash: hash };
})(typeof window !== "undefined" ? window : globalThis);
