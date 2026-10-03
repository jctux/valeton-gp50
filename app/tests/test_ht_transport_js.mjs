/* ht_transport.js session against a scripted fake pedal: hello, read slot, silent
 * slot, immediate final-chunk ACK, serialized requests, 7-bit tx ids, retries —
 * plus webmidi_device.js routing a GP-150 port to that session.
 *   node app/tests/test_ht_transport_js.mjs
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const HT = require(resolve(root, "app/static/ht_proto.js"));
globalThis.HtProto = HT;
const T = require(resolve(root, "app/static/ht_transport.js"));
const FIX = JSON.parse(readFileSync(resolve(here, "fixtures/gp150/ht_corpus.json"), "utf8"));
const fromHex = (s) => new Uint8Array(Buffer.from(s, "hex"));
const hex = (u) => Buffer.from(u).toString("hex");
const MSG = Object.fromEntries(FIX.messages.map((m) => [m.id, fromHex(m.raw)]));
const exportChunks = Object.keys(MSG).filter((k) => k.startsWith("export_stream_slot1_")).sort((a, z) => Number(a.split("_").pop()) - Number(z.split("_").pop())).map((k) => MSG[k]);
let pass = 0, fail = 0; const fails = [];
const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reqPayload = (w) => HT.parseLogical(HT.dec(HT.parseFrame(w).body.subarray(1)));
const isPresetReq = (w) => w[3] === 0x0f;
const FAST = { settleMs: 5, emptyTimeoutMs: 60, timeoutMs: 500, idleMs: 60 };
// for pedals that do answer: a generous hard cap so CPU contention can't expire a
// stream between chunk timers (timeoutMs only bounds a stream that keeps flowing)
const LIVE = { ...FAST, timeoutMs: 5000 };

// A fake pedal: answers hello, answers reads with the captured slot-0 export stream,
// stays silent (after its ACK) for slot 199, logs every host frame. Knobs:
//   ackSilent  — false: no ACK either for slot 199 ("produced no reply at all")
//   dead       — never answers anything (not even hello)
//   dropChunk  — index of a chunk to leave out (gap -> stream error)
//   onRead({n, slot, ack, stream}) — script the reply to the n-th 0x0f request
//   onHello({reply, stream})       — script the reply to a hello
function fakePedal({ ackSilent = true, dead = false, dropChunk = -1, name = "GP-150", onRead = null, onHello = null } = {}) {
  const sent = []; const input = { name, onmidimessage: null }; let ackedFinal = 0, reads = 0;
  const deliver = (u8) => input.onmidimessage && input.onmidimessage({ data: u8 });
  const emit = (u8, ms = 1) => setTimeout(() => deliver(u8), ms);
  // mutate(chunk, i) -> chunk lets a test corrupt one chunk of one stream
  const stream = (ms0 = 5, mutate = null) => exportChunks.forEach((c, i) => { if (i !== dropChunk) emit(mutate ? mutate(c, i) : c, ms0 + i * 2); });
  const output = { name, send(bytes) {
    const w = Uint8Array.from(bytes); sent.push(w); const f = HT.parseFrame(w);
    if (dead) return;
    if (f.family === 0x00 && f.tx4[1] === 0x01) return onHello ? onHello({ reply: (ms = 1) => emit(MSG.hello_reply, ms), stream }) : emit(MSG.hello_reply);
    if (f.family === 0x00) { if (f.tx4[3] === 0x0c) ackedFinal++; return; }
    if (f.family === 0x0f) {
      const payload = reqPayload(w);
      const slot = payload[8] | (payload[9] << 8);
      reads++;
      if (onRead) return onRead({ n: reads, slot, ack: () => emit(HT.ack(f.tx4[3])), stream });
      if (slot === 199 && !ackSilent) return; // no ACK, no stream
      emit(HT.ack(f.tx4[3]));
      if (slot === 199) return; // empty slot: silence after the ACK
      stream();
    }
  } };
  return { input, output, sent, deliver, finalAcks: () => ackedFinal, reads: () => reads };
}
const isHelloReq = (w) => w[3] === 0x00 && w[5] === 0x01;

// --- brief scenario --------------------------------------------------------------
const pedal = fakePedal();
const s = T.create(pedal.input, pedal.output, LIVE);
check("hello", await s.hello() === true);
const t0 = Date.now();
const p0 = await s.readPreset(0);
check("read slot 0", p0 && p0.length === 1128 && p0[0x2c] === 0x4e, p0 && p0.length);
check("final chunk acked once", pedal.finalAcks() === 1);
const ackIdx = pedal.sent.findIndex((w) => w[3] === 0x00 && w[7] === 0x0c);
check("ack sent", ackIdx > 0);
const silent = await s.readPreset(199);
check("silent slot -> null", silent === null);
check("silent slot bounded", Date.now() - t0 < 1500);
const [a, b] = await Promise.all([s.readPreset(0), s.readPreset(0)]);
check("serialized reads both ok", a && b && a.length === 1128 && b.length === 1128);
const reqs = pedal.sent.filter(isPresetReq);
check("tx ids increase", reqs.every((w, i) => i === 0 || w[7] > reqs[i - 1][7]));
await s.selectSlot(3);
const sel = [...pedal.sent].reverse().find(isPresetReq);
check("select uses flag 0", (() => { const pl = reqPayload(sel); return pl[10] === 0 && pl[8] === 3; })());
check("reads use flag 1", reqs.every((w) => reqPayload(w)[10] === 1));
check("every host byte is 7-bit inside F0..F7", pedal.sent.every((w) => w[0] === 0xf0 && w[w.length - 1] === 0xf7 && w.subarray(1, -1).every((x) => x <= 0x7f)));
s.close();
check("closed session refuses", await s.readPreset(0).then(() => false, (e) => /closed/.test(e.message)));

// --- R1: tx ids are SysEx data bytes, 1..0x7F, across a wrap -----------------------
{
  const s2 = T.create({ onmidimessage: null }, { send() {} }, FAST);
  const ids = Array.from({ length: 200 }, () => s2.nextTx());
  check("200 nextTx in 1..0x7F", ids.every((v) => Number.isInteger(v) && v >= 1 && v <= 0x7f), ids.filter((v) => !(v >= 1 && v <= 0x7f)).join(","));
  check("nextTx wraps (no repeats back-to-back)", ids.every((v, i) => i === 0 || v !== ids[i - 1]));
  s2.close();
}

// --- R3: the hello reply is a frame, not an ACK -------------------------------------
{
  const p = fakePedal();
  const s3 = T.create(p.input, p.output, FAST);
  const r = await s3.request(HT.hello());
  check("hello reply lands in frames", r.frames.length === 1 && r.frames[0].family === 0x00 && r.frames[0].tx4[1] === 0x02 && r.ack === false);
  s3.close();
}

// --- an unsolicited status frame arriving before the hello reply ------------------
{
  const p = fakePedal(); const out = p.output.send.bind(p.output);
  p.output.send = (bytes) => { const w = Uint8Array.from(bytes); if (w[3] === 0x00 && w[5] === 0x01) p.deliver(MSG.ident_reply); return out(bytes); };
  const s3b = T.create(p.input, p.output, LIVE);
  check("hello survives an unsolicited frame first", await s3b.hello() === true);
  s3b.close();
}

// --- R4: the final chunk is ACKed synchronously inside the input callback -----------
{
  const sent = []; const input = { onmidimessage: null };
  let syncAck = null;
  const output = { send(bytes) {
    const w = Uint8Array.from(bytes); sent.push(w);
    if (w[3] !== 0x0f) return;
    const tx = w[7];
    setTimeout(() => {
      input.onmidimessage({ data: HT.ack(tx) });
      exportChunks.slice(0, -1).forEach((c) => input.onmidimessage({ data: c }));
      const before = sent.length;
      input.onmidimessage({ data: exportChunks[exportChunks.length - 1] });
      syncAck = sent.length === before + 1 && hex(sent[before]) === hex(HT.ack(0x0c));
    }, 2);
  } };
  const s4 = T.create(input, output, LIVE);
  const p = await s4.readPreset(0);
  check("final chunk ACK sent before the callback returns", syncAck === true);
  check("sync-fed stream assembles", p && p.length === 1128);
  s4.close();
}

// --- silent slot with no ACK at all: hello decides empty vs dead --------------------
{
  const p = fakePedal({ ackSilent: false });
  const s5 = T.create(p.input, p.output, FAST);
  const t1 = Date.now();
  check("no-ACK silent slot -> null when pedal answers hello", await s5.readPreset(199) === null);
  check("no-ACK silent slot re-sent once, then one hello probe", p.reads() === 2 && p.sent.filter(isHelloReq).length === 1 && isHelloReq(p.sent[p.sent.length - 1]));
  check("no-ACK silent slot bounded", Date.now() - t1 < 1500);
  s5.close();
}
{
  const p = fakePedal({ dead: true });
  const s6 = T.create(p.input, p.output, FAST);
  const err = await s6.readPreset(5).then(() => null, (e) => e);
  check("dead pedal -> rejects 'not responding'", err && /not responding/.test(err.message), err && err.message);
  check("dead pedal: 2 reads + 1 hello", p.sent.filter(isPresetReq).length === 2 && p.sent.filter(isHelloReq).length === 1);
  s6.close();
}

// --- (a) the first read request is lost: re-sent, preset returned ------------------
{
  const p = fakePedal({ onRead: ({ n, ack, stream }) => { if (n === 1) return; ack(); stream(); } });
  const sa = T.create(p.input, p.output, LIVE);
  const r = await sa.readPreset(0);
  check("lost first request -> preset", r && r.length === 1128);
  check("lost first request -> exactly 2 reads, no hello", p.reads() === 2 && !p.sent.some(isHelloReq));
  sa.close();
}

// --- (b) no ACK; read #1's stream only starts after its silence window -> preset ----
// Event-driven: the pedal emits read #1's (un-ACKed) stream when the re-sent read
// #2 arrives, i.e. strictly after read #1 was judged silent.
{
  const p = fakePedal({ onRead: ({ n, stream }) => { if (n === 2) stream(); } });
  const sb = T.create(p.input, p.output, LIVE);
  const r = await sb.readPreset(0);
  check("late un-ACKed stream -> preset, not null", r && r.length === 1128, String(r));
  check("late un-ACKed stream: 2 reads, no hello", p.reads() === 2 && !p.sent.some(isHelloReq));
  sb.close();
}

// --- a stream whose first (offset-0) chunk is corrupt: retry, never "empty" --------
{
  const corruptFirst = (c, i) => { if (i !== 0) return c; const w = Uint8Array.from(c); w[2] ^= 0x01; return w; };
  const p = fakePedal({ onRead: ({ n, ack, stream }) => { ack(); stream(5, n === 1 ? corruptFirst : null); } });
  const sk = T.create(p.input, p.output, LIVE);
  const r = await sk.readPreset(0).then((v) => v, (e) => e);
  check("missing first chunk on read #1 -> preset on the retry", r instanceof Uint8Array && r.length === 1128, String(r && r.message || r));
  check("missing first chunk: exactly 2 reads", p.reads() === 2, `reads=${p.reads()}`);
  check("missing first chunk: final chunk ACKed once per stream", p.finalAcks() === 2, `finalAcks=${p.finalAcks()}`);
  sk.close();
  const q = fakePedal({ onRead: ({ ack, stream }) => { ack(); stream(5, corruptFirst); } });
  const sk2 = T.create(q.input, q.output, LIVE);
  const r2 = await sk2.readPreset(0).then((v) => ({ value: v }), (e) => e);
  check("first chunk always corrupt -> rejects with a stream error (never null)", r2 instanceof Error && /first chunk/.test(r2.message), JSON.stringify(r2 && r2.message || r2));
  check("first chunk always corrupt: 2 reads", q.reads() === 2, `reads=${q.reads()}`);
  sk2.close();
}

// --- a late stream that lands during the hello probe: read again, never null --------
{
  const p = fakePedal({ onRead: ({ n, ack, stream }) => { if (n >= 3) { ack(); stream(); } }, onHello: ({ reply, stream }) => { stream(1); reply(40); } });
  const sp = T.create(p.input, p.output, LIVE);
  const r = await sp.readPreset(0);
  check("stream during the probe -> read again -> preset", r && r.length === 1128 && p.reads() === 3, `${r && r.length} reads=${p.reads()}`);
  sp.close();
}
{
  const p = fakePedal({ onRead: ({ n, ack }) => { if (n >= 3) ack(); }, onHello: ({ reply, stream }) => { stream(1); reply(40); } });
  const sp = T.create(p.input, p.output, LIVE);
  const err = await sp.readPreset(0).then((v) => ({ value: v }), (e) => e);
  check("stream during the probe, then ACK + silence -> rejects (never null)", err instanceof Error, JSON.stringify(err));
  check("at most 2 reads after the probe", p.reads() === 4 && p.sent.filter(isHelloReq).length === 1, `reads=${p.reads()}`);
  sp.close();
}

// --- (c) selectSlot with no ACK on both attempts -> NOT_RESPONDING ------------------
{
  const p = fakePedal({ onRead: () => {} }); // hello still answered
  const sc = T.create(p.input, p.output, FAST);
  const err = await sc.selectSlot(4).then(() => null, (e) => e);
  check("select without ACK -> rejects not responding", err && /not responding/.test(err.message), err && err.message);
  check("select re-sent once, no hello", p.reads() === 2 && !p.sent.some(isHelloReq));
  sc.close();
}

// --- close() while a read is in flight -> "session closed", nothing more sent ------
{
  const p = fakePedal({ dead: true });
  const sx = T.create(p.input, p.output, FAST);
  const pending = sx.readPreset(0).then(() => null, (e) => e);
  await sleep(20);
  sx.close();
  const n = p.sent.length;
  const err = await pending;
  await sleep(150);
  check("close mid-read -> rejects 'session closed'", err && /session closed/.test(err.message), err && err.message);
  check("close mid-read -> no further bytes", p.sent.length === n, `${n} -> ${p.sent.length}`);
}

// --- stream gap: one retry, then a clear error -------------------------------------
{
  const p = fakePedal({ dropChunk: 3 });
  const s7 = T.create(p.input, p.output, LIVE);
  const err = await s7.readPreset(0).then(() => null, (e) => e);
  check("gapped stream rejects", err instanceof Error, err && err.message);
  check("gapped stream retried once", p.sent.filter(isPresetReq).length === 2);
  check("gapped stream final chunk still ACKed", p.finalAcks() === 2);
  s7.close();
}

// --- stale stream for another slot: retried, then accepted with a warning ----------
{
  const p = fakePedal(); const warns = [];
  const s8 = T.create(p.input, p.output, Object.assign({}, LIVE, { log: (lvl, msg) => { if (lvl === "warn") warns.push(msg); } }));
  const r = await s8.readPreset(5); // fake always answers with the index-0 preset
  check("index mismatch retried once", p.sent.filter(isPresetReq).length === 2);
  check("index mismatch accepted after retry with a warning", r && r.length === 1128 && warns.length === 1, warns.join("|"));
  s8.close();
}

// --- unsolicited device short messages are ACKed with their tx id ------------------
{
  const p = fakePedal();
  const s9 = T.create(p.input, p.output, LIVE);
  p.deliver(MSG.ident_reply); // family 0x10, tx 1 — arrives with no request in flight
  check("unsolicited message ACKed", p.sent.length === 1 && hex(p.sent[0]) === hex(HT.ack(1)), p.sent.map(hex).join(","));
  const bad = Uint8Array.from(MSG.ident_reply); bad[2] ^= 0x01;
  p.deliver(bad); // outer CRC wrong: dropped, no ACK
  check("bad-CRC frame dropped silently", p.sent.length === 1);
  s9.close();
}

// --- webmidi_device.js routes a GP-150 port to the ht session ----------------------
{
  const PRST = require(resolve(root, "app/static/prst.js"));
  globalThis.self = globalThis; globalThis.PRST = PRST; globalThis.HtTransport = T;
  const saved = Object.assign({}, T.DEFAULTS); Object.assign(T.DEFAULTS, LIVE);
  const p150 = fakePedal({ name: "GP-150" }), p50 = fakePedal({ name: "GP-50" });
  const portMap = (...ps) => new Map(ps.map((x, i) => [String(i), x]));
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { requestMIDIAccess: async () => ({ inputs: portMap(p50.input, p150.input), outputs: portMap(p50.output, p150.output) }) } });
  require(resolve(root, "app/static/webmidi_device.js"));
  const D = globalThis.WebMidiDevice;
  const [info, info2] = await Promise.all([D.connect(), D.connect()]);
  check("concurrent connect() share one attempt", info2 && info2.key === "gp150" && D.isConnected());
  check("GP-150 port wins over GP-50", info.key === "gp150" && info.port === "GP-150", JSON.stringify(info));
  check("connect said hello", p150.sent.length === 1 && hex(p150.sent[0]) === hex(MSG.hello) && p50.sent.length === 0);
  check("ht session exposed", !!(D._ht && D._ht()));
  const bad = Uint8Array.from(MSG.ident_reply); bad[2] ^= 0x01;
  for (let i = 0; i < 3; i++) p150.deliver(bad);
  check("stats() counts corrupt frames", D.stats().corruptFrames === 3, JSON.stringify(D.stats()));
  check("readNames -> [] on ht", (await D.readNames()).length === 0);
  check("readBankBlob refuses on ht", await D.readBankBlob(0x24).then(() => false, (e) => /GP-150/.test(e.message)));
  check("readSlotOrNull silent -> null", await D.readSlotOrNull(199) === null);
  const r0 = await D.readSlotOrNull(0);
  check("readSlotOrNull filled", r0 && r0.length === 1128);
  check("readSlotPrst empty throws", await D.readSlotPrst(199).then(() => false, (e) => /empty/.test(e.message)));
  check("readActivePrst", (await D.readActivePrst()).length === 1128 && reqPayload([...p150.sent].reverse().find(isPresetReq))[8] === 0xff);
  let rangeErr = null; try { D.selectSlot(200); } catch (e) { rangeErr = e; }
  check("selectSlot range is profile.slots", rangeErr && /0\.\.199/.test(rangeErr.message));
  await D.selectSlot(150);
  check("selectSlot sends flag-0 read", (() => { const pl = reqPayload([...p150.sent].reverse().find(isPresetReq)); return pl[8] === 150 && pl[10] === 0; })());
  const seen = [];
  const sum = await D.scanSlots([0, 199], ({ slot, prst, error }) => { seen.push([slot, prst ? prst.length : null, !!error]); });
  check("scanSlots", JSON.stringify(seen) === JSON.stringify([[0, 1128, false], [199, null, false]]) && sum.read === 1 && sum.empty === 1 && sum.errors === 0, JSON.stringify([seen, sum]));
  check("_sendStream refuses on ht", (() => { try { D._sendStream([], { confirm: true, validated: true }); return false; } catch (e) { return /GP-150/.test(e.message); } })());
  D.disconnect();
  check("disconnect closes the session", !D.isConnected() && D._ht() === null);
  // a pedal that never answers the handshake must not leave a half-open connection
  T.DEFAULTS.timeoutMs = FAST.timeoutMs; // the mute pedal's hello should time out fast
  const mute = fakePedal({ dead: true, name: "GP-150" });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { requestMIDIAccess: async () => ({ inputs: portMap(mute.input), outputs: portMap(mute.output) }) } });
  check("silent handshake -> connect rejects", await D.connect().then(() => false, (e) => /handshake/.test(e.message)));
  check("silent handshake -> not connected", !D.isConnected());
  Object.assign(T.DEFAULTS, saved);
}

console.log(`ht_transport: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
