/* static_api.js profile switching + 200-slot scan for the GP-150, with a fake
 * DeviceBridge. node app/tests/test_static_api_gp150_js.mjs */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url)); const root = resolve(here, "../..");
const read = (p) => readFileSync(resolve(root, p), "utf8");
const b = new Uint8Array(readFileSync(resolve(root, "re/gp150/evidence/000-New_GEN.prst")));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// minimal browser globals
const store = {}; globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } };
globalThis.location = { search: "?static=1" }; globalThis.document = { currentScript: null };
globalThis.addEventListener = () => {}; globalThis.Response = class { constructor(body, init) { this.body = body; this.status = (init && init.status) || 200; } async json() { return JSON.parse(this.body); } };
globalThis.atob = (s) => Buffer.from(s, "base64").toString("binary"); globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
globalThis.self = globalThis; globalThis.__VALETON_STATIC__ = true;
const data = { "presets.json": read("app/static/data/presets.json"), "fxid_ring.json": read("patch/fxid_ring.json"), "fxid_ring_gp150.json": read("patch/fxid_ring_gp150.json"), "bank_map.json": JSON.stringify({ snaptone: {}, ir: {} }) };
globalThis.fetch = async (url) => { const f = String(url).split("/").pop(); return { ok: f in data, json: async () => JSON.parse(data[f]) }; };
globalThis.PRST = require(resolve(root, "app/static/prst.js")); globalThis.PRST150 = require(resolve(root, "app/static/prst150.js"));
globalThis.PatchLib = require(resolve(root, "app/static/patchlib.js"));
let connectedKey = null; const reads = []; let readImpl = null; let corrupt = 0; const writes = [];
globalThis.DeviceBridge = { webmidiAvailable: () => true, connected: () => !!connectedKey, device: () => ({ key: connectedKey, name: connectedKey === "gp150" ? "GP-150" : "GP-50" }), connect: async () => {}, readNames: async () => [], readSlotOrNull: async (slot) => { reads.push(slot); if (readImpl) return readImpl(slot); return slot === 199 ? null : b; }, readSlotPrst: async () => b, selectSlot: async () => {}, stats: () => ({ corruptFrames: corrupt }),
  writeSlot: async (slot, prst) => { writes.push({ slot, prst: Uint8Array.from(prst) }); return { sent: 10, acks: 1, notified: true }; } };
const apiPath = resolve(root, "app/static/static_api.js");
require(apiPath);
let api = globalThis.__staticApi;
let pass = 0, fail = 0; const fails = []; const check = (l, ok, d) => { if (ok) pass++; else { fail++; fails.push(`${l}${d ? " — " + d : ""}`); } };
const get = async (p) => (await api.handle("GET", p, {})).json();
const waitScan = async () => { for (let i = 0; i < 2000 && (await get("/api/device/scan/status")).running; i++) await sleep(5); return get("/api/device/scan/status"); };
const lengths = () => new Set(Object.values(api.getAllSlotBytes()).map((u) => u.length));

await api.ensureLoaded();
let inv = await get("/api/device/inventory");
check("starts as gp50 bundle", inv.device.key === "gp50" && inv.domains.patch_slots[1] === 99 && inv.device.slots === 100);
// a GP-5 (or GP-50) keeps the upstream behaviour: the bundle's gp50 store, 100 slots
connectedKey = "gp5";
check("gp5 status reports the pedal", (await get("/api/device/status")).device.key === "gp5");
inv = await get("/api/device/inventory");
check("gp5 does not switch the store", inv.device.key === "gp50" && inv.domains.patch_slots[0] === 0 && inv.domains.patch_slots[1] === 99 && inv.patches.length === 100);
await api.switchProfile("gp5");
check("switchProfile(gp5) from gp50 is a no-op", (await get("/api/device/inventory")).device.key === "gp50");
connectedKey = "gp150";
const st = await get("/api/device/status");
check("status switched profile", st.device.key === "gp150");
inv = await get("/api/device/inventory");
check("switch_profile_resets_store", inv.device.key === "gp150" && inv.patches.length === 0 && inv.domains.patch_slots[1] === 199);
check("device slots from profile", inv.device.slots === 200);
await api.handle("POST", "/api/device/scan", {});
const sc = await waitScan();
check("scan_marks_silent_slot_empty", sc.total === 200 && sc.done === 200 && sc.empty === 1 && sc.errors === 0 && sc.written === 199, JSON.stringify(sc));
check("scan read every slot once, in order", reads.length === 200 && reads.every((s, i) => s === i));
inv = await get("/api/device/inventory");
check("200 patches incl. blank for 199", inv.patches.length === 200 && inv.patches.find((p) => p.slot === 199).empty === true);
check("filled slots named from the bytes", inv.patches.find((p) => p.slot === 0).name === "New GEN." && inv.patches.find((p) => p.slot === 0).empty === false);
check("full cache", api.hasFullScanCache() === true);
check("only GP-150 bytes in the gp150 store", [...lengths()].join() === "1128");

// a GP-5 after the GP-150: back to the upstream bundle store (gp50), not a gp5 store
connectedKey = "gp5";
await get("/api/device/status");
inv = await get("/api/device/inventory");
check("gp5 after gp150 -> bundle gp50 store", inv.device.key === "gp50" && inv.domains.patch_slots[1] === 99 && [...lengths()].join() === "552");
connectedKey = "gp150";
await get("/api/device/status");
check("gp150 again after gp5", (await get("/api/device/inventory")).device.key === "gp150");

// back to a GP-50: the GP-150 reads must not leak into the GP-50 inventory
connectedKey = "gp50";
check("status switches back", (await get("/api/device/status")).device.key === "gp50");
inv = await get("/api/device/inventory");
check("gp50 store is the bundle again", inv.device.key === "gp50" && inv.patches.length === 100 && [...lengths()].join() === "552");
check("gp150 cache is not a gp50 full cache", api.hasFullScanCache() === false);

// and back to the GP-150: the scan cache is restored without rescanning
connectedKey = "gp150";
await get("/api/device/status");
inv = await get("/api/device/inventory");
check("gp150 cache restored on reconnect", inv.device.key === "gp150" && inv.patches.length === 200 && inv.patches.find((p) => p.slot === 199).empty === true);

// a reload with a gp50-filed cache stays on the bundle profile (upstream behaviour)
{
  const saved = store["valeton_scanCache_v2"];
  store["valeton_scanCache_v2"] = JSON.stringify({ profileKey: "gp50", slots: {} });
  delete require.cache[apiPath]; connectedKey = null;
  require(apiPath); api = globalThis.__staticApi;
  await api.ensureLoaded();
  check("gp50 cache boots the bundle profile", (await get("/api/device/inventory")).device.key === "gp50");
  store["valeton_scanCache_v2"] = saved;
}

// a reload (fresh static_api) boots straight into the cached GP-150 profile
delete require.cache[apiPath]; connectedKey = null;
require(apiPath); api = globalThis.__staticApi;
await api.ensureLoaded();
inv = await get("/api/device/inventory");
check("reload boots into the cached profile", inv.device.key === "gp150" && inv.patches.length === 200 && api.hasFullScanCache() === true);

// the pedal changes mid-scan: the scan stops, nothing lands in the new store
connectedKey = "gp150"; await get("/api/device/status");
readImpl = async (slot) => { await sleep(1); return b; };
await api.handle("POST", "/api/device/scan", {});
while ((await get("/api/device/scan/status")).done < 5) await sleep(1);
connectedKey = "gp50"; await get("/api/device/status");
const sc2 = await waitScan();
check("device change aborts the scan", !sc2.running && /changed/.test(sc2.error || "") && sc2.done < 200, JSON.stringify(sc2));
check("no GP-150 bytes in the gp50 store after the abort", [...lengths()].join() === "552");

// corrupt frames surface in /api/device/status from the 3rd on (absent before)
{
  connectedKey = "gp150";
  corrupt = 2;
  const s2 = await get("/api/device/status");
  check("corrupt_frames absent below 3", !("corrupt_frames" in s2));
  corrupt = 3;
  check("corrupt_frames surfaced at 3", (await get("/api/device/status")).corrupt_frames === 3);
  corrupt = 0;
  connectedKey = "gp50";
  check("gp50 status JSON unchanged", JSON.stringify(Object.keys(await get("/api/device/status"))) === JSON.stringify(["connected", "device", "port"]));
}

// a pedal that stops answering: the scan gives up after 3 straight failures
connectedKey = "gp150"; await get("/api/device/status");
readImpl = async () => { throw new Error("pedal not responding"); };
await api.handle("POST", "/api/device/scan", {});
const sc3 = await waitScan();
check("3 straight read failures abort the scan", sc3.errors === 3 && sc3.done === 3 && /not responding/.test(sc3.error || ""), JSON.stringify(sc3));
readImpl = null;

// --- the cache holds the bytes a write ACTUALLY leaves in the target slot -----------------
// A GP-150 preset carries its slot in index byte 0x04, which the import sets on a copy
// (webmidi_write buildGp150WriteStream): the cache must hold that copy, never the
// source slot's index. GP-5/GP-50 files carry no slot: cached as-is, as before.
{
  const hex = (u) => Buffer.from(u).toString("hex");
  const C = globalThis.PRST150;
  const withIndex = (u, i) => { const z = Uint8Array.from(u); z[4] = i; return z; };
  const lsSlot = (slot) => { const c = JSON.parse(store["valeton_scanCache_v2"]); return c.slots[slot] ? new Uint8Array(Buffer.from(c.slots[slot].b64, "base64")) : null; };
  connectedKey = "gp150"; await get("/api/device/status");
  check("cache-sent: on the gp150 store", (await get("/api/device/inventory")).device.key === "gp150");
  const all = () => api.getAllSlotBytes();
  const src5 = all()[5];
  check("cache-sent: fixture slot 5 carries index 0 (the fake pedal returns slot 0's bytes)", src5[4] === 0);

  // write (no edits): slot 5 -> 12
  writes.length = 0;
  let r = await (await api.handle("POST", "/api/device/write", { patch_slot: 5, target_slot: 12, confirm: true })).json();
  check("write: ok", r.ok === true, JSON.stringify(r));
  check("write: the bridge got index 12", writes.length === 1 && writes[0].slot === 12 && writes[0].prst[4] === 12);
  check("write: cached slot 12 == the bytes sent", hex(all()[12]) === hex(writes[0].prst) && hex(all()[12]) === hex(withIndex(src5, 12)));
  check("write: persisted with index 12", lsSlot(12) && hex(lsSlot(12)) === hex(withIndex(src5, 12)));
  check("write: the source slot is untouched", hex(all()[5]) === hex(src5));

  // write with edits: rename + param, slot 5 -> 13
  writes.length = 0;
  r = await (await api.handle("POST", "/api/device/write", { patch_slot: 5, target_slot: 13, confirm: true, name: "Edited", params: { 5: { 0: 33 } } })).json();
  const want13 = withIndex(C.applyEdits(src5, { name: "Edited", params: { 5: { 0: 33 } } }), 13);
  check("write+edits: ok, verified name", r.ok === true && r.verified_name === "Edited", JSON.stringify(r));
  check("write+edits: sent == cached == applyEdits + index 13", writes.length === 1 && hex(writes[0].prst) === hex(want13) && hex(all()[13]) === hex(want13));
  check("write+edits: inventory shows the new name at 13", (await get("/api/device/inventory")).patches.find((p) => p.slot === 13).name === "Edited");

  // swap 12 <-> 199 (199 is the scan's nameless blank)
  const a12 = all()[12], z199 = all()[199];
  writes.length = 0;
  r = await (await api.handle("POST", "/api/device/swap", { slot_a: 12, slot_b: 199, confirm: true })).json();
  check("swap: ok", r.ok === true, JSON.stringify(r));
  check("swap: sent index 199 then 12", writes.length === 2 && writes[0].slot === 199 && writes[0].prst[4] === 199 && writes[1].slot === 12 && writes[1].prst[4] === 12);
  check("swap: cached 199 == sent (slot 12's preset, index 199)", hex(all()[199]) === hex(writes[0].prst) && hex(all()[199]) === hex(withIndex(a12, 199)));
  check("swap: cached 12 == sent (the blank, index 12)", hex(all()[12]) === hex(writes[1].prst) && hex(all()[12]) === hex(withIndex(z199, 12)));
  const inv2 = await get("/api/device/inventory");
  check("swap: names follow the presets", inv2.patches.find((p) => p.slot === 12).empty === true && inv2.patches.find((p) => p.slot === 199).name === "New GEN.");

  // setSlotBytes (Explorer: reorder commit, live keep/restore, clear) files the slot's index
  api.setSlotBytes(42, src5);
  check("setSlotBytes: index normalized to the slot", all()[42][4] === 42 && hex(all()[42]) === hex(withIndex(src5, 42)) && hex(lsSlot(42)) === hex(withIndex(src5, 42)));

  // build: a GP-150 preset has no N->S SnapTone record to repoint -> refused, nothing sent
  writes.length = 0;
  await api.handle("POST", "/api/device/templates/from-patch", { name: "T", source_slot: 5 });
  const tpl = (await get("/api/device/templates")).templates.find((t) => t.name === "T");
  const rb = await api.handle("POST", "/api/device/build", { template_id: tpl.id, snaptone_slot: 50, target_slot: 14, confirm: true });
  check("build gp150: refused before any write", rb.status === 400 && writes.length === 0, `${rb.status} ${writes.length}`);

  // GP-50: the bytes are cached exactly as sent (no index byte), as before
  connectedKey = "gp50"; await get("/api/device/status");
  check("gp50: back on the bundle store", (await get("/api/device/inventory")).device.key === "gp50");
  const g1 = all()[1];
  writes.length = 0;
  r = await (await api.handle("POST", "/api/device/write", { patch_slot: 1, target_slot: 7, confirm: true })).json();
  check("gp50 write: source bytes sent and cached unchanged", r.ok && writes.length === 1 && hex(writes[0].prst) === hex(g1) && hex(all()[7]) === hex(g1));
  api.setSlotBytes(8, g1);
  check("gp50 setSlotBytes: bytes unchanged", hex(all()[8]) === hex(g1));
  connectedKey = "gp150"; await get("/api/device/status");
}

console.log(`static_api gp150: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
