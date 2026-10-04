"""GP-150 / GP-180 'HT' SysEx protocol — framing, CRCs, requests, chunk streams.

Pure stdlib. Verified against 8,980 captured Valeton Suite <-> GP-180 messages and
live against a GP-150 (re/gp150/). Wire shape of every message:

    F0 7F <ocrc> <family> <t0 t1 t2 t3> <body...> F7

ocrc = CRC-8 poly 0x31 (init 0) over family..end-of-body, & 0x7F.

Short messages: body = 00 + nibbles(logical); logical = [01][icrc][len u16 LE][payload]
with icrc = CRC-8/0x31 over payload (unmasked).

Chunk streams (families 0x70 patch, 0x2C device-save, 0x24 file, 0x10 firmware):
t0 = 0x08, t1/t2 = 7-bit offset (119 x preceding chunks), t3 = transfer id,
body = [chunk index][nibbles(119-byte piece)]. 248 bytes per full chunk, final
chunk shorter. The concatenated decoded pieces form one logical message.
"""
from __future__ import annotations

from typing import List, NamedTuple, Tuple

POLY = 0x31
CHUNK = 119  # decoded bytes per full chunk (238 nibbles); captured final chunk: 65
OFFSET_STEP = 119  # offset field advances by this per chunk (7-bit split)
FULL_WIRE_LEN = 248
SLOT_ACTIVE = 0xFFFF
FAMILY_ACK = 0x00
FAMILY_PRESET_REQ = 0x0F
FAMILY_PATCH = 0x70
FAMILY_IMPORT_DONE = 0x08
FAMILY_SESSION = 0x0C  # host -> device "session open" (the Suite's settings read); needed after a power cycle
FAMILY_IDENT = 0x10  # device -> host ident reply to the session open (tx 1; the host ACKs it)
SESSION_OPEN_PAYLOAD = bytes((0x03, 0x01, 0x70, 0x10, 0x70, 0x10, 0x02, 0x00))
PRESET_LEN = 1128
PAYLOAD_HEAD_EXPORT = b"\x03\x03\x11\x30"
PAYLOAD_HEAD_IMPORT = b"\x01\x03\x11\x30"
IMPORT_BYTE_0A = 0x5C  # the exported file carries 0x58 here; Suite sends 0x5C on import
IMPORT_DONE_PAYLOAD = b"\x09\x03\x11\x30"  # logical payload of the pedal's 0x08 after an import


def crc8_31(data: bytes, init: int = 0) -> int:
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ POLY) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def ocrc(after_crc: bytes) -> int:
    return crc8_31(after_crc) & 0x7F


def icrc(payload: bytes) -> int:
    return crc8_31(payload)


def enc(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        out += bytes((b >> 4, b & 0x0F))
    return bytes(out)


def dec(nibbles: bytes) -> bytes:
    if len(nibbles) % 2:
        raise ValueError("nibble stream has odd length")
    if any(n > 0x0F for n in nibbles):
        raise ValueError("nibble stream contains a byte above 0x0F")
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


def logical(payload: bytes) -> bytes:
    n = len(payload)
    return bytes((0x01, icrc(payload), n & 0xFF, (n >> 8) & 0xFF)) + payload


def parse_logical(data: bytes) -> bytes:
    if len(data) < 4:
        raise ValueError("logical message too short")
    n = data[2] | (data[3] << 8)
    payload = data[4:4 + n]
    if len(payload) != n:
        raise ValueError(f"logical length {n} but only {len(payload)} bytes present")
    if icrc(payload) != data[1]:
        raise ValueError(f"inner CRC mismatch: {data[1]:#04x} != {icrc(payload):#04x}")
    return payload


class Frame(NamedTuple):
    family: int
    tx4: bytes  # raw[4:8]
    body: bytes  # raw[8:-1]


def frame(family: int, tx4: bytes, body: bytes) -> bytes:
    if len(tx4) != 4:
        raise ValueError("tx4 must be 4 bytes")
    after = bytes((family,)) + tx4 + body
    return b"\xf0\x7f" + bytes((ocrc(after),)) + after + b"\xf7"


def parse_frame(wire: bytes) -> Frame:
    if len(wire) < 9 or wire[:2] != b"\xf0\x7f" or wire[-1] != 0xF7:
        raise ValueError("not an F0 7F ... F7 HT frame")
    if ocrc(wire[3:-1]) != wire[2]:
        raise ValueError(f"outer CRC mismatch: {wire[2]:#04x} != {ocrc(wire[3:-1]):#04x}")
    return Frame(wire[3], wire[4:8], wire[8:-1])


def is_chunk(f: Frame) -> bool:
    return f.tx4[0] != 0


def chunk_fields(f: Frame) -> Tuple[int, int, int, bytes]:
    """(offset, transfer_id, chunk_index, decoded piece) of a chunk frame."""
    offset = (f.tx4[1] & 0x7F) | ((f.tx4[2] & 0x7F) << 7)
    return offset, f.tx4[3], f.body[0], dec(f.body[1:])


def short_payload(f: Frame) -> bytes:
    """The logical payload of a short (non-chunk) message. Body = one flag byte +
    nibbles(logical): the host sends flag 00; the pedal's captured 0x08 import
    notification carries 01."""
    if is_chunk(f) or len(f.body) < 9:
        raise ValueError("not a short HT message")
    return parse_logical(dec(f.body[1:]))


def short_message(family: int, tx_id: int, payload: bytes) -> bytes:
    return frame(family, bytes((0, 0, 0, tx_id & 0xFF)), b"\x00" + enc(logical(payload)))


def session_open() -> bytes:
    """The Suite's family-0x0C message sent right after the handshake. A power-cycled
    GP-150 ACKs preset reads but streams nothing until it has seen this (2026-10-04);
    the pedal answers with an ACK (tx 0) and an ident reply (family 0x10, tx 1)."""
    return short_message(FAMILY_SESSION, 0, SESSION_OPEN_PAYLOAD)


def is_ident_reply(f: "Frame") -> bool:
    return f.family == FAMILY_IDENT and not is_chunk(f)


def hello() -> bytes:
    return frame(FAMILY_ACK, bytes((0x00, 0x01, 0x03, 0x00)), b"\x00")


def ack(id_: int) -> bytes:
    return frame(FAMILY_ACK, bytes((0, 0, 0, id_ & 0xFF)), b"\x00")


def preset_request(tx_id: int, slot: int, select: bool = False) -> bytes:
    """Family 0x0F. select=False (flag 01) reads the slot without changing the
    active preset; select=True (flag 00) makes the pedal switch to it. slot
    SLOT_ACTIVE reads whatever is active."""
    if not (0 <= slot <= 0xFFFF):
        raise ValueError(f"slot out of range: {slot}")
    payload = bytes((0x03, 0x03, 0x11, 0x30, 0x11, 0x30, 0x02, 0x00,
                     slot & 0xFF, (slot >> 8) & 0xFF, 0x00 if select else 0x01))
    return short_message(FAMILY_PRESET_REQ, tx_id, payload)


def assemble_stream(frames: List[Frame]) -> Tuple[int, bytes]:
    """Validate + join a chunk stream -> (transfer_id, logical payload)."""
    if not frames:
        raise ValueError("empty stream")
    parts = sorted((chunk_fields(f) for f in frames), key=lambda t: t[2])
    tids = {p[1] for p in parts}
    if len(tids) != 1:
        raise ValueError(f"mixed transfer ids {sorted(tids)}")
    base = parts[0][2]
    data = bytearray()
    for n, (offset, _tid, idx, piece) in enumerate(parts):
        if idx != base + n:
            raise ValueError(f"chunk index gap at {idx} (expected {base + n})")
        if offset != OFFSET_STEP * n:
            raise ValueError(f"chunk offset {offset} != {OFFSET_STEP * n}")
        if n < len(parts) - 1 and len(piece) != CHUNK:
            raise ValueError(f"short chunk {idx} before the final chunk")
        data += piece
    if len(parts[-1][3]) == CHUNK:
        raise ValueError("stream has no final (short) chunk")
    return parts[0][1], parse_logical(bytes(data))


def preset_from_payload(payload: bytes) -> bytes:
    if payload[:4] not in (PAYLOAD_HEAD_EXPORT, PAYLOAD_HEAD_IMPORT):
        raise ValueError(f"unexpected preset payload head {payload[:4].hex()}")
    prst = payload[4:4 + PRESET_LEN]
    if len(prst) != PRESET_LEN:
        raise ValueError(f"preset payload is {len(prst)} bytes, expected {PRESET_LEN}")
    return prst


def import_payload(prst: bytes) -> bytes:
    if len(prst) != PRESET_LEN:
        raise ValueError(f"expected a {PRESET_LEN}-byte .prst, got {len(prst)}")
    b = bytearray(prst)
    b[0x0A] = IMPORT_BYTE_0A
    return PAYLOAD_HEAD_IMPORT + bytes(b)


def chunk_frames(family: int, transfer_id: int, data: bytes, host: bool = True) -> List[bytes]:
    """Split a logical message into chunk frames. Host->device chunk indexes are
    0-based; device->host (used only to rebuild captures) are 1-based."""
    out = []
    pieces = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    if len(pieces[-1]) == CHUNK:
        pieces.append(b"")  # never emit a full-size final chunk (the parser would wait)
    for n, piece in enumerate(pieces):
        off = OFFSET_STEP * n
        tx4 = bytes((0x08, off & 0x7F, (off >> 7) & 0x7F, transfer_id & 0xFF))
        idx = n if host else n + 1
        out.append(frame(family, tx4, bytes((idx,)) + enc(piece)))
    return out


def import_stream(transfer_id: int, prst: bytes) -> List[bytes]:
    """The Suite's patch-import stream for writing `prst` to the slot named by its
    own index byte (prst[4]). NOT verified on a GP-150 yet — gated by
    device_write.WRITE_VERIFIED['gp150']."""
    return chunk_frames(FAMILY_PATCH, transfer_id, logical(import_payload(prst)), host=True)
