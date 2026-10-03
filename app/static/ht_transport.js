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
 *   await s.writePreset(packets);     // import stream -> {sent, acks, notified}. UNGATED
 *                                     // transport primitive: the app goes through
 *                                     // WebMidiWrite.writeSlot (WRITE_VERIFIED gate)
 */
(function (root) {
  const DEFAULTS = {
    settleMs: 300, // quiet gap after each request (the pedal has a shallow input queue)
    timeoutMs: 3000, // per-request reply timeout (non-stream); stream hard cap adds emptyTimeoutMs
    idleMs: 800, // a stream with no new chunk for this long is incomplete
    emptyTimeoutMs: 1500, // no chunk this long after the ACK (or the send) = empty slot
    tickMs: 20,
    writePaceMs: 20, // gap between import chunks (the pedal ACKs once, after the final one)
    notifyTimeoutMs: 3000, // wait this long after the import ACK for the 0x08 "import done"
    transferId: 0x24, // writePreset(prst): the transfer id Suite used in the captured import
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
    // Multi-frame requests (writePreset): `lead` frames go out first, paceMs apart,
    // with this exchange's handler already listening; `wire` is the last frame and
    // every timer starts when it is sent. Until then nothing ends the exchange or
    // counts as its ACK (frames are still collected; `beforeLast` = how many of
    // `frames` arrived before the last frame went out; `sentFrames` = how many
    // frames were handed to the output, the last one included). `ackId` names the ACK that
    // counts (default: the request's tx id; any ACK for raw bytes / chunk frames);
    // `idleMs` overrides how long to wait after that ACK.
    function exchange(wire, { stream = false, timeoutMs = o.timeoutMs, until = null, lead = [], paceMs = 0, ackId = null, idleMs = o.idleMs } = {}) {
      let want = ackId; // the ACK id that counts; by default the request's tx id
      if (want === null) {
        try { const rf = HT.parseFrame(wire); if (rf.family !== HT.FAMILY_ACK && !HT.isChunk(rf)) want = rf.tx4[3]; } catch { /* raw bytes: any ACK counts */ }
      }
      return new Promise((resolve) => {
        const frames = [], chunks = [];
        let t0 = Date.now(), sending = lead.length > 0, beforeLast = 0, sentFrames = 0; // sending: lead frames still going out
        let ack = false, ackAt = 0, lastChunkAt = 0, done = false, tick = null;
        let sawTail = false; // non-zero-offset chunks seen with no offset-0 chunk before them
        const finish = (extra) => {
          if (done) return;
          done = true;
          if (current === handler) { current = null; abortCurrent = null; }
          clearInterval(tick);
          resolve(Object.assign({ ack, frames, chunks, beforeLast, sentFrames }, extra));
        };
        const handler = (f, w) => {
          if (f.family === HT.FAMILY_ACK && !HT.isChunk(f)) {
            // `00 0x 03 00` handshake replies are not ACKs (an ACK is `00 00 00 <id>`).
            if (f.tx4[1] !== 0) { frames.push(f); if (!stream && !sending && (!until || until(f))) finish(); return; }
            if (!ack && !sending && (want === null || f.tx4[3] === want)) { ack = true; ackAt = Date.now(); }
            return;
          }
          frames.push(f);
          if (!HT.isChunk(f)) { if (!stream && !sending && (!until || until(f))) finish(); return; } // reply / status message
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
          if (sending) return; // no verdicts until the last frame is out
          const now = Date.now();
          if (stream) {
            if (chunks.length) { if (now - lastChunkAt > o.idleMs) finish({ error: new Error("preset stream stopped before its final chunk") }); }
            else if (now - (ack ? ackAt : t0) > o.emptyTimeoutMs) {
              finish(sawTail ? { error: new Error("preset stream arrived without its first chunk") } : { silent: true });
            }
          } else if (ack && now - ackAt > idleMs) finish();
          if (now - t0 > timeoutMs + (stream ? o.emptyTimeoutMs : 0)) finish({ timeout: true });
        }, o.tickMs);
        if (!sending) { try { sentFrames++; send(wire); } catch (err) { finish({ error: err }); } return; }
        (async () => {
          try {
            for (const w of lead) { if (done) return; sentFrames++; send(w); await sleep(paceMs); }
            if (done) return;
            beforeLast = frames.length; t0 = Date.now(); sending = false;
            sentFrames++; send(wire);
          } catch (err) { finish({ error: err }); }
        })();
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

    // --- preset import (the GP-150 write) -------------------------------------
    const isImportDone = (f) => f.family === HT.FAMILY_IMPORT_DONE && !HT.isChunk(f);
    // The hint on every error that leaves the slot's state unknown.
    const readBack = (slot) => `read slot ${slot} back before retrying: it may or may not have been written`;
    const hexOf = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

    // `x` = a 1128-byte preset (imported to the slot in its own index byte 0x04) or
    // the 0x70 import frames themselves (e.g. WebMidiWrite's validated stream). The
    // frames must be one 0x70 stream that assembles into an import payload.
    function importFrames(x) {
      const isPreset = x instanceof Uint8Array || (Array.isArray(x) && typeof x[0] === "number");
      if (isPreset) {
        const prst = Uint8Array.from(x);
        if (prst.length !== HT.PRESET_LEN || prst[0] !== 0x11 || prst[1] !== 0x30 || prst[2] !== 0x64 || prst[3] !== 0x04) {
          throw new Error("writePreset: not a GP-150 .prst (expected 1128 bytes starting 11 30 64 04)");
        }
        x = HT.importStream(o.transferId, prst);
      }
      const packets = Array.from(x || [], (w) => Uint8Array.from(w));
      if (!packets.length) throw new Error("writePreset: nothing to send");
      const frames = packets.map((w, i) => {
        let f;
        try { f = HT.parseFrame(w); } catch (err) { throw new Error(`writePreset: packet ${i}: ${err.message}`); }
        if (f.family !== HT.FAMILY_PATCH || !HT.isChunk(f)) throw new Error(`writePreset: packet ${i} is not a patch (0x70) chunk`);
        return f;
      });
      let payload;
      try { payload = HT.assembleStream(frames).payload; } catch (err) { throw new Error(`writePreset: the frames do not assemble into one import stream (${err.message})`); }
      if (!HT.HEAD_IMPORT.every((v, i) => payload[i] === v)) throw new Error("writePreset: the stream does not carry an import payload (01 03 11 30)");
      return { packets, transferId: frames[0].tx4[3], slot: HT.presetFromPayload(payload)[4] };
    }

    // Import a preset: every chunk paceMs apart, then wait for the pedal's ACK (id =
    // transfer id) and its family-0x08 "import done" — all inside ONE exchange on
    // this session's own handler. onMessage already ACKs that 0x08 (it carries a tx
    // id); nothing here ACKs it again. Resolves {sent, acks, notified: true}; rejects
    // when the 0x08 never comes (ACK or not) or carries another payload than Suite's
    // capture (09 03 11 30). UNGATED: callers go through WebMidiWrite.writeSlot.
    function writePreset(x, wopts) {
      const w = Object.assign({ paceMs: o.writePaceMs, notifyTimeoutMs: o.notifyTimeoutMs }, wopts || {});
      let s;
      try { s = importFrames(x); } catch (err) { return Promise.reject(err); }
      const { packets, transferId, slot } = s;
      return job(async () => {
        if (closed) throw sessionClosed();
        const r = await exchange(packets[packets.length - 1], {
          lead: packets.slice(0, -1), paceMs: w.paceMs, ackId: transferId, until: isImportDone,
          idleMs: w.notifyTimeoutMs, timeoutMs: o.timeoutMs + w.notifyTimeoutMs,
        });
        if (r.aborted) { // close() mid-write: plain only if nothing went out yet
          if (!r.sentFrames) throw r.error;
          const when = r.sentFrames >= packets.length ? `after the import to slot ${slot} was sent` : `mid-import (${r.sentFrames} of ${packets.length} chunks sent)`;
          throw new Error(`session closed ${when} — ${readBack(slot)}`);
        }
        if (r.error) throw new Error(`GP-150 import to slot ${slot} failed while sending (${r.error.message}) — ${readBack(slot)}`);
        if (r.frames.slice(0, r.beforeLast).some(isImportDone)) log("warn", "a 0x08 notification arrived before the import was complete (ignored)");
        const note = r.frames.slice(r.beforeLast).find(isImportDone); // only an answer to the whole stream counts
        if (!note) {
          if (!r.ack) throw new Error(`GP-150 did not ACK the import to slot ${slot} (${NOT_RESPONDING}) — ${readBack(slot)}`);
          throw new Error(`GP-150 ACKed the import to slot ${slot} but sent no 0x08 'import done' within ${w.notifyTimeoutMs} ms — ${readBack(slot)}`);
        }
        let got;
        try { got = HT.shortPayload(note); } catch (err) { throw new Error(`GP-150 sent an unreadable 0x08 notification after the import to slot ${slot} (${err.message}) — ${readBack(slot)}`); }
        if (hexOf(got) !== hexOf(HT.IMPORT_DONE_PAYLOAD)) {
          throw new Error(`GP-150 answered the import to slot ${slot} with an unexpected 0x08 payload ${hexOf(got)} (Suite capture: ${hexOf(HT.IMPORT_DONE_PAYLOAD)}) — ${readBack(slot)}`);
        }
        return { sent: packets.length, acks: r.ack ? 1 : 0, notified: true };
      });
    }

    function close() {
      closed = true;
      if (abortCurrent) abortCurrent();
      if (input.onmidimessage === onMessage) input.onmidimessage = null;
    }

    return { request, hello, readPreset, selectSlot, writePreset, nextTx, close, stats: () => ({ badFrames }), _send: send };
  }

  const API = { create, DEFAULTS, NOT_RESPONDING };
  if (typeof module !== "undefined" && module.exports) module.exports = API; else root.HtTransport = API;
})(typeof self !== "undefined" ? self : this);
