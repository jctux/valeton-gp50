"use strict";
/*
 * explorer_edits.js — the Explorer's pure preset-edit helpers (no DOM), so node
 * tests drive exactly the bytes the Explorer writes (app/tests/test_explorer_edits_js.mjs).
 *
 * Every byte the Explorer builds for a preset goes through the codec of the device
 * the preset belongs to: PRST.codecFor(key), key = the inventory's device key. The
 * GP-50 codec (prst.js) run on a 1128-byte GP-150 file corrupts it, so a preset of
 * the other transport family is refused, never edited.
 *
 * Browser: window.ExplorerEdits (load after prst.js + prst150.js, before explorer.js).
 */
(function (root) {
  const PRST = root.PRST || (typeof module !== "undefined" && module.exports ? require("./prst.js") : null);

  const keyOf = (key) => (key && typeof key === "object" ? key.key : key) || "gp50";
  const codecOf = (key) => PRST.codecFor(keyOf(key));
  const isHt = (key) => { const p = PRST.DEVICES[keyOf(key)]; return !!(p && p.transport === "ht"); };

  // `prst` with the Explorer's pending edits (editsSpec), built by device `key`'s codec.
  // Returns a new array; the input is untouched.
  function buildEditedBytes(prst, key, edits) {
    const u = prst instanceof Uint8Array ? prst : Uint8Array.from(prst);
    const src = PRST.detect(u);
    if ((src.transport === "ht") !== isHt(key)) {
      throw new Error(`a ${src.name} preset cannot be edited as a ${PRST.profileFor(keyOf(key)).name} preset`);
    }
    return codecOf(key).applyEdits(u, edits);
  }

  // Longest preset name the editor allows: the codec layout's nameMax (GP-150: 13,
  // spec §3.2); GP-5/GP-50: 10 (factory names top out at 10).
  const nameMax = (key) => codecOf(key).layout.nameMax || 10;

  // What "Clear preset" writes to `slot`: GP-150 — factory "New GEN." with the slot's
  // index byte; GP-5/GP-50 — the slot-independent "GP-50" blank.
  const blankFor = (key, slot) => (isHt(key) ? codecOf(key).blankPrst(slot) : codecOf(key).blankPrst(keyOf(key)));

  // A preset row's label. A GP-150 scan stores a silent slot as a nameless blank
  // (patchlib marks it empty): it reads "(empty slot)". Any other name is shown as-is;
  // GP-5/GP-50 empties are named ("GP-50") so they never get the label. `name` = the
  // current name (a pending rename wins); omitted -> p.name.
  const EMPTY_LABEL = "(empty slot)";
  const rowName = (p, name) => (name == null ? p.name : name) || (p.empty && !p.name ? EMPTY_LABEL : "");

  // Preset-reorder writes whose SOURCE slot was empty in a GP-150 snapshot (nameless
  // blank): each puts a nameless factory blank on the pedal. The Explorer names them in
  // the Finalize confirm so no blank is written unseen. GP-5/GP-50: none (named empties).
  function emptySourceWrites(writes, names, key) {
    if (!isHt(key)) return [];
    return writes.filter((w) => !String(names[w.from] || "").trim());
  }

  const API = { buildEditedBytes, nameMax, blankFor, rowName, emptySourceWrites, EMPTY_LABEL };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else root.ExplorerEdits = API;
})(typeof self !== "undefined" ? self : this);
