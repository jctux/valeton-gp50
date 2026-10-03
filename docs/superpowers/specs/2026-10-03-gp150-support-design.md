# GP-150 support — design spec

Date: 2026-10-03. Status: approved design, pre-implementation.
Branch: `gp150-support` on `jctux/valeton-gp50`, to be PR'd to `drewmerc302/valeton-gp50`.

## 1. Goal

Add the Valeton GP-150 as a third supported device with full parity to the GP-50:
scan and browse presets, captures/IR views, backup/export `.prst`, rename, reorder
presets and blocks, clear, parameter edit, import — all from the browser over WebMIDI,
with the Python tooling as oracle. Writes are gated until verified on hardware.

## 2. What is already established (do not re-derive)

Verified live on a GP-150 (firmware unknown, USB PID 0x0186) on 2026-10-03, and
offline against the GP-180 capture corpus (`majabojarska/Valeton-GP180-Rev-Eng`,
8,980 SysEx messages, 205 captures):

- **The GP-150 does NOT speak the GP-5/GP-50 protocol.** Requests `[crc,01,00,02,12,40]`
  and Universal Device Inquiry get no reply. It speaks the GP-180 "HT" protocol.
- **Frame** (all messages, both directions):
  ```
  F0 7F <ocrc> <family> <t0 t1 t2 t3> <nibbles...> F7
  ```
  `ocrc` = CRC-8 poly 0x31, init 0, no reflection, over `family .. last byte before F7`,
  masked `& 0x7F`. (8,980/8,980 captured messages.)
- **Short messages**: after the 4-byte tx field comes one literal `00`, then the
  nibble-expanded (hi, lo) **logical message**:
  ```
  [01] [icrc] [len u16 LE] [payload: len bytes]
  ```
  `icrc` = CRC-8 poly 0x31, init 0, UNMASKED, over `payload`. (All 0x0f/0x10/0x14/0x18/
  0x20/0x08 etc. host and device messages in the corpus; export request reproduced
  byte-for-byte.)
- **ACK** (family 0x00, both directions): `F0 7F <ocrc> 00 00 00 00 <id> 00 F7`.
  `id` echoes the request tx (low byte) or, for chunk streams, the transfer id.
- **Hello**: host `F0 7F 51 00 00 01 03 00 00 F7` → device `F0 7F 4D 00 00 02 03 00 00 F7`.
- **Read preset** (family 0x0F), payload (11 bytes):
  `03 03 11 30 11 30 02 00 <slot u16 LE> <flag>` with `flag=01` (export/read without
  selecting). `slot=0xFFFF` = currently active preset. `flag=00` selects the slot on the
  pedal (Suite "select patch"; device then also emits a 0x18 status + the 0x70 stream).
  Device: ACK, then **family 0x70 stream**.
- **Chunk stream** (families 0x70, 0x2c, 0x24): wire
  `F0 7F <ocrc> <fam> <08> <off_lo7> <off_hi7> <transfer_id> <chunk_idx> <nibbles> F7`,
  248 bytes per full chunk = 119 decoded bytes; offset = `(off_lo7 | off_hi7<<7)` =
  119 × (number of preceding chunks); `chunk_idx` is 1-based device→host and 0-based
  host→device; final chunk shorter. Concatenated decoded bytes =
  `[01][icrc][len u16 LE] + payload`. For a preset read, payload (1132) =
  `03 03 11 30` + 1128-byte `.prst`. **The host must ACK the final chunk immediately**
  (`id = transfer_id`); a ~1.5 s delay made the pedal retransmit the stream.
- **Empty slot**: reading slot 199 produced no reply at all. Treat a 1.5 s silence after
  the ACK as "empty slot".
- **Reading does not change the active preset** (re-read of active after 3 slot reads:
  byte-identical).
- **Write / import preset** (from Suite captures `suite-triggered-import-patch-file-into-
  slot-01/04`, NOT yet sent to the GP-150): host sends a family-0x70 stream with
  logical message `[01][icrc][6c 04]` + payload `01 03 11 30` + the 1128-byte file where
  byte `0x0A` is `0x5C` (the exported file has `0x58`); chunk_idx is 0-based for
  host→device (1-based device→host). Device replies ACK (`id` = transfer_id), then a
  family-0x08 message whose logical payload is `09 03 11 30`, which the host ACKs.
  The slot comes from the file's index byte `0x04`.
- **Container**: 1128 bytes, documented in the gist
  `AlbertoBarba/GP150_PRST_FORMAT.md` (hardware-confirmed). Header: magic `11 30 64 04`,
  index at `0x04` (0–199), BPM `0x24`, patch volume `0x26`, name ASCII NUL-padded at
  `0x2C..0x6F`, chain order 12 × u8 at `0x78` (slot indices: 0 NR, 1 PRE, 2 WAH, 3 DST,
  4 N→S, 5 AMP, 6 CAB, 7 EQ, 8 MOD, 9 DLY, 10 RVB, 11 VOL; position 0 is always AMP).
  Blocks: 12 × 68 bytes from `0x84`, stored in chain order: `[enabled][00 00 00][type]
  [subtype][ext][engine] + 15 × float32 LE`. Footer 180 bytes at `0x3B4` (control
  assignments; copy verbatim). `0x0D..0x0F` is device-written, not a checksum; the
  pedal accepts zeros. No file CRC.
- **GP-150 vs GP-180 factory presets** (slot 0 vs `001-New GEN.prst`, slot 99 vs
  `100-Finger AC.prst`): identical except header `0x0D-0x0F` and footer control bytes
  (`0x448-0x460`, GP-180 has a third footswitch). The GP-180 dump is therefore a valid
  codec test corpus for the GP-150.
- **Catalog**: `/Applications/Runner.app/Wrapper/Runner.app/Frameworks/App.framework/
  flutter_assets/assets/data/module150_data.json` (Valeton Suite 2.1.0): 12 modules,
  348 entries, same shape as `module50_data.json`. Block `type` codes in the `.prst`
  are per-slot enumerations (spec Appendix A), NOT fxid low bytes; mapping
  (slot, type) → catalog entry comes from Appendix A names matched to catalog names.
- **Live-edit messages** exist (0x18 param write, 0x10 module enable, 0x14 variant
  select) and are partially mapped in the GP-180 repo (`effect-wire-schema.json`).
  They are NOT used in this design (see §7).

Evidence files: `re/gp150/evidence/` (probe logs, read presets), `re/gp150/probes/`
(the throwaway probe scripts that produced them).

## 3. Architecture

Approach: GP-150 becomes a third `DeviceProfile` with its **own codec module and own
transport module**, plugged in behind the two interfaces the app already consumes
(`PRST.*` and `WebMidiDevice.*`). Nothing in explorer/patchlib/static_api learns about
the wire format; they ask the profile.

```
                 profile.key == "gp150"            profile.key in {gp5, gp50}
PRST.codec(p) -> prst150.js                        prst.js (unchanged)
WebMidiDevice -> ht_transport.js (HT framing)      legacy nibble transport (unchanged)
patch/ (py)   -> prst150_format.py, ht_proto.py    prst_format.py, live_read.py, device_write.py
```

### 3.1 Device profile

`patch/prst_format.py` and `app/static/prst.js`:

```
GP150 = DeviceProfile(key="gp150", name="GP-150", header=b"\x11\x30\x64\x04",
                      prst_len=1128, devtag=b"", ring_file="fxid_ring_gp150.json",
                      midi_port="GP-150", usb_pid=0x0186)
```
plus two new fields on every profile, defaulted for GP-5/GP-50:
`slots` (100 | 200), `transport` ("legacy" | "ht"), `n_blocks` (10 | 12).
`detect()` matches the 4-byte magic then length 1128. Port detection order becomes
GP-150 → GP-50 → GP-5 (substring shadowing). `NAME_OFF/BODY_OFF` module constants stay
GP-50-only; every consumer that needs offsets goes through the codec (3.2).

### 3.2 Codec `prst150` (JS + Python, byte-identical behaviour)

Same method surface as `prst.js` so `patchlib.js`, `static_api.js`, `explorer.js`,
`convert_prst.js` and `webmidi_write.js` dispatch via `PRST.codecFor(profile)`:

| method | GP-150 behaviour |
|---|---|
| `detect(bytes)` | magic + len 1128 |
| `readName/writeName` | `0x2C`, max 68 bytes, NUL-padded; UI caps at 13 chars |
| `readVolBpm` / `writeVolBpm` | `0x26` / `0x24` (u8; BPM 40–300 stored low byte only — surface as read-only >255 caveat) |
| `readOrder/writeOrder` | 12-byte chain order at `0x78`; AMP (5) locked at position 0; **blocks move with the order** (blocks are stored in chain order, unlike GP-50) |
| `modelRecords` | 12 × `{pos, slot, type, subtype, ext, engine, enabled}` |
| `bypassMask` | from per-block `enabled` bytes |
| `paramFloats` | 12 × 15 float32 |
| `applyEdits` | name / vol / bpm / order / enabled / type / params; recompute `engine` per spec §6–7 rules; subtype/ext per type table |
| `blankPrst(slot)` | factory "New GEN." header + footer template with index set, name "New GEN." |
| `refixCrc` | no-op |
| `rebuild(name, body)` | not applicable; stream yields the whole file |
| `convert`, `checkConvertible` | refuse for gp150 (out of scope) |

Catalog lookup key for the GP-150 is `(slot, type)` not `fxid`; `patchlib` already
takes a ring keyed by integer, so the GP-150 ring is keyed by `slot<<8 | type` with the
same value shape (`module, name, fxtitle, type, origin, params[]`).

### 3.3 Catalog ring `fxid_ring_gp150.json`

`patch/build_ring.py` gets a `gp150` target: read `module150_data.json` from the user's
Suite install (new Catalyst path; keep the old desktop path as fallback), join with a
checked-in table `patch/gp150_type_map.json` = `{slot: {type: catalogName}}`
transcribed from spec Appendix A. Entries that have no catalog match are emitted with
Appendix A's own param names. Valeton JSON is never committed.

### 3.4 Transport `ht` (JS + Python)

`app/static/ht_transport.js` and `patch/ht_proto.py`, pure functions + one stateful
session:

- `ocrc(bytes)`, `icrc(bytes)`, `enc/dec nibbles`, `frame(family, tx4, logical)`,
  `logical(payload)`, `parseFrame(wire)`, `ack(id)`, `hello()`,
  `readPresetRequest(tx, slot, select=false)`, `importPresetStream(transfer_id, prst)`
  → list of wire chunks, `assembleStream(chunks)` → (transfer_id, payload) with
  offset/index validation.
- Session: single input/output, serialized request queue, tx counter (1..255 wrap),
  settle 300 ms, per-request timeout 3 s, chunk idle 800 ms, **final-chunk ACK sent from
  the input callback before any other work**, unsolicited device messages (0x18, 0x5c,
  0x30 …) logged and ACKed when they carry a tx id.
- `WebMidiDevice` keeps its public API (`connect, readNames, readSlotPrst,
  readActivePrst, selectSlot, scan, _sendStream, sync`) and routes to `ht` for
  `gp150`. `readNames` for the GP-150 = a full scan (no bulk name read is known);
  `selectSlot` = read request with `flag=00`.

### 3.5 Read path

- Scan: 200 sequential `readPreset(slot)`; ~0.35 s per filled slot, ≤1.5 s per empty
  slot; progress UI as today; cached per profile key in localStorage as today.
- Explorer shows 12 blocks, 200 slots, position-0 AMP lock in the reorder strip.
- Captures & IRs page: SnapTone/IR catalog reads for the GP-150 are **not** known;
  show the N→S block's reference (type/name) and hide the device catalog tabs until a
  read selector is found (tracked in §8).

### 3.6 Write path (gated)

Single write engine = full-preset import to a slot, mirroring the GP-50's 0x1D model:

1. `importPresetStream` built byte-for-byte against the two captured Suite imports
   (test fixture: GP-180 corpus files; the logical message incl. `icrc` must match).
2. `WRITE_VERIFIED["gp150"] = False`. `send_stream`/`_sendStream` refuse unless
   `allow_unverified` (CLI) / an explicit "I am testing" confirm (UI dev flag).
3. Verification protocol (one session, user present): read slot 199 (expect empty) →
   import a factory preset copy with index 199 → expect ACK + 0x08 → read back → compare
   byte-for-byte ignoring `0x0D..0x0F` → clear slot 199 by importing `blankPrst(199)`
   → read back. Only then flip the gate.
4. All edits (rename, reorder, param, enable, clear, block reorder) = `applyEdits` →
   import to the same slot. Live edit on the active slot: first verified write tells us
   whether the pedal reloads the active patch; if not, follow with `selectSlot`.

### 3.7 UI/product surface

Device badge "GP-150", slot count from profile, 12-block chain, converter page hides
the GP-150 in its matrix, README + CONTEXT.md gain the GP-150 rows, `docs/reddit-blurb`
untouched.

## 4. Data flow (read)

```
user clicks Scan
  → WebMidiDevice.scan() → ht.session.request(readPresetRequest(tx, slot))
  → frames in: ACK(tx) ; 0x70 chunks … final short chunk → ack(transfer_id) NOW
  → assembleStream → logical(icrc check) → payload[4:] = 1128-byte prst
  → PRST.codecFor(gp150).detect/readName → static_api cache → patchlib inventory
```

## 5. Error handling

- Bad `ocrc`/`icrc` on an inbound frame: drop frame, count, surface after 3.
- Stream gap (offset ≠ 119×(idx−1)) or missing final chunk: discard, retry once.
- No ACK for a request within 3 s: one retry, then "pedal not responding — close
  Valeton Suite?" (BLE session did not interfere in testing, but keep the hint).
- Empty-slot silence is not an error.
- Chrome 152 regression note stays (nibble SysEx is still used).

## 6. Testing

- `app/tests/test_ht_proto.py` + `test_ht_js.mjs`: every builder reproduces the
  corpus bytes (hello, export request tx 0x23, import streams slot 1 and 4 incl. all
  chunk headers and `icrc`, ACKs); parser round-trips all 33 captured 0x70 streams.
- `test_prst150_format.py` + `test_prst150_js.mjs`: byte-exact decode/encode round
  trip over the 200-file GP-180 dump (copied under `app/tests/fixtures/gp150/` only if
  the upstream license allows; otherwise fetched by a script) plus the GP-150 reads in
  `re/gp150/evidence/`; `applyEdits` oracle JS vs Python.
- Existing GP-5/GP-50 suites must stay green (no behaviour change for them).
- Hardware checklist in `re/gp150/DEVICE_READ.md` / `DEVICE_WRITE.md` with the
  verification protocol of §3.6.

## 7. Out of scope

GP-150 ↔ GP-5/GP-50 preset conversion; SnapTone/IR/NAM upload (family 0x24 integrity
nibbles unsolved upstream); granular live-edit messages (0x18/0x10/0x14) — possible
phase 3 for lower-latency knob turns; Bluetooth; GP-180 (likely works unchanged, not
tested — port name "Valeton GP-180 MIDI").

## 8. Open questions (tracked, non-blocking)

1. Bulk name read / SnapTone catalog selectors for the HT protocol (connect capture
   shows 0x0c reads with different selectors returning 0x10/0x18/0x2c streams; decode
   later).
2. Does a 0x70 import to the active slot reload the live patch? (first gated write).
3. Device firmware version and the 0x10 reply semantics (identical bytes on GP-180 and
   GP-150, so not an identity message).

## 9. Delivery

Phase 1 (read-only, shippable): §3.1–3.5 + tests. Phase 2: §3.6 + gate flip + docs.
PR to upstream after phase 2, with README section "GP-150".
