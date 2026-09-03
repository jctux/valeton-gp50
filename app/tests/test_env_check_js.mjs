/*
 * env_check.js detector tests: which browsers get flagged for the Chromium 152
 * Web MIDI SysEx regression (issue #3), and which don't.
 *
 *   node app/tests/test_env_check_js.mjs
 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
// The IIFE binds to `this` (module.exports) under CommonJS and skips the DOM part.
const { EnvCheck } = require(resolve(here, "../static/env_check.js"));

let pass = 0, fail = 0;
const fails = [];
const check = (label, ok, detail) => { if (ok) { pass++; return; } fail++; fails.push(`${label}${detail ? " — " + detail : ""}`); };

const uad = (v, extra = []) => ({ userAgentData: { brands: [{ brand: "Not?A_Brand", version: "24" }, { brand: "Chromium", version: v }, ...extra] } });
const ua = (s) => ({ userAgent: s });

// chromiumMajor: userAgentData first, UA string fallback, null off-Chromium.
check("uad 152", EnvCheck.chromiumMajor(uad("152")) === 152);
check("uad 153", EnvCheck.chromiumMajor(uad("153")) === 153);
check("uad edge brand", EnvCheck.chromiumMajor(uad("152", [{ brand: "Microsoft Edge", version: "152" }])) === 152);
check("ua chrome", EnvCheck.chromiumMajor(ua("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")) === 152);
check("ua edge", EnvCheck.chromiumMajor(ua("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")) === 151);
check("ua firefox", EnvCheck.chromiumMajor(ua("Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:128.0) Gecko/20100101 Firefox/128.0")) === null);
check("ua safari", EnvCheck.chromiumMajor(ua("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15")) === null);
check("empty nav", EnvCheck.chromiumMajor({}) === null);
check("null nav falls back to global navigator (Node: not Chromium)", EnvCheck.chromiumMajor(null) === null);
check("bad version", EnvCheck.chromiumMajor(uad("abc")) === null);

// sysexBrokenMessage: only the listed majors, message names the fix.
const msg152 = EnvCheck.sysexBrokenMessage(uad("152"));
check("152 flagged", typeof msg152 === "string" && msg152.length > 0);
check("152 names Chrome 152", /Chrome 152/.test(msg152 || ""), msg152);
check("152 points at 153", /153/.test(msg152 || ""), msg152);
check("151 fine", EnvCheck.sysexBrokenMessage(uad("151")) === null);
check("153 fine", EnvCheck.sysexBrokenMessage(uad("153")) === null);
check("firefox fine", EnvCheck.sysexBrokenMessage(ua("Firefox/128.0")) === null);
check("ua-string 152 flagged", EnvCheck.sysexBrokenMessage(ua("Chrome/152.0.7977.75")) !== null);
check("list is 152 only", JSON.stringify(EnvCheck.BROKEN_SYSEX_MAJORS) === "[152]");

console.log(`${pass} passed, ${fail} failed`);
for (const f of fails) console.log("  FAIL " + f);
process.exit(fail ? 1 : 0);
