#!/usr/bin/env python3
"""GP-50 host->device write TRANSPORT (packet builder) + a hard-gated sender.

GP-150 (HT transport): a whole-preset import over a family-0x70 chunk stream,
built byte-for-byte like Valeton Suite's captured import
(build_gp150_write_stream / validate_gp150_stream); send_stream() sends it only
while WRITE_VERIFIED["gp150"] is True (flipped 2026-10-04 after the supervised
hardware write, re/gp150/DEVICE_WRITE.md).
The GP-5/GP-50 (0x1D) description below is unchanged.

The wire format is cracked (see re/SNAPTONE_PROTOCOL.md): each packet is
  BUF = [crc, cmd, index, length, *payload]     # crc = CRC-8/0x07 over BUF, slot 0
  wire = F0 + nibble-expand(BUF, hi-first) + F7
This module builds byte-identical packets (verified below against a real Suite
capture) but DOES NOT send speculative writes: send_stream() refuses unless the
caller passes confirm=True AND every packet was validated against captured bytes.

SAFETY: the pedal wedged once from unvalidated traffic. Never send a guessed
write command. A patch write needs its command byte + slot addressing decoded
from a Suite patch-import capture first (see re/DEVICE_WRITE.md)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from typing import Dict, List, Optional, Tuple  # noqa: E402

from patch import ht_proto as ht  # noqa: E402
from patch import prst150_format as f150  # noqa: E402
from patch.prst_format import (  # noqa: E402
    GP150,
    NAME_OFF,
    DeviceProfile,
    check_length,
    crc8,
    detect,
)

# Which devices' WRITE protocol is capture-verified. The read protocol (0x40/0x41)
# is confirmed shared, and the .prst container + CRC are identical, so the write
# format below is very likely shared too — but the write COMMAND byte, the 0x11 0x4F
# header, and the 19-byte block size were only ever confirmed against GP-50 Suite
# captures. Until a GP-5 patch-import is captured, a GP-5 write reuses the GP-50
# constants on faith; send_stream() refuses it unless explicitly allowed. See
# re/DEVICE_WRITE.md. The GP-150 (HT import stream, below) is byte-exact against
# Suite's capture and passed the supervised hardware write on 2026-10-04
# (re/gp150/DEVICE_WRITE.md).
WRITE_VERIFIED = {"gp50": True, "gp5": False, "gp150": True}  # gp150: verified on hardware 2026-10-04 (re/gp150/DEVICE_WRITE.md)


def build_packet(cmd: int, index: int, payload: bytes) -> list:
    """Return wire bytes (incl F0/F7) for one host->device packet."""
    buf = [0, cmd & 0xFF, index & 0xFF, len(payload) & 0xFF, *payload]
    buf[0] = crc8(buf)
    wire = [0xF0]
    for b in buf:
        wire += [b >> 4, b & 0x0F]
    wire.append(0xF7)
    return wire


PATCH_WRITE_CMD = 0x1D  # host->device patch write (decoded from Suite import captures)
PATCH_BLOCK = 19  # payload bytes per write block
PATCH_HDR = bytes([0x11, 0x4F])  # constant marker before the slot byte


def _expected_payload_len(profile: DeviceProfile) -> int:
    """Reassembled write payload = 6-byte header + prst[NAME_OFF:]."""
    return 6 + (profile.prst_len - NAME_OFF)


def build_patch_write_stream(prst: bytes, slot: int) -> list:
    """Reconstruct Suite's exact patch-import stream for writing `prst` to `slot`.

    Validated byte-for-byte (29/29) against two real GP-50 Suite captures (US Lead
    -> slots 1 and 99). Device-agnostic in shape — the payload is prst[NAME_OFF:],
    so a 507-byte GP-5 .prst yields a shorter stream automatically. Format:
      device_payload = [0x11, 0x4F, slot, 0x00, 0x00, 0x00] + prst[0x19:]
      (the 6-byte header replaces the .prst body's leading FF FF FF FF sentinel;
       `slot` is the 0-based device index)
    streamed as PATCH_WRITE_CMD in 19-byte blocks, index 0..N.
    Returns wire-byte packets (each incl F0/F7). Does NOT send — see send_stream."""
    if not 0 <= slot <= 0xFF:
        raise ValueError(f"slot out of range: {slot}")
    profile = detect(prst)
    if profile.transport == "ht":  # GP-150: a whole-preset HT import, not 0x1D blocks
        return build_gp150_write_stream(prst, slot)
    check_length(prst, profile)  # a recognized GP-5 or GP-50 .prst
    payload = PATCH_HDR + bytes([slot, 0x00, 0x00, 0x00]) + prst[NAME_OFF:]
    return [
        build_packet(PATCH_WRITE_CMD, i // PATCH_BLOCK, payload[i : i + PATCH_BLOCK])
        for i in range(0, len(payload), PATCH_BLOCK)
    ]


# --- GP-150: whole-preset import over the HT protocol --------------------------

GP150_TRANSFER_ID = 0x24  # the transfer id Suite used in the captured import
GP150_PACE = 0.02  # s between import chunks (the pedal ACKs once, after the final one)
GP150_NOTIFY_TIMEOUT = 3.0  # s to wait for the 0x08 "import done" after the pedal's ACK


def build_gp150_write_stream(prst: bytes, slot: int, transfer_id: int = GP150_TRANSFER_ID) -> List[bytes]:
    """Suite's patch-import stream for writing a GP-150 `prst` to `slot`: family-0x70
    chunks (0-based indexes, 119 bytes each, a shorter final one) carrying
    [01][icrc][6c 04] + 01 03 11 30 + the file with byte 0x0A = 0x5C. The pedal takes
    the slot from the file's own index byte 0x04, so that byte is set to `slot` (on a
    copy). Byte-for-byte equal to the captured Suite import
    (app/tests/test_device_write_gp150.py). Builds only; send_stream sends."""
    if not f150.detect(prst):
        raise ValueError("not a GP-150 .prst (expected 1128 bytes starting 11 30 64 04)")
    if not (isinstance(slot, int) and 0 <= slot < GP150.slots):
        raise ValueError(f"slot out of range 0..{GP150.slots - 1}: {slot!r}")
    if not (isinstance(transfer_id, int) and 1 <= transfer_id <= 0x7F):
        raise ValueError(f"transfer id must be 1..0x7F (a SysEx data byte): {transfer_id!r}")
    b = bytearray(prst)
    f150.write_index(b, slot)
    return ht.import_stream(transfer_id, bytes(b))


def validate_gp150_stream(packets, slot: Optional[int] = None) -> Tuple[bool, str]:
    """Re-parse a GP-150 import stream before sending it (the GP-150 twin of
    validate_stream). Every frame: F0 7F .. F7, 7-bit data, outer CRC valid, a
    family-0x70 chunk, one transfer id, chunk index i (0-based, host->device) at
    offset 119*i, full-size except a shorter final chunk. The joined logical message:
    inner CRC valid, exactly 01 03 11 30 + a 1128-byte GP-150 preset with byte 0x0A
    = 0x5C and an index byte 0x04 that is a GP-150 slot (== `slot` when given).
    Returns (ok, reason)."""
    if not packets:
        return False, "empty stream"
    frames = []
    tid = None
    last = len(packets) - 1
    for i, w in enumerate(packets):
        w = bytes(w)
        if len(w) < 10 or w[0] != 0xF0 or w[1] != 0x7F or w[-1] != 0xF7:
            return False, f"packet {i}: not an F0 7F .. F7 HT frame"
        if any(b > 0x7F for b in w[1:-1]):
            return False, f"packet {i}: a data byte above 0x7F"
        try:
            f = ht.parse_frame(w)
            if f.family != ht.FAMILY_PATCH or f.tx4[0] != 0x08 or not f.body:
                return False, f"packet {i}: not a patch (0x70) chunk"
            offset, t, idx, _piece = ht.chunk_fields(f)
        except ValueError as e:
            return False, f"packet {i}: {e}"
        if t < 1:
            return False, f"packet {i}: transfer id 0 (must be 1..0x7F)"
        if tid is None:
            tid = t
        elif t != tid:
            return False, f"packet {i}: transfer id {t:#04x} != {tid:#04x}"
        if idx != i:
            return False, f"packet {i}: chunk index {idx} (expected {i}, 0-based)"
        if offset != ht.OFFSET_STEP * i:
            return False, f"packet {i}: offset {offset} != {ht.OFFSET_STEP * i}"
        if i < last and len(w) != ht.FULL_WIRE_LEN:
            return False, f"packet {i}: short chunk before the final one"
        if i == last and len(w) >= ht.FULL_WIRE_LEN:
            return False, "no final short chunk (the pedal would wait for more)"
        frames.append(f)
    try:
        _tid, payload = ht.assemble_stream(frames)
    except ValueError as e:
        return False, str(e)
    if payload[:4] != ht.PAYLOAD_HEAD_IMPORT:
        return False, f"payload head {payload[:4].hex()} is not the import head {ht.PAYLOAD_HEAD_IMPORT.hex()}"
    if len(payload) != 4 + ht.PRESET_LEN:
        return False, f"import payload is {len(payload)} bytes, expected {4 + ht.PRESET_LEN}"
    prst = payload[4:]
    if not f150.detect(prst):
        return False, "payload is not a GP-150 preset (no 11 30 64 04 magic)"
    if prst[0x0A] != ht.IMPORT_BYTE_0A:
        return False, f"byte 0x0A is {prst[0x0A]:#04x}; Suite's import sends {ht.IMPORT_BYTE_0A:#04x}"
    index = prst[f150.IDX_OFF]
    if index >= GP150.slots:
        return False, f"preset index byte {index} is not a GP-150 slot (0..{GP150.slots - 1})"
    if slot is not None and index != slot:
        return False, f"preset index byte {index} != target slot {slot}"
    return True, "ok"


def _read_back(slot: int) -> str:
    """The hint on every error that leaves the slot's state unknown."""
    return f"read slot {slot} back before retrying: it may or may not have been written"


def _is_import_done(f) -> bool:
    return f.family == ht.FAMILY_IMPORT_DONE and not ht.is_chunk(f)


def _send_gp150_stream(port_name, packets, allow_unverified, session, pace, notify_timeout):
    """The gated GP-150 import. Refuses unless WRITE_VERIFIED["gp150"] or
    allow_unverified; re-validates; then, in ONE Session.exchange: every chunk
    `pace` s apart, then waits for the pedal's ACK (id = transfer id) and its
    family-0x08 "import done" notification. The session ACKs that 0x08 itself (it
    ACKs every device message carrying a tx id) — nothing here ACKs it again.
    Raises RuntimeError unless the 0x08 arrives with Suite's payload 09 03 11 30."""
    if not (allow_unverified or WRITE_VERIFIED.get(GP150.key, False)):
        raise RuntimeError(
            "refusing to send: the GP-150 patch import (HT 0x70 stream) is not verified "
            "on hardware yet (WRITE_VERIFIED['gp150'] is False). Only the supervised "
            "verification write may pass allow_unverified=True."
        )
    packets = [bytes(p) for p in packets]
    ok, why = validate_gp150_stream(packets)
    if not ok:
        raise RuntimeError(f"refusing to send: the GP-150 import stream did not validate ({why})")
    slot = ht.preset_from_payload(ht.assemble_stream([ht.parse_frame(w) for w in packets])[1])[f150.IDX_OFF]
    tid = ht.parse_frame(packets[0]).tx4[3]
    from patch import ht_scan  # noqa: PLC0415 — keeps this module importable without the CLI

    s = session if session is not None else ht_scan.Session(port=port_name or ht_scan.PORT)
    try:
        r = s.exchange(packets[-1], lead=packets[:-1], pace=pace, ack_id=tid, until=_is_import_done,
                       idle=notify_timeout, timeout=s.timeout + notify_timeout)
    finally:
        if session is None:
            s.close()
    if r.error is not None:
        raise RuntimeError(f"GP-150 import to slot {slot} failed while sending ({r.error}) — {_read_back(slot)}")
    if any(_is_import_done(f) for f in r.frames[:r.before_last]):
        s.log("warn", "a 0x08 notification arrived before the import was complete (ignored)")
    notes = [f for f in r.frames[r.before_last:] if _is_import_done(f)]  # answers to the whole stream only
    if not notes:
        if not r.ack:
            raise RuntimeError(
                f"GP-150 did not ACK the import to slot {slot} ({ht_scan.NOT_RESPONDING}) — {_read_back(slot)}"
            )
        raise RuntimeError(
            f"GP-150 ACKed the import to slot {slot} but sent no 0x08 'import done' within "
            f"{notify_timeout:g} s — {_read_back(slot)}"
        )
    try:
        got = ht.short_payload(notes[0])
    except ValueError as e:
        raise RuntimeError(
            f"GP-150 sent an unreadable 0x08 notification after the import to slot {slot} ({e}) — {_read_back(slot)}"
        )
    if got != ht.IMPORT_DONE_PAYLOAD:
        raise RuntimeError(
            f"GP-150 answered the import to slot {slot} with an unexpected 0x08 payload {got.hex()} "
            f"(Suite capture: {ht.IMPORT_DONE_PAYLOAD.hex()}) — {_read_back(slot)}"
        )
    return {"sent": len(packets), "acks": 1 if r.ack else 0, "notified": True}


def _nib_decode(mid):
    return [(mid[i] << 4) | mid[i + 1] for i in range(0, len(mid) - 1, 2)]


def _legacy_payload_lens() -> Dict[int, DeviceProfile]:
    """Reassembled 0x1D payload length -> profile, for the legacy (GP-5/GP-50)
    transport only. The GP-150 never writes with 0x1D packets (it imports over HT,
    build_gp150_write_stream), so a 1109-byte 0x1D stream is not a GP-150 write."""
    from patch.prst_format import DEVICES

    return {_expected_payload_len(p): p for p in DEVICES.values() if p.transport != "ht"}


def _is_ht_stream(packets) -> bool:
    """An HT (GP-150) stream: F0 7F frames. Legacy packets are nibbles after F0."""
    return bool(packets) and len(packets[0]) > 1 and packets[0][0] == 0xF0 and packets[0][1] == 0x7F


def validate_stream(packets: list) -> tuple:
    """Confirm a patch-write stream is well-formed before sending (the gate for
    arbitrary edited patches, since they can't match a Suite capture). Checks every
    packet's CRC, that cmd is the patch-write command, indices are contiguous from 0,
    and the reassembled payload has the expected header + a length matching a known
    device (GP-50 or GP-5). Returns (ok, reason). A GP-150 (HT) stream is checked
    by validate_gp150_stream instead."""
    if _is_ht_stream(packets):
        return validate_gp150_stream(packets)
    payload = bytearray()
    for i, w in enumerate(packets):
        if not w or w[0] != 0xF0 or w[-1] != 0xF7:
            return False, f"packet {i}: not F0..F7 framed"
        buf = _nib_decode(w[1:-1])
        if len(buf) < 4:
            return False, f"packet {i}: truncated"
        crc, cmd, index, length = buf[0], buf[1], buf[2], buf[3]
        if crc8(buf[1:]) != crc:
            return False, f"packet {i}: bad CRC"
        if cmd != PATCH_WRITE_CMD:
            return (
                False,
                f"packet {i}: cmd {cmd:#04x} != patch-write {PATCH_WRITE_CMD:#04x}",
            )
        if index != i:
            return False, f"packet {i}: non-contiguous index {index}"
        if length != len(buf) - 4:
            return False, f"packet {i}: length {length} != payload {len(buf) - 4}"
        payload += bytes(buf[4 : 4 + length])
    valid_lens = {n: p.name for n, p in _legacy_payload_lens().items()}
    if len(payload) not in valid_lens:
        return (
            False,
            f"payload {len(payload)} bytes, expected one of "
            f"{sorted(valid_lens)} (GP-50/GP-5)",
        )
    if payload[:2] != PATCH_HDR:
        return False, f"payload header {payload[:2].hex()} != {PATCH_HDR.hex()}"
    return True, "ok"


def verify_against_capture(path: str) -> tuple:
    """Rebuild every host->device packet in a MIDI Monitor capture from its
    decoded (cmd,index,payload) and confirm it matches the captured wire bytes."""
    ok = bad = 0
    for ln in open(path, errors="ignore"):
        if "F7" not in ln:
            continue
        is_hd = ("To " in ln or "to " in ln) and "From" not in ln
        if not is_hd:
            continue
        m = re.search(r"F0((?:\s+[0-9A-Fa-f]{2})+)\s+F7", ln)
        if not m:
            continue
        wire = [0xF0] + [int(x, 16) for x in m.group(1).split()] + [0xF7]
        buf = _nib_decode(wire[1:-1])
        if len(buf) < 4:
            continue
        rebuilt = build_packet(buf[1], buf[2], bytes(buf[4 : 4 + buf[3]]))
        if rebuilt == wire:
            ok += 1
        else:
            bad += 1
    return ok, bad


def _infer_device_key(packets: list):
    """Best-effort device key from a stream's reassembled payload length, or None.
    Any HT (F0 7F) stream is a GP-150 write."""
    if _is_ht_stream(packets):
        return GP150.key
    total = 0
    for w in packets:
        if not w or w[0] != 0xF0 or w[-1] != 0xF7:
            return None
        buf = _nib_decode(w[1:-1])
        if len(buf) >= 4:
            total += buf[3]  # per-packet payload length
    p = _legacy_payload_lens().get(total)
    return p.key if p is not None else None


def send_stream(
    port_name,
    packets,
    confirm=False,
    validated=False,
    ack_wait=0.15,
    allow_unverified=False,
    session=None,
    pace=GP150_PACE,
    notify_timeout=GP150_NOTIFY_TIMEOUT,
):
    """Send pre-built, VALIDATED packets to the device. Refuses otherwise.

    packets: list of wire-byte lists. Requires confirm=True and validated=True.
    Also refuses a stream for a device whose write protocol is not capture-verified
    (WRITE_VERIFIED) unless allow_unverified=True — the GP-5 write command/header are
    assumed identical to the GP-50's but have never been confirmed against a GP-5
    capture, and blind-sending a wrong opcode has wedged this hardware before.
    Paces like Suite: after each block, wait for the device's ACK sysex (up to
    ack_wait s) before the next block — the device has a shallow MIDI queue and
    overrunning it has wedged the pedal. Returns the count of ACKs seen.

    A GP-150 (HT) stream goes to _send_gp150_stream instead: same confirm/validated
    rule, refused while WRITE_VERIFIED["gp150"] is False unless allow_unverified;
    `session` (an open ht_scan.Session — reuse it, a second session on the same
    port would double-ACK) or else `port_name` (default "GP-150"); chunks `pace` s
    apart; then the ACK + 0x08 within the session timeout + `notify_timeout`.
    Returns {"sent", "acks", "notified"}; raises RuntimeError otherwise."""
    if not (confirm and validated):
        raise RuntimeError(
            "refusing to send: device writes require confirm=True and packets "
            "validated byte-for-byte against a Suite capture (see re/DEVICE_WRITE.md)"
        )
    if _is_ht_stream(packets):
        return _send_gp150_stream(port_name, packets, allow_unverified, session, pace, notify_timeout)
    if not allow_unverified:
        key = _infer_device_key(packets)
        if key is None:
            raise RuntimeError(
                "refusing to send: unrecognized patch-write stream (its payload length "
                "matches no GP-5/GP-50 preset) — build it with build_patch_write_stream"
            )
        if not WRITE_VERIFIED.get(key, False):
            raise RuntimeError(
                f"refusing to send: the {key} patch-write protocol is not "
                f"capture-verified (write command/header assumed from the GP-50). "
                f"Capture a GP-5 Suite import to confirm, or pass "
                f"allow_unverified=True to override at your own risk."
            )
    import time
    import mido  # noqa: local import so the builder works without MIDI installed

    acks = 0
    with mido.open_input(port_name) as inp, mido.open_output(port_name) as out:
        time.sleep(0.1)
        for _ in inp.iter_pending():
            pass  # drain
        for w in packets:
            out.send(mido.Message("sysex", data=w[1:-1]))
            t0 = time.time()
            while time.time() - t0 < ack_wait:
                if any(m.type == "sysex" for m in inp.iter_pending()):
                    acks += 1
                    break
                time.sleep(0.005)
    return acks


if __name__ == "__main__":
    cap = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "/Users/drewmerc/Desktop/valeton_import_capture.txt"
    )
    ok, bad = verify_against_capture(cap)
    print(
        f"builder reproduces captured host->device packets: {ok} ok, {bad} mismatched"
    )
