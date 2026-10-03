"use strict";
/*
 * ht_transport.js — one HT-protocol session over a MIDI in/out pair (GP-150).
 *
 * Owns the one-at-a-time request queue, the tx counter, stream assembly and the
 * IMMEDIATE final-chunk ACK (the pedal retransmits the whole stream if that ACK
 * is late). Pure protocol bytes come from ht_proto.js (window.HtProto).
 *
 * Browser: window.HtTransport. Node tests: module.exports, with fake ports —
 * `input` is anything with a settable `onmidimessage`, `output` anything with
 * `send(bytes)`. The session installs ONE input handler for its lifetime; every
 * exchange plugs into it (`current`) instead of overwriting input.onmidimessage,
 * so unsolicited device messages between requests still get ACKed.
 *
 *   const s = HtTransport.create(input, output);
 *   await s.hello();                  // true when the pedal answers the handshake
 *   const prst = await s.readPreset(7); // Uint8Array(1128), or null for an empty slot
 *   await s.selectSlot(7);            // switch the pedal (read request with flag 0)
 */
(function (root) {
  const DEFAULTS = {
    settleMs: 300, // quiet gap after each request (the pedal has a shallow input queue)
    timeoutMs: 3000, // per-request reply timeout (non-stream); stream hard cap adds emptyTimeoutMs
    idleMs: 800, // a stream with no new chunk for this long is incomplete
    emptyTimeoutMs: 1500, // no chunk this long after the ACK (or the send) = empty slot
    tickMs: 20,
    log: null, // (level, message, detail) => void; default: console.warn for "warn"
  };
  const NOT_RESPONDING = "pedal not responding — close Valeton Suite if it's open, and check the USB cable";

  function create(input, output, opts) {
    const HT = root.HtProto || (typeof module !== "undefined" && module.exports ? require("./ht_proto.js") : null);
    if (!HT) throw new Error("ht_proto.js (window.HtProto) is not loaded");
    const o = Object.assign({}, DEFAULTS, opts || {});
    const log = (level, msg, detail) => {
      if (o.log) o.log(level, msg, detail);
      else if (level === "warn" && typeof console !== "undefined") console.warn(`[ht] ${msg}`);
    };
    let chain = Promise.resolve(), tx = 0x10, closed = false, badFrames = 0;
    let current = null, abortCurrent = null; // the in-flight exchange's frame handler / aborter

    // tx ids travel as one SysEx data byte: 1..0x7F (0 means "no id"; 0x80+ is a status byte).
    const nextTx = () => { tx = (tx % 0x7f) + 1; return tx; };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const send = (wire) => output.send(wire);
    const ackNow = (id) => { try { send(HT.ack(id)); } catch (err) { log("warn", `could not ACK id ${id}: ${err.message}`); } };

    // Run `fn` after every queued request has finished and settled. One in flight, always.
    function job(fn) {
      const run = chain.then(() => { if (closed) throw new Error("session closed"); return fn(); });
      chain = run.then(() => sleep(o.settleMs), () => sleep(o.settleMs));
      return run;
    }

    // Every inbound message lands here. ACK duties come FIRST, before the frame is
    // handed on or any stream is assembled: the final (short) chunk of any stream
    // is ACKed right now (late ACK => the pedal resends the whole stream), and a
    // device short message carrying a tx id (0x10 reply, 0x18 status, 0x08 import
    // notify ...) is ACKed with that id. A frame with a bad CRC is dropped unACKed.
    function onMessage(e) {
      const d = e && e.data;
      if (!d || d.length < 9 || d[0] !== 0xf0 || d[1] !== 0x7f) return;
      const w = d instanceof Uint8Array ? d : Uint8Array.from(d);
      let f;
      try { f = HT.parseFrame(w); } catch (err) {
        badFrames++;
        if (badFrames === 3) log("warn", `dropped ${badFrames} corrupt frames from the pedal (${err.message})`);
        return;
      }
      const chunk = HT.isChunk(f);
      if (chunk && w.length < HT.FULL_WIRE_LEN) ackNow(f.tx4[3]);
      else if (!chunk && f.family !== HT.FAMILY_ACK && f.tx4[3] !== 0) ackNow(f.tx4[3]);
      if (current) current(f, w);
      else if (f.family !== HT.FAMILY_ACK || chunk) log("debug", `unsolicited frame family 0x${f.family.toString(16)}`, f);
    }
    input.onmidimessage = onMessage;

    // One exchange: send `wire`, collect frames until done. Resolves (never rejects)
    // with {ack, frames, chunks} plus one of {payload, transferId} | {silent} |
    // {error} | {timeout} | {error, aborted} (session closed). `frames` = every
    // non-ACK frame, chunks included.
    //  - non-stream: done on the first non-ACK, non-chunk frame that `until(frame)`
    //    accepts (default: any), or idleMs after the ACK.
    //  - stream: done on the final (short) chunk (assembled), or `silent` when no
    //    chunk arrived emptyTimeoutMs after the ACK (or after the send if no ACK).
    function exchange(wire, { stream = false, timeoutMs = o.timeoutMs, until = null } = {}) {
      let want = null; // the request's tx id; the device's ACK echoes it
      try { const rf = HT.parseFrame(wire); if (rf.family !== HT.FAMILY_ACK && !HT.isChunk(rf)) want = rf.tx4[3]; } catch { /* raw bytes: any ACK counts */ }
      return new Promise((resolve) => {
        const frames = [], chunks = [], t0 = Date.now();
        let ack = false, ackAt = 0, lastChunkAt = 0, done = false, tick = null;
        let sawTail = false; // non-zero-offset chunks seen with no offset-0 chunk before them
        const finish = (extra) => {
          if (done) return;
          done = true;
          if (current === handler) { current = null; abortCurrent = null; }
          clearInterval(tick);
          resolve(Object.assign({ ack, frames, chunks }, extra));
        };
        const handler = (f, w) => {
          if (f.family === HT.FAMILY_ACK && !HT.isChunk(f)) {
            // `00 0x 03 00` handshake replies are not ACKs (an ACK is `00 00 00 <id>`).
            if (f.tx4[1] !== 0) { frames.push(f); if (!stream && (!until || until(f))) finish(); return; }
            if (!ack && (want === null || f.tx4[3] === want)) { ack = true; ackAt = Date.now(); }
            return;
          }
          frames.push(f);
          if (!HT.isChunk(f)) { if (!stream && (!until || until(f))) finish(); return; } // reply / status message
          if (!stream) return;
          // offset 0 opens a stream: drop any partial leftovers (e.g. a resend), and
          // ignore a tail with no head — an older stream's, or ours with its first
          // chunk lost (sawTail: then it is a stream error at the silence verdict,
          // never "empty"; our own offset-0 chunk resets `chunks` before that).
          const offset = (f.tx4[1] & 0x7f) | ((f.tx4[2] & 0x7f) << 7);
          if (offset === 0) chunks.length = 0;
          else if (!chunks.length) { sawTail = true; return; }
          chunks.push(f);
          lastChunkAt = Date.now();
          if (w.length < HT.FULL_WIRE_LEN) { // final chunk (already ACKed in onMessage)
            try { const { transferId, payload } = HT.assembleStream(chunks); finish({ transferId, payload }); }
            catch (err) { finish({ error: err }); }
          }
        };
        current = handler;
        abortCurrent = () => finish({ error: new Error("session closed"), aborted: true });
        tick = setInterval(() => {
          const now = Date.now();
          if (stream) {
            if (chunks.length) { if (now - lastChunkAt > o.idleMs) finish({ error: new Error("preset stream stopped before its final chunk") }); }
            else if (now - (ack ? ackAt : t0) > o.emptyTimeoutMs) {
              finish(sawTail ? { error: new Error("preset stream arrived without its first chunk") } : { silent: true });
            }
          } else if (ack && now - ackAt > o.idleMs) finish();
          if (now - t0 > timeoutMs + (stream ? o.emptyTimeoutMs : 0)) finish({ timeout: true });
        }, o.tickMs);
        try { send(wire); } catch (err) { finish({ error: err }); }
      });
    }

    const request = (wire, opts2) => job(() => exchange(wire, opts2));

    // The handshake: host `00 01 03 00` -> pedal `00 02 03 00` (a frame, not an ACK).
    const isHandshakeReply = (f) => f.family === HT.FAMILY_ACK && f.tx4[1] === 0x02;
    async function alive() {
      const r = await exchange(HT.hello(), { until: isHandshakeReply }); // an unsolicited 0x18 must not end it
      return r.frames.some(isHandshakeReply);
    }
    const hello = () => job(alive);

    const checkSlot = (slot, active) => {
      if (active && slot === HT.SLOT_ACTIVE) return;
      if (!(Number.isInteger(slot) && slot >= 0 && slot < HT.SLOT_ACTIVE)) throw new Error(`slot out of range: ${slot}`);
    };

    const sessionClosed = () => new Error("session closed");

    // Read one preset without selecting it (flag 1). Returns the 1128-byte .prst,
    // or null for an empty slot. The pedal ACKs every read request, so:
    //  - ACK, then emptyTimeoutMs of silence            -> null (empty slot, spec §2)
    //  - no ACK and no stream                           -> re-send once (new tx); if
    //    that is silent and un-ACKed too, a hello probe decides: answered -> null
    //    (covers a pedal that does not ACK empty-slot reads), else NOT_RESPONDING
    //  - a chunk shows up during the probe              -> a late stream: discard it
    //    and read again (at most 2 more reads), never null on that path
    //  - garbled / stalled / timed-out stream, or a     -> one retry; any chunk at
    //    stream missing its first chunk                     all rules out null
    //  - preset index != slot (a stale resend)          -> one retry, then kept + warning
    function readPreset(slot) {
      return job(async () => {
        checkSlot(slot, true);
        const read = async (attempt) => {
          if (attempt > 1) await sleep(o.settleMs); // let any tail of an earlier stream pass
          if (closed) throw sessionClosed();
          const r = await exchange(HT.presetRequest(nextTx(), slot, false), { stream: true });
          if (r.aborted) throw r.error;
          return r;
        };
        let lastErr = null, mismatched = null, unacked = 0, attempt = 0, budget = 2, lateStream = false;
        while (budget-- > 0) {
          const r = await read(++attempt);
          if (r.payload) {
            let prst;
            try { prst = HT.presetFromPayload(r.payload); } catch (err) { lastErr = err; continue; }
            if (slot === HT.SLOT_ACTIVE || prst[4] === slot) return prst;
            if (mismatched) { log("warn", `slot ${slot} answered with preset index ${prst[4]} twice; keeping it`); return prst; }
            mismatched = prst; lastErr = new Error(`slot ${slot} answered with preset index ${prst[4]}`);
            continue;
          }
          if (r.silent && r.frames.some((f) => HT.isChunk(f))) { // chunks came, just no usable stream: never "empty"
            lastErr = new Error("preset stream arrived without its first chunk");
            continue;
          }
          if (r.silent && r.ack) {
            if (!lateStream) return null; // ACKed, then nothing: an empty slot
            lastErr = new Error(`slot ${slot}: a late preset stream, then silence — read it again`);
            continue;
          }
          if (r.silent) { // neither an ACK nor a stream: a lost / late request
            unacked++;
            if (budget > 0 || lateStream || unacked < 2) { lastErr = new Error(NOT_RESPONDING); continue; }
            // both reads un-ACKed and silent: is the pedal there at all?
            if (closed) throw sessionClosed();
            const p = await exchange(HT.hello(), { until: isHandshakeReply });
            if (p.aborted) throw p.error;
            if (p.frames.some((f) => HT.isChunk(f))) { // the stream was just late
              lateStream = true; budget = 2; lastErr = new Error(NOT_RESPONDING);
              continue;
            }
            if (p.frames.some(isHandshakeReply)) return null; // pedal is there; the slot is silent
            throw new Error(NOT_RESPONDING);
          }
          lastErr = r.error || new Error(r.timeout ? "preset read timed out" : "preset stream incomplete");
        }
        throw lastErr || new Error(NOT_RESPONDING);
      });
    }

    // Switch the pedal to `slot` (flag 0). The pedal ACKs, sends a 0x18 status and
    // the preset stream; both are ACKed and drained here. No ACK (and no stream)
    // -> one re-send, then NOT_RESPONDING. A hello answer alone is not success.
    function selectSlot(slot) {
      return job(async () => {
        checkSlot(slot, false);
        for (let attempt = 1; attempt <= 2; attempt++) {
          if (attempt > 1) await sleep(o.settleMs);
          if (closed) throw sessionClosed();
          const r = await exchange(HT.presetRequest(nextTx(), slot, true), { stream: true });
          if (r.aborted) throw r.error;
          if (r.ack || r.chunks.length) return; // the pedal took it
        }
        throw new Error(NOT_RESPONDING);
      });
    }

    function close() {
      closed = true;
      if (abortCurrent) abortCurrent();
      if (input.onmidimessage === onMessage) input.onmidimessage = null;
    }

    return { request, hello, readPreset, selectSlot, nextTx, close, stats: () => ({ badFrames }), _send: send };
  }

  const API = { create, DEFAULTS, NOT_RESPONDING };
  if (typeof module !== "undefined" && module.exports) module.exports = API; else root.HtTransport = API;
})(typeof self !== "undefined" ? self : this);
