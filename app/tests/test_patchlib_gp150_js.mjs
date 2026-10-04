/* patchlib.js over GP-150 presets: 12 blocks, layout-driven, ring keyed by slot<<24|type.
 *   node app/tests/test_patchlib_gp150_js.mjs
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const PRST = require(resolve(root, "app/static/prst.js"));
globalThis.PRST150 = require(resolve(root, "app/static/prst150.js"));
const PatchLib = require(resolve(root, "app/static/patchlib.js"));
const ring = JSON.parse(readFileSync(resolve(root, "patch/fxid_ring_gp150.json"), "utf8"));
const b = new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence/000-New_GEN.prst")));
let pass = 0, fail = 0; const fails = [];
const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };

const C = PRST.codecFor(PRST.GP150);
check("codecFor gp150", C && C.key === "gp150");
check("codecFor gp50 is PRST", PRST.codecFor("gp50") === PRST && PRST.layout.N_BLOCKS === 10);
const lib = PatchLib.make(ring, {}, PRST.GP150);
check("BLOCK_NAMES", JSON.stringify(lib.BLOCK_NAMES) === JSON.stringify(["NR", "PRE", "WAH", "DST", "N->S", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "VOL"]));
const inv = lib.inventory([{ slot: 0, bytes: b }]);
const p = inv.patches[0];
check("name", p.name === "New GEN.", p.name);
check("12 blocks", p.blocks.length === 12);
check("order", JSON.stringify(p.order) === JSON.stringify(PRST150.readOrder(b)));
check("AMP not movable", p.blocks[5].movable === false && p.blocks[0].movable === true);
check("settings", p.settings.patch_vol === 50 && p.settings.bpm === 120 && p.settings.fs1.length === 0);
check("amp label", typeof p.blocks[5].label === "string" && p.blocks[5].label.startsWith("AMP"));
check("params use 15-stride", p.blocks[5].params.every((q) => q.algId < 15));
// engine 0x06 (non-VOL) => None
const eng = PRST150.blocksBySlot(b).map((x) => x.engine);
p.blocks.forEach((blk, k) => { if (eng[k] === 0x06 && blk.block !== "VOL") check(`None slot ${k}`, blk.model === "None" && blk.type === null && blk.params.length === 0); });
const facets = lib.facets(inv.patches);
check("facets", facets.blocks.length > 0);
const models = lib.modelsForBlock("DLY", inv.snaptones);
check("DLY models from ring", models.length > 5 && models.every((m) => (m.fxid >>> 24) === 9));
check("pos = chain position (default order)", p.blocks.every((blk, k) => blk.pos === p.order.indexOf(k)));

// A preset the PEDAL reordered (RVB moved to chain position 6, 2026-10-04): blocks[k] is
// slot k read from its home record; the chain order comes from the order table.
{
  const rb = new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence/199-reordered-by-pedal.prst")));
  const q = lib.inventory([{ slot: 199, bytes: rb }]).patches[0];
  check("reordered: order table", JSON.stringify(q.order) === "[5,0,1,2,3,4,10,6,7,8,9,11]", JSON.stringify(q.order));
  const at = (pos) => q.blocks[q.order[pos]];
  check("reordered: chain position 6 is RVB (None)", at(6).block === "RVB" && at(6).model === "None" && at(6).active === false && at(6).pos === 6);
  check("reordered: DLY is Sweet Echo, on, at chain position 10", q.blocks[9].block === "DLY" && q.blocks[9].model === "Sweet Echo" && q.blocks[9].active === true && q.blocks[9].pos === 10 && at(10) === q.blocks[9]);
  check("reordered: DLY params from record 9", q.blocks[9].params.length > 0 && q.blocks[9].params.every((x) => Number.isFinite(x.value)));
  check("reordered: CAB (record 6) is not mislabelled", q.blocks[6].block === "CAB" && q.blocks[6].pos === 7);
  check("reordered: movable unchanged", q.blocks.map((x) => x.movable).join() === p.blocks.map((x) => x.movable).join());
  // 099-Finger_AC: NR moved to chain position 10 by its author
  const fa = lib.inventory([{ slot: 99, bytes: new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence/099-Finger_AC.prst"))) }]).patches[0];
  check("Finger AC: NR at chain position 10, RVB a real reverb", fa.blocks[0].pos === 10 && fa.blocks[10].block === "RVB" && fa.blocks[10].model !== "None");
}
console.log(`patchlib gp150: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
