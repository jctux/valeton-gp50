"use strict";
/*
 * prst150.js — the 1128-byte GP-150 .prst container, browser port of
 * patch/prst150_format.py (same layout constants, same edit semantics; verified
 * byte-for-byte by app/tests/test_prst150_js.mjs). Exposes the method surface
 * prst.js has, so patchlib/static_api/explorer can treat it as "the codec" for
 * profile gp150.
 *
 * Layout: the order table at 0x78 lists slot ids by chain position; the 12 block
 * records at 0x84 sit at FIXED indexes — slot s is record DEFAULT_POS[s], whatever
 * the order table says (hardware, 2026-10-04: the pedal's own reorder changed only
 * the order table). A reorder rewrites only the order table; the engine byte belongs
 * to the effect in the slot (ENGINES, from patch/gp150_engines.json), never to the
 * chain position. 0x0D..0x0F, 0x43C and 0x445 are device-owned and never written.
 */
(function (root) {
  const MAGIC = [0x11, 0x30, 0x64, 0x04];
  const PRST_LEN = 1128, IDX_OFF = 0x04, BPM_OFF = 0x24, VOL_OFF = 0x26, NAME_OFF = 0x2c, NAME_LEN = 0x44;
  const ORDER_OFF = 0x78, BLOCKS_OFF = 0x84, BLOCK_LEN = 0x44, N_BLOCKS = 12, N_PARAMS = 15, FOOTER_OFF = 0x3b4;
  const SLOTS = ["NR", "PRE", "WAH", "DST", "N->S", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "VOL"];
  const AMP_SLOT = 5, VOL_SLOT = 11; // pinned first / last (VOL is last in every corpus file)
  // record r holds slot DEFAULT_ORDER[r]; slot s lives at record DEFAULT_POS[s]
  const DEFAULT_ORDER = [AMP_SLOT, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, VOL_SLOT];
  const DEFAULT_POS = SLOTS.map((_, s) => DEFAULT_ORDER.indexOf(s));
  // The "None" effect: engine 0x06. Type 3 is None in NONE_SLOTS (no real type 3 in the
  // spec; the ring's per-slot "None" entries); AMP, DST, DLY, RVB and VOL have a real
  // type 3. VOL's "Volume" also carries 0x06.
  const ENGINE_BYPASS = 0x06, NONE_TYPE = 3, NONE_SLOTS = [0, 1, 2, 4, 6, 7, 8];
  // Per slot: [engine for a type the corpus never shows (null: none known — WAH),
  // {type: engine}] — a copy of patch/gp150_engines.json (scripts/gp150_engines.py),
  // checked equal by app/tests/test_prst150_js.mjs.
  const ENGINES = [
    [0x05, { 1: 0x05, 7: 0x05, 8: 0x05 }], // NR
    [0x03, { 0: 0x03, 1: 0x03, 2: 0x03, 6: 0x03, 8: 0x03, 11: 0x03, 16: 0x03, 20: 0x03, 26: 0x00, 34: 0x03, 36: 0x03, 43: 0x03, 68: 0x03, 81: 0x03 }], // PRE
    [null, {}], // WAH
    [0x07, { 1: 0x07, 3: 0x07, 4: 0x07, 13: 0x07, 17: 0x07, 20: 0x07, 21: 0x07, 25: 0x07, 27: 0x07, 36: 0x07, 39: 0x07, 43: 0x07, 46: 0x07, 47: 0x07, 53: 0x07, 57: 0x07, 58: 0x07, 59: 0x07, 64: 0x07, 67: 0x07, 72: 0x07, 73: 0x07, 78: 0x07, 89: 0x07, 90: 0x07, 93: 0x07, 95: 0x07, 104: 0x07, 105: 0x07, 110: 0x07, 115: 0x07, 117: 0x07, 118: 0x08, 119: 0x07, 122: 0x08 }], // DST
    [0x00, { 27: 0x00, 33: 0x00 }], // N->S
    [0x00, { 0: 0x00, 1: 0x00, 2: 0x01, 7: 0x01, 9: 0x03, 11: 0x00, 15: 0x01, 20: 0x00, 25: 0x01, 26: 0x00, 33: 0x01, 35: 0x01, 73: 0x01 }], // AMP
    [0x1a, { 0: 0x1a, 16: 0x1a, 32: 0x1a, 48: 0x1a, 60: 0x0a, 64: 0x1a, 80: 0x1a, 96: 0x1a, 112: 0x1a, 128: 0x1a, 144: 0x1a, 160: 0x1a, 176: 0x1a, 192: 0x1a, 208: 0x1a, 224: 0x1a, 240: 0x1a }], // CAB
    [0x01, { 53: 0x01, 54: 0x01, 57: 0x01, 60: 0x01 }], // EQ
    [0x04, { 1: 0x04, 2: 0x04, 8: 0x04, 17: 0x04, 18: 0x04, 21: 0x04, 23: 0x04, 25: 0x04, 32: 0x04, 33: 0x04, 41: 0x01, 45: 0x04, 48: 0x04 }], // MOD
    [0x0b, { 0: 0x0b, 3: 0x0b, 4: 0x0b, 6: 0x0b, 13: 0x0b, 29: 0x0b, 31: 0x0b }], // DLY
    [0x0c, { 0: 0x0c, 1: 0x0c, 2: 0x0c, 3: 0x0c, 4: 0x0c, 6: 0x0c, 8: 0x0c, 9: 0x0c, 13: 0x0c, 18: 0x0c }], // RVB
    [0x06, { 3: 0x06 }], // VOL
  ];
  const layout = {
    BLOCK_NAMES: SLOTS.slice(), MOVABLE_BLOCKS: new Set(SLOTS.filter((s) => s !== "AMP" && s !== "VOL")),
    PARAMS_PER_BLOCK: N_PARAMS, N_BLOCKS, AMP_INDEX: AMP_SLOT, VOL_INDEX: VOL_SLOT, NS_INDEX: 4, CAB_INDEX: 6,
    lockedFirst: true, lockedLast: true, hasFootswitches: false, nsIsRegularBlock: true, emptyName: "",
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
  // offset of `slot`'s record: its fixed home index, never the chain position
  const recordOff = (slot) => { if (!(Number.isInteger(slot) && slot >= 0 && slot < N_BLOCKS)) throw new Error(`block slot out of range: ${slot}`); return BLOCKS_OFF + DEFAULT_POS[slot] * BLOCK_LEN; };
  function blockOf(b, slot) {
    b = u8(b); const o = recordOff(slot), d = dv(b), params = [], order = readOrder(b);
    if (!isPermutation(order)) throw new Error("chain order is not a permutation of 0..11");
    for (let i = 0; i < N_PARAMS; i++) params.push(d.getFloat32(o + 8 + i * 4, true));
    return { slot, rec: DEFAULT_POS[slot], pos: order.indexOf(slot), enabled: b[o], type: b[o + 4], subtype: b[o + 5], ext: b[o + 6], engine: b[o + 7], params };
  }
  const blocksBySlot = (b) => SLOTS.map((_, s) => blockOf(b, s));
  function setBlock(b, slot, f) { const o = recordOff(slot); for (const [off, k] of [[0, "enabled"], [4, "type"], [5, "subtype"], [6, "ext"], [7, "engine"]]) if (f[k] != null) b[o + off] = Number(f[k]) & 0xff; }
  function setParam(b, slot, i, v) { if (!(i >= 0 && i < N_PARAMS)) throw new Error(`param index out of range: ${i}`); dv(b).setFloat32(recordOff(slot) + 8 + i * 4, Number(v), true); }
  // Reorder: rewrites ONLY the order table (records never move). AMP first, VOL last.
  function writeOrder(b, order) {
    order = Array.from(order, Number);
    if (!isPermutation(order)) throw new Error("chain order must be a permutation of 0..11");
    if (order[0] !== AMP_SLOT) throw new Error("chain position 0 must be AMP (slot 5)");
    if (order[N_BLOCKS - 1] !== VOL_SLOT) throw new Error("chain position 11 must be VOL (slot 11)");
    for (let i = 0; i < N_BLOCKS; i++) b[ORDER_OFF + i] = order[i];
  }
  // Engine byte for effect `type` in `slot` (prst150_format.slot_engine): None -> 0x06;
  // else the corpus engine of (slot, type), or the slot's default for an unseen type;
  // refused when the slot's real engine is unknown (WAH) — a wrong engine silences the preset.
  function slotEngine(slot, type) {
    slot = Number(slot); type = Number(type);
    if (!(Number.isInteger(slot) && slot >= 0 && slot < N_BLOCKS)) throw new Error(`block slot out of range: ${slot}`);
    if (type === NONE_TYPE && NONE_SLOTS.includes(slot)) return ENGINE_BYPASS;
    const [def, byType] = ENGINES[slot];
    const e = Object.prototype.hasOwnProperty.call(byType, type) ? byType[type] : def;
    if (e == null) throw new Error(`no engine byte is known for a ${SLOTS[slot]} effect (type ${type}) on the GP-150: no preset read so far uses one — set it on the pedal, save and rescan`);
    return e;
  }
  // the ring's per-slot "None" entry: type 3 in a NONE_SLOTS slot
  const isNoneModel = (slot, key) => NONE_SLOTS.includes(Number(slot)) && (Number(key) & 0xff) === NONE_TYPE;
  const modelKey = (slot, type, subtype = 0, ext = 0) => ((slot << 24) | (ext << 16) | (subtype << 8) | type) >>> 0;
  const modelRecords = (b) => blocksBySlot(b).map((blk, s) => [blk.type, s, (blk.ext << 16) | (blk.subtype << 8) | blk.type]);
  const modelRecOffset = () => -1;
  function bypassMask(b) { let m = 0; blocksBySlot(b).forEach((blk, s) => { if (blk.enabled) m |= 1 << s; }); return m >>> 0; }
  const paramFloats = (b) => blocksBySlot(b).flatMap((blk) => blk.params);
  const fsOffset = () => -1;
  const readFootswitches = () => [0, 0];
  const refixCrc = () => {};
  // Explorer edit spec -> new bytes (prst150_format.apply_edits): keys are SLOTS and
  // address the slot's home record; "order" rewrites only the order table; a model pick
  // writes the type + the slot's engine for it; a None pick also turns the block off.
  function applyEdits(prst, edits) {
    prst = u8(prst); check(prst); edits = edits || {};
    const b = Uint8Array.from(prst);
    if (edits.order != null) writeOrder(b, edits.order);
    for (const [slot, key] of Object.entries(edits.models || {})) {
      const k = Number(key) >>> 0, s = Number(slot), type = k & 0xff;
      const engine = slotEngine(s, type);
      setBlock(b, s, { type, subtype: (k >> 8) & 0xff, ext: (k >> 16) & 0xff, engine });
      if (s === AMP_SLOT && (type === 2 || type === 7)) setBlock(b, s, { ext: 1 });
      if (s === 4 && type === 0) setBlock(b, s, { subtype: 1 }); // empty N->S carries subtype 1 (spec App. A)
      if (isNoneModel(s, k)) setBlock(b, s, { enabled: 0 }); // every None block in the corpus is off; a bypass edit below may override
    }
    for (const [slot, ps] of Object.entries(edits.params || {})) for (const [i, v] of Object.entries(ps)) setParam(b, Number(slot), Number(i), v);
    for (const [slot, on] of Object.entries(edits.bypass || {})) setBlock(b, Number(slot), { enabled: on ? 1 : 0 });
    const s = edits.settings || {};
    writeVolBpm(b, s.patch_vol, s.bpm);
    if (edits.name != null) writeName(b, String(edits.name));
    return b;
  }
  const BLANK_B64 = "ETBkBAAAAAAQMFgEABVIPP//DAAQDA8DAwMEECgAAMggMFAAeAAyAAAAAABOZXcgR0VOLgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwMDwDBQABAgMEBgcICQoLAAAAAAMAAAYAAMhCAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAABQAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAADAAAgQQAAjEIAAKBCAACAPwAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAYAAEhCAABIQgAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAQAAABwAASEIAAEhCAABIQgAANEIAAEhCAABcQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAhAAAAAAAgQQAAQEEAAIA/AABIQwAAyEIAAEDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAABABABoAAAAAAACCQgAAAAAAAAAAAAAAAAAAmEEACDVGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAABgAAyEIAAAAAAAAAAAAAAAAAAAAAAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAAAGAADIQgAAAD8AAEhCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAsAACBBAIDZQwAAoEEAAAAAAACAPwAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAQAADAAAcEEAAEhCAAAgQgAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAADAAAGAADIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUDAMAAwAAAANAAAABAAAAGAwcAALAAAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIBAAAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIAAAAAcDAEAAAAAACAMCQAdA4AAAQAAAAAAgAAAAAAAAAAAAABAAAAAQAAAAAAAAAAAAAA"; // same string as patch/prst150_format.BLANK_B64 (factory "New GEN.")
  const b64bytes = (s) => (typeof atob === "function" ? Uint8Array.from(atob(s), (c) => c.charCodeAt(0)) : Uint8Array.from(Buffer.from(s, "base64")));
  function blankPrst(slot = 0) { const b = b64bytes(BLANK_B64); writeIndex(b, Number(slot)); return b; }

  const API = {
    key: "gp150", layout, MAGIC, PRST_LEN, NAME_OFF, NAME_LEN, ORDER_OFF, BLOCKS_OFF, BLOCK_LEN, N_BLOCKS, N_PARAMS, FOOTER_OFF, SLOTS, AMP_SLOT, VOL_SLOT,
    DEFAULT_ORDER, DEFAULT_POS, ENGINES, ENGINE_BYPASS, NONE_TYPE, NONE_SLOTS, isNoneModel,
    detect, readIndex, writeIndex, readName, writeName, readVolBpm, writeVolBpm, readOrder, writeOrder, isPermutation,
    blockOf, blocksBySlot, setBlock, setParam, slotEngine, modelKey, modelRecords, modelRecOffset, bypassMask, paramFloats,
    fsOffset, readFootswitches, refixCrc, applyEdits, blankPrst, BLANK_B64,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = API; else root.PRST150 = API;
})(typeof self !== "undefined" ? self : this);
