"use strict";
/*
 * webmidi_device.js — read/select client for the GP-5 / GP-50 / GP-150 over WebMIDI.
 *
 * GP-5 / GP-50 ("legacy" transport): the browser-side twin of patch/live_read.py +
 * patch/select_patch.py: same CRC-8/0x07, same nibble framing, same reassembly.
 * Proven byte-for-byte against the Python path on live hardware (see
 * re/DEVICE_READ.md, the WebMIDI section).
 *
 * GP-150 ("ht" transport, profile.transport === "ht"): every call routes to an
 * HtTransport session (ht_transport.js + ht_proto.js, loaded before this file).
 * No bulk name read exists there, so readNames() is [] and a scan reads each slot
 * (readSlotOrNull: null = empty slot); selectSlot is a read request with flag 0;
 * _sendStream hands the import stream to the session's writePreset, refused while
 * WebMidiWrite.WRITE_VERIFIED.gp150 is false unless allowUnverified.
 *
 * READ + SELECT, plus one gated raw-send primitive (_sendStream) used only by
 * webmidi_write.js. All patch-write building/validation/gating lives there; this
 * module just owns the port + the one-at-a-time chain and refuses to move write
 * bytes without an explicit confirm+validated. A bad write can wedge the pedal
 * (power-cycle to recover), so that gate is load-bearing.
 *
 * Needs window.PRST (prst.js) for device profiles + rebuild(), and for the GP-150
 * window.HtProto + window.HtTransport. Chrome/Edge only.
 *
 *   const dev = await WebMidiDevice.connect();      // {key,name}; throws if none
 *   const names = await WebMidiDevice.readNames();  // [{slot,name}, ...]
 *   await WebMidiDevice.selectSlot(7);              // Program Change (non-destructive)
 *   const prst = await WebMidiDevice.readSlotPrst(7); // select + 0x41 + rebuild -> Uint8Array
 *   const p = await WebMidiDevice.readSlotOrNull(7);  // GP-150: null for an empty slot
 */
(function (root) {
  const PRST = root.PRST;
  const CATSEL = 0x12;
  const SEL_NAMES = 0x40;
  const SEL_BODY = 0x41;
  // Cadence, mirrored from live_read.py: the pedal has a shallow input queue and
  // wedges if requests outrun it. One at a time, settle after each.
  const POST_PC_MS = 300; // settle after a Program Change before reading
  const IDLE_MS = 400; // reply stream considered done after this much quiet
  const READ_TIMEOUT_MS = 2500;
  const SETTLE_MS = 200; // quiet gap after each request completes

  // --- SysEx codec (port of live_read.py) -----------------------------------
  function crc8(bytes) {
    let c = 0;
    for (const b of bytes) { c ^= b; for (let i = 0; i < 8; i++) c = (c & 0x80) ? ((c << 1) ^ 0x07) & 0xff : (c << 1) & 0xff; }
    return c;
  }
  function buildRequest(selector) {
    const buf = [0, 0x01, 0x00, 0x02, CATSEL, selector];
    buf[0] = crc8(buf);
    return buf;
  }
  const toWire = (buf) => buf.flatMap((b) => [b >> 4, b & 0xf]);
  const nibDecode = (arr) => {
    const out = [];
    for (let i = 0; i + 1 < arr.length; i += 2) out.push((arr[i] << 4) | arr[i + 1]);
    return out;
  };
  function reassemble(replies) {
    const byCmd = new Map();
    for (const b of replies) {
      if (b.length < 4) continue;
      if (!byCmd.has(b[1])) byCmd.set(b[1], []);
      byCmd.get(b[1]).push([b[2], b.slice(4)]);
    }
    const out = new Map();
    for (const [cmd, chunks] of byCmd) {
      chunks.sort((a, z) => a[0] - z[0]);
      out.set(cmd, chunks.flatMap((c) => c[1]));
    }
    return out;
  }
  function splitNames(blob, hdr = 2, rec = 20) {
    const names = [];
    const dv = new DataView(new Uint8Array(blob).buffer);
    for (let i = hdr; i + rec <= blob.length; i += rec) {
      const idx = dv.getUint32(i, true);
      let nm = "";
      for (let j = i + 4; j < i + rec; j++) { if (blob[j] === 0) break; nm += String.fromCharCode(blob[j]); }
      names.push({ slot: idx, name: nm.trim() });
    }
    return names;
  }

  // GP-150 first (its own protocol), then GP-50 before "GP-5" (substring).
  const findPort = (map) => {
    const ports = [...map.values()];
    return ports.find((p) => (p.name || "").includes("GP-150"))
      || ports.find((p) => (p.name || "").includes("GP-50"))
      || ports.find((p) => (p.name || "").includes("GP-5")) || null;
  };
  const profileForPort = (name) =>
    (name || "").includes("GP-150") ? PRST.GP150 : (name || "").includes("GP-50") ? PRST.GP50 : PRST.GP5;

  // --- connection state ------------------------------------------------------
  let access = null, input = null, output = null, profile = null;
  let namesCache = null;
  let chain = Promise.resolve(); // serializes all device requests (one at a time)
  let ht = null; // HtTransport session when profile.transport === "ht" (it owns its own queue)
  let connecting = null; // the in-flight connect(): concurrent callers share it
  const isHt = () => !!(profile && profile.transport === "ht");

  function assertReady() {
    if (!input || !output) throw new Error("not connected — call WebMidiDevice.connect() first");
    if (!root.PRST) throw new Error("prst.js (window.PRST) is not loaded");
    if (isHt() && !ht) throw new Error("GP-150 session is not open — reconnect");
  }
  function assertSlot(slot) {
    const n = profile ? profile.slots : 100;
    if (!(Number.isInteger(slot) && slot >= 0 && slot < n)) throw new Error(`slot ${slot} out of range 0..${n - 1}`);
  }

  // Run `fn` after every previously-queued request has finished + settled.
  function serialize(fn) {
    const run = chain.then(fn, fn);
    chain = run.then(
      () => new Promise((r) => setTimeout(r, SETTLE_MS)),
      () => new Promise((r) => setTimeout(r, SETTLE_MS))
    );
    return run;
  }

  // One request/reply exchange: send `wire`, collect nibble-decoded SysEx frames
  // until the stream goes idle, reassemble, return the longest blob.
  function exchange(wire) {
    return new Promise((resolve, reject) => {
      const replies = [];
      const handler = (e) => {
        const d = Array.from(e.data);
        if (d[0] === 0xf0 && d[d.length - 1] === 0xf7) replies.push(nibDecode(d.slice(1, -1)));
      };
      input.onmidimessage = handler;
      try { output.send(wire); }
      catch (err) { input.onmidimessage = null; return reject(err); }
      const t0 = performance.now(); let last = t0, seen = 0;
      const tick = () => {
        if (replies.length > seen) { seen = replies.length; last = performance.now(); }
        const now = performance.now();
        if (now - t0 > READ_TIMEOUT_MS || (seen > 0 && now - last > IDLE_MS)) {
          input.onmidimessage = null;
          const banks = reassemble(replies);
          const blob = [...banks.values()].sort((a, z) => z.length - a.length)[0] || [];
          return resolve({ blob, frames: replies.length });
        }
        setTimeout(tick, 25);
      };
      tick();
    });
  }

  // Re-entrant: a second connect() while one is in flight gets the same promise —
  // two GP-150 sessions on one port would steal each other's input handler.
  function connect() {
    if (!connecting) connecting = doConnect().finally(() => { connecting = null; });
    return connecting;
  }

  async function doConnect() {
    if (!navigator.requestMIDIAccess) throw new Error("this browser has no WebMIDI (use Chrome or Edge)");
    if (ht) { ht.close(); ht = null; } // a reconnect must not leave the old session's input handler behind
    access = await navigator.requestMIDIAccess({ sysex: true });
    input = findPort(access.inputs);
    output = findPort(access.outputs);
    if (!input || !output) { disconnect(); throw new Error("no GP-5 / GP-50 / GP-150 MIDI port found — connect it and close Valeton Suite"); }
    profile = profileForPort(input.name);
    namesCache = null;
    if (isHt()) {
      if (!root.HtTransport || !root.HtProto) { disconnect(); throw new Error("ht_proto.js / ht_transport.js are not loaded"); }
      ht = root.HtTransport.create(input, output);
      let ok = false;
      try { ok = await ht.hello(); } catch { ok = false; }
      if (!ok) { disconnect(); throw new Error("GP-150 did not answer the handshake / session open — unplug/replug USB or close Valeton Suite"); }
    }
    return { key: profile.key, name: profile.name, port: input.name };
  }

  function disconnect() {
    if (ht) ht.close();
    ht = null;
    input = output = access = profile = namesCache = null;
  }

  const isConnected = () => !!(input && output);
  const device = () => (profile ? { key: profile.key, name: profile.name } : null);
  // Link health. corruptFrames: GP-150 frames dropped for a bad CRC (always 0 on GP-5/GP-50).
  const stats = () => ({ corruptFrames: ht ? ht.stats().badFrames : 0 });

  function readNames() {
    assertReady();
    if (isHt()) return Promise.resolve((namesCache = [])); // no bulk name read on the GP-150: scan supplies names
    return serialize(async () => {
      const { blob } = await exchange([0xf0, ...toWire(buildRequest(SEL_NAMES)), 0xf7]);
      namesCache = splitNames(blob);
      return namesCache;
    });
  }

  // Read an arbitrary catalog selector (e.g. 0x24 SnapTone names, 0x20 User IR
  // names) and return the longest reassembled blob. Same transport as readNames.
  function readBankBlob(selector) {
    assertReady();
    if (isHt()) return Promise.reject(new Error("SnapTone/IR catalog read is not supported on the GP-150 yet"));
    return serialize(async () => (await exchange([0xf0, ...toWire(buildRequest(selector)), 0xf7])).blob);
  }

  function selectSlot(slot) {
    assertReady();
    if (isHt()) { assertSlot(slot); return ht.selectSlot(slot); }
    if (!(slot >= 0 && slot <= 99)) throw new Error(`slot ${slot} out of range 0..99`);
    return serialize(async () => {
      output.send([0xc0, slot & 0x7f]); // Program Change — non-destructive
      await new Promise((r) => setTimeout(r, POST_PC_MS));
    });
  }

  // Read the CURRENTLY active patch body (0x41) and rebuild a .prst. `name` labels
  // the patch (the device body carries no name). One retry on a short/raced read.
  function readActivePrst(name = "") {
    assertReady();
    if (isHt()) {
      return ht.readPreset(root.HtProto.SLOT_ACTIVE).then((p) => { if (!p) throw new Error("the GP-150 sent no active preset"); return p; });
    }
    return serialize(async () => {
      const wire = [0xf0, ...toWire(buildRequest(SEL_BODY)), 0xf7];
      const strip = (blob) => (blob[0] === CATSEL && blob[1] === SEL_BODY ? blob.slice(2) : blob);
      const wantLen = PRST.bodyLen(profile);
      let body = strip((await exchange(wire)).blob);
      if (body.length !== wantLen) {
        await new Promise((r) => setTimeout(r, 400));
        body = strip((await exchange(wire)).blob);
      }
      if (body.length !== wantLen) {
        throw new Error(`body read landed ${body.length} bytes (expected ${wantLen})`);
      }
      return PRST.rebuild(name, body, profile);
    });
  }

  // Select slot `slot`, then read its body back as a .prst. Uses the cached name
  // from readNames() (fetched once if not already cached).
  async function readSlotPrst(slot) {
    assertReady();
    if (isHt()) {
      const p = await readSlotOrNull(slot);
      if (!p) throw new Error(`slot ${slot} is empty (no reply)`);
      return p;
    }
    if (!namesCache) await readNames();
    const hit = (namesCache || []).find((n) => n.slot === slot);
    await selectSlot(slot);
    return readActivePrst(hit ? hit.name : `slot${slot}`);
  }

  // One slot's .prst, or null when the slot is empty (GP-150 only: the pedal stays
  // silent). GP-5/GP-50 have no "empty" read, so this is readSlotPrst there.
  async function readSlotOrNull(slot) {
    assertReady();
    if (isHt()) { assertSlot(slot); return ht.readPreset(slot); }
    return readSlotPrst(slot);
  }

  // Read `slots` (an array, or a count = 0..n-1) one at a time, calling
  // onEach({slot, prst}) — prst null for an empty slot — or onEach({slot, error}).
  // Resolves {read, empty, errors}. Never aborts on a per-slot failure.
  async function scanSlots(slots, onEach) {
    const list = Array.isArray(slots) ? slots : Array.from({ length: slots }, (_, i) => i);
    const sum = { read: 0, empty: 0, errors: 0 };
    for (const slot of list) {
      let prst = null;
      try { prst = await readSlotOrNull(slot); } catch (error) { sum.errors++; if (onEach) await onEach({ slot, error }); continue; }
      if (prst) sum.read++; else sum.empty++;
      if (onEach) await onEach({ slot, prst });
    }
    return sum;
  }

  // Gated raw sender for a pre-built, pre-validated write stream. Owns the port
  // and the one-at-a-time chain, so writes can't race reads. All write gating
  // (build/validate/WRITE_VERIFIED/confirm) lives in webmidi_write.js; this just
  // refuses to move bytes without an explicit confirm+validated from that layer.
  // Paces like Suite: one block, wait for the device ACK (shallow queue), repeat.
  // GP-150: the HT import stream goes to the session's writePreset (its queue, its
  // input handler, its auto-ACK of the 0x08) — and only while
  // WebMidiWrite.WRITE_VERIFIED.gp150 is true or the caller passes allowUnverified.
  function _sendStream(packets, opts) {
    assertReady();
    const { confirm = false, validated = false, ackWaitMs = 150, allowUnverified = false } = opts || {};
    if (!(confirm && validated)) throw new Error("refusing to send: _sendStream requires confirm && validated");
    const htFrames = (packets || []).some((w) => w && w[0] === 0xf0 && w[1] === 0x7f);
    if (isHt()) {
      const W = root.WebMidiWrite;
      if (!allowUnverified && !(W && W.WRITE_VERIFIED && W.WRITE_VERIFIED.gp150 === true)) {
        throw new Error("refusing to send: GP-150 writes are not verified on hardware yet (WRITE_VERIFIED.gp150 is false) — pass { allowUnverified: true } only for the supervised verification write");
      }
      if (!packets || !packets.length || packets.some((w) => !w || w[0] !== 0xf0 || w[1] !== 0x7f)) {
        throw new Error("refusing to send: the GP-150 takes an HT import stream (F0 7F frames), not GP-5/GP-50 packets");
      }
      return ht.writePreset(packets);
    }
    if (htFrames) throw new Error(`refusing to send: GP-150 (HT) frames to a ${profile.name}`);
    return serialize(async () => {
      let acks = 0, pending = 0;
      input.onmidimessage = (e) => { if (e.data[0] === 0xf0) pending++; };
      try {
        for (const w of packets) {
          const before = pending;
          output.send(w);
          const t0 = performance.now();
          while (performance.now() - t0 < ackWaitMs && pending === before) {
            await new Promise((r) => setTimeout(r, 5));
          }
          if (pending > before) acks++;
        }
      } finally { input.onmidimessage = null; }
      return { sent: packets.length, acks };
    });
  }

  root.WebMidiDevice = {
    connect, disconnect,
    isConnected, device, stats, readNames, readBankBlob, selectSlot, readActivePrst, readSlotPrst, readSlotOrNull, scanSlots, _sendStream,
    _ht: () => ht, // the GP-150 HtTransport session (null otherwise) — tests / webmidi_write
    // exposed for tests / the probe
    _codec: { crc8, buildRequest, toWire, nibDecode, reassemble, splitNames, findPort },
  };
})(typeof self !== "undefined" ? self : this);
