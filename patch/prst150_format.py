"""The 1128-byte GP-150 .prst container — single source of truth for its layout.

Layout (hardware-confirmed; see docs/superpowers/specs/2026-10-03-gp150-support-design.md §2
and the public GP150_PRST_FORMAT.md analysis):

  0x000  4   magic 11 30 64 04
  0x004  1   preset index 0..199
  0x00D  3   device-written field (not a checksum; zero is accepted) — leave as-is
  0x024  1   BPM (low byte of 40..300)
  0x026  1   patch volume 0..100
  0x02C  68  name, ASCII, NUL-terminated/padded
  0x078  12  order table: order[pos] = slot index (SLOTS) at chain position pos;
             order[0] is always AMP (5), order[11] always VOL (11) (every corpus file)
  0x084  12 x 68  block records at FIXED indexes: slot s is record DEFAULT_POS[s]
             (record r holds slot DEFAULT_ORDER[r] = AMP NR PRE WAH DST N->S CAB EQ MOD
             DLY RVB VOL), whatever the order table says. Record: [enabled][0 0 0][type]
             [subtype][ext][engine] + 15 f32 LE
  0x3B4  180 footer: controller assignments (copied verbatim)
  0x43C  1   device-owned "saved on the pedal" flag; 0x445 device-owned enable bits
             (bit0 MOD, bit1 DLY, bit2 RVB, bit3 VOL) — never written by the codec

Reorder (hardware, 2026-10-04: the pedal's own reorder of re/gp150/evidence/
199-reordered-by-pedal.prst changed only 0x7E..0x82): a reorder rewrites ONLY the
order table. The engine byte belongs to the effect in the slot; it is set from the
per-(slot, type) table patch/gp150_engines.json when a block's model changes, never
from the chain position.

stdlib-only: imported by the web app oracle tests and the MIDI CLI alike.
"""
from __future__ import annotations

import base64
import json
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple

MAGIC = b"\x11\x30\x64\x04"
PRST_LEN = 1128
IDX_OFF = 0x04
BPM_OFF = 0x24
VOL_OFF = 0x26
NAME_OFF = 0x2C
NAME_LEN = 0x44
ORDER_OFF = 0x78
BLOCKS_OFF = 0x84
BLOCK_LEN = 0x44
N_BLOCKS = 12
N_PARAMS = 15
FOOTER_OFF = 0x3B4

SLOTS = ["NR", "PRE", "WAH", "DST", "N->S", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "VOL"]
AMP_SLOT = 5
VOL_SLOT = 11  # pinned last (see write_order)
MOVABLE = [s for s in SLOTS if s not in ("AMP", "VOL")]
# The order table of an unreordered preset. Record r always holds slot DEFAULT_ORDER[r]:
# a slot's record sits at its HOME index DEFAULT_POS[slot], whatever the order table says.
DEFAULT_ORDER = [AMP_SLOT, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, VOL_SLOT]
DEFAULT_POS = [DEFAULT_ORDER.index(s) for s in range(len(SLOTS))]

ENGINE_BYPASS = 0x06  # engine of the "None" effect in any slot; VOL's real type 3 ("Volume") carries it too
# Slots whose type 3 is the "None" effect: the spec (App. A) has no real type 3 there,
# and the catalog ring has a per-slot "None" entry (slot<<24 | 3). AMP/DST/DLY/RVB/VOL
# have a real type 3, so a None block there is told apart only by engine 0x06.
NONE_TYPE = 3
NONE_SLOTS = (0, 1, 2, 4, 6, 7, 8)


def _load_engines() -> List[Tuple[Optional[int], Dict[int, int]]]:
    """patch/gp150_engines.json (scripts/gp150_engines.py) -> per slot (default, {type: engine})."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gp150_engines.json")) as fh:
        t = json.load(fh)
    rows = t["slots"]
    if [r["slot"] for r in rows] != SLOTS:
        raise ValueError("gp150_engines.json: slots do not match SLOTS")
    return [(r["default"], {int(k): int(v) for k, v in r["engines"].items()}) for r in rows]


# Per slot: (engine for a type the corpus never shows — None when no real effect was
# ever seen there, i.e. WAH — and {type: engine} learned from the corpus).
ENGINES = _load_engines()


def detect(b: bytes) -> bool:
    return len(b) == PRST_LEN and bytes(b[:4]) == MAGIC


def _check(b: bytes) -> None:
    if len(b) != PRST_LEN:
        raise ValueError(f"expected a {PRST_LEN}-byte GP-150 .prst, got {len(b)}")


def read_index(b: bytes) -> int:
    return b[IDX_OFF]


def write_index(b: bytearray, index: int) -> None:
    if not 0 <= index <= 199:
        raise ValueError(f"preset index out of range: {index}")
    b[IDX_OFF] = index


def read_name(b: bytes) -> str:
    return bytes(b[NAME_OFF:NAME_OFF + NAME_LEN]).split(b"\0")[0].decode("latin1", "replace").strip()


def write_name(b: bytearray, name: str) -> None:
    raw = name.encode("latin1", "replace")[:NAME_LEN - 1]
    b[NAME_OFF:NAME_OFF + NAME_LEN] = raw.ljust(NAME_LEN, b"\0")


def read_vol_bpm(b: bytes) -> Tuple[int, int]:
    return b[VOL_OFF], b[BPM_OFF]


def write_vol_bpm(b: bytearray, vol: Optional[int] = None, bpm: Optional[int] = None) -> None:
    if vol is not None:
        b[VOL_OFF] = max(0, min(100, int(vol)))
    if bpm is not None:
        b[BPM_OFF] = max(40, min(255, int(bpm)))  # one byte at 0x24 (0x25 is 0 in every scanned preset); clamp, never truncate


def read_order(b: bytes) -> List[int]:
    return list(b[ORDER_OFF:ORDER_OFF + N_BLOCKS])


def _record_off(slot: int) -> int:
    """Offset of `slot`'s block record: its fixed home index, never the chain position."""
    if not 0 <= slot < N_BLOCKS:
        raise ValueError(f"block slot out of range: {slot}")
    return BLOCKS_OFF + DEFAULT_POS[slot] * BLOCK_LEN


def _check_order(order: Sequence[int]) -> None:
    if sorted(order) != list(range(N_BLOCKS)):
        raise ValueError("chain order must be a permutation of 0..11")


def block_of(b: bytes, slot: int) -> Dict:
    """`slot`'s block, read from its home record; `pos` = its chain position (order table)."""
    o = _record_off(slot)
    order = read_order(b)
    _check_order(order)
    raw = bytes(b[o:o + BLOCK_LEN])
    return {
        "slot": slot, "rec": DEFAULT_POS[slot], "pos": order.index(slot),
        "enabled": raw[0], "type": raw[4], "subtype": raw[5], "ext": raw[6],
        "engine": raw[7], "params": list(struct.unpack_from("<15f", raw, 8)),
    }


def blocks_by_slot(b: bytes) -> List[Dict]:
    return [block_of(b, s) for s in range(N_BLOCKS)]


def set_block(b: bytearray, slot: int, enabled=None, type=None, subtype=None, ext=None, engine=None) -> None:
    o = _record_off(slot)
    for off, val in ((0, enabled), (4, type), (5, subtype), (6, ext), (7, engine)):
        if val is not None:
            b[o + off] = int(val) & 0xFF


def set_param(b: bytearray, slot: int, i: int, value: float) -> None:
    if not 0 <= i < N_PARAMS:
        raise ValueError(f"param index out of range: {i}")
    struct.pack_into("<f", b, _record_off(slot) + 8 + i * 4, float(value))


def write_order(b: bytearray, order: Sequence[int]) -> None:
    """Reorder the chain: rewrites ONLY the order table (block records never move —
    the pedal's own reorder changes nothing else). order[0] must be AMP (the pedal
    mutes otherwise) and order[11] VOL (every corpus file; untested elsewhere)."""
    order = [int(x) for x in order]
    _check_order(order)
    if order[0] != AMP_SLOT:
        raise ValueError("chain position 0 must be AMP (slot 5)")
    if order[-1] != VOL_SLOT:
        raise ValueError("chain position 11 must be VOL (slot 11)")
    b[ORDER_OFF:ORDER_OFF + N_BLOCKS] = bytes(order)


def slot_engine(slot: int, type_: int) -> int:
    """Engine byte for effect `type_` in `slot`: the "None" model is 0x06; else the
    corpus engine of (slot, type), or the slot's most common engine for a type the
    corpus never shows. Refuses a slot whose real engine is unknown (WAH: no preset
    in the corpus holds a real wah) — a wrong engine silences the preset on the pedal."""
    if not 0 <= int(slot) < N_BLOCKS:
        raise ValueError(f"block slot out of range: {slot}")
    slot, type_ = int(slot), int(type_)
    if slot in NONE_SLOTS and type_ == NONE_TYPE:
        return ENGINE_BYPASS
    default, by_type = ENGINES[slot]
    engine = by_type.get(type_, default)
    if engine is None:
        raise ValueError(f"no engine byte is known for a {SLOTS[slot]} effect (type {type_}) on the GP-150: "
                         f"no preset read so far uses one — set it on the pedal, save and rescan")
    return engine


def is_none_model(slot: int, key: int) -> bool:
    """True for the ring's per-slot "None" entry (type 3 in a NONE_SLOTS slot)."""
    return int(slot) in NONE_SLOTS and (int(key) & 0xFF) == NONE_TYPE


def model_key(slot: int, type_: int, subtype: int = 0, ext: int = 0) -> int:
    return (slot << 24) | (ext << 16) | (subtype << 8) | type_


def model_records(b: bytes) -> List[Tuple[int, int, int]]:
    """Per slot (SLOT order): (type, slot, low) with low = ext<<16|subtype<<8|type,
    so (slot<<24)|low is the ring key — the shape patchlib expects ([idx, cat, fxlow])."""
    out = []
    for s, blk in enumerate(blocks_by_slot(b)):
        out.append((blk["type"], s, (blk["ext"] << 16) | (blk["subtype"] << 8) | blk["type"]))
    return out


def bypass_mask(b: bytes) -> int:
    mask = 0
    for s, blk in enumerate(blocks_by_slot(b)):
        if blk["enabled"]:
            mask |= 1 << s
    return mask


def param_floats(b: bytes) -> List[float]:
    out: List[float] = []
    for blk in blocks_by_slot(b):
        out.extend(blk["params"])
    return out


def apply_edits(prst: bytes, edits: Optional[Dict]) -> bytes:
    """Explorer edit spec -> new bytes. Keys index blocks by SLOT (0..11); every edit
    addresses the slot's home record. "order" rewrites only the order table (records
    and engines untouched). A "models" edit writes the type and the slot's engine for
    it (slot_engine); picking a "None" model also turns the block off unless the edit's
    bypass says otherwise. Device-owned bytes (0x0D..0x0F, 0x43C, 0x445) are never written."""
    _check(prst)
    edits = edits or {}
    b = bytearray(prst)
    if edits.get("order") is not None:
        write_order(b, edits["order"])
    for slot, key in (edits.get("models") or {}).items():
        slot, key = int(slot), int(key)
        type_ = key & 0xFF
        engine = slot_engine(slot, type_)
        set_block(b, slot, type=type_, subtype=(key >> 8) & 0xFF, ext=(key >> 16) & 0xFF, engine=engine)
        if slot == AMP_SLOT and type_ in (2, 7):
            set_block(b, slot, ext=1)
        if slot == 4 and type_ == 0:
            set_block(b, slot, subtype=1)  # empty N->S carries subtype 1 (spec App. A)
        if is_none_model(slot, key):
            set_block(b, slot, enabled=0)  # every None block in the corpus is off; a bypass edit below may override
    for slot, ps in (edits.get("params") or {}).items():
        for i, v in ps.items():
            set_param(b, int(slot), int(i), float(v))
    for slot, on in (edits.get("bypass") or {}).items():
        set_block(b, int(slot), enabled=1 if on else 0)
    s = edits.get("settings") or {}
    write_vol_bpm(b, s.get("patch_vol"), s.get("bpm"))
    if edits.get("name") is not None:
        write_name(b, str(edits["name"]))
    return bytes(b)


# Factory preset 1 ("New GEN.") as read from a GP-150 (re/gp150/evidence/000-New_GEN.prst),
# base64. Used as the "cleared slot" content; only the index byte is changed.
BLANK_B64 = (
    "ETBkBAAAAAAQMFgEABVIPP//DAAQDA8DAwMEECgAAMggMFAAeAAyAAAAAABOZXcgR0VOLgAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwMDwDBQABAgMEBgcICQoLAAAA"
    "AAMAAAYAAMhCAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAQAABQAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEA"
    "AAAAAAADAAAgQQAAjEIAAKBCAACAPwAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAMAAAYAAEhCAABIQgAASEIAAEhCAABIQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"
    "AAAAQAAABwAASEIAAEhCAABIQgAANEIAAEhCAABcQgAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "EAAAAhAAAAAAAgQQAAQEEAAIA/AABIQwAAyEIAAEDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AQAAABABABoAAAAAAACCQgAAAAAAAAAAAAAAAAAAmEEACDVGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAwAABgAAyEIAAAAAAAAAAAAAAAAAAAAAAABIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAADAAAGAADIQgAAAD8AAEhCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAQAAAAAAAAsAACBBAIDZQwAAoEEAAAAAAACAPwAASEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAABAAAAAQAADAAAcEEAAEhCAAAgQgAAgD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAEAAAADAAAGAADIQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAUDAMAAwAAAANAAAABAAAAGAwcAALAAAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIBAAAAAAAAAA"
    "AAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEL//wAAAAAAAAAAyEIAAAAA"
    "cDAEAAAAAACAMCQAdA4AAAQAAAAAAgAAAAAAAAAAAAABAAAAAQAAAAAAAAAAAAAA"
)


def blank(slot: int) -> bytes:
    b = bytearray(base64.b64decode(BLANK_B64))
    write_index(b, slot)
    return bytes(b)
