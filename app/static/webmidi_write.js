"use strict";
/*
 * webmidi_write.js — host->device patch WRITE transport for the browser.
 *
 * Port of patch/device_write.py: builds the exact packet stream Valeton Suite
 * sends on a patch import, and gate-sends it over WebMIDI. Kept separate from
 * webmidi_device.js (read/select) because writing is the one operation that can
 * wedge the pedal — this file carries all of that risk and all of its gates.
 *
 * The pure builder/validator (buildPatchWriteStream / validateStream) are
 * verified byte-for-byte against the Python oracle (app/tests/test_write_js.mjs),
 * whose builder is in turn verified 29/29 against real GP-50 Suite captures.
 *
 * writeSlot() REFUSES unless: confirm===true, the stream validates, and the
 * device's write protocol is capture-verified (WRITE_VERIFIED) — mirroring
 * device_write.send_stream. GP-5 write is unverified and refused unless
 * allowUnverified is set. Needs window.PRST and (to send) window.WebMidiDevice.
 *
 * GP-150 (HT transport): a 1128-byte preset becomes Suite's whole-preset import
 * stream (family-0x70 chunks, ht_proto.js importStream), byte-exact against the
 * captured Suite import (app/tests/test_write_gp150_js.mjs); validateStream
 * re-parses every frame. Never sent while WRITE_VERIFIED.gp150 is false unless
 * allowUnverified (the supervised hardware verification write only).
 */
(function (root) {
  const PRST = root.PRST || (typeof module !== "undefined" && module.exports ? require("./prst.js") : null);
  const htProto = () => {
    const HT = root.HtProto || (typeof module !== "undefined" && module.exports ? require("./ht_proto.js") : null);
    if (!HT) throw new Error("ht_proto.js (window.HtProto) is not loaded");
    return HT;
  };

  const PATCH_WRITE_CMD = 0x1d; // host->device patch write (from Suite import captures)
  const PATCH_BLOCK = 19; // payload bytes per write block
  const PATCH_HDR = [0x11, 0x4f]; // constant marker before the slot byte
  const NAME_OFF = 0x19;
  // Which devices' WRITE protocol is capture-verified (see device_write.py).
  const WRITE_VERIFIED = { gp50: true, gp5: false, gp150: false };
  const ACK_WAIT_MS = 150; // wait for the device ACK after each block (shallow queue)
  const GP150_TRANSFER_ID = 0x24; // the transfer id Suite used in the captured import

  const crc8 = (bytes) => PRST.crc8(bytes);
  const nibDecode = (mid) => {
    const out = [];
    for (let i = 0; i + 1 < mid.length; i += 2) out.push((mid[i] << 4) | mid[i + 1]);
    return out;
  };
  const expectedPayloadLen = (profile) => 6 + (profile.prstLen - NAME_OFF);
  // Reassembled 0x1D payload length -> profile, legacy (GP-5/GP-50) transport only:
  // the GP-150 never writes 0x1D packets, so a 1109-byte 0x1D stream is not a write.
  function legacyPayloadLens() {
    const out = {};
    for (const p of Object.values(PRST.DEVICES)) if (p.transport !== "ht") out[expectedPayloadLen(p)] = p;
    return out;
  }
  // An HT (GP-150) stream: F0 7F frames. Legacy packets carry nibbles after F0.
  const isHtStream = (packets) => !!(packets && packets.length && packets[0] && packets[0][0] === 0xf0 && packets[0][1] === 0x7f);
  const htProfile = () => Object.values(PRST.DEVICES).find((p) => p.transport === "ht");

  function buildPacket(cmd, index, payload) {
    const buf = [0, cmd & 0xff, index & 0xff, payload.length & 0xff, ...payload];
    buf[0] = crc8(buf);
    const wire = [0xf0];
    for (const b of buf) wire.push(b >> 4, b & 0x0f);
    wire.push(0xf7);
    return wire;
  }

  // Suite's exact patch-import stream for writing `prst` to `slot`. The 6-byte
  // header replaces the .prst body's leading FF FF FF FF sentinel; payload is
  // prst[0x19:], streamed in 19-byte blocks, index 0..N. Returns wire packets;
  // does NOT send.
  // A GP-150 preset (1128 bytes) yields the HT import stream (buildGp150WriteStream).
  function buildPatchWriteStream(prst, slot) {
    prst = prst instanceof Uint8Array ? prst : Uint8Array.from(prst);
    if (!(slot >= 0 && slot <= 0xff)) throw new Error(`slot out of range: ${slot}`);
    const profile = PRST.detect(prst); // throws if not a known GP-5/GP-50 .prst
    if (profile.transport === "ht") return buildGp150WriteStream(prst, slot);
    if (prst.length !== profile.prstLen) {
      throw new Error(`expected a ${profile.prstLen}-byte ${profile.name} .prst, got ${prst.length}`);
    }
    const payload = [...PATCH_HDR, slot, 0x00, 0x00, 0x00, ...prst.subarray(NAME_OFF)];
    const packets = [];
    for (let i = 0; i < payload.length; i += PATCH_BLOCK) {
      packets.push(buildPacket(PATCH_WRITE_CMD, Math.floor(i / PATCH_BLOCK), payload.slice(i, i + PATCH_BLOCK)));
    }
    return packets;
  }

  // Suite's patch-import stream for a GP-150 preset into `slot`: family-0x70 chunks
  // (0-based indexes, 119 bytes each, a shorter final one) carrying [01][icrc][6c 04]
  // + 01 03 11 30 + the file with byte 0x0A = 0x5C. The pedal takes the slot from
  // the file's own index byte 0x04, set here on a copy. Mirrors
  // device_write.build_gp150_write_stream. Returns wire packets; does NOT send.
  function buildGp150WriteStream(prst, slot, transferId = GP150_TRANSFER_ID) {
    prst = prst instanceof Uint8Array ? prst : Uint8Array.from(prst);
    const C = PRST.codecFor("gp150"), P = htProfile();
    if (!C.detect(prst)) throw new Error("not a GP-150 .prst (expected 1128 bytes starting 11 30 64 04)");
    if (!(Number.isInteger(slot) && slot >= 0 && slot < P.slots)) throw new Error(`slot out of range 0..${P.slots - 1}: ${slot}`);
    if (!(Number.isInteger(transferId) && transferId >= 1 && transferId <= 0x7f)) throw new Error(`transfer id must be 1..0x7F (a SysEx data byte): ${transferId}`);
    const b = Uint8Array.from(prst);
    C.writeIndex(b, slot);
    return htProto().importStream(transferId, b).map((w) => Array.from(w));
  }

  // Re-parse a GP-150 import stream before sending (mirrors
  // device_write.validate_gp150_stream). Every frame: F0 7F .. F7, 7-bit, outer
  // CRC, a 0x70 chunk, one transfer id, index i (0-based) at offset 119*i,
  // full-size except a shorter final one. The joined message: inner CRC, exactly
  // 01 03 11 30 + a 1128-byte GP-150 preset with 0x0A = 0x5C whose index byte is a
  // GP-150 slot (== `slot` when given).
  function validateGp150Stream(packets, slot) {
    const HT = htProto(), P = htProfile();
    if (!packets || !packets.length) return [false, "empty stream"];
    const frames = [];
    let tid = null;
    const last = packets.length - 1;
    for (let i = 0; i < packets.length; i++) {
      const w = packets[i] ? Uint8Array.from(packets[i]) : new Uint8Array(0);
      if (w.length < 10 || w[0] !== 0xf0 || w[1] !== 0x7f || w[w.length - 1] !== 0xf7) return [false, `packet ${i}: not an F0 7F .. F7 HT frame`];
      if (w.subarray(1, -1).some((b) => b > 0x7f)) return [false, `packet ${i}: a data byte above 0x7F`];
      let f, c;
      try {
        f = HT.parseFrame(w);
        if (f.family !== HT.FAMILY_PATCH || f.tx4[0] !== 0x08 || !f.body.length) return [false, `packet ${i}: not a patch (0x70) chunk`];
        c = HT.chunkFields(f);
      } catch (e) { return [false, `packet ${i}: ${e.message}`]; }
      if (c.transferId < 1) return [false, `packet ${i}: transfer id 0 (must be 1..0x7F)`];
      if (tid === null) tid = c.transferId;
      else if (c.transferId !== tid) return [false, `packet ${i}: transfer id ${c.transferId} != ${tid}`];
      if (c.index !== i) return [false, `packet ${i}: chunk index ${c.index} (expected ${i}, 0-based)`];
      if (c.offset !== HT.OFFSET_STEP * i) return [false, `packet ${i}: offset ${c.offset} != ${HT.OFFSET_STEP * i}`];
      if (i < last && w.length !== HT.FULL_WIRE_LEN) return [false, `packet ${i}: short chunk before the final one`];
      if (i === last && w.length >= HT.FULL_WIRE_LEN) return [false, "no final short chunk (the pedal would wait for more)"];
      frames.push(f);
    }
    let payload;
    try { payload = HT.assembleStream(frames).payload; } catch (e) { return [false, e.message]; }
    if (!HT.HEAD_IMPORT.every((v, j) => payload[j] === v)) return [false, `payload head ${Array.from(payload.subarray(0, 4))} is not the import head 01 03 11 30`];
    if (payload.length !== 4 + HT.PRESET_LEN) return [false, `import payload is ${payload.length} bytes, expected ${4 + HT.PRESET_LEN}`];
    const prst = payload.subarray(4);
    if (!PRST.codecFor("gp150").detect(prst)) return [false, "payload is not a GP-150 preset (no 11 30 64 04 magic)"];
    if (prst[0x0a] !== HT.IMPORT_BYTE_0A) return [false, `byte 0x0A is ${prst[0x0a]}; Suite's import sends ${HT.IMPORT_BYTE_0A}`];
    const index = prst[4];
    if (index >= P.slots) return [false, `preset index byte ${index} is not a GP-150 slot (0..${P.slots - 1})`];
    if (slot !== undefined && slot !== null && index !== slot) return [false, `preset index byte ${index} != target slot ${slot}`];
    return [true, "ok"];
  }

  // Confirm a stream is well-formed before sending. Mirrors validate_stream. An HT
  // (GP-150) stream is checked by validateGp150Stream (`slot` optional, HT only).
  function validateStream(packets, slot) {
    if (isHtStream(packets)) return validateGp150Stream(packets, slot);
    const payload = [];
    for (let i = 0; i < packets.length; i++) {
      const w = packets[i];
      if (!w || w[0] !== 0xf0 || w[w.length - 1] !== 0xf7) return [false, `packet ${i}: not F0..F7 framed`];
      const buf = nibDecode(w.slice(1, -1));
      if (buf.length < 4) return [false, `packet ${i}: truncated`];
      const [crc, cmd, index, length] = buf;
      if (crc8(buf.slice(1)) !== crc) return [false, `packet ${i}: bad CRC`];
      if (cmd !== PATCH_WRITE_CMD) return [false, `packet ${i}: cmd ${cmd} != patch-write ${PATCH_WRITE_CMD}`];
      if (index !== i) return [false, `packet ${i}: non-contiguous index ${index}`];
      if (length !== buf.length - 4) return [false, `packet ${i}: length ${length} != payload ${buf.length - 4}`];
      for (let j = 0; j < length; j++) payload.push(buf[4 + j]);
    }
    const validLens = {};
    for (const [n, p] of Object.entries(legacyPayloadLens())) validLens[n] = p.name;
    if (!(payload.length in validLens)) {
      return [false, `payload ${payload.length} bytes, expected one of ${Object.keys(validLens).sort()} (GP-50/GP-5)`];
    }
    if (payload[0] !== PATCH_HDR[0] || payload[1] !== PATCH_HDR[1]) {
      return [false, `payload header ${payload[0]},${payload[1]} != ${PATCH_HDR}`];
    }
    return [true, "ok"];
  }

  function inferDeviceKey(packets) {
    if (isHtStream(packets)) return htProfile().key; // any HT stream is a GP-150 write
    let total = 0;
    for (const w of packets) {
      if (!w || w[0] !== 0xf0 || w[w.length - 1] !== 0xf7) return null;
      const buf = nibDecode(w.slice(1, -1));
      if (buf.length >= 4) total += buf[3];
    }
    const p = legacyPayloadLens()[total];
    return p ? p.key : null;
  }

  // Gate-send a patch write to `slot`. REFUSES unless confirm===true, the stream
  // validates, it matches the connected pedal's transport, and the device's write
  // protocol is verified. GP-5/GP-50: paces like Suite, one block, wait for the
  // device ACK (up to ACK_WAIT_MS), then the next. GP-150: the import stream via
  // the HT session (WebMidiDevice._sendStream -> HtTransport writePreset), resolving
  // {sent, acks, notified}; refused while WRITE_VERIFIED.gp150 is false unless
  // allowUnverified.
  async function writeSlot(slot, prst, { confirm = false, allowUnverified = false } = {}) {
    const dev = root.WebMidiDevice;
    if (!dev || !dev.isConnected()) throw new Error("not connected — WebMidiDevice.connect() first");
    const packets = buildPatchWriteStream(prst, slot);
    const [ok, reason] = validateStream(packets, slot);
    if (!ok) throw new Error(`refusing to send: stream did not validate (${reason})`);
    if (!confirm) throw new Error("refusing to send: writeSlot requires { confirm: true }");
    const key = inferDeviceKey(packets);
    const connected = typeof dev.device === "function" ? dev.device() : null;
    const target = connected && PRST.DEVICES[connected.key];
    if (target && (target.transport === "ht") !== isHtStream(packets)) {
      const what = key && PRST.DEVICES[key] ? PRST.DEVICES[key].name : "this";
      throw new Error(`refusing to send: a ${what} preset cannot be written to the connected ${target.name}`);
    }
    if (!allowUnverified) {
      if (key === "gp150" && !WRITE_VERIFIED.gp150) {
        throw new Error("refusing to send: GP-150 writes are unverified — the import stream matches Suite's capture but has not been tested on a GP-150 yet (WRITE_VERIFIED.gp150 is false). Pass { allowUnverified: true } only for the supervised verification write.");
      }
      if (!key) throw new Error("refusing to send: unrecognized patch-write stream (matches no known preset length)");
      if (!WRITE_VERIFIED[key]) {
        throw new Error(`refusing to send: the ${key} patch-write protocol is not capture-verified. Pass { allowUnverified: true } to override at your own risk.`);
      }
    }
    return dev._sendStream(packets, { confirm: true, validated: true, ackWaitMs: ACK_WAIT_MS, allowUnverified });
  }

  const API = {
    PATCH_WRITE_CMD, PATCH_BLOCK, PATCH_HDR, WRITE_VERIFIED, GP150_TRANSFER_ID,
    buildPacket, buildPatchWriteStream, validateStream, expectedPayloadLen, inferDeviceKey, writeSlot,
    buildGp150WriteStream, validateGp150Stream, legacyPayloadLens, isHtStream,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else root.WebMidiWrite = API;
})(typeof self !== "undefined" ? self : this);
