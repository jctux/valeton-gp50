# GP-150 device WRITE verification — status

Date: 2026-10-03. Branch `gp150-support`.

| What | Status |
|---|---|
| Import stream (`device_write.build_gp150_write_stream` / `webmidi_write.js`) | Byte-exact against Suite's captured import (`test_device_write_gp150.py`, `test_write_gp150_js.mjs`). |
| Gated sender (`device_write.send_stream(..., session=s)`, `ht_transport.js writePreset`) | Tested against a scripted fake pedal only. |
| `patch/ht_write_verify.py` (the six-step protocol below) | Tested against a scripted fake pedal only (`app/tests/test_ht_write_verify.py`). **Not yet run against the pedal.** |
| Hardware run (bottom of this file) | **PENDING — not run.** Nothing in that section has been observed yet. |
| Gate | `WRITE_VERIFIED["gp150"] = False` in `patch/device_write.py` **and** `app/static/webmidi_write.js`. |

## What the script does

`patch/ht_write_verify.py <slot>` uses ONE `ht_scan.Session` for every read and both
writes. A second session on the port would ACK the pedal's 0x08 twice. Every write goes
through `device_write.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s)`.
The script never changes `WRITE_VERIFIED`. It prints one `N PASS|FAIL <step> — <detail>`
line per step and **stops at the first FAIL**. It writes nothing unless step 2 proved the
target slot empty.

| # | Step | PASS means |
|---|---|---|
| 1 | hello + read active | the pedal answered the handshake; the active preset read back as a 1128-byte GP-150 preset (kept for step 6) |
| 2 | target slot is empty | `read(<slot>)` returned nothing; `last_status` is printed (`empty-acked` = ACK then silence, `empty-unacked` = no ACK but the pedal answered a hello) |
| 3 | import WRITE TEST | a copy of slot 0, renamed "WRITE TEST", was imported to `<slot>`: all 10 chunks went out and the pedal sent its 0x08 "import done" (`ACK` / `NO ACK of the transfer id` is printed) |
| 4 | read back == sent | `<slot>` reads back byte-identical to what was sent, except 0x0A (the import sends 0x5C, exports carry 0x58) and 0x0D..0x0F (device-written). Prints `back[0x0A]` and the ignored differences |
| 5 | import blank, read back | `blank(<slot>)` was imported the same way and reads back named "New GEN." (the slot is left holding that blank) |
| 6 | active preset unchanged | the active preset reads back byte-identical to step 1's read |

From step 3 on, every FAIL ends with **"read slot `<slot>` back before retrying"**: the
slot may or may not hold the import.

---

## PENDING — hardware run (user, pedal on USB)

**Status: NOT RUN.** Fill in each result as observed. Do not copy the expected values.
**Do not flip `WRITE_VERIFIED["gp150"]` unless every row below is PASS and check (a) is "yes".**

Setup: GP-150 on USB, Valeton Suite **closed**, `.venv-midi` present (see `DEVICE_READ.md`).

| Field | Record |
|---|---|
| Date / time | _(fill in)_ |
| Pedal firmware (pedal system menu) | _(fill in)_ |
| Host (macOS version, `./.venv-midi/bin/python --version`) | _(fill in)_ |
| Pre-check: `./.venv-midi/bin/python patch/ht_scan.py read 199` | output (must say `slot 199: empty (…)`): _(fill in)_ |
| Command | `./.venv-midi/bin/python patch/ht_write_verify.py 199` |
| Exit status (`echo $?`: 0 = six PASS, 1 = a FAIL) | _(fill in)_ |

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
| Step 2 `last_status` for the empty slot (`empty-acked` / `empty-unacked`) | _(fill in)_ |
| Step 3 and step 5: `ACK` or `NO ACK of the transfer id` | _(fill in)_ |
| Step 4 `back[0x0A]` (does the pedal store 0x5c or 0x58?) | _(fill in)_ |
| Step 4 ignored differences (subset of 0x00a 0x00d 0x00e 0x00f) | _(fill in)_ |
| Step 5 differences from `blank(199)` outside the ignored offsets | _(fill in)_ |
| Any `[warn]` / `[debug]` lines (unsolicited 0x18 frames, "0x08 arrived before the import was complete", corrupt frames) | _(fill in)_ |
| Pedal screen/sound during the run: did anything change? | _(fill in)_ |
| Pedal still responsive afterwards (footswitch, preset knob)? | _(fill in)_ |
| **(a)** Select slot 199 on the pedal: does it play? (it should hold "New GEN.") | _(fill in)_ |
| **(b)** Optional, spec §8.2: does an import to the ACTIVE slot update the screen/sound without a reselect? (procedure below) | _(fill in)_ |

Full console output, verbatim:

```
_(paste the whole output of ht_write_verify.py here)_
```

### On a FAIL

1. Stop. Do not re-run the script, do not retry blindly, and do not flip the gate.
2. If the FAIL was at step 3 or later, read the slot back:
   `./.venv-midi/bin/python patch/ht_scan.py read 199`. Record what it says (empty, or the
   name + the file it saved under `device_scan_gp150/`).
3. If the pedal stopped responding (no handshake: `./.venv-midi/bin/python patch/ht_scan.py hello`), power-cycle it.
4. Record the full console output above, plus steps 2–3, under "Failure record".

Failure record: _(fill in only if a step FAILed)_

### Optional: spec §8.2 (import to the active slot)

Only after all six steps PASS. This writes a renamed copy of the **active** preset's own
slot, then writes the original back. If the active preset has unsaved edits on the pedal,
save or discard them first. Start `./.venv-midi/bin/python` in the repo root and paste
part A one line at a time:

```python
# part A: back up, then import a renamed copy to the active preset's own slot
from patch import device_write as dw, ht_proto as ht, prst150_format as f150, ht_write_verify as hwv
from patch.ht_scan import Session, save
s = Session(log=lambda lvl, msg: print(f"[{lvl}] {msg}")); assert s.hello()
active = s.read(ht.SLOT_ACTIVE); slot = f150.read_index(active); stored = s.read(slot)
assert stored is not None and hwv.same_except(stored, active), "active != its stored slot (unsaved edits?) — stop here"
print("slot", slot, repr(f150.read_name(stored)), "backup ->", save(stored, slot, "device_scan_gp150/backup_8_2"))
b = bytearray(stored); f150.write_name(b, "SPEC 8.2"); pk = dw.build_gp150_write_stream(bytes(b), slot); ok, why = dw.validate_gp150_stream(pk, slot=slot); assert ok, why
print(dw.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s))
```

Look at the pedal **without touching it**. Does the screen show "SPEC 8.2", or did the
sound change, without a reselect? Record the answer in row (b). Then paste part B
to restore the original:

```python
# part B: restore the original and check it
pk = dw.build_gp150_write_stream(stored, slot); ok, why = dw.validate_gp150_stream(pk, slot=slot); assert ok, why
print(dw.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s))
print("restored:", hwv.same_except(s.read(slot), stored)); s.close()
```

If any line raises, stop, run `s.close()`, then read the slot back with
`./.venv-midi/bin/python patch/ht_scan.py read <slot>`. The backup `.prst` is in
`device_scan_gp150/backup_8_2/`.

### Only then

Every step row PASS, (a) = yes: the controller flips the **Python** gate
(`WRITE_VERIFIED["gp150"] = True` in `patch/device_write.py`, plus its tests; Task 12
Steps 3–4). The browser gate (`webmidi_write.js`) stays `false` until Task 13.
