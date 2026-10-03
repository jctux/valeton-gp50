"use strict";
/*
 * patchlib.js — decode a set of .prst presets into the Explorer's inventory,
 * facets, and per-block model lists, entirely client-side. Port of app/patchlib.py
 * (the _load / _blocks_for / facets / models_for_block path). Verified field-exact
 * against /api/device/{inventory,facets,models} by app/tests/test_patchlib_js.mjs.
 *
 * Fed by the fxid ring catalog (patch/fxid_ring{,_gp5}.json) + optional bank_map
 * (SnapTone/User-IR device names). Preset bytes come from a device scan (WebMIDI)
 * or a bundled snapshot — this module is source-agnostic.
 *
 *   const lib = PatchLib.make(ring, bankMap, PRST.GP50);
 *   const inv = lib.inventory([{slot, bytes, name?}, ...]);  // {patches, snaptones, irs}
 *   const facets = lib.facets(inv.patches);
 *   const models = lib.modelsForBlock("DST", inv.snaptones);
 */
(function (root) {
  const PRST = root.PRST || (typeof module !== "undefined" && module.exports ? require("./prst.js") : null);

  const BLOCK_NAMES = ["NR", "PRE", "DST", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "N->S"];
  // Blocks that can be reordered in the signal chain; the rest (DST·N->S·AMP·CAB·EQ)
  // are a fixed atomic core. See re/DEVICE_BLOCKORDER.md.
  const MOVABLE_BLOCKS = new Set(["NR", "PRE", "MOD", "DLY", "RVB"]);
  const NS_CAT = 0x0f, CAB_CAT = 0x0a, AMP_CATS = [0x07, 0x08];
  const USER_IR_BASE = 0x100000;

  function make(ring, bankMap, profile) {
    const C = PRST.codecFor(profile), L = C.layout;
    const BLOCK_NAMES = L.BLOCK_NAMES, MOVABLE_BLOCKS = L.MOVABLE_BLOCKS, STRIDE = L.PARAMS_PER_BLOCK;
    // ring: {fxidInt: entry}; normalize keys to ints for lookup
    const R = {};
    for (const k of Object.keys(ring || {})) R[Number(k)] = ring[k];
    bankMap = bankMap || {};
    const bankSnap = bankMap.snaptone || {};
    const bankIr = bankMap.ir || {};
    const devName = (L.emptyName == null ? (profile.name || "") : L.emptyName).toUpperCase();

    const modelEntry = (cat, fxlow) => {
      const e = R[((cat << 24) | fxlow) >>> 0];
      if (e) return e;
      // GP-150 fxlow carries subtype/ext bits; ring keys are (slot<<24)|type.
      return L.nsIsRegularBlock ? (R[((cat << 24) | (fxlow & 0xff)) >>> 0] || null) : null;
    };
    const modelName = (cat, fxlow) => { const e = modelEntry(cat, fxlow); return e ? (e.name || e.fxtitle) : null; };

    // blocks whose catalog spans >1 type (so the type adds info)
    const byBlock = {};
    for (const e of Object.values(R)) (byBlock[e.module] = byBlock[e.module] || new Set()).add(e.type);
    const multiType = new Set(Object.keys(byBlock).filter((b) => byBlock[b].size > 1));

    const blockLabel = (block, btype, name) => {
      const parts = [block];
      if (btype && multiType.has(block)) parts.push(btype);
      if (name) parts.push(name);
      return parts.join(" · ");
    };
    const cabName = (fxlow) => {
      if (fxlow >= USER_IR_BASE) { const slot = fxlow - USER_IR_BASE; return bankIr[slot] || `User IR ${slot + 1}`; }
      return modelName(L.nsIsRegularBlock ? L.CAB_INDEX : CAB_CAT, fxlow);
    };
    const round2 = (v) => Math.round((v + Number.EPSILON) * 100) / 100;
    const fmtParam = (v, toggle, unit) => {
      if (toggle) return Math.round(v) !== 0 ? "On" : "Off";
      const s = Math.abs(v - Math.round(v)) < 1e-4 ? String(Math.round(v)) : v.toFixed(2);
      return unit ? `${s} ${unit}`.trim() : s;
    };
    const paramsFor = (entry, floats, blockIndex) => {
      if (!entry) return [];
      const base = blockIndex * STRIDE, out = [];
      for (const p of entry.params || []) {
        const slot = base + p.algId;
        if (slot >= floats.length) continue;
        const val = floats[slot];
        out.push({
          name: p.name, value: round2(val), display: fmtParam(val, p.toggle, p.unit || ""),
          toggle: p.toggle, unit: p.unit || "", algId: p.algId,
          min: p.min ?? 0, max: p.max ?? 100, step: p.step ?? 1,
        });
      }
      return out;
    };

    const footswitches = (b) => {
      if (!L.hasFootswitches) return [[], []];
      const off = C.fsOffset(b);
      if (off < 0) return [[], []];
      const d = new DataView(b.buffer, b.byteOffset, b.byteLength);
      const a = d.getUint32(off, true), c = d.getUint32(off + 4, true);
      const bits = (m) => { const r = []; for (let i = 0; i < L.N_BLOCKS; i++) if ((m >> i) & 1) r.push(i); return r; };
      return [bits(a), bits(c)];
    };
    const patchSettings = (b) => {
      const [vol, bpm] = C.readVolBpm(b);
      const [fs1, fs2] = footswitches(b);
      return { patch_vol: vol, bpm, fs1, fs2 };
    };

    const blocksFor = (b, nsLabel) => {
      const mask = C.bypassMask(b);
      const recs = C.modelRecords(b);
      const floats = C.paramFloats(b);
      const engines = L.nsIsRegularBlock && C.blocksBySlot ? C.blocksBySlot(b).map((x) => x.engine) : null;
      const out = [];
      BLOCK_NAMES.forEach((block, k) => {
        const [idx, cat, fxlow] = k < recs.length ? recs[k] : [0, 0, 0];
        let e, model, btype, official = null, fxid;
        if (engines && engines[k] === 0x06 && block !== "VOL") {
          // engine 0x06 = the "None"/bypassed effect, whatever the type byte says
          const nk = ((k << 24) | 3) >>> 0, ne = R[nk];
          e = null; model = "None"; btype = null; official = null; fxid = ne && ne.name === "None" ? nk : 0;
        } else if (block === "N->S" && !L.nsIsRegularBlock) {
          e = modelEntry(NS_CAT, idx);
          model = idx ? (nsLabel[idx] || null) : null;
          btype = "SnapTone";
          fxid = idx ? ((NS_CAT << 24) | idx) >>> 0 : 0;
        } else {
          e = modelEntry(cat, fxlow);
          model = e ? (e.name || e.fxtitle) : (L.nsIsRegularBlock ? `Type ${idx}` : null);
          btype = e ? e.type : null;
          official = e ? (e.origin || null) : null;
          if (block === "CAB" && !L.nsIsRegularBlock) model = cabName(fxlow) || model;
          fxid = (fxlow || cat) ? (((cat << 24) | fxlow) >>> 0) : 0;
        }
        out.push({
          block, active: !!((mask >> k) & 1), type: btype ?? null, model: model ?? null,
          official: official ?? null, index: idx, fxid, type_code: idx, movable: MOVABLE_BLOCKS.has(block),
          label: blockLabel(block, btype, model),
          label_official: blockLabel(block, btype, official || model),
          params: paramsFor(e, floats, k),
        });
      });
      return out;
    };

    const isEmptyName = (name) => (name || "").trim().toUpperCase() === devName;

    // presets: [{slot, bytes(Uint8Array), name?}]  (name = fallback if body name empty)
    function inventory(presets) {
      const patches = [];
      const raw = {};
      for (const { slot, bytes, name: fallback } of presets) {
        const b = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes);
        const recs = C.modelRecords(b); // each: [idx, cat, fxlow]
        const nsRec = recs[L.NS_INDEX] || [0, 0, 0], cabRec = recs[L.CAB_INDEX] || [0, 0, 0], ampRec = recs[L.AMP_INDEX] || [0, 0x07, 0];
        const nsIdx = L.nsIsRegularBlock ? 0 : nsRec[0];
        const cab = cabRec[2], amp = ampRec[2], ampCat = ampRec[1];
        const name = C.readName(b) || fallback || "";
        const p = {
          slot, name, empty: isEmptyName(name),
          uses_snaptone: nsIdx !== 0, snaptone_slot: nsIdx,
          ir_slot: cab, amp_slot: amp, snaptone_name: "",
          ir_name: cabName(cab) || `Cab #${cab}`,
          amp_name: modelName(ampCat, amp) || `Amp #${amp}`,
          blocks: [], layout_blocks: L.N_BLOCKS, settings: patchSettings(b),
        };
        patches.push(p);
        raw[slot] = b;
      }

      // SnapTone identity: union of bank_map slots + slots patches reference
      const used = {};
      for (const p of patches) if (p.snaptone_slot) (used[p.snaptone_slot] = used[p.snaptone_slot] || []).push(p.name);
      const slots = [...new Set([...Object.keys(used).map(Number), ...Object.keys(bankSnap).map(Number)])].sort((a, z) => a - z);
      const snaptones = slots.map((slot) => ({ slot, name: bankSnap[slot] || (used[slot] || []).slice().sort().join("/") }));
      const slotLabel = {}; snaptones.forEach((s) => (slotLabel[s.slot] = s.name));
      for (const p of patches) {
        p.snaptone_name = p.snaptone_slot ? (slotLabel[p.snaptone_slot] || "") : "";
        p.blocks = blocksFor(raw[p.slot], slotLabel);
        p.order = C.readOrder(raw[p.slot]); // chain[pos] = block(record) index
      }

      // IR/Cab inventory: full catalog + usage counts
      const useCount = {};
      for (const p of patches) if (!p.uses_snaptone) useCount[p.ir_slot] = (useCount[p.ir_slot] || 0) + 1;
      const irs = [];
      for (const [fxidStr, e] of Object.entries(R)) {
        if (e.module !== "CAB") continue;
        const fxlow = Number(fxidStr) & 0xffffff;
        const isUser = (e.name || "").includes("User IR");
        const nm = isUser ? cabName(fxlow) : (e.name || e.fxtitle);
        irs.push({ slot: fxlow, name: nm || `Cab #${fxlow}`, type: e.type || "", is_user_ir: isUser, used: useCount[fxlow] || 0 });
      }
      irs.sort((a, z) => (a.is_user_ir - z.is_user_ir) || (a.slot - z.slot));
      return { patches, snaptones, irs };
    }

    function facets(patches) {
      const blocks = {};
      for (const p of patches) for (const blk of p.blocks) {
        if (!blk.active) continue;
        const d = blocks[blk.block] || (blocks[blk.block] = { types: new Set(), models: {} });
        if (blk.type) d.types.add(blk.type);
        if (blk.model) d.models[blk.model] = { official: blk.official ?? null, type: blk.type ?? null };
      }
      const order = {}; BLOCK_NAMES.forEach((b, i) => (order[b] = i));
      return {
        blocks: Object.keys(blocks).sort((a, z) => (order[a] ?? 99) - (order[z] ?? 99)).map((b) => ({
          block: b,
          types: [...blocks[b].types].sort(),
          models: Object.keys(blocks[b].models).sort().map((m) => ({ model: m, official: blocks[b].models[m].official, type: blocks[b].models[m].type })),
        })),
      };
    }

    function modelsForBlock(block, snaptones) {
      if (block === "N->S" && !L.nsIsRegularBlock) {
        const nsParams = (modelEntry(NS_CAT, 0) || {}).params || [];
        return (snaptones || []).map((s) => ({
          fxid: ((NS_CAT << 24) | s.slot) >>> 0, name: s.name, official: null, type: "SnapTone",
          label: blockLabel(block, "SnapTone", s.name), label_official: blockLabel(block, "SnapTone", s.name), params: nsParams,
        }));
      }
      const out = [];
      for (const [fxidStr, e] of Object.entries(R)) {
        if (e.module !== block) continue;
        const fxid = Number(fxidStr), fxlow = fxid & 0xffffff;
        const name = block === "CAB" ? cabName(fxlow) : (e.name || e.fxtitle);
        const official = e.origin || null, btype = e.type || "", nm = name || `#${fxlow}`;
        out.push({ fxid, name: nm, official, type: btype, label: blockLabel(block, btype, nm), label_official: blockLabel(block, btype, official || nm), params: e.params || [] });
      }
      out.sort((a, z) => ((a.fxid & 0x100000) - (z.fxid & 0x100000)) || (a.name < z.name ? -1 : a.name > z.name ? 1 : 0));
      return out;
    }

    return { inventory, facets, modelsForBlock, BLOCK_NAMES, layout: L };
  }

  const API = { make, BLOCK_NAMES };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else root.PatchLib = API;
})(typeof self !== "undefined" ? self : this);
