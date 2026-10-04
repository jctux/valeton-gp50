/* app/static/prst150.js vs patch/prst150_format.py — byte-for-byte.
 *   node app/tests/test_prst150_js.mjs            # runs prst150_oracle.py
 */
import { existsSync } from "node:fs";
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
for (const rec of O.files) {
  const b = fromB64(rec.prstB64);
  check(`${rec.path} detect`, P.detect(b));
  check(`${rec.path} name`, P.readName(b) === rec.name, P.readName(b));
  check(`${rec.path} index`, P.readIndex(b) === rec.index);
  check(`${rec.path} volbpm`, JSON.stringify(P.readVolBpm(b)) === JSON.stringify(rec.volBpm));
  check(`${rec.path} order`, JSON.stringify(P.readOrder(b)) === JSON.stringify(rec.order));
  check(`${rec.path} models`, JSON.stringify(P.modelRecords(b)) === JSON.stringify(rec.models), JSON.stringify(P.modelRecords(b)));
  check(`${rec.path} bypass`, P.bypassMask(b) === rec.bypass);
  check(`${rec.path} floats`, near(P.paramFloats(b), rec.floats));
  const edited = P.applyEdits(b, edit);
  check(`${rec.path} applyEdits`, eq(edited, fromB64(rec.editedB64)));
  check(`${rec.path} applyEdits None models`, eq(P.applyEdits(b, O.editNone), fromB64(rec.editedNoneB64)));
  check(`${rec.path} applyEdits None + bypass on`, eq(P.applyEdits(b, O.editNoneOn), fromB64(rec.editedNoneOnB64)));
  for (const t of rec.twoStep) {
    const out = P.applyEdits(P.applyEdits(b, { order: t.moved }), { order: t.orig });
    check(`${rec.path} two-step drag of slot ${t.slot}`, eq(out, fromB64(t.outB64)));
  }
  check(`${rec.path} applyEdits noop`, eq(P.applyEdits(b, {}), b));
  check(`${rec.path} input untouched`, eq(b, fromB64(rec.prstB64)));
}
check("blank 199", eq(P.blankPrst(199), fromB64(O.blank199B64)));
check("NONE_SLOTS mirror", JSON.stringify(P.NONE_SLOTS) === "[0,1,2,4,6,7,8]" && P.NONE_TYPE === 3);
check("engineFor None", (P.NONE_SLOTS || []).length === 7 && P.NONE_SLOTS.every((s) => [1, 5, 11].every((pos) => P.engineFor(pos, s, 3, []) === 0x06)) && P.engineFor(4, 3, 3, []) === 0x07);
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
{ // New GEN.: DLY (Pure Delay, 0x0B) to position 1 and back -> 0x0B again; WAH (None) stays 0x06
  const ng = O.files.find((r) => r.path.endsWith("000-New_GEN.prst"));
  const b = fromB64(ng.prstB64);
  const dly = ng.twoStep.find((t) => t.slot === 9), wah = ng.twoStep.find((t) => t.slot === 2);
  const d = P.blocksBySlot(fromB64(dly.outB64))[9], w = P.blocksBySlot(fromB64(wah.outB64))[2];
  check("two-step: DLY engine back to 0x0B, file round-trips", d.pos === 9 && d.type === 0 && d.engine === 0x0b && eq(fromB64(dly.outB64), b));
  check("two-step: None WAH stays 0x06, file round-trips", w.pos === 3 && w.type === 3 && w.engine === 0x06 && eq(fromB64(wah.outB64), b));
}
console.log(`prst150.js: ${pass} passed, ${fail} failed (${O.files.length} files)`);
for (const f of fails) console.log("  FAIL " + f);
process.exit(fail ? 1 : 0);
