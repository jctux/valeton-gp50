/* GP-150 write path in the browser, gated.
 *  - webmidi_write.js builds Suite's import stream byte-for-byte (the captured
 *    import in fixtures/gp150/ht_corpus.json is the oracle; patch/device_write.py
 *    must agree), and validateStream re-parses every frame;
 *  - the legacy payload-length maps never contain the GP-150;
 *  - HtTransport.writePreset against a scripted fake pedal: chunks in order and
 *    paced, ACK + 0x08 awaited, the 0x08 ACKed exactly once (by the session's own
 *    handler, which stays installed), clear errors on silence;
 *  - writeSlot / _sendStream refuse the GP-150 while WRITE_VERIFIED.gp150 is false
 *    unless allowUnverified, sending zero bytes; GP-5/GP-50 writeSlot unchanged.
 *   node app/tests/test_write_gp150_js.mjs
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
const HT = require(resolve(root, "app/static/ht_proto.js"));
globalThis.HtProto = HT;
const PRST = require(resolve(root, "app/static/prst.js"));
globalThis.PRST = PRST;
const T = require(resolve(root, "app/static/ht_transport.js"));
globalThis.HtTransport = T;
const WW = require(resolve(root, "app/static/webmidi_write.js"));

const FIX = JSON.parse(readFileSync(resolve(here, "fixtures/gp150/ht_corpus.json"), "utf8"));
const fromHex = (s) => new Uint8Array(Buffer.from(s, "hex"));
const hex = (w) => (w ? Buffer.from(Uint8Array.from(w)).toString("hex") : "<none>");
const MSG = Object.fromEntries(FIX.messages.map((m) => [m.id, fromHex(m.raw)]));
const stream = (prefix) => Object.keys(MSG).filter((k) => k.startsWith(prefix))
  .sort((a, z) => Number(a.split("_").pop()) - Number(z.split("_").pop())).map((k) => MSG[k]);
const evidence = (name) => new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence", name)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let pass = 0, fail = 0; const fails = [];
const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };
const rejects = (p, re) => p.then((v) => ({ ok: false, why: `resolved ${JSON.stringify(v)}` }), (e) => ({ ok: re.test(e.message), why: e.message }));

const exportPrst = HT.presetFromPayload(HT.assembleStream(stream("export_stream_slot1_").map(HT.parseFrame)).payload);
const theirs = stream("import_stream_slot1_");
const finger = evidence("099-Finger_AC.prst");
const NOTIFY_TX = 0x0d; // tx id of the captured 0x08 import notification

// --- 1. the Suite capture is the oracle ------------------------------------------
{
  const before = hex(exportPrst);
  const ours = WW.buildPatchWriteStream(exportPrst, 0);
  check("oracle: 10 chunks", ours.length === 10 && theirs.length === 10, `${ours.length}`);
  ours.forEach((w, i) => check(`oracle: chunk ${i} byte-exact vs Suite`, hex(w) === hex(theirs[i])));
  check("oracle: the exported file is not mutated", hex(exportPrst) === before && exportPrst[0x0a] === 0x58 && exportPrst[4] === 0);
  check("oracle: packets are plain arrays like the legacy builder", ours.every((w) => Array.isArray(w)));
  check("oracle: validates", WW.validateStream(ours)[0] === true && WW.validateStream(ours, 0)[0] === true);
  check("oracle: buildGp150WriteStream is the same stream", hex(WW.buildGp150WriteStream(exportPrst, 0).flat()) === hex(ours.flat()));
  check("oracle: matches HtProto.importStream", HT.importStream(0x24, exportPrst).every((w, i) => hex(w) === hex(ours[i])));
}

// --- 2. JS == patch/device_write.py ----------------------------------------------
{
  const py = [".venv-app/bin/python", ".venv-midi/bin/python"].map((p) => resolve(root, p)).find((p) => existsSync(p)) || "python3";
  const out = JSON.parse(execFileSync(py, ["-c", `
import sys, os, json
sys.path.insert(0, ${JSON.stringify(root)})
from patch import device_write as dw
out = {}
for name in ("000-New_GEN.prst", "099-Finger_AC.prst", "100-active.prst"):
    prst = open(os.path.join(${JSON.stringify(root)}, "re", "gp150", "evidence", name), "rb").read()
    for slot in (0, 1, 99, 199):
        pk = dw.build_gp150_write_stream(prst, slot)
        out[name + "@" + str(slot)] = {"packets": [w.hex() for w in pk], "validate": list(dw.validate_gp150_stream(pk, slot)),
                                       "wrongSlot": list(dw.validate_gp150_stream(pk, (slot + 1) % 200))}
print(json.dumps(out))
`], { cwd: root }).toString());
  check("py parity: 12 vectors", Object.keys(out).length === 12);
  for (const [key, rec] of Object.entries(out)) {
    const [name, slot] = key.split("@");
    const js = WW.buildPatchWriteStream(evidence(name), Number(slot));
    check(`py parity: ${key} packets`, js.length === rec.packets.length && js.every((w, i) => hex(w) === rec.packets[i]));
    check(`py parity: ${key} validate`, WW.validateStream(js, Number(slot))[0] === rec.validate[0] && rec.validate[0] === true);
    check(`py parity: ${key} wrong slot`, WW.validateStream(js, (Number(slot) + 1) % 200)[0] === rec.wrongSlot[0] && rec.wrongSlot[0] === false);
  }
}

// --- 3. validateStream re-parses every frame -------------------------------------
function reframe(w, { piece, idx, tid, off, family } = {}) {
  const f = HT.parseFrame(Uint8Array.from(w)), c = HT.chunkFields(f);
  piece = piece ?? c.piece; idx = idx ?? c.index; tid = tid ?? c.transferId; off = off ?? c.offset;
  return Array.from(HT.frame(family ?? f.family, [0x08, off & 0x7f, (off >> 7) & 0x7f, tid], Uint8Array.from([idx, ...HT.enc(piece)])));
}
{
  const good = WW.buildPatchWriteStream(finger, 199);
  const v = (pk, slot) => WW.validateStream(pk, slot);
  check("validate: good stream", v(good)[0] === true && v(good, 199)[0] === true, v(good)[1]);
  const crc = good.map((w) => w.slice()); crc[3][2] ^= 0x01;
  check("validate: mutated outer CRC rejected", v(crc)[0] === false && /packet 3/.test(v(crc)[1]), v(crc)[1]);
  const nib = good.map((w) => w.slice()); nib[3][20] ^= 0x01;
  check("validate: changed nibble with a stale CRC rejected", v(nib)[0] === false);
  const piece = HT.chunkFields(HT.parseFrame(Uint8Array.from(good[3]))).piece.slice(); piece[10] ^= 0x01;
  const inner = good.slice(0, 3).concat([reframe(good[3], { piece })], good.slice(4));
  check("validate: mutated inner CRC rejected", v(inner)[0] === false && /CRC/.test(v(inner)[1]), v(inner)[1]);
  const skipped = good.slice(0, 3).concat(good.slice(4));
  check("validate: skipped chunk index rejected", v(skipped)[0] === false && /index/.test(v(skipped)[1]), v(skipped)[1]);
  check("validate: renumbered chunk rejected", v(good.slice(0, 3).concat([reframe(good[3], { idx: 5 })], good.slice(4)))[0] === false);
  const oneBased = HT.chunkFrames(HT.FAMILY_PATCH, 0x24, HT.logical(HT.importPayload(finger)), false).map((w) => Array.from(w));
  check("validate: 1-based (device->host) numbering rejected", v(oneBased)[0] === false);
  const off = v(good.slice(0, 3).concat([reframe(good[3], { off: 119 * 3 + 1 })], good.slice(4)));
  check("validate: bad offset rejected", off[0] === false && /offset/.test(off[1]), off[1]);
  const wrong = v(good, 5);
  check("validate: wrong slot byte rejected", wrong[0] === false && /199/.test(wrong[1]), wrong[1]);
  const b250 = Uint8Array.from(finger); b250[4] = 250;
  const r250 = v(HT.importStream(0x24, b250).map((w) => Array.from(w)));
  check("validate: index byte outside 0..199 rejected", r250[0] === false && /250/.test(r250[1]), r250[1]);
  const noFinal = v(good.slice(0, -1));
  check("validate: no short final chunk rejected", noFinal[0] === false && /final/.test(noFinal[1]), noFinal[1]);
  const exportShaped = HT.chunkFrames(HT.FAMILY_PATCH, 0x24, HT.logical(Uint8Array.from([0x03, 0x03, 0x11, 0x30, ...finger]))).map((w) => Array.from(w));
  check("validate: export-shaped payload rejected", v(exportShaped)[0] === false);
  const trailing = HT.chunkFrames(HT.FAMILY_PATCH, 0x24, HT.logical(Uint8Array.from([...HT.importPayload(finger), 0]))).map((w) => Array.from(w));
  check("validate: trailing payload byte rejected", v(trailing)[0] === false);
  check("validate: wrong family rejected", v(good.map((w) => reframe(w, { family: 0x2c })))[0] === false);
  check("validate: mixed transfer ids rejected", v(good.slice(0, 3).concat([reframe(good[3], { tid: 0x25 })], good.slice(4)))[0] === false);
  check("validate: a hello is not a chunk", v([Array.from(HT.hello())])[0] === false);
  const eightBit = Array.from(HT.frame(HT.FAMILY_PATCH, [0x08, 0, 0, 0x80], Uint8Array.from([0, ...HT.enc([0])])));
  check("validate: data byte above 0x7F rejected", v([eightBit])[0] === false);
  check("validate: empty stream rejected", v([])[0] === false);
  // transfer id 0 is not a transfer id (1..0x7F)
  const b199 = Uint8Array.from(finger); b199[4] = 199;
  check("validate: transfer id 0x24 accepted", v(HT.importStream(0x24, b199).map((w) => Array.from(w)), 199)[0] === true);
  const t0 = v(HT.importStream(0, b199).map((w) => Array.from(w)), 199);
  check("validate: transfer id 0 rejected", t0[0] === false && /transfer id/.test(t0[1]), t0[1]);
  // the 0x0A import marker and the magic (crafted without importPayload, which forces 0x0A)
  const raw = (prst) => HT.chunkFrames(HT.FAMILY_PATCH, 0x24, HT.logical(Uint8Array.from([0x01, 0x03, 0x11, 0x30, ...prst]))).map((w) => Array.from(w));
  const okB = Uint8Array.from(b199); okB[0x0a] = 0x5c;
  check("validate: crafted import stream accepted", v(raw(okB), 199)[0] === true, v(raw(okB), 199)[1]);
  for (const val of [0x58, 0x00]) {
    const m = Uint8Array.from(okB); m[0x0a] = val;
    const r0a = v(raw(m), 199);
    check(`validate: byte 0x0A = 0x${val.toString(16)} rejected`, r0a[0] === false && /0x0A/.test(r0a[1]), r0a[1]);
  }
  const mg = Uint8Array.from(okB); mg[1] ^= 0xff;
  const rmg = v(raw(mg), 199);
  check("validate: bad magic rejected", rmg[0] === false && /magic/.test(rmg[1]), rmg[1]);
  check("validateGp150Stream exported", typeof WW.validateGp150Stream === "function" && WW.validateGp150Stream(good, 199)[0] === true);
}

// --- 4. R-LEGACY: the legacy payload-length maps skip "ht" devices -----------------
{
  const lens = WW.legacyPayloadLens();
  check("legacy map: only GP-5/GP-50", Object.values(lens).map((p) => p.key).sort().join() === "gp5,gp50", JSON.stringify(Object.values(lens).map((p) => p.key)));
  check("legacy map: no ht transport", Object.values(lens).every((p) => p.transport === "legacy"));
  check("legacy map: 488 / 533 bytes", Object.keys(lens).map(Number).sort((a, z) => a - z).join() === "488,533");
  const payload = [0x11, 0x4f, 7, 0, 0, 0, ...finger.subarray(0x19)]; // a 0x1D stream with a GP-150 body
  const bogus = [];
  for (let i = 0; i < payload.length; i += 19) bogus.push(WW.buildPacket(0x1d, i / 19, payload.slice(i, i + 19)));
  const r = WW.validateStream(bogus);
  check("legacy map: a 1109-byte 0x1D stream is rejected", r[0] === false && /1109/.test(r[1]), r[1]);
  check("legacy map: ... and infers no device", WW.inferDeviceKey(bogus) === null);
  check("inferDeviceKey: HT stream -> gp150", WW.inferDeviceKey(WW.buildPatchWriteStream(finger, 3)) === "gp150");
  check("WRITE_VERIFIED.gp150 is false", WW.WRITE_VERIFIED.gp150 === false);
}

// --- 5. HtTransport.writePreset against a fake pedal -------------------------------
// After the final (short) 0x70 chunk the pedal ACKs the transfer id, then sends
// the captured 0x08 notification (tx 0x0D) `notifyDelay` ms later.
function importPedal({ ack = true, notify = true, notifyRaw = MSG.import_notify_08, notifyDelay = 30, dead = false, onChunk = null, failAt = -1, name = "GP-150" } = {}) {
  const sent = [], sentAt = []; const input = { name, onmidimessage: null };
  let notifiedAt = 0;
  const deliver = (u8) => input.onmidimessage && input.onmidimessage({ data: u8 });
  const emit = (u8, ms = 1) => setTimeout(() => deliver(u8), ms);
  const output = { name, send(bytes) {
    const w = Uint8Array.from(bytes);
    const f = HT.parseFrame(w);
    if (f.family === HT.FAMILY_PATCH && f.body[0] === failAt) throw new Error("MIDI output went away"); // failAt: a port error
    sent.push(w); sentAt.push(Date.now());
    if (dead) return;
    if (f.family === 0x00 && f.tx4[1] === 0x01) return emit(MSG.hello_reply);
    if (f.family === 0x0c) { emit(MSG.ack_tx0); return emit(MSG.ident_reply, 3); } // hello()'s session open
    if (f.family !== HT.FAMILY_PATCH || !HT.isChunk(f)) return;
    if (onChunk) onChunk(f.body[0], { emit, deliver });
    if (w.length < HT.FULL_WIRE_LEN) {
      if (ack) emit(HT.ack(f.tx4[3]), 2);
      if (notify) setTimeout(() => { notifiedAt = Date.now(); deliver(notifyRaw); }, notifyDelay);
    }
  } };
  const acksFor = (id) => sent.filter((w) => hex(w) === hex(HT.ack(id))).length;
  return { input, output, sent, sentAt, deliver, notifiedAt: () => notifiedAt, acksFor };
}
// Happy paths resolve on the 0x08, so their generous timeouts cost nothing and keep
// CPU contention from expiring them; SHORT is for the paths that must time out.
const W = { settleMs: 5, timeoutMs: 5000, idleMs: 1000, emptyTimeoutMs: 1500, writePaceMs: 5, notifyTimeoutMs: 3000 };
const SHORT = Object.assign({}, W, { timeoutMs: 400, notifyTimeoutMs: 250 });
const pk199 = WW.buildPatchWriteStream(finger, 199);
{
  const p = importPedal({ notifyDelay: 60 });
  const s = T.create(p.input, p.output, W);
  const handler = p.input.onmidimessage;
  const r = await s.writePreset(pk199);
  const notifiedBeforeResolve = p.notifiedAt();
  check("write: result", r && r.sent === 10 && r.acks === 1 && r.notified === true, JSON.stringify(r));
  check("write: chunks sent in order", pk199.every((w, i) => p.sent[i] && hex(w) === hex(p.sent[i])));
  check("write: resolved only after the 0x08 arrived", notifiedBeforeResolve > 0);
  check("write: exactly ONE ACK for the 0x08 (the session's auto-ACK)", p.acksFor(NOTIFY_TX) === 1, `${p.acksFor(NOTIFY_TX)}`);
  check("write: nothing else sent (no ACK of the transfer id, no extra frames)", p.sent.length === 11 && p.acksFor(0x24) === 0, `${p.sent.length}`);
  check("write: session input handler untouched", p.input.onmidimessage === handler && typeof handler === "function");
  const gaps = p.sentAt.slice(1, 10).map((t, i) => t - p.sentAt[i]);
  check("write: chunks paced writePaceMs apart", gaps.length === 9 && Math.min(...gaps) >= W.writePaceMs - 1, gaps.join(","));
  check("write: every host byte 7-bit", p.sent.every((w) => w[0] === 0xf0 && w[w.length - 1] === 0xf7 && w.subarray(1, -1).every((x) => x <= 0x7f)));
  s.close();
}
{ // a preset (Uint8Array) is turned into the import stream, slot = its own index byte
  const p = importPedal();
  const s = T.create(p.input, p.output, W);
  const r = await s.writePreset(finger);
  const want = HT.importStream(0x24, finger);
  check("write(prst): sends importStream(0x24, prst)", r.notified && want.every((w, i) => hex(w) === hex(p.sent[i])) && p.sent.length === 11);
  s.close();
}
{ // ACK, then no 0x08 -> a clear rejection after notifyTimeoutMs
  const p = importPedal({ notify: false });
  const s = T.create(p.input, p.output, SHORT);
  const t0 = Date.now();
  const r = await rejects(s.writePreset(pk199), /0x08/);
  check("write: no 0x08 -> rejects with a clear error", r.ok, r.why);
  check("write: no 0x08 -> only after the notify timeout", Date.now() - t0 >= SHORT.notifyTimeoutMs);
  check("write: no 0x08 -> no stray ACKs or retries", p.sent.length === 10);
  s.close();
}
{ // nothing at all
  const p = importPedal({ dead: true });
  const s = T.create(p.input, p.output, SHORT);
  const r = await rejects(s.writePreset(pk199), /did not ACK/);
  check("write: dead pedal -> rejects 'did not ACK'", r.ok, r.why);
  check("write: dead pedal -> the stream went out once", p.sent.length === 10);
  s.close();
}
{ // a 0x08 with another payload is not Suite's "import done"
  const other = HT.frame(HT.FAMILY_IMPORT_DONE, [0, 0, 0, 0x0e], Uint8Array.from([0x01, ...HT.enc(HT.logical([0x09, 0x03, 0x11, 0x31]))]));
  const p = importPedal({ notifyRaw: other });
  const s = T.create(p.input, p.output, W);
  const r = await rejects(s.writePreset(pk199), /unexpected/);
  check("write: unexpected 0x08 payload -> rejects", r.ok, r.why);
  check("write: ... still ACKed once by the session", p.acksFor(0x0e) === 1);
  s.close();
}
{ // the 0x08 without the ACK still means the import landed
  const p = importPedal({ ack: false });
  const s = T.create(p.input, p.output, W);
  const r = await s.writePreset(pk199);
  check("write: 0x08 without ACK -> acks 0, notified", r.acks === 0 && r.notified === true, JSON.stringify(r));
  s.close();
}
{ // an unsolicited status frame mid-stream: ACKed once, does not end the write
  const p = importPedal({ onChunk: (idx, { emit }) => { if (idx === 4) emit(MSG.ident_reply); } });
  const s = T.create(p.input, p.output, W);
  const r = await s.writePreset(pk199);
  check("write: unsolicited frame mid-stream -> still waits for the 0x08", r.notified === true);
  check("write: unsolicited frame ACKed once, 0x08 ACKed once", p.acksFor(1) === 1 && p.acksFor(NOTIFY_TX) === 1);
  check("write: chunks still contiguous", hex(p.sent.filter((w) => w[3] === HT.FAMILY_PATCH).flatMap((w) => Array.from(w))) === hex(pk199.flat()));
  s.close();
}
{ // a stray 0x08 / transfer-id ACK while chunks are still going out: no early finish
  const p = importPedal({ onChunk: (idx, { deliver }) => { if (idx === 3) { deliver(HT.ack(0x24)); deliver(MSG.import_notify_08); } } });
  const s = T.create(p.input, p.output, W);
  const r = await s.writePreset(pk199);
  check("write: a mid-stream 0x08 does not end the write", r.notified && p.sent.filter((w) => w[3] === HT.FAMILY_PATCH).length === 10, `${p.sent.length}`);
  check("write: both 0x08s ACKed by the session, once each", p.acksFor(NOTIFY_TX) === 2);
  s.close();
  const q = importPedal({ ack: false, notify: false, onChunk: (idx, { deliver }) => { if (idx === 3) { deliver(HT.ack(0x24)); deliver(MSG.import_notify_08); } } });
  const s2 = T.create(q.input, q.output, SHORT);
  const r2 = await rejects(s2.writePreset(pk199), /did not ACK/);
  check("write: only a mid-stream ACK + 0x08 -> still 'did not ACK'", r2.ok && q.sent.filter((w) => w[3] === HT.FAMILY_PATCH).length === 10, r2.why);
  s2.close();
}
{ // one request in flight: a hello queued behind the write goes out after it
  const p = importPedal();
  const s = T.create(p.input, p.output, W);
  const [r, alive] = await Promise.all([s.writePreset(pk199), s.hello()]);
  const iHello = p.sent.findIndex((w) => hex(w) === hex(HT.hello()));
  const iNoteAck = p.sent.findIndex((w) => hex(w) === hex(HT.ack(NOTIFY_TX)));
  check("write: serialized with other requests", r.notified && alive && iHello > iNoteAck && iNoteAck >= 10, `${iHello} ${iNoteAck}`);
  s.close();
}
{ // close() mid-write: rejects "session closed", no further chunks
  const p = importPedal();
  const s = T.create(p.input, p.output, Object.assign({}, W, { writePaceMs: 40 }));
  const pending = rejects(s.writePreset(pk199), /session closed/);
  await sleep(60);
  s.close();
  const n = p.sent.length;
  const r = await pending;
  await sleep(80);
  check("write: close mid-write -> rejects 'session closed'", r.ok, r.why);
  check("write: close mid-write -> no further bytes", p.sent.length === n && n < 10, `${n} -> ${p.sent.length}`);
}
// --- errors that leave the slot unknown say "read slot N back before retrying" ------
const READ_BACK = /read slot 199 back before retrying/;
{
  const cases = [
    ["no 0x08", importPedal({ notify: false }), SHORT],
    ["dead pedal", importPedal({ dead: true }), SHORT],
    ["unexpected 0x08 payload", importPedal({ notifyRaw: HT.frame(HT.FAMILY_IMPORT_DONE, [0, 0, 0, 0x0e], Uint8Array.from([0x01, ...HT.enc(HT.logical([0x09, 0x03, 0x11, 0x31]))])) }), W],
    ["unreadable 0x08", importPedal({ notifyRaw: (() => { const l = Uint8Array.from(HT.logical(HT.IMPORT_DONE_PAYLOAD)); l[1] ^= 0xff; return HT.frame(HT.FAMILY_IMPORT_DONE, [0, 0, 0, 0x0e], Uint8Array.from([0x01, ...HT.enc(l)])); })() }), W],
    ["port error mid-stream", importPedal({ failAt: 4 }), W],
  ];
  for (const [what, p, opts] of cases) {
    const s = T.create(p.input, p.output, opts);
    const r = await rejects(s.writePreset(pk199), READ_BACK);
    check(`read-back hint: ${what}`, r.ok, r.why);
    s.close();
  }
  const pf = importPedal({ failAt: 4 });
  const sf = T.create(pf.input, pf.output, W);
  const rf = await rejects(sf.writePreset(pk199), /failed while sending/);
  check("port error mid-stream -> 'failed while sending', nothing after it", rf.ok && pf.sent.length === 4, `${rf.why} sent=${pf.sent.length}`);
  sf.close();
}
{ // close() after the final chunk went out (waiting for the 0x08): the slot may be written
  const p = importPedal({ notify: false });
  const s = T.create(p.input, p.output, W);
  const pending = rejects(s.writePreset(pk199), /session closed/);
  for (let i = 0; i < 200 && p.sent.length < 10; i++) await sleep(5);
  await sleep(20);
  s.close();
  const r = await pending;
  check("close after the final chunk -> 'session closed' + read-back hint", r.ok && READ_BACK.test(r.why) && /after the import/.test(r.why), r.why);
}
{ // close() mid-stream: an incomplete stream went out; still unknown until read back
  const p = importPedal();
  const s = T.create(p.input, p.output, Object.assign({}, W, { writePaceMs: 40 }));
  const pending = rejects(s.writePreset(pk199), /session closed/);
  await sleep(60);
  s.close();
  const r = await pending;
  check("close mid-stream -> 'session closed' + read-back hint", r.ok && READ_BACK.test(r.why) && /of 10 chunks/.test(r.why), r.why);
}
{ // closed before anything went out: plain "session closed", nothing sent
  const p = importPedal();
  const s = T.create(p.input, p.output, W);
  const first = s.hello(); // occupies the queue
  const pending = rejects(s.writePreset(pk199), /session closed/);
  s.close();
  await first.catch(() => {});
  const r = await pending;
  check("closed before sending -> plain 'session closed', no chunks", r.ok && !READ_BACK.test(r.why) && !p.sent.some((w) => w[3] === HT.FAMILY_PATCH), r.why);
}

{ // the session refuses frames that are not one 0x70 import stream, before sending
  const p = importPedal();
  const s = T.create(p.input, p.output, W);
  const legacy = WW.buildPacket(0x1d, 0, [1, 2, 3]);
  const a = await rejects(s.writePreset([legacy]), /HT frame|not an F0 7F/);
  const b = await rejects(s.writePreset([Array.from(HT.hello())]), /0x70/);
  const c = await rejects(s.writePreset(pk199.slice(0, -1)), /final|assemble/);
  const d = await rejects(s.writePreset(new Uint8Array(1128)), /magic|GP-150/);
  check("write: refuses legacy packets", a.ok, a.why);
  check("write: refuses non-0x70 frames", b.ok, b.why);
  check("write: refuses a stream that does not assemble", c.ok, c.why);
  check("write: refuses a non-GP-150 preset", d.ok, d.why);
  check("write: refusals send nothing", p.sent.length === 0, `${p.sent.length}`);
  s.close();
}

// --- 6. WebMidiDevice + writeSlot: the gate ------------------------------------------
{
  globalThis.WebMidiWrite = WW; // as in the browser (webmidi_write.js sets window.WebMidiWrite)
  const saved = Object.assign({}, T.DEFAULTS); Object.assign(T.DEFAULTS, W);
  const p150 = importPedal({ name: "GP-150" });
  const portMap = (...ps) => new Map(ps.map((x, i) => [String(i), x]));
  const useNavigator = (...pedals) => Object.defineProperty(globalThis, "navigator", { configurable: true, value: { requestMIDIAccess: async () => ({ inputs: portMap(...pedals.map((x) => x.input)), outputs: portMap(...pedals.map((x) => x.output)) }) } });
  useNavigator(p150);
  require(resolve(root, "app/static/webmidi_device.js"));
  const D = globalThis.WebMidiDevice;
  const info = await D.connect();
  check("device: GP-150 connected", info.key === "gp150");
  const n0 = p150.sent.length; // the hello
  let r = await rejects(WW.writeSlot(199, finger, { confirm: true }), /unverified/);
  check("gate: writeSlot(gp150) without allowUnverified refuses", r.ok, r.why);
  r = await rejects(WW.writeSlot(199, finger, { confirm: true, allowUnverified: false }), /GP-150/);
  check("gate: the refusal names the GP-150", r.ok, r.why);
  r = await rejects(WW.writeSlot(199, finger, { allowUnverified: true }), /confirm/);
  check("gate: writeSlot without confirm refuses", r.ok, r.why);
  let syncErr = null; try { D._sendStream(pk199, { confirm: true, validated: true }); } catch (e) { syncErr = e; }
  check("gate: _sendStream(ht) without allowUnverified refuses", syncErr && /not verified|unverified/.test(syncErr.message) && /GP-150/.test(syncErr.message), syncErr && syncErr.message);
  syncErr = null; try { D._sendStream([WW.buildPacket(0x1d, 0, [1])], { confirm: true, validated: true, allowUnverified: true }); } catch (e) { syncErr = e; }
  check("gate: _sendStream(ht) refuses legacy packets", syncErr && /HT/.test(syncErr.message), syncErr && syncErr.message);
  const gp50 = PRST.convert(new Uint8Array(readFileSync(resolve(here, "fixtures/gp5/65-Puppy.prst"))), "gp50");
  r = await rejects(WW.writeSlot(7, gp50, { confirm: true, allowUnverified: true }), /GP-150|connected/);
  check("gate: a GP-50 preset is not written to a GP-150", r.ok, r.why);
  check("gate: every refusal sent zero bytes", p150.sent.length === n0, `${n0} -> ${p150.sent.length}`);
  const w = await WW.writeSlot(199, finger, { confirm: true, allowUnverified: true });
  check("allowUnverified: writeSlot resolves {sent, acks, notified}", w && w.sent === 10 && w.acks === 1 && w.notified === true, JSON.stringify(w));
  const chunks = p150.sent.slice(n0, n0 + 10);
  check("allowUnverified: the validated slot-199 stream went out", chunks.length === 10 && pk199.every((x, i) => hex(x) === hex(chunks[i])));
  check("allowUnverified: 0x08 ACKed once", p150.acksFor(NOTIFY_TX) === 1);
  D.disconnect();

  // GP-5/GP-50 writeSlot is unchanged: same packets, one ACK-paced block at a time
  const ackEcho = { sent: [], input: { name: "GP-50", onmidimessage: null } };
  ackEcho.output = { name: "GP-50", send(bytes) { ackEcho.sent.push(Array.from(bytes)); setTimeout(() => ackEcho.input.onmidimessage && ackEcho.input.onmidimessage({ data: Uint8Array.of(0xf0, 0x00, 0xf7) }), 1); } };
  useNavigator(ackEcho);
  const info50 = await D.connect();
  check("legacy: GP-50 connected", info50.key === "gp50");
  r = await rejects(WW.writeSlot(7, finger, { confirm: true, allowUnverified: true }), /GP-150|connected/);
  check("legacy: a GP-150 preset is not written to a GP-50", r.ok && ackEcho.sent.length === 0, r.why);
  const lw = await WW.writeSlot(7, gp50, { confirm: true });
  const legacyPk = WW.buildPatchWriteStream(gp50, 7);
  check("legacy: writeSlot result unchanged", lw.sent === legacyPk.length && lw.acks === legacyPk.length && lw.notified === undefined, JSON.stringify(lw));
  check("legacy: the same 0x1D packets", ackEcho.sent.length === legacyPk.length && legacyPk.every((x, i) => hex(x) === hex(ackEcho.sent[i])));
  D.disconnect();
  Object.assign(T.DEFAULTS, saved);
}

console.log(`write gp150: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
