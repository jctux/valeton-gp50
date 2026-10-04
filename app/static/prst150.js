"use strict";
/*
 * prst150.js — the 1128-byte GP-150 .prst container, browser port of
 * patch/prst150_format.py (same layout constants, same edit semantics; verified
 * byte-for-byte by app/tests/test_prst150_js.mjs). Exposes the method surface
 * prst.js has, so patchlib/static_api/explorer can treat it as "the codec" for
 * profile gp150.
 */
(function (root) {
  const MAGIC = [0x11, 0x30, 0x64, 0x04];
  const PRST_LEN = 1128, IDX_OFF = 0x04, BPM_OFF = 0x24, VOL_OFF = 0x26, NAME_OFF = 0x2c, NAME_LEN = 0x44;
  const ORDER_OFF = 0x78, BLOCKS_OFF = 0x84, BLOCK_LEN = 0x44, N_BLOCKS = 12, N_PARAMS = 15, FOOTER_OFF = 0x3b4;
  const SLOTS = ["NR", "PRE", "WAH", "DST", "N->S", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "VOL"];
  const AMP_SLOT = 5;
  const CANONICAL_ENGINE = [null, 0x05, 0x03, 0x07, 0x07, 0x00, 0x1a, 0x01, 0x04, 0x0b, 0x0c, 0x06];
  // The "None" effect: engine 0x06 at any chain position (corpus). Type 3 is None in
  // NONE_SLOTS (no real type 3 in the spec; the ring's per-slot "None" entries); AMP,
  // DST, DLY, RVB and VOL have a real type 3. VOL's "Volume" also carries 0x06 at pos 11.
  const ENGINE_BYPASS = 0x06, VOL_SLOT = 11, NONE_TYPE = 3, NONE_SLOTS = [0, 1, 2, 4, 6, 7, 8];
  const ENGINE_OVERRIDES = { "0:16": 0x1a, "3:117": 0x08, "3:118": 0x08, "3:122": 0x08, "4:57": 0x07, "4:64": 0x07, "4:122": 0x08, "4:112": 0x1a, "6:60": 0x0a, "1:26": 0x00, "8:41": 0x01, "8:54": 0x01, "7:25": 0x04 };
  const layout = {
    BLOCK_NAMES: SLOTS.slice(), MOVABLE_BLOCKS: new Set(SLOTS.filter((s) => s !== "AMP")),
    PARAMS_PER_BLOCK: N_PARAMS, N_BLOCKS, AMP_INDEX: AMP_SLOT, NS_INDEX: 4, CAB_INDEX: 6,
    lockedFirst: true, hasFootswitches: false, nsIsRegularBlock: true, emptyName: "",
    nameMax: 13, // editor cap (spec §3.2); the file holds up to 67 bytes
  };

  const u8 = (b) => (b instanceof Uint8Array ? b : Uint8Array.from(b));
  const dv = (b) => new DataView(b.buffer, b.byteOffset, b.byteLength);
  const check = (b) => { if (b.length !== PRST_LEN) throw new Error(`expected a ${PRST_LEN}-byte GP-150 .prst, got ${b.length}`); };

  const detect = (b) => { b = u8(b); return b.length === PRST_LEN && MAGIC.every((v, i) => b[i] === v); };
  const readIndex = (b) => u8(b)[IDX_OFF];
  function writeIndex(b, index) { if (!(index >= 0 && index <= 199)) throw new Error(`preset index out of range: ${index}`); b[IDX_OFF] = index; }
  function readName(b) { b = u8(b); let s = ""; for (let i = NAME_OFF; i < NAME_OFF + NAME_LEN; i++) { if (b[i] === 0) break; s += String.fromCharCode(b[i]); } return s.trim(); }
  function writeName(b, name) { const n = Math.min(name.length, NAME_LEN - 1); for (let i = 0; i < NAME_LEN; i++) b[NAME_OFF + i] = i < n ? name.charCodeAt(i) & 0xff : 0; }
  const readVolBpm = (b) => { b = u8(b); return [b[VOL_OFF], b[BPM_OFF]]; };
  function writeVolBpm(b, vol, bpm) { if (vol != null) b[VOL_OFF] = Math.max(0, Math.min(100, Math.trunc(Number(vol)))); if (bpm != null) b[BPM_OFF] = Math.trunc(Number(bpm)) & 0xff; }
  const readOrder = (b) => Array.from(u8(b).subarray(ORDER_OFF, ORDER_OFF + N_BLOCKS));
  function isPermutation(order) { if (!order || order.length !== N_BLOCKS) return false; const seen = new Set(); for (const v of order) { if (!Number.isInteger(v) || v < 0 || v >= N_BLOCKS || seen.has(v)) return false; seen.add(v); } return true; }
  const blockOff = (pos) => { if (!(pos >= 0 && pos < N_BLOCKS)) throw new Error(`block position out of range: ${pos}`); return BLOCKS_OFF + pos * BLOCK_LEN; };
  function blockAt(b, pos) {
    b = u8(b); const o = blockOff(pos), d = dv(b), params = [];
    for (let i = 0; i < N_PARAMS; i++) params.push(d.getFloat32(o + 8 + i * 4, true));
    return { pos, enabled: b[o], type: b[o + 4], subtype: b[o + 5], ext: b[o + 6], engine: b[o + 7], params };
  }
  function blocksBySlot(b) {
    const order = readOrder(b), out = new Array(N_BLOCKS).fill(null);
    order.forEach((slot, pos) => { out[slot] = blockAt(b, pos); });
    if (out.some((x) => x === null)) throw new Error("chain order is not a permutation of 0..11");
    return out;
  }
  function setBlock(b, pos, f) { const o = blockOff(pos); for (const [off, k] of [[0, "enabled"], [4, "type"], [5, "subtype"], [6, "ext"], [7, "engine"]]) if (f[k] != null) b[o + off] = Number(f[k]) & 0xff; }
  function setParam(b, pos, i, v) { if (!(i >= 0 && i < N_PARAMS)) throw new Error(`param index out of range: ${i}`); dv(b).setFloat32(blockOff(pos) + 8 + i * 4, Number(v), true); }
  function writeOrder(b, order) {
    order = Array.from(order, Number);
    if (!isPermutation(order)) throw new Error("chain order must be a permutation of 0..11");
    if (order[0] !== AMP_SLOT) throw new Error("chain position 0 must be AMP (slot 5)");
    const cur = readOrder(b), blocks = {};
    cur.forEach((slot, pos) => { blocks[slot] = b.slice(blockOff(pos), blockOff(pos) + BLOCK_LEN); });
    order.forEach((slot, pos) => { b.set(blocks[slot], blockOff(pos)); });
    for (let i = 0; i < N_BLOCKS; i++) b[ORDER_OFF + i] = order[i];
  }
  // Engines are firmware-written; recomputed only when a block's type/position changes.
  function engineFor(pos, slot, type, params) {
    if (type === NONE_TYPE && NONE_SLOTS.includes(slot)) return ENGINE_BYPASS;
    if (pos === 0) {
      const p = Array.from(params).concat(new Array(N_PARAMS).fill(0));
      if (type === 33 || type === 2 || type === 7) return 0x01;
      if (type === 9) return 0x03;
      if (type === 1) { if (p[4] !== 0) return 0x01; return p[3] >= 50 ? 0x03 : 0x00; }
      return (p[4] !== 0 || p[5] !== 0) ? 0x01 : 0x00;
    }
    const ov = ENGINE_OVERRIDES[`${slot}:${type}`];
    return ov !== undefined ? ov : CANONICAL_ENGINE[pos];
  }
  const modelKey = (slot, type, subtype = 0, ext = 0) => ((slot << 24) | (ext << 16) | (subtype << 8) | type) >>> 0;
  const modelRecords = (b) => blocksBySlot(b).map((blk, s) => [blk.type, s, (blk.ext << 16) | (blk.subtype << 8) | blk.type]);
  const modelRecOffset = () => -1;
  function bypassMask(b) { let m = 0; blocksBySlot(b).forEach((blk, s) => { if (blk.enabled) m |= 1 << s; }); return m >>> 0; }
  const paramFloats = (b) => blocksBySlot(b).flatMap((blk) => blk.params);
  const fsOffset = () => -1;
  const readFootswitches = () => [0, 0];
  const refixCrc = () => {};
  function refreshEngine(b, pos, slot) { const blk = blockAt(b, pos); setBlock(b, pos, { engine: engineFor(pos, slot, blk.type, blk.params) }); }
  function applyEdits(prst, edits) {
    prst = u8(prst); check(prst); edits = edits || {};
    const b = Uint8Array.from(prst);
    const oldOrder = readOrder(b);
    if (edits.order != null) writeOrder(b, edits.order);
    const order = readOrder(b), posOf = {}, moved = new Set(), remodeled = new Set();
    order.forEach((slot, pos) => { posOf[slot] = pos; if (oldOrder.indexOf(slot) !== pos) moved.add(slot); });
    for (const [slot, key] of Object.entries(edits.models || {})) {
      const k = Number(key) >>> 0, s = Number(slot), pos = posOf[s];
      remodeled.add(s);
      setBlock(b, pos, { type: k & 0xff, subtype: (k >> 8) & 0xff, ext: (k >> 16) & 0xff });
      if (s === AMP_SLOT && ((k & 0xff) === 2 || (k & 0xff) === 7)) setBlock(b, pos, { ext: 1 });
      if (s === 4 && (k & 0xff) === 0) setBlock(b, pos, { subtype: 1 });
    }
    for (const [slot, ps] of Object.entries(edits.params || {})) for (const [i, v] of Object.entries(ps)) setParam(b, posOf[Number(slot)], Number(i), v);
    for (const [slot, on] of Object.entries(edits.bypass || {})) setBlock(b, posOf[Number(slot)], { enabled: on ? 1 : 0 });
    const s = edits.settings || {};
    writeVolBpm(b, s.patch_vol, s.bpm);
    if (edits.name != null) writeName(b, String(edits.name));
    for (const slot of Array.from(new Set([...moved, ...remodeled])).sort((x, y) => x - y)) {
      const pos = posOf[slot];
      // a "None" block that only moved stays None (engine 0x06 at any position)
      if (!remodeled.has(slot) && slot !== VOL_SLOT && b[blockOff(pos) + 7] === ENGINE_BYPASS) continue;
      refreshEngine(b, pos, slot);
    }
    return b;
  }
  const BLANK_B64 = "ETBkBAAAAAAQMFgEABVIPP//DAAQDA8DAwMEECgAAMggMFAAeAAyAAAAAABOZXcgR0VOLgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwMDwDBQABAgMEBgcICQoLAAAAAAMAAAYAAMhCAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAABQAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAADAAAgQQAAjEIAAKBCAACAPwAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAYAAEhCAABIQgAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAQAAABwAASEIAAEhCAABIQgAANEIAAEhCAABcQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAhAAAAAAAgQQAAQEEAAIA/AABIQwAAyEIAAEDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAABABABoAAAAAAACCQgAAAAAAAAAAAAAAAAAAmEEACDVGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAABgAAyEIAAAAAAAAAAAAAAAAAAAAAAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAAAGAADIQgAAAD8AAEhCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAsAACBBAIDZQwAAoEEAAAAAAACAPwAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAQAADAAAcEEAAEhCAAAgQgAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAADAAAGAADIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUDAMAAwAAAANAAAABAAAAGAwcAALAAAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIBAAAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIAAAAAcDAEAAAAAACAMCQAdA4AAAQAAAAAAgAAAAAAAAAAAAABAAAAAQAAAAAAAAAAAAAA"; // same string as patch/prst150_format.BLANK_B64 (factory "New GEN.")
  const b64bytes = (s) => (typeof atob === "function" ? Uint8Array.from(atob(s), (c) => c.charCodeAt(0)) : Uint8Array.from(Buffer.from(s, "base64")));
  function blankPrst(slot = 0) { const b = b64bytes(BLANK_B64); writeIndex(b, Number(slot)); return b; }

  const API = {
    key: "gp150", layout, MAGIC, PRST_LEN, NAME_OFF, NAME_LEN, ORDER_OFF, BLOCKS_OFF, BLOCK_LEN, N_BLOCKS, N_PARAMS, FOOTER_OFF, SLOTS, AMP_SLOT,
    ENGINE_BYPASS, NONE_TYPE, NONE_SLOTS,
    detect, readIndex, writeIndex, readName, writeName, readVolBpm, writeVolBpm, readOrder, writeOrder, isPermutation,
    blockAt, blocksBySlot, setBlock, setParam, engineFor, modelKey, modelRecords, modelRecOffset, bypassMask, paramFloats,
    fsOffset, readFootswitches, refixCrc, applyEdits, blankPrst, BLANK_B64,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = API; else root.PRST150 = API;
})(typeof self !== "undefined" ? self : this);
