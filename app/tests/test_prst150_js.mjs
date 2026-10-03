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
  check(`${rec.path} applyEdits noop`, eq(P.applyEdits(b, {}), b));
  check(`${rec.path} input untouched`, eq(b, fromB64(rec.prstB64)));
}
check("blank 199", eq(P.blankPrst(199), fromB64(O.blank199B64)));
let threw = false; try { P.writeOrder(fromB64(O.files[0].prstB64), [0, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]); } catch { threw = true; }
check("writeOrder requires AMP first", threw);
console.log(`prst150.js: ${pass} passed, ${fail} failed (${O.files.length} files)`);
for (const f of fails) console.log("  FAIL " + f);
process.exit(fail ? 1 : 0);
