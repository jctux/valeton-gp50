# Valeton GP-50 Editor

A browser-based editor for the **Valeton GP-50** (and GP-5), built by reverse-engineering
the pedal's MIDI SysEx protocol from scratch. It reads and writes the device live over
**WebMIDI** — no vendor SDK, no drivers, no backend.

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
build/make-template) is reverse-engineered and working over WebMIDI. The app only
talks to the physical pedal when you explicitly connect and scan/write via the
Explorer or Captures & IRs pages.
