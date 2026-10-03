# GP-150 device WRITE verification — status

Date: 2026-10-03. Branch `gp150-support`.

| What | Status |
|---|---|
| Import stream (`device_write.build_gp150_write_stream` / `webmidi_write.js`) | Byte-exact against Suite's captured import (`test_device_write_gp150.py`, `test_write_gp150_js.mjs`). |
| Gated sender (`device_write.send_stream(..., session=s)`, `ht_transport.js writePreset`) | Tested against a scripted fake pedal only. |
| `patch/ht_write_verify.py` (the six-step protocol below) | Tested against a scripted fake pedal only (`app/tests/test_ht_write_verify.py`). **Not yet run against the pedal.** |
| Hardware run (bottom of this file) | **PENDING — not run.** Nothing in that section has been observed yet. |
| Gate | `WRITE_VERIFIED["gp150"] = False` in `patch/device_write.py` **and** `app/static/webmidi_write.js`. |

**Numbering:** the script and `ht_scan.py` use internal slots 0–199. The pedal's display
and Suite number presets from 001, so display number = slot + 1 (slot 199 = preset 200).

## What the script does

`patch/ht_write_verify.py <slot>` uses ONE `ht_scan.Session` for every read and both
writes. A second session on the port would ACK the pedal's 0x08 twice. Every write goes
through `device_write.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s)`.
The script never changes `WRITE_VERIFIED`. It prints one `N PASS|FAIL <step> — <detail>`
line per step and **stops at the first FAIL**. It writes nothing unless step 2 proved the
target slot empty.

| # | Step | PASS means |
|---|---|---|
| 1 | hello + read active | The pedal answered the handshake. The active preset read back as a 1128-byte GP-150 preset (kept for step 6) and is **not** the target slot; if it is, step 1 refuses. |
| 2 | target slot is empty | **Two** reads of `<slot>` both came back empty with `last_status=empty-acked`. Each waits 3 s of silence after the ACK, then watches the input for 2 s more. It FAILs on a preset, on a late preset stream (a discarded 0x70 chunk), on corrupt frames, or on `empty-unacked`: with no ACK, an empty slot and a lost request look the same. There is no override. |
| 3 | import WRITE TEST | Two backups are saved first, and their paths printed (`  backup: …`), under `device_scan_gp150/write_verify_backup/`: the active preset, and exactly what goes to `<slot>`. Then a copy of slot 0, renamed "WRITE TEST", was imported to `<slot>`. All 10 chunks went out and the pedal sent its 0x08 "import done". The line says `ACK` or `NO ACK of the transfer id`. |
| 4 | read back == sent | `<slot>` reads back byte-identical to what was sent, except 0x0A (the import sends 0x5C, exports carry 0x58) and 0x0D..0x0F (device-written). It prints `back[0x0A]` and the ignored differences. |
| 5 | import blank, read back | `blank(<slot>)` was imported the same way and reads back named "New GEN.". The slot is left holding that blank, so it is no longer empty. |
| 6 | active preset unchanged | The active preset reads back byte-identical to step 1's read. |

From step 3 on, every FAIL ends with **"read slot `<slot>` back before retrying"**: the
slot may or may not hold the import.

§8.2 (does the pedal refresh the active preset on rewrite?) will be a scripted, tested mode added in Task 13 — do not improvise it.

---

## PENDING — hardware run (user, pedal on USB)

**Status: NOT RUN.** Fill in each result as observed. Do not copy the expected values.
**Do not flip `WRITE_VERIFIED["gp150"]` unless every step row below is PASS and check (a) is "yes".**

Setup: GP-150 on USB, Valeton Suite **closed**, `.venv-midi` present (see `DEVICE_READ.md`).

**REQUIRED before the write script:** a completed `./.venv-midi/bin/python patch/ht_scan.py scan`,
with all 200 slots saved under `device_scan_gp150/`. That scan is the real backup.
Record its `done:` line in `DEVICE_READ.md` step 3. If it reports **un-ACKed** empty slots,
step 2 here will refuse; stop and report back.

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
| Exit status (`echo "exit ${pipestatus[1]}"` right after the tee pipeline: 0 = six PASS, 1 = a FAIL, 2 = bad arguments, 130 = Ctrl-C) | _(fill in)_ |

| # | Step | Line printed (PASS / FAIL + detail, verbatim) |
|---|---|---|
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
| Step 4 ignored differences (a subset of 0x00a 0x00d 0x00e 0x00f) | _(fill in)_ |
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

A FAIL at step 1 or 2 wrote nothing (the STOPPED block says so). Re-run only after
fixing the cause:
- for "holds a preset" / "probably NOT empty": pick another slot that the scan lists as empty.
- for "gave no ACK": do not re-run; report back.

Failure record: _(fill in only if a step FAILed)_

### Only then

If every step row is PASS and (a) is yes, the controller flips the **Python** gate
(`WRITE_VERIFIED["gp150"] = True` in `patch/device_write.py`, plus its tests; Task 12
Steps 3–4). The browser gate (`webmidi_write.js`) stays `false` until Task 13.
