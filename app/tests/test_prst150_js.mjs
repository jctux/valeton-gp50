/* app/static/prst150.js vs patch/prst150_format.py — byte-for-byte.
 *   node app/tests/test_prst150_js.mjs            # runs prst150_oracle.py
 */
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const P = require(resolve(here, "../static/prst150.js"));
const py = [".venv-app/bin/python", "python3"].map((p) => (p.includes("/") ? resolve(repoRoot, p) : p)).find((p) => !p.includes("/") || existsSync(p));
const O = JSON.parse(execFileSync(py, [resolve(here, "prst150_oracle.py")], { cwd: repoRoot, maxBuffer: 256 * 1024 * 1024 }).toString());
const fromB64 = (s) => new Uint8Array(Buffer.from(s, "base64"));
const eq = (a, z) => a.length === z.length && a.every((v, i) => v === z[i]);
const near = (a, z) => a.length === z.length && a.every((v, i) => Math.abs(v - z[i]) < 1e-5);
let pass = 0, fail = 0; const fails = [];
const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };

const edit = O.edit;
const blkFields = (blk) => ({ slot: blk.slot, rec: blk.rec, pos: blk.pos, enabled: blk.enabled, type: blk.type, subtype: blk.subtype, ext: blk.ext, engine: blk.engine });
for (const rec of O.files) {
  const b = fromB64(rec.prstB64);
  check(`${rec.path} detect`, P.detect(b));
  check(`${rec.path} name`, P.readName(b) === rec.name, P.readName(b));
  check(`${rec.path} index`, P.readIndex(b) === rec.index);
  check(`${rec.path} volbpm`, JSON.stringify(P.readVolBpm(b)) === JSON.stringify(rec.volBpm));
  check(`${rec.path} order`, JSON.stringify(P.readOrder(b)) === JSON.stringify(rec.order));
  check(`${rec.path} models`, JSON.stringify(P.modelRecords(b)) === JSON.stringify(rec.models), JSON.stringify(P.modelRecords(b)));
  check(`${rec.path} blocksBySlot (home records)`, JSON.stringify(P.blocksBySlot(b).map(blkFields)) === JSON.stringify(rec.blocks.map(blkFields)), JSON.stringify(P.blocksBySlot(b).map(blkFields)));
  check(`${rec.path} bypass`, P.bypassMask(b) === rec.bypass);
  check(`${rec.path} floats`, near(P.paramFloats(b), rec.floats));
  const edited = P.applyEdits(b, edit);
  check(`${rec.path} applyEdits`, eq(edited, fromB64(rec.editedB64)));
  check(`${rec.path} applyEdits None models`, eq(P.applyEdits(b, O.editNone), fromB64(rec.editedNoneB64)));
  check(`${rec.path} applyEdits None + bypass on`, eq(P.applyEdits(b, O.editNoneOn), fromB64(rec.editedNoneOnB64)));
  check(`${rec.path} applyEdits DLY Sweet Echo + reorder`, eq(P.applyEdits(b, O.editDly), fromB64(rec.editedDlyB64)));
  { // every slot re-picks its stored model: same bytes as Python (stored engines kept)
    const models = {}; for (const blk of P.blocksBySlot(b)) models[blk.slot] = P.modelKey(blk.slot, blk.type, blk.subtype, blk.ext);
    let got; try { got = P.applyEdits(b, { models }); } catch { got = null; }
    check(`${rec.path} re-pick of every stored model == Python`, rec.repickB64 === null ? got === null : got && eq(got, fromB64(rec.repickB64)));
  }
  { // a reorder rewrites the order table only
    const order = [5, 10, ...P.readOrder(b).slice(1, 11).filter((s) => s !== 10), 11];
    const out = P.applyEdits(b, { order });
    const diff = []; for (let i = 0; i < b.length; i++) if (out[i] !== b[i]) diff.push(i);
    check(`${rec.path} reorder touches only 0x78..0x83`, diff.every((i) => i >= 0x78 && i < 0x84) && eq(out.subarray(0x84), b.subarray(0x84)), diff.map((i) => i.toString(16)).join());
    check(`${rec.path} drag and back round-trips`, eq(P.applyEdits(out, { order: P.readOrder(b) }), b));
  }
  check(`${rec.path} applyEdits noop`, eq(P.applyEdits(b, {}), b));
  check(`${rec.path} input untouched`, eq(b, fromB64(rec.prstB64)));
}
check("blank 199", eq(P.blankPrst(199), fromB64(O.blank199B64)));
check("NONE_SLOTS mirror", JSON.stringify(P.NONE_SLOTS) === "[0,1,2,4,6,7,8]" && P.NONE_TYPE === 3);
check("home records mirror", JSON.stringify(P.DEFAULT_ORDER) === JSON.stringify(O.defaultOrder) && JSON.stringify(P.DEFAULT_POS) === JSON.stringify(O.defaultPos));
{ // the engine table: embedded copy == patch/gp150_engines.json, slotEngine == slot_engine
  const T = JSON.parse(readFileSync(resolve(repoRoot, "patch/gp150_engines.json"), "utf8"));
  const want = T.slots.map((r) => [r.default, r.engines, r.counts]);
  check("ENGINES == patch/gp150_engines.json (default, engines, counts)", JSON.stringify(P.ENGINES) === JSON.stringify(want), JSON.stringify(P.ENGINES));
  let bad = 0;
  for (let s = 0; s < 12; s++) for (let t = 0; t < 256; t++) {
    let got; try { got = P.slotEngine(s, t); } catch { got = -1; }
    if (got !== O.slotEngine[s][t]) bad++;
  }
  check("slotEngine == slot_engine (12 slots x 256 types)", bad === 0, `${bad} mismatches`);
  bad = 0;
  for (let s = 0; s < 12; s++) for (let t = 0; t < 256; t++) if (P.pickAllowed(s, t) !== O.pickAllowed[s][t]) bad++;
  check("pickAllowed == pick_allowed (12 slots x 256 types)", typeof P.pickAllowed === "function" && bad === 0, `${bad} mismatches`);
  bad = 0;
  for (let s = 0; s < 12; s++) for (let t = 0; t < 256; t++) if (P.pickAllowed(s, t) !== (O.slotEngine[s][t] !== -1)) bad++;
  check("pickAllowed is exactly 'slotEngine does not refuse'", bad === 0, `${bad} mismatches`);
  check("keepsStoredEngine == keeps_stored_engine", typeof P.keepsStoredEngine === "function" && O.keeps.every(([s, t, st, se, want]) => P.keepsStoredEngine(s, t, st, se) === want));
  // the refusals: ambiguous pairs (AMP 1 {0:18,1:2,3:4}, PRE 11 {0:1,3:3}, DST 117 {7:1,8:1}),
  // AMP type 3 (never a real amp in the corpus), unseen types in a multi-engine slot
  const REFUSED = /^no unambiguous engine byte is known for (\S+) type (\d+): set it on the pedal, save, rescan, run scripts\/gp150_engines\.py and copy the table into prst150\.js ENGINES$/;
  for (const [s, t] of [[5, 1], [1, 11], [3, 117], [5, 3], [6, 33], [4, 57], [8, 54], [2, 4]]) {
    let e = null; try { P.slotEngine(s, t); } catch (x) { e = x; }
    const m = e && REFUSED.exec(e.message);
    check(`refused: ${P.SLOTS[s]} type ${t}`, !P.pickAllowed(s, t) && m && m[1] === P.SLOTS[s] && Number(m[2]) === t, e && e.message);
    e = null; try { P.applyEdits(P.blankPrst(3), { models: { [s]: P.modelKey(s, t) } }); } catch (x) { e = x; }
    check(`applyEdits refuses ${P.SLOTS[s]} type ${t}`, e && REFUSED.test(e.message), e && e.message);
  }
  check("allowed: DLY unseen type 40 -> 0x0B", P.pickAllowed(9, 40) && P.slotEngine(9, 40) === 0x0b);
  check("allowed: every seen RVB type -> 0x0C", [0, 1, 2, 3, 4, 6, 8, 9, 13, 18].every((t) => P.slotEngine(10, t) === 0x0c));
  check("allowed: NR (seen and unseen) -> 0x05", [1, 7, 8, 16, 33].every((t) => P.slotEngine(0, t) === 0x05));
  check("allowed: CAB type 60 (one engine) -> 0x0A", P.pickAllowed(6, 60) && P.slotEngine(6, 60) === 0x0a);
  check("allowed: N->S NAM types -> 0x00", P.slotEngine(4, 27) === 0x00 && P.slotEngine(4, 33) === 0x00);
  { // a None pick is still type 3 / 0x06 / off
    const out = P.applyEdits(P.blankPrst(3), { models: { 8: P.modelKey(8, 3), 6: P.modelKey(6, 3) } });
    check("None pick: type 3, engine 0x06, off", [8, 6].every((s) => { const x = P.blocksBySlot(out)[s]; return x.type === 3 && x.engine === 0x06 && x.enabled === 0; }));
  }
  { // re-picking the stored model keeps its engine: 100-active AMP type 1 carries 0x01 (majority 0x00)
    const act = fromB64(O.files.find((r) => r.path.endsWith("100-active.prst")).prstB64);
    const amp = P.blocksBySlot(act)[5];
    const out = P.applyEdits(act, { models: { 5: P.modelKey(5, 1, amp.subtype, amp.ext) } });
    check("re-pick of the stored AMP type 1 keeps engine 0x01, no rewrite", amp.type === 1 && amp.engine === 0x01 && eq(out, act));
    const gen = fromB64(O.files.find((r) => r.path.endsWith("000-New_GEN.prst")).prstB64);
    let e = null; try { P.applyEdits(gen, { models: { 5: P.modelKey(5, 3) } }); } catch (x) { e = x; }
    check("a None-stored AMP (type 3, 0x06): picking AMP type 3 is a new pick -> refused", P.blocksBySlot(gen)[5].engine === 0x06 && e && /AMP type 3/.test(e.message), e && e.message);
  }
  let err = null; try { P.slotEngine(2, 4); } catch (e) { err = e; }
  check("slotEngine refuses a real WAH (no engine known)", err && /WAH/.test(err.message), err && err.message);
  err = null; try { P.applyEdits(fromB64(O.files[0].prstB64), { models: { 2: P.modelKey(2, 4) } }); } catch (e) { err = e; }
  check("applyEdits refuses a real WAH pick", err && /WAH/.test(err.message));
  check("no position engine left", P.engineFor === undefined && P.blockAt === undefined);
}
let threw = false; try { P.writeOrder(fromB64(O.files[0].prstB64), [0, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]); } catch { threw = true; }
check("writeOrder requires AMP first", threw);
for (const bad of [[5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 10], [5, 11, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10]]) {
  const b0 = fromB64(O.files[0].prstB64), b = b0.slice();
  let e1 = null, e2 = null;
  try { P.writeOrder(b, bad); } catch (e) { e1 = e; }
  try { P.applyEdits(b0, { order: bad }); } catch (e) { e2 = e; }
  check(`writeOrder requires VOL last (${bad.slice(-2)})`, e1 && /VOL/.test(e1.message) && e2 && /VOL/.test(e2.message) && eq(b, b0), e1 && e1.message);
}
const L = P.layout;
check("layout pins VOL last", L.lockedLast === true && L.VOL_INDEX === 11 && !L.MOVABLE_BLOCKS.has("VOL") && !L.MOVABLE_BLOCKS.has("AMP") && L.MOVABLE_BLOCKS.size === 10);
check("isNoneModel mirror", typeof P.isNoneModel === "function" && P.isNoneModel(8, P.modelKey(8, 3)) && !P.isNoneModel(3, P.modelKey(3, 3)) && !P.isNoneModel(8, P.modelKey(8, 41)));
{ // ground truth: the pedal's own reorder (2026-10-04) changed only the order table
  const file = (n) => fromB64(O.files.find((r) => r.path.endsWith(n)).prstB64);
  const before = file("199-before-pedal-reorder.prst"), pedal = file("199-reordered-by-pedal.prst");
  const DEVICE_OWNED = [0x0e, 0x0f, 0x43c, 0x445];
  const out = P.applyEdits(before, { order: [5, 0, 1, 2, 3, 4, 10, 6, 7, 8, 9, 11] });
  const diff = []; for (let i = 0; i < out.length; i++) if (out[i] !== pedal[i] && !DEVICE_OWNED.includes(i)) diff.push(i);
  check("order edit reproduces the pedal's reorder (outside device bytes)", diff.length === 0, diff.map((i) => i.toString(16)).join());
  const bl = P.blocksBySlot(pedal), dly = bl[9], rvb = bl[10];
  check("pedal file: DLY = record 9, Sweet Echo 0x0B on, chain position 10", dly.rec === 9 && dly.pos === 10 && dly.type === 13 && dly.engine === 0x0b && dly.enabled === 1);
  check("pedal file: RVB = record 10, None, chain position 6", rvb.rec === 10 && rvb.pos === 6 && rvb.type === 3 && rvb.engine === 0x06 && rvb.enabled === 0);
}
{ // BPM clamp (one byte at 0x24): 300 -> 255, 10 -> 40, never truncated by & 0xff
  const b = P.blankPrst(3); P.writeVolBpm(b, null, 300); check("bpm 300 clamps to 255", P.readVolBpm(b)[1] === 255);
  P.writeVolBpm(b, null, 10); check("bpm 10 clamps to 40", P.readVolBpm(b)[1] === 40);
  const out = P.applyEdits(P.blankPrst(3), { settings: { bpm: 300 } }); check("applyEdits bpm 300 -> 255", P.readVolBpm(out)[1] === 255);
}
console.log(`prst150.js: ${pass} passed, ${fail} failed (${O.files.length} files)`);
for (const f of fails) console.log("  FAIL " + f);
process.exit(fail ? 1 : 0);
