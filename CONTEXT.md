# CONTEXT — domain language for the Valeton Companion

Use these terms exactly; they map 1:1 to modules and UI copy.

## Domain

- **Device** — the GP-5, the GP-50 or the GP-150 (below). The GP-5 and GP-50 are
  siblings: same .prst container, same SysEx protocol, same effect catalog (GP-5's
  is a strict subset of the GP-50's). A **device profile**
  (`prst_format.DeviceProfile`, keys `gp5`/`gp50`/`gp150`) carries the three things
  that differ between the siblings: 20-byte header, .prst length (507 vs 552), and
  the 0xFF-block device tag; plus `slots`/`transport`/`n_blocks` (100/"legacy"/10
  for both siblings). `prst_format.detect()` identifies a .prst's device.
  Reads/scans/conversions work on both. Device WRITE is capture-verified for the
  GP-50 and hardware-verified for the GP-150 (`device_write.WRITE_VERIFIED`); a GP-5
  write reuses the GP-50 opcodes on faith and is gated behind `allow_unverified`
  until a GP-5 import is captured.
- **GP-150** — the third device (`prst_format.GP150`: 200 slots, transport `"ht"`,
  12 blocks). Not a GP-5/GP-50 sibling. **Container**: 1128 B, magic `11 30 64 04`,
  layout in `patch/prst150_format.py` / `app/static/prst150.js` (the "codec";
  callers get it from `PRST.codecFor(profile)`). **Protocol**: the GP-180 "HT"
  SysEx protocol (`patch/ht_proto.py` / `app/static/ht_proto.js`, browser session
  `ht_transport.js`, read-only CLI `patch/ht_scan.py`; see `re/gp150/DEVICE_READ.md`).
  `hello()` = the handshake plus Valeton Suite's family-0x0C **session open**: a
  power-cycled pedal ACKs reads but streams nothing until the session is open.
  No bulk name read: a scan reads every slot, and a slot that stays silent is
  **empty** (stored as a nameless blank).
  - **Block slots** 0–11 are NR PRE WAH DST N→S AMP CAB EQ MOD DLY RVB VOL. Blocks
    are keyed by slot everywhere (`blocks[k]`, edit specs, `blocksBySlot`).
  - **Fixed home records**: each slot's 68-byte block record (12 records from 0x84)
    sits at a fixed index whatever the chain order, `DEFAULT_POS` =
    `[1,2,3,4,5,0,6,7,8,9,10,11]` for NR … VOL (record 0 AMP, 1 NR … 5 N→S, 6 CAB …
    11 VOL).
  - **Order table** (12 B at 0x78): slot ids by chain position, default
    `[5,0,1,2,3,4,6,7,8,9,10,11]`. **A reorder rewrites only the order table**, as the
    pedal's own reorder does (hardware, 2026-10-04). **AMP (slot 5) is locked at chain
    position 0** and VOL (slot 11) at 11: the codec refuses any other order.
  - **Engines per slot**: a block's engine byte (record +7) belongs to its effect,
    never to its chain position, and is never recomputed on a reorder. A model change
    takes the engine for that (slot, type) from `patch/gp150_engines.json` (learned
    from scanned presets by `scripts/gp150_engines.py`; the JS copy `ENGINES` in
    `prst150.js` is synced by hand, and `test_prst150_js.mjs` fails until it matches).
    An engine-0x06 block other than VOL is the "None" effect (type 3, off); VOL's
    normal engine is 0x06. A model pick needs an unambiguous engine: one engine for the
    (slot, type) in the table's counts, or one engine for every seen type in the slot
    (NR, EQ, DLY, RVB, VOL). Ambiguous pairs and unseen types in PRE, WAH, DST, N->S,
    AMP, CAB and MOD are refused (`pick_allowed`; the Explorer greys them out); a re-pick
    of the stored model keeps its engine.
  - **Models**: a block names its model by `(slot, type)`, where type is a per-slot
    enumeration (not an fxid low byte). The ring `patch/fxid_ring_gp150.json` is keyed
    **`(slot << 24) | type`**.
  - **Device-written bytes**: 0x0A (the import stream carries 0x5C, the pedal stores
    0x58), 0x0D..0x0F (written by the pedal; it accepts zeros), 0x43C ("saved on the
    pedal" flag) and 0x445 (enable bits: bit0 MOD, bit1 DLY, bit2 RVB, bit3 VOL). The
    codec never writes them, and read-back compares ignore them. No file CRC.
  - **Write** = a whole-preset import into one slot (family-0x70 stream, the same one
    Valeton Suite sends to import a `.prst`; the slot comes from byte 0x04). Both
    gates are open: `device_write.WRITE_VERIFIED["gp150"]` and
    `webmidi_write.WRITE_VERIFIED.gp150`, hardware-verified 2026-10-04
    (`re/gp150/DEVICE_WRITE.md`). The pedal doesn't reload the active preset after an
    import: re-select it on the pedal to hear the change.
  - No conversion to or from the GP-5/GP-50.
- **Patch** — one device preset slot (index 0–99; 0–199 on the GP-150). Serialized
  as a **.prst** file (layout: `patch/prst_format.py`; 552 B on GP-50, 507 B on
  GP-5; GP-150 above). A patch whose name is the factory default (the device
  name, `"GP-50"` / `"GP-5"`) is an **empty slot** (safe write target).
- **Preset conversion (GP-5 ↔ GP-50)** — reshaping a .prst between devices
  (`patch/convert.py`). Not an effect transcode: the 390-byte 0x02 tone block +
  name + VOL/BPM/footswitches are portable and rewrapped in the target skeleton.
  GP-5 → GP-50 is always lossless; GP-50 → GP-5 refuses (unless forced) when a
  block uses one of the 3 GP-50-only models. Surfaced on the Convert page's
  "Preset" sub-tab.
- **SnapTone** — a NAM capture loaded on the device (user slots 50–79 of the
  amp catalog). The tone core of a patch. Referenced by a patch's **N→S**
  block (category 0x0F, slot index; 0 = none — a SnapTone bypasses AMP+CAB).
- **User IR** — a user-uploaded cabinet impulse response. In fxid space these
  live at CAB fxlow ≥ 0x100000 ("User IR N" = slot N−1). Real device names come
  from a sync (bank_map).
- **Template** — a saved whole-patch effects chain ("effects wrapper", e.g.
  "Metal", "80s Clean") stored computer-side in templates.json. **Build a
  patch from a capture** = stamp a template onto a SnapTone (repoint its N→S,
  refix CRC) and write the result to a slot.
- **Block** — one of the 10 chain positions (NR PRE DST AMP CAB EQ MOD DLY RVB
  N→S; the GP-150 has 12, see above). A block references a **model** by
  fxid = (category << 24) | fxlow; the decoded catalog is per-device
  (`patch/fxid_ring.json` for GP-50, `patch/fxid_ring_gp5.json` for GP-5), both
  built by `patch/build_ring.py`.
- **Block-library entry** — one saved block (model + params), the block-level
  sibling of a template. Stored in block_library.json.
- **bank_map** — `patch/bank_map.json`: authoritative device names for
  SnapTone slots and User IRs, produced only by a live sync
  (`patch/read_bank_map.py`).
- **Scan** — the ~60–90 s one-preset-at-a-time full read of the device into
  `device_scan/` (no bulk read exists; GP-150: about 70 s for 200 slots, into
  `device_scan_gp150/` from `patch/ht_scan.py scan`). **Sync** — the quick catalog/IR name
  read that refreshes bank_map.
- **Refit / A2→A1** — distilling a NAM A2 capture into the 0.5.x A1
  architecture the GP-50 accepts (a2a1/, the Convert page).

## Architecture seams

- **prst_format** (`patch/prst_format.py`) — the .prst byte layout: offsets,
  CRC-8/0x07, name codec, record magics, rebuild(), plus the device profiles
  (GP-5/GP-50/GP-150) and detect(). The only module allowed to know GP-5/GP-50
  offsets; GP-150 offsets live in `prst150_format` (JS mirror `prst150.js`).
  Golden-file tested against presetExports/ and the GP-5 fixtures.
- **convert** (`patch/convert.py`) — GP-5 ↔ GP-50 preset reshaping (see the
  domain term). stdlib-only; round-trip tested against both corpora.
- **device_protocol** (`patch/device_protocol.py`) — the stdout wire schema
  between app/device_io.py and the MIDI subprocess scripts. Both sides import
  it; contract-tested without hardware.
- **ui_core** (`app/static/ui_core.js`, `window.UI`) — page-agnostic frontend
  primitives (toast, confirm/prompt modals, fetch helpers, downloads, User-IR
  threshold). Preset Explorer and Device Inspector are its two adapters.
  Empty-slot truth and slot domains come from the backend inventory
  (`patch.empty`, `inventory.domains`) — no frontend re-derives them.
- **distill_protocol** (`a2a1/distill_protocol.py`) — the stdout token
  contract (DISTILL_ESR:/FORMAT:) between app/engine.py and the a2a1 train
  scripts. Both sides import it; contract-tested without torch.
- **jsonstore** (`app/jsonstore.py`) — shared JSON-list persistence (atomic
  tmp-swap + lock) under blocklib and templates_store.
- **patchlib** (`app/patchlib.py`) — inventory + edit semantics layered on
  prst_format: catalog resolution, SnapTone identity, usage, clone/repoint.
  Device-aware: detects the source presets' device (`_device()`) and loads the
  matching ring; `inventory.device` drives the UI's device badge.
- **patch/** — hardened device-I/O runtime only; archived RE probes live in
  `re/probes/` (not product code).
