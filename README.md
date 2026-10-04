# Valeton GP-50 Editor

A browser-based editor for the **Valeton GP-50** (and GP-5), built by reverse-engineering
the pedal's MIDI SysEx protocol from scratch. It reads and writes the device live over
**WebMIDI** — no vendor SDK, no drivers, no backend. The **GP-150** is supported too,
over USB, minus a few features ([GP-150](#gp-150)).

**Live demo:** [valeton-gp50-woad.vercel.app](https://valeton-gp50-woad.vercel.app) —
zero-setup, runs entirely in-browser. Chrome or Edge, pedal on USB.

**What it does:**

- **Preset Explorer** — reads every preset off the connected pedal; full signal chain
  per preset, block chips color-coded by type.
- **Live editing** — edit a preset's blocks, models, and parameters in the browser;
  changes mirror straight to the pedal over WebMIDI.
- **Model picker** — swap any block's model using Valeton's official hardware names,
  not just internal IDs.
- **Preset rename** — edit a preset's name and write it back to the device.
- **Preset & block reordering** — drag to rearrange presets in a bank, or blocks
  within a preset's signal chain; each reorder is one batched, minimal write.
- **Clear Preset** — wipe a slot back to a factory-blank preset.
- **Captures & IRs browser** — every SnapTone capture and User IR on the device,
  plus reusable templates.
- **Capture usage lookup** — pick a SnapTone or IR and see exactly which presets
  reference it.
- **Build a patch from a capture** — wrap a template's effects chain around a
  SnapTone/IR and write the result to a slot.
- **Make a template from a preset** — save any preset's effects chain as a reusable
  template.
- **Preset Converter** — convert `.prst` preset files between the GP-5 and GP-50
  formats, entirely client-side.

Everything above runs client-side. The live demo is the whole app; the local FastAPI
server ([Setup](#setup)) is only for development and for the legacy in-repo NAM
converter.

## GP-150

The Explorer also reads and writes a **Valeton GP-150** over USB. The GP-150 doesn't
speak the GP-5/GP-50 protocol, so it has its own preset codec (`app/static/prst150.js`,
`patch/prst150_format.py`) and its own MIDI transport (`app/static/ht_proto.js` +
`ht_transport.js`, `patch/ht_proto.py`). The rest of the app picks them up through the
device profile.

**Supported.** Everything in this list was run on a real GP-150 on 2026-10-04 unless it
says otherwise ([`re/gp150/DEVICE_WRITE.md`](re/gp150/DEVICE_WRITE.md)). Two items were
covered only in part: block-knob parameter edits went to the pedal only as the default
values written by a Sweet Echo model pick, and the preset select is sent by the Explorer
only while the pedal is connected (the request itself was checked from Python).

- **Scan and browse all 200 preset slots**: name, the **12-block** chain (NR, PRE, WAH,
  DST, N→S, AMP, CAB, EQ, MOD, DLY, RVB, VOL) in its stored order, models and
  parameters. A scan reads the slots one by one: about 70 s for 200 presets.
- **Back up / export**: **⬇ Download edited .prst** with no edits saves a preset as its
  1128-byte `.prst`, byte-for-byte what the pedal sent. `patch/ht_scan.py scan` saves
  all 200 slots to `device_scan_gp150/` ([`re/gp150/DEVICE_READ.md`](re/gp150/DEVICE_READ.md)).
- **Rename** (up to 13 characters), **parameter edits** (block knobs, Patch VOL) and
  **bypass** (block on/off), in the Explorer's Live edit. Patch VOL was changed on the
  pedal; block knobs only through the defaults of the Sweet Echo pick.
- **Block order**: drag blocks in the chain strip. **AMP stays first and VOL last**, and
  the codec refuses any other order. A reorder rewrites only the preset's order table,
  which is what the pedal does when you reorder on the pedal: the block records and
  their engine bytes never move. The reorder bytes were checked on the pedal (written
  from Python); the drag itself was only tested in automated Chromium.
- **Model change** (verified for DLY on hardware; other slots allowed only when the
  engine byte is unambiguous in the corpus, otherwise the picker greys the model out):
  pick another model for a block by its GP-150 name. Picking "None" also turns the block
  off. The names come from a prebuilt catalog
  (`patch/fxid_ring_gp150.json`). `patch/build_ring.py gp150` regenerates it from the
  public format notes plus a local Valeton Suite install; Valeton's `module150_data.json`
  itself is never committed. A type code that isn't mapped yet shows as `Type <n>`.
- **Clear Preset**: resets a slot to the factory "New GEN." blank.
- **Import a `.prst` to a slot**: every write sends the whole 1128-byte preset with the
  import stream Valeton Suite uses for a `.prst` file, about 1 s per preset. Copy/Paste,
  Swap and preset reorder in the Explorer send the same stream, but they weren't each
  run on the pedal. To write a file from disk (needs `.venv-midi`, see
  [`re/gp150/DEVICE_READ.md`](re/gp150/DEVICE_READ.md)), run this from the repo root:

  ```python
  from patch import device_write as dw, ht_scan
  prst = open("my-preset.prst", "rb").read()
  slot = 42  # internal slot 0..199; the pedal shows it as preset 043
  with ht_scan.Session() as s:
      assert s.hello(), "no handshake: close Valeton Suite"
      pk = dw.build_gp150_write_stream(prst, slot)
      ok, why = dw.validate_gp150_stream(pk, slot=slot)
      dw.send_stream(None, pk, confirm=True, validated=ok, session=s)
  ```
- **Select a preset**: with the pedal connected, clicking a preset row switches the pedal
  to that preset. The select request was checked on the pedal from Python.

**Not supported:**

- **SnapTone / IR upload, and SnapTone / IR names**: the request that reads the
  GP-150's capture and IR catalog hasn't been decoded, so the Captures & IRs features
  (names, usage lookup, build from a capture) are GP-5/GP-50 only.
- **Conversion**: the Preset Converter refuses GP-150 files (different effect catalog).
  GP-5 ↔ GP-50 conversion is unchanged.
- **Bluetooth**: USB only.
- **Model picks with no known engine byte**: each block carries an engine byte that
  depends on the effect, and a wrong one silences the preset. The editor picks a model
  only when `patch/gp150_engines.json` shows one engine for it (or one engine for the
  whole slot: NR, EQ, DLY, RVB, VOL); anything else is greyed out in the picker. That is
  every WAH (no preset read so far has a real wah), most AMP models, and a few PRE, DST,
  N→S, CAB and MOD ones. The committed table was learned from the maintainer's full
  200-slot scan plus `re/gp150/evidence/` (203 distinct files); the scan isn't in the
  repo, so `python3 scripts/gp150_engines.py --check` against the evidence alone reports
  a difference. To teach a model: set it on your pedal and save, rescan your own pedal
  (`patch/ht_scan.py scan`), run `python3 scripts/gp150_engines.py` (it rewrites
  `patch/gp150_engines.json` from your scan + the evidence), and copy the new table into
  `ENGINES` in `app/static/prst150.js` (`app/tests/test_prst150_js.mjs` fails until the
  two match).

**Known quirks:**

- **After writing the active preset, re-select it on the pedal** (turn the preset knob
  away and back). The pedal stores the write at once, but keeps playing the old version
  until it reloads the preset, so a Live edit isn't heard until you re-select.
- **The pedal's chain screen draws AMP in a fixed middle position**, whatever the
  stored order. The Explorer shows the stored order, with AMP first.
- **A power-cycled pedal streams nothing until the host opens a session**: it
  acknowledges preset reads and then stays silent. The app and the CLI send Valeton
  Suite's session-open message after the handshake, so you don't need to do anything.
- **Chain-strip drag**: in one Chrome install the chips didn't start dragging on a quick
  click-and-move (automated Chromium drags fine). Press and hold a chip for a moment,
  then move it.
- **BPM above 255**: only the low byte of the tempo (`0x24`) is decoded, and the
  Explorer's BPM slider goes to 300. Keep a GP-150 preset's BPM at 255 or below.
- **Numbering**: the Explorer and the scripts number slots 0–199. The pedal and Valeton
  Suite number presets from 001, so slot 199 is preset 200 on the pedal.

**Using it:** Chrome or Edge, GP-150 on **USB**. **Close Valeton Suite if the handshake
fails**: it holds the MIDI port. If it still fails, unplug and replug the USB cable. The
GP-150 path runs in the backend-free static mode, so use the static build
(`node scripts/build_static_site.mjs`, then serve `dist/`; the GP-150 catalog is already
in `app/static/data/`) or, on the local server, open
`http://127.0.0.1:8756/explorer?static=1`. The FastAPI backend's device routes are
GP-5/GP-50 only. Then click **⟳ Scan device** (or **⟳ Rescan device** when a preset
list is already shown). The Chrome 152 known issue under [Run](#run) is a Web MIDI
SysEx bug, so expect it to hit the GP-150 too.

**Hardware verification** (one GP-150, USB PID 0x0186, firmware version not recorded;
macOS, Valeton Suite closed):

| Date | What ran on the pedal | Result |
|---|---|---|
| 2026-10-03 | Throwaway probes: handshake, preset read, chunk stream, the immediate ACK of the final chunk | Three presets read off the pedal; reads don't change the active preset ([`re/gp150/probes/`](re/gp150/probes/), [`re/gp150/evidence/`](re/gp150/evidence/)) |
| 2026-10-04 | `patch/ht_scan.py scan` (after adding the session open) | 200/200 presets in 70 s; byte `0x04` == slot for 200/200 ([`DEVICE_READ.md`](re/gp150/DEVICE_READ.md)) |
| 2026-10-04 | `patch/ht_write_verify.py 199 --placeholder` | 7/7 steps PASS: import ACKed and confirmed by the pedal's 0x08 "import done", read-back identical outside the device-owned bytes, original restored, active preset untouched |
| 2026-10-04 | Edit checklist on slots 199 and 185 | All PASS. In the Explorer: rename, Patch VOL, DLY model change + on (echo audible), Clear on slot 185 (== factory blank). From Python with the codec's bytes: block reorder (echo audible; the Explorer then showed the stored order) and select. Both slots restored byte-identical from the scan backup |

The checklist found one bug, now fixed: the first codec moved block records on a
reorder, which silenced the preset. A reorder done on the pedal itself showed that only
the order table changes, so the codec now does the same.

**How it was reverse-engineered, and credits:** the GP-150 ignores the GP-5/GP-50
requests and the Universal Device Inquiry. It speaks the GP-180's "HT" SysEx protocol.

- [majabojarska/Valeton-GP180-Rev-Eng](https://github.com/majabojarska/Valeton-GP180-Rev-Eng)
  (GPL-3) captured Valeton Suite talking to a GP-180. The framing, both CRCs, the
  preset-read and import requests, the chunked stream and the session open were worked
  out from those captures and its SysEx corpus. Nothing from it is vendored, except 35
  captured messages used as byte-exact test vectors
  (`app/tests/fixtures/gp150/ht_corpus.json`, each with its capture file and frame).
  The tests read its 200-preset dump only from a separate checkout (`GP180_DUMP_DIR`).
- AlbertoBarba's public
  [GP150_PRST_FORMAT.md](https://gist.github.com/AlbertoBarba/ec59feecba60ca956eeb6970f0ac0055)
  documents the 1128-byte preset layout and the effect type tables. The pedal showed two
  corrections: block records sit at fixed indexes and only the order table moves, and
  the engine byte belongs to the effect, not to its chain position.

Details: [`re/gp150/DEVICE_READ.md`](re/gp150/DEVICE_READ.md) (wire facts, read rules,
CLI), [`re/gp150/DEVICE_WRITE.md`](re/gp150/DEVICE_WRITE.md) (write verification and
the Explorer checklist) and the
[design spec](docs/superpowers/specs/2026-10-03-gp150-support-design.md).

## Maintenance status: no test hardware

I sold my GP-50 in September 2026 and no longer own a Valeton pedal. The app was
developed and tested against a real GP-50 and still works, but I can't reproduce
device-side bugs myself anymore, so a fix depends on what you send. Changes to the device
read/write path are now checked against the test suite only, not a live pedal.

If you open an issue about talking to the pedal, please include:

- Your browser and its exact version (from `chrome://version`), plus your OS
- Pedal model (GP-50, GP-5 or GP-150) and firmware version
- What you did, what you expected, and what happened (screenshots help)
- The browser console output (DevTools → Console) from the failing action
- If you can, a raw MIDI capture of the failure, e.g. with
  [MIDI Monitor](https://www.snoize.com/midimonitor/) on macOS. This is the single most
  useful thing you can attach.
- **GP-150 owners:** the output of `./.venv-midi/bin/python patch/ht_scan.py read active`
  (with Valeton Suite closed; setup in [`re/gp150/DEVICE_READ.md`](re/gp150/DEVICE_READ.md)),
  plus the `.prst` file it saves under `device_scan_gp150/`.

## Getting NAM captures onto the pedal

**That moved to its own project:
[nam-a2a1-converter](https://github.com/drewmerc302/nam-a2a1-converter)** — a standalone
desktop app (Windows / macOS / Linux, optional NVIDIA acceleration).
**[Download a release →](https://github.com/drewmerc302/nam-a2a1-converter/releases)**

The GP-50 only accepts NAM **A1**, and there's no A2→A1 format downgrade — they're
different neural architectures, so the weights don't transfer. The converter **distills**
instead: render a DI through the A2 model, then train an A1 to reproduce that output.
It's device-agnostic, so it serves any A1-only device or plugin, not just the GP-50.

This repo still carries the engine it grew out of (`a2a1/`, wired into the local app),
but the converter you actually want is the standalone one.

## Screenshots

|  |  |
| --- | --- |
| ![Preset Explorer — full preset list with block chips](docs/screenshots/01-preset-explorer.png) **Preset Explorer** — every preset on the pedal, block chips color-coded by type. | ![Preset detail — signal chain, per-block params, live edit](docs/screenshots/02-preset-detail.png) **Preset detail** — full signal chain, per-block params, live edit straight to the pedal. |
| ![Model picker — official hardware names for a block](docs/screenshots/03-model-picker.png) **Model picker** — swap a block's model, official hardware names included. | ![Captures & IRs — templates and SnapTone captures](docs/screenshots/04-captures-and-irs.png) **Captures & IRs** — saved templates plus every SnapTone capture on the device. |
| ![SnapTone usage — which patches reference a capture](docs/screenshots/05-snaptone-usage.png) **Capture usage** — see exactly which patches reference a SnapTone. | ![Build a patch from a capture](docs/screenshots/06-build-patch.png) **Build a patch** — wrap a template around a SnapTone and write it to a slot. |
| ![Make a template from a preset](docs/screenshots/07-make-template.png) **Make a template** — save any preset's effects chain as a reusable wrapper. | ![Preset Converter — GP-5 to GP-50 conversion](docs/screenshots/08-preset-converter.png) **Preset Converter** — convert `.prst` presets between the GP-5 and GP-50. |

## Setup

You don't need any of this to use the app — open the
[live demo](https://valeton-gp50-woad.vercel.app). Local setup is for development.

```bash
cd /Users/drewmerc/workspace/valeton

# web app venv
python3 -m venv .venv-app && ./.venv-app/bin/python -m pip install fastapi "uvicorn[standard]" python-multipart pytest httpx
```

<details>
<summary>Optional: the in-repo NAM engine (superseded by <a href="https://github.com/drewmerc302/nam-a2a1-converter">nam-a2a1-converter</a>)</summary>

```bash
# engine venv (A2 render + train + 0.7.0 export; 0.5.x via in-process transcode)
python3 -m venv .venv && ./.venv/bin/python -m pip install -r a2a1/requirements-a2.txt   # NAM 0.13.0
```

One venv, not two — see [`a2a1/README.md`](a2a1/README.md) for why the old second one
is gone. The default DI is `refs/v3_0_0.wav` (official NAM input). Get it via the
trainer, or generate a synthetic fallback:
`./.venv/bin/python a2a1/make_di.py refs/v3_0_0.wav`.
</details>

## Run

```bash
./run.sh
```

Then open **http://127.0.0.1:8756**.

- **Preset Explorer / Captures & IRs:** connect the pedal over WebMIDI (Chrome/Edge,
  HTTPS or localhost), scan, and browse/edit real device data live — see the
  itemized feature list above.
  - **Known issue: Chrome 152 cannot talk to the pedal.** Chrome 152 has a Web MIDI
    regression that corrupts SysEx framing (the pedal receives `F0 F0 … F7 F7` and
    ignores it; replies are dropped). The pedal shows as connected but every scan
    fails with "no reply". Use Chrome 153 or newer, or Chrome Beta/Canary. Not a
    firmware or cable problem; see [issue #3](https://github.com/drewmerc302/valeton-gp50/issues/3).
- **Preset Converter:** drop in a `.prst` and convert between GP-5 and GP-50 formats.
- **NAM converter:** present in the local build only, and only with the engine venv
  above. The hosted build links to the standalone converter instead.

## Tests

```bash
./.venv-app/bin/python -m pytest app/tests -q -m "not slow"   # fast unit/API/frontend suite
./.venv-app/bin/python -m pytest app/tests/test_e2e.py -q -s  # slow: real headless browser conversion + screenshots
```

## Layout

- `app/` — the FastAPI web app + the WebMIDI frontend (device I/O, decoders, editor UI).
- `a2a1/` — GP-50 MIDI RE tooling, plus the original A2→A1 engine
  ([README](a2a1/README.md)); the shipped converter now lives in
  [nam-a2a1-converter](https://github.com/drewmerc302/nam-a2a1-converter).
- `docs/`, `re/`, `design/` — protocol notes, RE captures, and format research.
- `refs/` — sample models + the DI input.
- `MVP_REQUIREMENTS.md`, `AUTONOMY.md`, `STATUS.md` — the MVP spec, the build-loop
  protocol, and live build status.

## License

[MIT](LICENSE)

## Scope

Live device read/write (Explorer, live edit, reorder, rename, clear, capture usage,
build/make-template) is reverse-engineered and working over WebMIDI. The GP-150 has
scan, backup, edits, block order, clear and preset writes, but no SnapTone/IR features
([GP-150](#gp-150)). The app only
talks to the physical pedal when you explicitly connect and scan/write via the
Explorer or Captures & IRs pages.
