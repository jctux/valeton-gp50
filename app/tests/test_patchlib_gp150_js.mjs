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
console.log(`patchlib gp150: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
