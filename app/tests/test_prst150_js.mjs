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
  const want = T.slots.map((r) => [r.default, Object.fromEntries(Object.entries(r.engines).map(([k, v]) => [k, v]))]);
  check("ENGINES == patch/gp150_engines.json", JSON.stringify(P.ENGINES) === JSON.stringify(want), JSON.stringify(P.ENGINES));
  let bad = 0;
  for (let s = 0; s < 12; s++) for (let t = 0; t < 256; t++) {
    let got; try { got = P.slotEngine(s, t); } catch { got = -1; }
    if (got !== O.slotEngine[s][t]) bad++;
  }
  check("slotEngine == slot_engine (12 slots x 256 types)", bad === 0, `${bad} mismatches`);
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
