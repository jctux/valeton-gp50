"use strict";
/*
 * ht_proto.js — the GP-150 / GP-180 "HT" SysEx protocol, browser port of
 * patch/ht_proto.py (same names, same bytes; see that file's docstring for the
 * wire layout). Pure functions only; the session/queue lives in ht_transport.js.
 * Exposes window.HtProto (browser) / module.exports (node tests).
 */
(function (root) {
  const POLY = 0x31, CHUNK = 119, OFFSET_STEP = 119, FULL_WIRE_LEN = 248;
  const SLOT_ACTIVE = 0xffff, FAMILY_ACK = 0x00, FAMILY_PRESET_REQ = 0x0f, FAMILY_PATCH = 0x70, FAMILY_IMPORT_DONE = 0x08;
  const PRESET_LEN = 1128, IMPORT_BYTE_0A = 0x5c;
  const HEAD_EXPORT = [0x03, 0x03, 0x11, 0x30], HEAD_IMPORT = [0x01, 0x03, 0x11, 0x30];
  const IMPORT_DONE_PAYLOAD = [0x09, 0x03, 0x11, 0x30]; // logical payload of the pedal's 0x08 after an import
  const u8 = (b) => (b instanceof Uint8Array ? b : Uint8Array.from(b));
  const concat = (...arrs) => { const p = arrs.map(u8); const out = new Uint8Array(p.reduce((s, a) => s + a.length, 0)); let o = 0; for (const a of p) { out.set(a, o); o += a.length; } return out; };
  const eq4 = (a, b) => a.length >= 4 && b.every((v, i) => a[i] === v);
  // tx ids, transfer ids and chunk indexes travel as SysEx data bytes: must be <= 0x7F.
  const id7 = (v, what) => { if (!Number.isInteger(v) || v < 0 || v > 0x7f) throw new Error(`${what} out of 7-bit range: ${v}`); return v; };

  function crc8_31(bytes, init = 0) {
    let c = init;
    for (const b of u8(bytes)) { c ^= b; for (let i = 0; i < 8; i++) c = (c & 0x80) ? ((c << 1) ^ POLY) & 0xff : (c << 1) & 0xff; }
    return c;
  }
  const ocrc = (afterCrc) => crc8_31(afterCrc) & 0x7f;
  const icrc = (payload) => crc8_31(payload);
  function enc(data) { data = u8(data); const out = new Uint8Array(data.length * 2); data.forEach((b, i) => { out[i * 2] = b >> 4; out[i * 2 + 1] = b & 0x0f; }); return out; }
  function dec(nib) {
    nib = u8(nib);
    if (nib.length % 2) throw new Error("nibble stream has odd length");
    const out = new Uint8Array(nib.length / 2);
    for (let i = 0; i < nib.length; i += 2) { if (nib[i] > 0x0f || nib[i + 1] > 0x0f) throw new Error("nibble stream contains a byte above 0x0F"); out[i / 2] = (nib[i] << 4) | nib[i + 1]; }
    return out;
  }
  const logical = (payload) => { payload = u8(payload); const n = payload.length; return concat([0x01, icrc(payload), n & 0xff, (n >> 8) & 0xff], payload); };
  function parseLogical(data) {
    data = u8(data);
    if (data.length < 4) throw new Error("logical message too short");
    const n = data[2] | (data[3] << 8), payload = data.subarray(4, 4 + n);
    if (payload.length !== n) throw new Error(`logical length ${n} but only ${payload.length} bytes present`);
    if (icrc(payload) !== data[1]) throw new Error(`inner CRC mismatch: ${data[1]} != ${icrc(payload)}`);
    return payload;
  }
  function frame(family, tx4, body) {
    tx4 = u8(tx4); if (tx4.length !== 4) throw new Error("tx4 must be 4 bytes");
    const after = concat([family], tx4, body);
    return concat([0xf0, 0x7f, ocrc(after)], after, [0xf7]);
  }
  function parseFrame(wire) {
    wire = u8(wire);
    if (wire.length < 9 || wire[0] !== 0xf0 || wire[1] !== 0x7f || wire[wire.length - 1] !== 0xf7) throw new Error("not an F0 7F ... F7 HT frame");
    const after = wire.subarray(3, wire.length - 1);
    if (ocrc(after) !== wire[2]) throw new Error(`outer CRC mismatch: ${wire[2]} != ${ocrc(after)}`);
    return { family: wire[3], tx4: wire.slice(4, 8), body: wire.slice(8, wire.length - 1) };
  }
  const isChunk = (f) => f.tx4[0] !== 0;
  const chunkFields = (f) => ({ offset: (f.tx4[1] & 0x7f) | ((f.tx4[2] & 0x7f) << 7), transferId: f.tx4[3], index: f.body[0], piece: dec(f.body.subarray(1)) });
  // The logical payload of a short (non-chunk) message: body = one flag byte +
  // nibbles(logical). Host messages carry flag 00; the pedal's 0x08 import notify 01.
  function shortPayload(f) {
    if (isChunk(f) || f.body.length < 9) throw new Error("not a short HT message");
    return parseLogical(dec(f.body.subarray(1)));
  }
  const shortMessage = (family, txId, payload) => frame(family, [0, 0, 0, id7(txId, "tx id") & 0xff], concat([0x00], enc(logical(payload))));
  const hello = () => frame(FAMILY_ACK, [0x00, 0x01, 0x03, 0x00], [0x00]);
  const ack = (id) => frame(FAMILY_ACK, [0, 0, 0, id7(id, "ack id") & 0xff], [0x00]);
  function presetRequest(txId, slot, select = false) {
    if (!(slot >= 0 && slot <= 0xffff)) throw new Error(`slot out of range: ${slot}`);
    return shortMessage(FAMILY_PRESET_REQ, txId, [0x03, 0x03, 0x11, 0x30, 0x11, 0x30, 0x02, 0x00, slot & 0xff, (slot >> 8) & 0xff, select ? 0x00 : 0x01]);
  }
  function assembleStream(frames) {
    if (!frames.length) throw new Error("empty stream");
    const parts = frames.map(chunkFields).sort((a, z) => a.index - z.index);
    if (new Set(parts.map((p) => p.transferId)).size !== 1) throw new Error("mixed transfer ids");
    const base = parts[0].index; const pieces = [];
    parts.forEach((p, n) => {
      if (p.index !== base + n) throw new Error(`chunk index gap at ${p.index}`);
      if (p.offset !== OFFSET_STEP * n) throw new Error(`chunk offset ${p.offset} != ${OFFSET_STEP * n}`);
      if (n < parts.length - 1 && p.piece.length !== CHUNK) throw new Error(`short chunk ${p.index} before the final chunk`);
      pieces.push(p.piece);
    });
    if (parts[parts.length - 1].piece.length === CHUNK) throw new Error("stream has no final (short) chunk");
    return { transferId: parts[0].transferId, payload: parseLogical(concat(...pieces)) };
  }
  function presetFromPayload(payload) {
    payload = u8(payload);
    if (!(eq4(payload, HEAD_EXPORT) || eq4(payload, HEAD_IMPORT))) throw new Error("unexpected preset payload head");
    const prst = payload.slice(4, 4 + PRESET_LEN);
    if (prst.length !== PRESET_LEN) throw new Error(`preset payload is ${prst.length} bytes, expected ${PRESET_LEN}`);
    return prst;
  }
  function importPayload(prst) {
    prst = u8(prst); if (prst.length !== PRESET_LEN) throw new Error(`expected a ${PRESET_LEN}-byte .prst, got ${prst.length}`);
    const b = Uint8Array.from(prst); b[0x0a] = IMPORT_BYTE_0A;
    return concat(HEAD_IMPORT, b);
  }
  function chunkFrames(family, transferId, data, host = true) {
    data = u8(data); id7(transferId, "transfer id");
    const pieces = [];
    for (let i = 0; i < data.length; i += CHUNK) pieces.push(data.subarray(i, i + CHUNK));
    if (!pieces.length || pieces[pieces.length - 1].length === CHUNK) pieces.push(new Uint8Array(0));
    return pieces.map((piece, n) => {
      const off = OFFSET_STEP * n, idx = id7(host ? n : n + 1, "chunk index");
      return frame(family, [0x08, off & 0x7f, (off >> 7) & 0x7f, transferId & 0xff], concat([idx], enc(piece)));
    });
  }
  const importStream = (transferId, prst) => chunkFrames(FAMILY_PATCH, transferId, logical(importPayload(prst)), true);

  const API = { crc8_31, ocrc, icrc, enc, dec, logical, parseLogical, frame, parseFrame, isChunk, chunkFields, shortPayload, shortMessage, hello, ack, presetRequest, assembleStream, presetFromPayload, importPayload, chunkFrames, importStream, SLOT_ACTIVE, FAMILY_ACK, FAMILY_PRESET_REQ, FAMILY_PATCH, FAMILY_IMPORT_DONE, FULL_WIRE_LEN, PRESET_LEN, CHUNK, OFFSET_STEP, IMPORT_BYTE_0A, HEAD_IMPORT, HEAD_EXPORT, IMPORT_DONE_PAYLOAD };
  if (typeof module !== "undefined" && module.exports) module.exports = API; else root.HtProto = API;
})(typeof self !== "undefined" ? self : this);
