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
let connectedKey = null; const reads = []; let readImpl = null;
globalThis.DeviceBridge = { webmidiAvailable: () => true, connected: () => !!connectedKey, device: () => ({ key: connectedKey, name: connectedKey === "gp150" ? "GP-150" : "GP-50" }), connect: async () => {}, readNames: async () => [], readSlotOrNull: async (slot) => { reads.push(slot); if (readImpl) return readImpl(slot); return slot === 199 ? null : b; }, readSlotPrst: async () => b, selectSlot: async () => {} };
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

// a pedal that stops answering: the scan gives up after 3 straight failures
connectedKey = "gp150"; await get("/api/device/status");
readImpl = async () => { throw new Error("pedal not responding"); };
await api.handle("POST", "/api/device/scan", {});
const sc3 = await waitScan();
check("3 straight read failures abort the scan", sc3.errors === 3 && sc3.done === 3 && /not responding/.test(sc3.error || ""), JSON.stringify(sc3));
readImpl = null;

console.log(`static_api gp150: ${pass} passed, ${fail} failed`); for (const f of fails) console.log("  FAIL " + f); process.exit(fail ? 1 : 0);
