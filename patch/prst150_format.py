"""The 1128-byte GP-150 .prst container — single source of truth for its layout.

Layout (hardware-confirmed; see docs/superpowers/specs/2026-10-03-gp150-support-design.md §2
and the public GP150_PRST_FORMAT.md analysis):

  0x000  4   magic 11 30 64 04
  0x004  1   preset index 0..199
  0x00D  3   device-written field (not a checksum; zero is accepted) — leave as-is
  0x024  1   BPM (low byte of 40..300)
  0x026  1   patch volume 0..100
  0x02C  68  name, ASCII, NUL-terminated/padded
  0x078  12  chain order: order[pos] = slot index (SLOTS); order[0] is always AMP (5),
             order[11] always VOL (11) (203/203 corpus files)
  0x084  12 x 68  blocks IN CHAIN ORDER: [enabled][0 0 0][type][subtype][ext][engine] + 15 f32 LE
  0x3B4  180 footer: controller assignments (copied verbatim)

stdlib-only: imported by the web app oracle tests and the MIDI CLI alike.
"""
from __future__ import annotations

import base64
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
VOL_SLOT = 11  # pinned last: position 11's engine is 0x06, the "None" engine (see write_order)
MOVABLE = [s for s in SLOTS if s not in ("AMP", "VOL")]
# The order table of an unreordered preset. Record r always holds slot DEFAULT_ORDER[r]:
# a slot's record sits at its HOME index DEFAULT_POS[slot], whatever the order table says.
DEFAULT_ORDER = [AMP_SLOT, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, VOL_SLOT]
DEFAULT_POS = [DEFAULT_ORDER.index(s) for s in range(len(SLOTS))]

# spec §6: canonical engine per chain POSITION (position 0 = AMP is computed)
CANONICAL_ENGINE = [None, 0x05, 0x03, 0x07, 0x07, 0x00, 0x1A, 0x01, 0x04, 0x0B, 0x0C, 0x06]
# spec §6.3: (slot, type) -> engine overrides
ENGINE_OVERRIDES = {
    (0, 16): 0x1A,
    (3, 117): 0x08, (3, 118): 0x08, (3, 122): 0x08,
    (4, 57): 0x07, (4, 64): 0x07, (4, 122): 0x08, (4, 112): 0x1A,
    (6, 60): 0x0A,
    (1, 26): 0x00,
    (8, 41): 0x01, (8, 54): 0x01,
    (7, 25): 0x04,
}
ENGINE_BYPASS = 0x06  # engine of the "None" effect, at any chain position (corpus); VOL's
# real type 3 ("Volume") also carries it, at position 11
# Slots whose type 3 is the "None" effect: the spec (App. A) has no real type 3 there,
# and the catalog ring has a per-slot "None" entry (slot<<24 | 3). AMP/DST/DLY/RVB/VOL
# have a real type 3, so a None block there is told apart only by engine 0x06.
NONE_TYPE = 3
NONE_SLOTS = (0, 1, 2, 4, 6, 7, 8)


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
        b[BPM_OFF] = int(bpm) & 0xFF  # only the low byte is stored (spec §2)


def read_order(b: bytes) -> List[int]:
    return list(b[ORDER_OFF:ORDER_OFF + N_BLOCKS])


def _block_off(pos: int) -> int:
    if not 0 <= pos < N_BLOCKS:
        raise ValueError(f"block position out of range: {pos}")
    return BLOCKS_OFF + pos * BLOCK_LEN


def block_at(b: bytes, pos: int) -> Dict:
    o = _block_off(pos)
    raw = bytes(b[o:o + BLOCK_LEN])
    return {
        "pos": pos, "enabled": raw[0], "type": raw[4], "subtype": raw[5], "ext": raw[6],
        "engine": raw[7], "params": list(struct.unpack_from("<15f", raw, 8)),
    }


def blocks_by_slot(b: bytes) -> List[Dict]:
    order = read_order(b)
    out: List[Optional[Dict]] = [None] * N_BLOCKS
    for pos, slot in enumerate(order):
        out[slot] = block_at(b, pos)
    if any(x is None for x in out):
        raise ValueError("chain order is not a permutation of 0..11")
    return out  # type: ignore[return-value]


def set_block(b: bytearray, pos: int, enabled=None, type=None, subtype=None, ext=None, engine=None) -> None:
    o = _block_off(pos)
    for off, val in ((0, enabled), (4, type), (5, subtype), (6, ext), (7, engine)):
        if val is not None:
            b[o + off] = int(val) & 0xFF


def set_param(b: bytearray, pos: int, i: int, value: float) -> None:
    if not 0 <= i < N_PARAMS:
        raise ValueError(f"param index out of range: {i}")
    struct.pack_into("<f", b, _block_off(pos) + 8 + i * 4, float(value))


def write_order(b: bytearray, order: Sequence[int]) -> None:
    """Reorder the chain: moves each slot's 68-byte block to its new position and
    rewrites the order table. order[0] must be AMP (the pedal mutes otherwise) and
    order[11] VOL: every corpus file has it there, and position 11's engine is 0x06,
    so a real effect moved there would read back as "None"."""
    order = [int(x) for x in order]
    if sorted(order) != list(range(N_BLOCKS)):
        raise ValueError("chain order must be a permutation of 0..11")
    if order[0] != AMP_SLOT:
        raise ValueError("chain position 0 must be AMP (slot 5)")
    if order[-1] != VOL_SLOT:
        raise ValueError("chain position 11 must be VOL (slot 11)")
    cur = read_order(b)
    blocks = {slot: bytes(b[_block_off(pos):_block_off(pos) + BLOCK_LEN]) for pos, slot in enumerate(cur)}
    for pos, slot in enumerate(order):
        o = _block_off(pos)
        b[o:o + BLOCK_LEN] = blocks[slot]
    b[ORDER_OFF:ORDER_OFF + N_BLOCKS] = bytes(order)


def engine_for(pos: int, slot: int, type_: int, params: Sequence[float]) -> int:
    """Engine byte for a block at chain position `pos`. Engines are written by the
    firmware (a disabled block keeps its engine); this is only used when a block's
    type or position changes. AMP (pos 0) rules are best effort (spec 6-7); else the
    (slot, type) overrides; else the canonical engine of the chain position. The
    "None" model (type 3 in NONE_SLOTS) is engine 0x06 wherever it sits."""
    if slot in NONE_SLOTS and type_ == NONE_TYPE:
        return ENGINE_BYPASS
    if pos == 0:
        p = list(params) + [0.0] * N_PARAMS
        if type_ == 33 or type_ in (2, 7):
            return 0x01
        if type_ == 9:
            return 0x03
        if type_ == 1:
            if p[4] != 0:
                return 0x01
            return 0x03 if p[3] >= 50 else 0x00
        return 0x01 if (p[4] != 0 or p[5] != 0) else 0x00
    if (slot, type_) in ENGINE_OVERRIDES:
        return ENGINE_OVERRIDES[(slot, type_)]
    return CANONICAL_ENGINE[pos]


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


def _refresh_engine(b: bytearray, pos: int, slot: int) -> None:
    blk = block_at(b, pos)
    set_block(b, pos, engine=engine_for(pos, slot, blk["type"], blk["params"]))


def apply_edits(prst: bytes, edits: Optional[Dict]) -> bytes:
    """Explorer edit spec -> new bytes. Keys index blocks by SLOT (0..11). Engine
    bytes are recomputed only for blocks whose model changed or that moved; a "None"
    block (type 3 + engine 0x06, not VOL) that only moved keeps engine 0x06. Picking
    a "None" model also turns the block off unless the edit's bypass says otherwise."""
    _check(prst)
    edits = edits or {}
    b = bytearray(prst)
    old_order = read_order(b)
    if edits.get("order") is not None:
        write_order(b, edits["order"])
    order = read_order(b)
    moved = set(slot for pos, slot in enumerate(order) if old_order.index(slot) != pos)
    remodeled = set()
    pos_of = {slot: pos for pos, slot in enumerate(order)}
    for slot, key in (edits.get("models") or {}).items():
        key = int(key)
        pos = pos_of[int(slot)]
        remodeled.add(int(slot))
        set_block(b, pos, type=key & 0xFF, subtype=(key >> 8) & 0xFF, ext=(key >> 16) & 0xFF)
        if int(slot) == AMP_SLOT and (key & 0xFF) in (2, 7):
            set_block(b, pos, ext=1)
        if int(slot) == 4 and (key & 0xFF) == 0:
            set_block(b, pos, subtype=1)  # empty N->S carries subtype 1 (spec App. A)
        if is_none_model(int(slot), key):
            set_block(b, pos, enabled=0)  # every None block in the corpus is off; a bypass edit below may override
    for slot, ps in (edits.get("params") or {}).items():
        for i, v in ps.items():
            set_param(b, pos_of[int(slot)], int(i), float(v))
    for slot, on in (edits.get("bypass") or {}).items():
        set_block(b, pos_of[int(slot)], enabled=1 if on else 0)
    s = edits.get("settings") or {}
    write_vol_bpm(b, s.get("patch_vol"), s.get("bpm"))
    if edits.get("name") is not None:
        write_name(b, str(edits["name"]))
    for slot in sorted(moved | remodeled):
        pos = pos_of[slot]
        o = _block_off(pos)
        if slot not in remodeled and slot != VOL_SLOT and b[o + 4] == NONE_TYPE and b[o + 7] == ENGINE_BYPASS:
            continue  # a "None" block that only moved stays None (engine 0x06 at any position)
        _refresh_engine(b, pos, slot)
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
