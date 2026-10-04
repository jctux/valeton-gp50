# GP-150 device WRITE verification — status

Date: 2026-10-03. Branch `gp150-support`.

| What | Status |
|---|---|
| Import stream (`device_write.build_gp150_write_stream` / `webmidi_write.js`) | Byte-exact against Suite's captured import (`test_device_write_gp150.py`, `test_write_gp150_js.mjs`). |
| Gated sender (`device_write.send_stream(..., session=s)`, `ht_transport.js writePreset`) | Fake-pedal tests, then the pedal: the Python sender in the 2026-10-04 run below, the browser sender in the Task 13 checklist. |
| `patch/ht_write_verify.py` (the seven-step protocol below, steps 0–6) | Fake-pedal tests (`app/tests/test_ht_write_verify.py`), then run on the pedal 2026-10-04 in `--placeholder` mode on slot 199: all 7 steps PASS (run below). |
| Hardware run | **Done 2026-10-04.** The pedal had no empty slot, so the run used `--placeholder`. See "Run 2026-10-04" and the Task 13 checklist at the end of this file. |
| Gate | **Open.** `WRITE_VERIFIED["gp150"] = True` in `patch/device_write.py` (after the 2026-10-04 run) and `WRITE_VERIFIED.gp150 = true` in `app/static/webmidi_write.js` (after the Explorer's edit flows moved to the GP-150 codec, Task 13). |

**Numbering:** the script and `ht_scan.py` use internal slots 0–199. The pedal's display
and Suite number presets from 001, so display number = slot + 1 (slot 199 = preset 200).

## What the script does

`patch/ht_write_verify.py <slot>` uses ONE `ht_scan.Session` for every read and both
writes. A second session on the port would ACK the pedal's 0x08 twice. Every write goes
through `device_write.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s)`.
The script never changes `WRITE_VERIFIED`. It prints one `N PASS|FAIL <step> — <detail>`
line per step, steps 0–6 (seven lines), and **stops at the first FAIL**. It writes nothing
unless steps 0 and 2 showed the target slot empty.

| # | Step | PASS means |
|---|---|---|
| 0 | scan precondition | Checked **before any MIDI port is opened.** `device_scan_gp150/scan_summary.json` from `./.venv-midi/bin/python patch/ht_scan.py scan` must exist, must not be aborted, must cover all 200 slots, and must record `<slot>` as `empty-acked`. Otherwise this step FAILs, naming the scan command. The line shows the file's date and the counts, and lists any slots the scan could not read (those are NOT in the backup). |
| 1 | hello + read active | The pedal answered the handshake. The active preset read back as a 1128-byte GP-150 preset (kept for step 6) and is **not** the target slot; if it is, step 1 refuses. |
| 2 | target slot is empty | **Two** reads of `<slot>` both came back empty with `last_status=empty-acked`. Each waits 3 s of silence after the ACK, then watches the input for 2 s more. It FAILs on a preset, on a late preset stream (a discarded 0x70 chunk), on a partial stream the read retried past (any chunk frame in any of its exchanges), on corrupt frames, or on `empty-unacked`: with no ACK, an empty slot and a lost request look the same. There is no override. |
| 3 | import WRITE TEST | Two backups are saved first, and their paths printed (`  backup: …`), under `device_scan_gp150/write_verify_backup/`: the active preset, and exactly what goes to `<slot>`. Then a copy of slot 0, renamed "WRITE TEST", was imported to `<slot>`. All 10 chunks went out and the pedal sent its 0x08 "import done". The line says `ACK` or `NO ACK of the transfer id`. |
| 4 | read back == sent | `<slot>` reads back byte-identical to what was sent, except the device-owned bytes (`IGNORE`): 0x0A (the import sends 0x5C, exports carry 0x58), 0x0D..0x0F (device-written), 0x43C ("saved on the pedal" flag) and 0x445 (enable bits: bit0 MOD, bit1 DLY, bit2 RVB, bit3 VOL). It prints `back[0x0A]` and the ignored differences. |
| 5 | import blank, read back | `blank(<slot>)` was imported the same way and reads back named "New GEN.". The slot is left holding that blank, so it is no longer empty. |
| 6 | active preset unchanged | The active preset reads back byte-identical to step 1's read. |

From step 3 on, every FAIL ends with **"read slot `<slot>` back before retrying"**: the
slot may or may not hold the import.

`--placeholder` mode is for a pedal with no empty slot. Step 0 requires the target slot's scanned bytes to equal those of at least 10 other scanned slots (except byte 0x04), which shows it holds a factory placeholder and not a user preset. Step 2 re-reads the slot, and step 5 writes the original back and reads it again.

§8.2 (does the pedal refresh the active preset on rewrite?) was answered by hand in the Task 13 checklist: **no**. An import into the active slot is stored, but the pedal keeps playing the old version until the preset is re-selected.

---

## Hardware run template (empty-slot mode)

**Status:** not used. The 2026-10-04 run used `--placeholder` mode because the pedal had no empty slot; its results are in "Run 2026-10-04" below. This template stays for a pedal that has an empty slot. Fill in each result as observed. Do not copy the expected values.
**Do not flip `WRITE_VERIFIED["gp150"]` unless every step row below is PASS and check (a) is "yes".**

Setup: GP-150 on USB, Valeton Suite **closed**, `.venv-midi` present (see `DEVICE_READ.md`).

**REQUIRED before the write script, and enforced by its step 0:** a completed
`./.venv-midi/bin/python patch/ht_scan.py scan`. Run it with no `--out`, so all 200 slots are
saved under `device_scan_gp150/` with `device_scan_gp150/scan_summary.json`. That scan is the
real backup. Record its `done:` line in `DEVICE_READ.md` step 3. The target slot must be
listed as empty-ACKed. If the scan reports **un-ACKed** empty slots, steps 0 and 2 here will
refuse; stop and report back.

Run, from the repo root:

```bash
./.venv-midi/bin/python patch/ht_scan.py read 199      # must print: slot 199: empty (…)
mkdir -p device_scan_gp150
./.venv-midi/bin/python -u patch/ht_write_verify.py 199 2>&1 | tee device_scan_gp150/write_verify_199.log
echo "exit ${pipestatus[1]}"                            # zsh, immediately after the pipeline
```

| Field | Record |
|---|---|
| Date / time | _(fill in)_ |
| Pedal firmware (pedal system menu) | _(fill in)_ |
| Host (macOS version, `./.venv-midi/bin/python --version`) | _(fill in)_ |
| Precondition: the `done:` line of the completed `ht_scan.py scan` | _(fill in)_ |
| Pre-check: `./.venv-midi/bin/python patch/ht_scan.py read 199` | output (must say `slot 199: empty (…)`): _(fill in)_ |
| Command | `./.venv-midi/bin/python -u patch/ht_write_verify.py 199 2>&1 \| tee device_scan_gp150/write_verify_199.log` |
| Exit status (`echo "exit ${pipestatus[1]}"` right after the tee pipeline: 0 = all seven steps PASS, 1 = a FAIL, 2 = bad arguments, 130 = Ctrl-C) | _(fill in)_ |

| # | Step | Line printed (PASS / FAIL + detail, verbatim) |
|---|---|---|
| 0 | scan precondition | _(fill in)_ |
| 1 | hello + read active | _(fill in)_ |
| 2 | target slot is empty | _(fill in)_ |
| 3 | import WRITE TEST | _(fill in)_ |
| 4 | read back == sent | _(fill in)_ |
| 5 | import blank, read back | _(fill in)_ |
| 6 | active preset unchanged | _(fill in)_ |

| Observation | Record |
|---|---|
| Step 2 `last_status` of both reads (PASS requires `empty-acked, empty-acked`) | _(fill in)_ |
| Step 3 backup paths (the two `  backup: …` lines) | _(fill in)_ |
| Step 3 and step 5: `ACK` or `NO ACK of the transfer id` | _(fill in)_ |
| Step 4 `back[0x0A]` (does the pedal store 0x5c or 0x58?) | _(fill in)_ |
| Step 4 ignored differences (a subset of 0x00a 0x00d 0x00e 0x00f 0x43c 0x445) | _(fill in)_ |
| Step 5 differences from `blank(199)` outside the ignored offsets | _(fill in)_ |
| Any `[warn]` / `[debug]` lines (unsolicited 0x18 frames, "0x08 arrived before the import was complete", corrupt frames) | _(fill in)_ |
| Pedal screen/sound during the run: did anything change? | _(fill in)_ |
| Pedal still responsive afterwards (footswitch, preset knob)? | _(fill in)_ |
| **(a)** select preset 200 on the pedal's display (internal slot 199; Suite numbers presets from 001) and confirm the screen shows 'New GEN.'. Does it make sound? | _(fill in)_ |

Full console output, verbatim (from `device_scan_gp150/write_verify_199.log`):

```
_(paste the whole output of ht_write_verify.py here)_
```

### On a FAIL

1. Stop. Do not re-run the script, do not retry blindly, and do not flip the gate.
2. If the FAIL was at step 3 or later, read the slot back:
   `./.venv-midi/bin/python patch/ht_scan.py read 199`. Record what it says: empty, or the
   name and the file it saved under `device_scan_gp150/`.
3. If the pedal stopped responding (`./.venv-midi/bin/python patch/ht_scan.py hello` gets
   no answer), power-cycle it.
4. Record the full console output above, plus steps 2–3, under "Failure record".

A FAIL at step 0, 1 or 2 wrote nothing (the STOPPED block says so; step 0 did not even
open a MIDI port). Re-run only after fixing the cause:
- for step 0: run the full scan (`./.venv-midi/bin/python patch/ht_scan.py scan`) to completion,
  or pick a slot it lists as empty-ACKed.
- for "holds a preset" / "probably NOT empty": pick another slot that the scan lists as empty.
- for "gave no ACK": do not re-run; report back.

Failure record: _(fill in only if a step FAILed)_

### Only then

If every step row is PASS and (a) is yes, the controller flips the **Python** gate
(`WRITE_VERIFIED["gp150"] = True` in `patch/device_write.py`, plus its tests; Task 12
Steps 3–4). The browser gate (`webmidi_write.js`) stays `false` until Task 13.


## Run 2026-10-04 08:01 — slot 199 (display 200), `--placeholder` mode, USB, Valeton Suite closed

Pedal had NO empty slot (slots 111..199 = 89 byte-identical factory "It's GP-150" placeholders), so the test used the placeholder mode: write, read back, restore the original from the scan, read back.

```
GP-150 write verification: target slot 199 (display 200), source slot 0, WRITE_VERIFIED['gp150'] = False (this run passes allow_unverified)
0 PASS scan precondition (placeholder) — /Users/jc/Music/valeton-gp50/device_scan_gp150/scan_summary.json (2026-10-04 07:44): slot 199 holds the factory placeholder "It's GP-150", byte-identical (except 0x04) to 88 other slots e.g. [111, 112, 113, 114, 115]; the original is restored in step 5
1 PASS hello + read active — handshake answered; active preset index 100 'Nothin-GT1', 1128 bytes
2 PASS target slot holds the scanned placeholder — slot 199 still holds the scanned placeholder index 199 "It's GP-150"
  backup: active preset -> /Users/jc/Music/valeton-gp50/device_scan_gp150/write_verify_backup/100-Nothin_GT1.prst
  backup: write-test preset (for slot 199) -> /Users/jc/Music/valeton-gp50/device_scan_gp150/write_verify_backup/199-WRITE_TEST.prst
3 PASS import WRITE TEST — copy of slot 0 (index 0 'New GEN.') renamed 'WRITE TEST' -> slot 199: 10 chunks sent, ACK, 0x08 'import done' received
4 PASS read back == sent — read back index 199 'WRITE TEST' == sent; back[0x0A] = 0x58 (the import sent 0x5c); ignored differences: 0x00e 0x00f
5 PASS restore the original — original index 199 "It's GP-150" written back: 10 chunks sent, ACK, 0x08 'import done' received; read back == original
6 PASS active preset unchanged — identical to step 1's read (index 100 'Nothin-GT1')
all 7 steps PASS — copy this whole output into re/gp150/DEVICE_WRITE.md
manual check (record the answer in re/gp150/DEVICE_WRITE.md):
  a. select preset 200 on the pedal's display (internal slot 199; Suite numbers presets from 001) and confirm the screen shows "It's GP-150"; play a few notes: does it sound?
  note: §8.2 (does the pedal refresh the active preset on rewrite?) will be a scripted, tested mode added in Task 13 — do not improvise it.
```

- exit status: 0 (all seven steps PASS)
- `back[0x0A]` = 0x58: the pedal stores the export form (the import sent 0x5C); device-written offsets that differed: 0x0E, 0x0F
- ACK of the transfer id: yes, both imports; 0x08 'import done' received both times
- step 2 status: slot held the scanned placeholder (placeholder mode; empty-slot statuses not applicable)
- backups: device_scan_gp150/write_verify_backup/100-Nothin_GT1.prst, 199-WRITE_TEST.prst (+ the full scan under device_scan_gp150/)
- `[warn]`/`[debug]` lines: none
- pedal responsive afterwards: yes
- (a) manual check — preset 200 on the display shows "It's GP-150" and plays: YES (user, 2026-10-04 08:04: "shows It's GP-150 and sounds fine, as other It's GP-150 tones")
- **Gate:** `device_write.WRITE_VERIFIED["gp150"] = True` as of this run. The browser gate (`webmidi_write.WRITE_VERIFIED.gp150`) stays false until Task 13 makes the Explorer's edit flows use the GP-150 codec.

## Task 13 browser/edit checklist — 2026-10-04 (slot 199, user at the pedal)

| Step | Result |
|---|---|
| rename (13 chars, Keep changes) | PASS — read-back == expected except 0x0E/0x0F; display updated at once |
| header Patch VOL 50→60 + DLY → Sweet Echo + on (Live edit, Keep) | PASS — read-back == expected except 0x0E/0x0F and 0x445 (device enable bits); quieter, echo audible |
| Clear preset (run on slot 185 by mistake) | PASS — slot == `f150.blank(185)` byte for byte |
| reorder with the OLD codec (records moved, engines by position) | **FAIL** — loud click, no sound (engine 0x0C on the delay record); with 0x0B: sound but inert delay. Slot restored from the step-3 bytes |
| pedal's own reorder (RVB moved, saved on the pedal) | changed only the order table 0x7E..0x82 → codec corrected (fixed home records, order-table-only reorder) |
| reorder with the CORRECTED codec (DLY right after AMP) | PASS — read-back == expected except 0x0E/0x0F; echo present; Explorer (after hard reload + rescan) shows the stored order |
| select (flag 00) | PASS — pedal switches, 0x18 + stream pushed |
| restore 199 and 185 from the scan backup | PASS — both read back byte-identical; active preset left on slot 100 |

Findings: byte 0x445 = enable bits (MOD, DLY, RVB, VOL), 0x43C = "saved on the pedal" flag, both device-written; an import into the ACTIVE slot does not refresh the live preset until re-selected (§8.2); the pedal's chain screen draws AMP in a fixed middle position; an accidental power-off lost nothing (imports are persisted immediately). The chain-strip drag did not work in the user's Chrome (works in automated Chromium) — UX follow-up.
