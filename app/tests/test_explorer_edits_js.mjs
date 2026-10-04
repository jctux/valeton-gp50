/* The Explorer's edit path for the GP-150 (app/static/explorer_edits.js, the pure
 * helpers explorer.js calls):
 *  - buildEditedBytes(prst, key, edits) — what live edit / keep writes — routes
 *    through PRST.codecFor(key) and is byte-for-byte with
 *    patch/prst150_format.apply_edits for Explorer-shaped edit specs (the GP-50
 *    codec on the same bytes is NOT, which is the bug this guards against), refuses a
 *    preset of the other transport family, and is unchanged for GP-5/GP-50;
 *  - the model picker: an entry from PatchLib.modelsForBlock (ring key
 *    slot<<24|type) plus its param defaults lands as that type + those params;
 *  - nameMax(key): 13 for the GP-150, 10 for GP-5/GP-50;
 *  - rowName(p, name): nameless empty slots read "(empty slot)";
 *  - blankFor(key, slot), emptySourceWrites(writes, names, key);
 *  - explorer.js / explorer.html actually use them.
 *   node app/tests/test_explorer_edits_js.mjs
 */
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
globalThis.self = globalThis;
const PRST = require(resolve(root, "app/static/prst.js"));
globalThis.PRST = PRST;
const PatchLib = require(resolve(root, "app/static/patchlib.js"));
const X = require(resolve(root, "app/static/explorer_edits.js"));

let pass = 0, fail = 0; const fails = [];
const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };
const hex = (u) => Buffer.from(u).toString("hex");
const evidence = (name) => new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence", name)));
const EVID = ["000-New_GEN.prst", "099-Finger_AC.prst", "100-active.prst"];
const C150 = PRST.codecFor("gp150");
const ring = JSON.parse(readFileSync(resolve(root, "patch/fxid_ring_gp150.json"), "utf8"));
const lib = PatchLib.make(ring, {}, PRST.GP150);

// the Explorer's param default (explorer.js resolveDefault, verbatim)
function resolveDefault(pd) {
  const d = Number(pd.default);
  if (Number.isFinite(d)) return d;
  if (pd.toggle) return 0;
  const min = Number(pd.min ?? 0);
  const max = Number(pd.max ?? 100);
  if ((pd.unit || "") === "ms") return Math.min(Math.max(500, min), max);
  if (min < 0) return 0;
  return Math.round((min + max) / 2);
}
// What explorer.js builds: getEdit() + applyModel() + editsSpec(), keyed by block index (== slot).
function explorerSpec({ params = {}, bypass = {}, settings = {}, models = {}, name = null, order = null } = {}) {
  const spec = { params, bypass, settings, footswitches: {}, models };
  if (name != null) spec.name = name;
  if (order != null) spec.order = order;
  return spec;
}
const pick = (block, pred) => lib.modelsForBlock(block, []).find(pred);
const dlyModel = pick("DLY", (m) => (m.fxid & 0xff) === 4); // Ping Pong Delay
const modNone = pick("MOD", (m) => m.name === "None");
const withDefaults = (m) => Object.fromEntries((m.params || []).map((pd) => [pd.algId, resolveDefault(pd)]));
const SPECS = [
  ["rename", explorerSpec({ name: "Oracle 13 chr" })],
  ["param", explorerSpec({ params: { 5: { 0: 33 } } })],
  ["bypass", explorerSpec({ bypass: { 10: false, 0: true } })],
  ["settings", explorerSpec({ settings: { patch_vol: 60, bpm: 100 } })],
  ["dly-model", explorerSpec({ models: { 9: dlyModel.fxid }, params: { 9: withDefaults(dlyModel) } })],
  ["mod-none", explorerSpec({ models: { 8: modNone.fxid }, params: { 8: withDefaults(modNone) } })],
  ["rvb-after-amp", explorerSpec({ order: [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11] })],
  ["combined", explorerSpec({ name: "Oracle", params: { 5: { 0: 33 } }, bypass: { 10: false }, settings: { patch_vol: 60, bpm: 100 },
    models: { 9: dlyModel.fxid }, order: [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11] })],
];

// --- 1. the Python oracle (prst150_format.apply_edits) on Explorer-shaped specs --------
const py = [".venv-app/bin/python", ".venv-midi/bin/python"].map((p) => resolve(root, p)).find((p) => existsSync(p)) || "python3";
const oracle = JSON.parse(execFileSync(py, ["-c", `
import sys, os, json, base64
sys.path.insert(0, ${JSON.stringify(root)})
from patch import prst150_format as f150
specs = json.loads(sys.stdin.read())
out = {}
for name in ${JSON.stringify(EVID)}:
    b = open(os.path.join(${JSON.stringify(root)}, "re", "gp150", "evidence", name), "rb").read()
    for label, spec in specs:
        out[name + "|" + label] = base64.b64encode(f150.apply_edits(b, spec)).decode()
print(json.dumps(out))
`], { cwd: root, input: JSON.stringify(SPECS), maxBuffer: 64 * 1024 * 1024 }).toString());

check("picker: DLY type-4 model found in the ring", !!dlyModel && dlyModel.fxid === ((9 << 24) | 4) >>> 0, dlyModel && dlyModel.fxid.toString(16));
check("picker: MOD None entry found", !!modNone && modNone.fxid === ((8 << 24) | 3) >>> 0);
for (const name of EVID) {
  const base = evidence(name);
  for (const [label, spec] of SPECS) {
    const tag = `${name}[${label}]`;
    const want = Buffer.from(oracle[`${name}|${label}`], "base64");
    let got;
    try { got = X.buildEditedBytes(base, "gp150", spec); } catch (e) { check(tag, false, "threw: " + e.message); continue; }
    check(`${tag} == prst150_format.apply_edits`, hex(got) === hex(want));
    check(`${tag} input untouched`, hex(base) === hex(evidence(name)));
  }
  // the bug this guards against: the GP-50 codec on a GP-150 file
  let legacy = null;
  try { legacy = PRST.applyEdits(base, SPECS[0][1]); } catch { legacy = null; }
  check(`${name}: the GP-50 codec does NOT produce the GP-150 edit`, legacy === null || hex(legacy) !== oracle[`${name}|rename`]);
}

// --- 2. what the picker edit means on the bytes -----------------------------------------
{
  const base = evidence("100-active.prst");
  const out = X.buildEditedBytes(base, "gp150", SPECS.find(([l]) => l === "dly-model")[1]);
  const dly = C150.blocksBySlot(out)[9];
  check("picker: DLY type byte = fxid & 0xff", dly.type === (dlyModel.fxid & 0xff) && dly.subtype === 0 && dly.ext === 0);
  check("picker: DLY params = the ring defaults", dlyModel.params.every((pd) => Math.abs(dly.params[pd.algId] - resolveDefault(pd)) < 1e-6));
  check("picker: DLY engine from its position", dly.engine === C150.engineFor(dly.pos, 9, dly.type, dly.params));
  const none = C150.blocksBySlot(X.buildEditedBytes(base, "gp150", SPECS.find(([l]) => l === "mod-none")[1]))[8];
  check("picker: MOD None -> type 3 + engine 0x06 (corpus None)", none.type === 3 && none.engine === 0x06);
  const inv = lib.inventory([{ slot: 100, bytes: X.buildEditedBytes(base, "gp150", SPECS.find(([l]) => l === "mod-none")[1]) }]);
  check("picker: MOD None decodes as None", inv.patches[0].blocks[8].model === "None");
  let maxAlg = 0; for (const e of Object.values(ring)) for (const pd of e.params || []) maxAlg = Math.max(maxAlg, pd.algId);
  check("ring: every algId fits the 15 floats of a block", maxAlg < C150.N_PARAMS, String(maxAlg));
  const moved = C150.blocksBySlot(X.buildEditedBytes(base, "gp150", SPECS.find(([l]) => l === "rvb-after-amp")[1]));
  check("drag RVB after AMP: RVB at position 1", moved[10].pos === 1 && C150.readOrder(X.buildEditedBytes(base, "gp150", SPECS.find(([l]) => l === "rvb-after-amp")[1]))[1] === 10);
  check("drag RVB after AMP: None blocks stay None", [0, 1, 2, 3, 4, 6].every((s) => moved[s].engine === 0x06));
}

// --- 3. transport-family guard + GP-5/GP-50 unchanged -------------------------------------
{
  const gp150 = evidence("000-New_GEN.prst");
  let err = null; try { X.buildEditedBytes(gp150, "gp50", explorerSpec({ name: "X" })); } catch (e) { err = e; }
  check("guard: a GP-150 preset under a GP-50 key is refused", err && /GP-150/.test(err.message), err && err.message);
  const gp5 = new Uint8Array(readFileSync(resolve(here, "fixtures/gp5/65-Puppy.prst")));
  const gp50 = PRST.convert(gp5, "gp50");
  err = null; try { X.buildEditedBytes(gp50, "gp150", explorerSpec({ name: "X" })); } catch (e) { err = e; }
  check("guard: a GP-50 preset under the GP-150 key is refused", err && /GP-50/.test(err.message), err && err.message);
  const spec = explorerSpec({ name: "Lead", params: { 1: { 0: 12 } }, bypass: { 2: false }, settings: { patch_vol: 40 } });
  check("gp50: identical to PRST.applyEdits", hex(X.buildEditedBytes(gp50, "gp50", spec)) === hex(PRST.applyEdits(gp50, spec)));
  check("gp5 bytes on the gp50 store key: identical to PRST.applyEdits", hex(X.buildEditedBytes(gp5, "gp50", spec)) === hex(PRST.applyEdits(gp5, spec)));
  check("gp5 key: identical to PRST.applyEdits", hex(X.buildEditedBytes(gp5, "gp5", spec)) === hex(PRST.applyEdits(gp5, spec)));
  check("no key = gp50 (the Explorer's default)", hex(X.buildEditedBytes(gp50, null, spec)) === hex(PRST.applyEdits(gp50, spec)));
}

// --- 4. name length, row label, blank, empty reorder sources -------------------------------
{
  check("nameMax gp150 = 13", X.nameMax("gp150") === 13);
  check("nameMax gp50 = 10", X.nameMax("gp50") === 10);
  check("nameMax gp5 = 10", X.nameMax("gp5") === 10);
  check("nameMax default = 10", X.nameMax(null) === 10 && X.nameMax(undefined) === 10);
  const thirteen = "Thirteen Char";
  const named = X.buildEditedBytes(evidence("000-New_GEN.prst"), "gp150", explorerSpec({ name: thirteen }));
  check("a 13-char GP-150 name round-trips", thirteen.length === 13 && C150.readName(named) === thirteen);

  check("rowName: nameless empty slot", X.rowName({ name: "", empty: true }, "") === "(empty slot)");
  check("rowName: empty slot renamed by a pending edit", X.rowName({ name: "", empty: true }, "Foo") === "Foo");
  check("rowName: GP-50 empty keeps its device name", X.rowName({ name: "GP-50", empty: true }, "GP-50") === "GP-50");
  check("rowName: GP-50 empty with its name cleared stays blank (as before)", X.rowName({ name: "GP-50", empty: true }, "") === "");
  check("rowName: a named preset is unchanged", X.rowName({ name: "Lead", empty: false }, "Lead") === "Lead");
  check("rowName: a nameless non-empty preset stays blank", X.rowName({ name: "", empty: false }, "") === "");
  check("rowName: name omitted -> p.name", X.rowName({ name: "Lead", empty: false }) === "Lead" && X.rowName({ name: "", empty: true }) === "(empty slot)");
  const inv = lib.inventory([{ slot: 7, bytes: (() => { const z = C150.blankPrst(7); C150.writeName(z, ""); return z; })() }]);
  check("a scan-stored silent slot is empty and labelled", inv.patches[0].empty === true && X.rowName(inv.patches[0], inv.patches[0].name) === "(empty slot)");

  const blank = X.blankFor("gp150", 199);
  check("blankFor gp150: New GEN. with the slot's index", C150.readName(blank) === "New GEN." && blank[4] === 199 && blank.length === 1128);
  check("blankFor gp50: the GP-50 blank", hex(X.blankFor("gp50", 12)) === hex(PRST.blankPrst("gp50")));
  check("blankFor default: the GP-50 blank", hex(X.blankFor(null, 3)) === hex(PRST.blankPrst("gp50")));

  const names = { 0: "Lead", 1: "", 2: "Clean", 3: "" };
  const writes = [{ slot: 0, from: 1 }, { slot: 1, from: 2 }, { slot: 2, from: 0 }, { slot: 3, from: 3 }];
  check("emptySourceWrites gp150: writes copying a nameless slot", JSON.stringify(X.emptySourceWrites(writes, names, "gp150").map((w) => w.slot)) === "[0,3]");
  check("emptySourceWrites gp50: never (GP-50 empties are named)", X.emptySourceWrites(writes, names, "gp50").length === 0);
}

// --- 5. explorer.js / explorer.html are wired to the helpers -------------------------------
{
  const js = readFileSync(resolve(root, "app/static/explorer.js"), "utf8");
  const html = readFileSync(resolve(root, "app/static/explorer.html"), "utf8");
  check("explorer.js: no direct PRST.applyEdits (GP-50 codec) call", !/PRST\.applyEdits\(/.test(js));
  check("explorer.js: live write + keep use buildEditedBytes", (js.match(/ExplorerEdits\.buildEditedBytes\(liveBase, devKey\(\), editsSpec\(slot\)\)/g) || []).length === 2);
  check("explorer.js: name input capped by nameMax", /nameInput\.maxLength = maxName/.test(js) && /ExplorerEdits\.nameMax\(devKey\(\)\)/.test(js));
  check("explorer.js: no hard-coded maxLength = 10", !/maxLength = 10/.test(js));
  check("explorer.js: rows labelled via rowName", (js.match(/rowLabel\(p\)/g) || []).length >= 2 && /ExplorerEdits\.rowName\(/.test(js));
  check("explorer.js: clear uses blankFor", /ExplorerEdits\.blankFor\(key, p\.slot\)/.test(js));
  check("explorer.js: reorder confirm names empty sources", /ExplorerEdits\.emptySourceWrites\(/.test(js));
  const at = (s) => html.indexOf(s);
  check("explorer.html loads explorer_edits.js after prst150.js and before explorer.js",
    at("explorer_edits.js") > at("prst150.js") && at("prst150.js") > 0 && at("explorer_edits.js") < at("/static/explorer.js"));
}

console.log(`explorer edits: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
