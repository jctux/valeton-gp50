# GP-150 device READ protocol — status

Date: 2026-10-03. Branch `gp150-support`.

| What | Status |
|---|---|
| Wire facts below | Verified live on a GP-150 (USB PID 0x0186) with the throwaway probes in `re/gp150/probes/` (logs + presets in `re/gp150/evidence/`), and offline against the GP-180 Suite capture corpus (`app/tests/fixtures/gp150/ht_corpus.json`). |
| `patch/ht_proto.py` / `app/static/ht_proto.js` builders + parsers | Byte-exact against the corpus (`test_ht_proto.py`, `test_ht_proto_js.mjs`). |
| `patch/ht_scan.py` (CLI + `Session`) | Tested against a scripted fake port pair only (`app/tests/test_ht_scan.py`). **Not yet run against the pedal.** |
| Browser scan (`ht_transport.js` via the Explorer) | Tested against a fake pedal only (`test_ht_transport_js.mjs`, `test_static_api_gp150_js.mjs`). **Not yet run against the pedal.** |
| Hardware checklist (bottom of this file) | **PENDING — not run.** Nothing in that section has been observed yet. |

## Wire facts (spec §2, condensed)

- **Not the GP-5/GP-50 protocol.** The legacy request `[crc,01,00,02,12,40]` and the
  Universal Device Inquiry get no reply. The GP-150 speaks the GP-180 "HT" protocol.
- **Frame** (every message, both directions): `F0 7F <ocrc> <family> <t0 t1 t2 t3> <body…> F7`.
  `ocrc` = CRC-8 poly 0x31, init 0, no reflection, over `family … last body byte`, `& 0x7F`.
- **Short message** body: `00` + nibbles (hi, lo) of the logical message
  `[01][icrc][len u16 LE][payload]`, `icrc` = CRC-8/0x31 over the payload, **unmasked**.
- **ACK** (family 0x00): `F0 7F <ocrc> 00 00 00 00 <id> 00 F7`, `id` = the request's tx id
  (or, for a stream, its transfer id). Live: the read-active request (tx 1) was ACKed with
  `F0 7F 74 00 00 00 00 01 00 F7` (`evidence/connect_and_active_log.json`).
- **tx ids** are SysEx data bytes: **1..0x7F**, wrapping 0x7F → 1 (0 = no id; 0x80+ is a
  status byte that WebMIDI rejects).
- **Hello**: host `F0 7F 51 00 00 01 03 00 00 F7` → pedal `F0 7F 4D 00 00 02 03 00 00 F7`.
  The reply is a family-0x00 **frame** with `t1 = 02`, *not* an ACK (an ACK is `00 00 00 <id>`).
- **Read preset** (family 0x0F), 11-byte payload
  `03 03 11 30 11 30 02 00 <slot u16 LE> <flag>`. `flag 01` = read without selecting
  (what this tool uses); `flag 00` = select the slot on the pedal. `slot FFFF` = the
  active preset. Pedal: ACK, then a **family-0x70 chunk stream**.
- **Chunk stream**: `F0 7F <ocrc> <fam> 08 <off_lo7> <off_hi7> <transfer_id> <chunk_idx> <nibbles> F7`.
  248 wire bytes per full chunk = **119** decoded bytes; offset = 119 × preceding chunks;
  `chunk_idx` 1-based device→host (0-based host→device); the final chunk is shorter.
  Concatenated pieces = `[01][icrc][len]` + payload; for a read the payload is
  `03 03 11 30` + the 1128-byte `.prst` (10 chunks).
- **The final chunk must be ACKed immediately** (`id = transfer_id`). A ~1.5 s late ACK
  made the pedal retransmit the whole stream.
- **Empty slot**: reading slot 199 produced no stream. The probe filtered family-0x00
  frames, so whether an ACK came before the silence is **not recorded** — open question (a).
- **Reading does not change the active preset** (re-read of the active preset after three
  slot reads was byte-identical).
- **Preset byte 0x04** held the slot index on both slot reads made so far (slot 0 → 0,
  slot 99 → 99; the read of the active preset returned index 100). Only those data points
  exist — open question (b).

## Read rules (`patch/ht_scan.py` `Session` == `app/static/ht_transport.js`)

One port pair, one request in flight, 300 ms settle after each request, 5 ms input poll.
Every inbound frame gets its ACK duty done the moment it is pulled off the port, before
anything else looks at it: a stream's final (short) chunk → `ack(transfer_id)`; a device
short message with a tx id (0x10, 0x18, 0x08 …) → `ack(tx)`. A frame with a bad outer CRC
is dropped and counted (warning at 3).

| `read(slot)` sees | Result |
|---|---|
| ACK + stream that assembles, index byte == slot | the 1128-byte preset |
| ACK, then 1.5 s of silence | `None` — empty slot (`last_status = "empty-acked"`) |
| no ACK and no stream | re-send once with a new tx; silent and un-ACKed again → hello probe: answered → `None` (`"empty-unacked"`), no answer → `NotResponding` |
| chunks arrive during that hello probe | a late stream: read again (≤ 2 more), never `None` |
| stream without its offset-0 chunk, gap, stall, bad `icrc` | one retry, then `StreamError` — never `None` |
| index byte != slot | one retry, then kept with a warning |

Spec §2 says slot 199 "produced no reply at all". If that means no ACK, the un-ACKed path
above is how empty slots get detected. Each one then costs 2 reads + a hello (≈ 2 × 1.8 s, plus the hello)
instead of 1 read (≈ 1.8 s: 1.5 s silence + 0.3 s settle). Question (a) settles which path the pedal takes.

## CLI — `patch/ht_scan.py` (read-only)

Needs `.venv-midi`:
`python3 -m venv .venv-midi && ./.venv-midi/bin/python -m pip install mido python-rtmidi`.
Close Valeton Suite first; it holds the port.

```bash
./.venv-midi/bin/python patch/ht_scan.py hello          # "GP-150 answered the handshake"
./.venv-midi/bin/python patch/ht_scan.py read 0         # slot 0: index=0 name='…' -> device_scan_gp150/000-<name>.prst
./.venv-midi/bin/python patch/ht_scan.py read active    # the preset loaded on the pedal (saved under its index)
./.venv-midi/bin/python patch/ht_scan.py scan           # all 200 slots -> device_scan_gp150/ + scan_summary.json
./.venv-midi/bin/python patch/ht_scan.py scan --out DIR
./.venv-midi/bin/python patch/ht_scan.py watch          # re-read the active preset every 2 s, print byte diffs
```

`scan` prints one line per slot (name + ms, or `(empty — ACK, then silence)` /
`(empty — no ACK; pedal answered hello)`, or `ERROR …`), then:

```
empty slots: [...]
index byte 0x04 == slot for N/N presets[; mismatches (slot, index): [...]]
done: N presets, M empty (A ACKed, U un-ACKed), E errors, Ts (avg … per preset, … per empty slot)
```

`scan_summary.json` has the same data per slot (`status`, `ms`, `name`, `index`, `file`).
It stops after 3 failed slots in a row (pedal gone) and still writes the summary.
`device_scan_gp150/` is gitignored. Copy the files you want to keep as fixtures into
`re/gp150/evidence/` yourself.

`watch` names every changed byte inside the block area by chain position and slot, e.g.
`pos <p> (<slot>) block byte +0x04 (type <old> -> <new>)`. Block layout: `+0x00` enabled, `+0x04` type,
`+0x05` subtype, `+0x06` ext, `+0x07` engine, `+0x08…` 15 × float32 params.

### Type codes

`patch/gp150_type_map.json` (from the public spec, Appendix A) has
AMP 59 · DST 35 · CAB 18 · PRE 14 · DLY 14 · MOD 10 · RVB 10 · N→S 7 · EQ 5 · NR 3 ·
WAH 3 · VOL 1 entries. Every non-"None" type in the three evidence presets is in the map.
Engine-0x06 blocks are the "None" effect. A type that is missing shows as
`Type <n>` in the Explorer. To learn one: run `watch`, switch that block's model on the
pedal, and note the `+0x04` change and the model name on the pedal's screen. Then add it to
the map under its slot and rebuild the ring:

```json
"DLY": { "…": "…", "<type>": { "name": "<name on the pedal>", "origin": "", "params": [], "source": "hardware watch 2026-10-xx" } }
```

```bash
python3 patch/build_ring.py gp150
```

---

## PENDING — hardware checklist (user, pedal on USB)

**Status: NOT RUN.** Fill in each result as observed. Do not copy the expected values.
Setup: GP-150 on USB, Valeton Suite **closed**, `.venv-midi` present (see above).

| # | Run | Record |
|---|---|---|
| 1 | `./.venv-midi/bin/python patch/ht_scan.py hello` | output: _(fill in)_ |
| 2 | `./.venv-midi/bin/python patch/ht_scan.py read active` | index, name, file: _(fill in)_ |
| 3 | `./.venv-midi/bin/python patch/ht_scan.py scan` | the `done:` line (presets / empty / ACKed vs un-ACKed / errors / total s / avg ms): _(fill in)_ |
| 3a | | `empty slots:` line: _(fill in)_ |
| 3b | | `index byte 0x04 == slot …` line: _(fill in)_ |
| 3c | | any `ERROR` / `[ht]` warning lines: _(fill in)_ |
| 3d | step on a footswitch / turn the preset knob | pedal still responsive? _(fill in)_ |
| 4 | `./.venv-midi/bin/python patch/ht_scan.py read active` again, then `cmp` it with step 2's file | identical? (reads must not change the active preset): _(fill in)_ |
| 5 | Chrome: `./run.sh`, open `http://127.0.0.1:8756/explorer?static=1` (or the static build: `node scripts/build_static_site.mjs`, serve `dist/`), click **⟳ Scan device** / **⟳ Rescan device** | progress reaches 200? wall time: _(fill in)_; names identical to the CLI scan (`device_scan_gp150/scan_summary.json`)? _(fill in)_; empty slots listed with no name? _(fill in)_ |
| 6 | `./.venv-midi/bin/python patch/ht_scan.py watch` while switching models on the pedal | each `(slot, type) → name` learned, and any type not yet in `gp150_type_map.json`: _(fill in)_ |
| 7 | copy 5 **user** (non-factory) presets from `device_scan_gp150/` into `re/gp150/evidence/` | file names: _(fill in)_ |

### Open questions for the user to answer from the run above

- **(a) Does an empty-slot read get an ACK before the silence, or no ACK at all?**
  Read it off step 3: `M empty (A ACKed, U un-ACKed)` (also `empty_acked` /
  `empty_unacked` in `scan_summary.json`). Answer: _(fill in)_
- **(b) Is preset byte 0x04 always the slot index?** (Seen on slots 0 and 99 only.)
  Read it off step 3b: `index byte 0x04 == slot for N/N presets`, plus any mismatches. Answer: _(fill in)_
