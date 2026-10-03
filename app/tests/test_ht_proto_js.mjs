/* Byte-for-byte: app/static/ht_proto.js vs the captured Suite messages, and vs
 * patch/ht_proto.py for the import stream (python is the oracle).
 *   node app/tests/test_ht_proto_js.mjs
 */
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const HT = require(resolve(here, "../static/ht_proto.js"));
const FIX = JSON.parse(readFileSync(resolve(here, "fixtures/gp150/ht_corpus.json"), "utf8"));
const hex = (u) => Buffer.from(u).toString("hex");
const fromHex = (s) => new Uint8Array(Buffer.from(s, "hex"));
const MSG = Object.fromEntries(FIX.messages.map((m) => [m.id, fromHex(m.raw)]));
const stream = (prefix) => Object.keys(MSG).filter((k) => k.startsWith(prefix))
  .sort((a, z) => Number(a.split("_").pop()) - Number(z.split("_").pop())).map((k) => MSG[k]);

let pass = 0, fail = 0; const fails = [];
const check = (label, ok, detail) => { if (ok) pass++; else { fail++; fails.push(`${label}${detail ? " — " + detail : ""}`); } };
const throws = (fn) => { try { fn(); return false; } catch { return true; } };

for (const m of FIX.messages) {
  const w = fromHex(m.raw);
  let f; try { f = HT.parseFrame(w); } catch (e) { check(`${m.id} parse`, false, e.message); continue; }
  check(`${m.id} roundtrip`, hex(HT.frame(f.family, f.tx4, f.body)) === m.raw);
}
check("hello", hex(HT.hello()) === hex(MSG.hello));
check("ack 1", hex(HT.ack(1)) === hex(MSG.ack_tx1_host));
check("ack 0x0c", hex(HT.ack(0x0c)) === hex(MSG.export_final_ack));
check("export req", hex(HT.presetRequest(0x23, 0)) === hex(MSG.export_req_slot1));
check("select 2", hex(HT.presetRequest(0x63, 2, true)) === hex(MSG.select_slot2));
check("read active", hex(HT.presetRequest(1, HT.SLOT_ACTIVE)) === hex(MSG.read_active));

const frames = stream("export_stream_slot1_").map(HT.parseFrame);
const { transferId, payload } = HT.assembleStream(frames);
check("stream tid", transferId === 0x0c);
const prst = HT.presetFromPayload(payload);
check("stream prst", prst.length === 1128 && prst[0] === 0x11 && prst[0x2c] === 0x4e);
check("gap rejected", throws(() => HT.assembleStream(frames.slice(0, 3).concat(frames.slice(4)))));

// ids must stay 7-bit SysEx data bytes
check("ack id guard", throws(() => HT.ack(0x80)));
check("short id guard", throws(() => HT.presetRequest(0x80, 0)));
check("chunk tid guard", throws(() => HT.chunkFrames(HT.FAMILY_PATCH, 0x80, new Uint8Array(10))));
check("chunk index guard", throws(() => HT.chunkFrames(HT.FAMILY_PATCH, 1, new Uint8Array(119 * 128 + 1))));
check("ack 0x7f ok", !throws(() => HT.ack(0x7f)));

const ours = HT.importStream(0x24, prst);
const theirs = stream("import_stream_slot1_");
check("import chunk count", ours.length === theirs.length);
ours.forEach((w, i) => check(`import chunk ${i}`, theirs[i] && hex(w) === hex(theirs[i])));

// short-message payloads: host flag 00, the pedal's 0x08 import notify flag 01
check("shortPayload request", hex(HT.shortPayload(HT.parseFrame(MSG.export_req_slot1))) === "0303113011300200000001");
const note = HT.parseFrame(MSG.import_notify_08);
check("shortPayload 0x08 notify", note.family === HT.FAMILY_IMPORT_DONE && note.body[0] === 0x01 && hex(HT.shortPayload(note)) === hex(HT.IMPORT_DONE_PAYLOAD) && hex(HT.IMPORT_DONE_PAYLOAD) === "09031130");
check("shortPayload refuses a chunk", throws(() => HT.shortPayload(HT.parseFrame(MSG.import_stream_slot1_0))));
check("shortPayload refuses an ACK", throws(() => HT.shortPayload(HT.parseFrame(MSG.import_done_ack))));

// python oracle: same bytes from patch/ht_proto.py
const py = [".venv-app/bin/python", "python3"].map((p) => (p.includes("/") ? resolve(repoRoot, p) : p)).find((p) => !p.includes("/") || existsSync(p));
const pyHex = execFileSync(py, ["-c", `
import sys, json
sys.path.insert(0, ${JSON.stringify(repoRoot)})
from patch import ht_proto as ht
prst = open(${JSON.stringify(resolve(repoRoot, "re/gp150/evidence/099-Finger_AC.prst"))}, "rb").read()
print(json.dumps([w.hex() for w in ht.import_stream(0x31, prst)]))
`]).toString().trim();
const pyStream = JSON.parse(pyHex);
const jsStream = HT.importStream(0x31, new Uint8Array(readFileSync(resolve(repoRoot, "re/gp150/evidence/099-Finger_AC.prst")))).map(hex);
check("py/js import parity", JSON.stringify(pyStream) === JSON.stringify(jsStream));

console.log(`ht_proto.js: ${pass} passed, ${fail} failed`);
for (const f of fails) console.log("  FAIL " + f);
process.exit(fail ? 1 : 0);
