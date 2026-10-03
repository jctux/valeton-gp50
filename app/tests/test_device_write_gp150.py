"""patch/device_write.py — the GP-150 write path (HT 0x70 import stream), gated.

The Suite capture in app/tests/fixtures/gp150/ht_corpus.json is the byte-exact
oracle: Suite imported the file it had just exported from slot 1 (index 0), so
build_gp150_write_stream(that file, 0) must reproduce every captured wire frame,
including the 0x0A = 0x5C byte and both CRCs. Sending is exercised only against
a scripted fake pedal (no MIDI ports): WRITE_VERIFIED["gp150"] stays False until
Task 12's supervised hardware write, so send_stream refuses without
allow_unverified=True. Python 3.9-compatible (also run under .venv-midi).
"""
import json
import os
import time

import pytest

from patch import device_write as dw
from patch import ht_proto as ht
from patch import ht_scan
from patch import prst150_format as f150

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = json.load(open(os.path.join(ROOT, "app", "tests", "fixtures", "gp150", "ht_corpus.json")))
MSG = {m["id"]: bytes.fromhex(m["raw"]) for m in FIX["messages"]}
EVID = os.path.join(ROOT, "re", "gp150", "evidence")
GP5 = os.path.join(ROOT, "app", "tests", "fixtures", "gp5", "65-Puppy.prst")
NOTIFY_TX = 0x0D  # tx id of the captured 0x08 import notification


def stream(prefix):
    keys = sorted((k for k in MSG if k.startswith(prefix)), key=lambda k: int(k.rsplit("_", 1)[1]))
    return [MSG[k] for k in keys]


def captured_import():
    return stream("import_stream_slot1_")


def exported_prst():
    frames = [ht.parse_frame(w) for w in stream("export_stream_slot1_")]
    return ht.preset_from_payload(ht.assemble_stream(frames)[1])


def evidence(name):
    with open(os.path.join(EVID, name), "rb") as fh:
        return fh.read()


def finger():
    return evidence("099-Finger_AC.prst")


def reframe(wire, piece=None, idx=None, tid=None, off=None, family=None):
    """Rebuild one chunk frame with some fields changed (outer CRC recomputed)."""
    f = ht.parse_frame(wire)
    offset, t, i, p = ht.chunk_fields(f)
    piece = p if piece is None else piece
    idx = i if idx is None else idx
    tid = t if tid is None else tid
    off = offset if off is None else off
    tx4 = bytes((0x08, off & 0x7F, (off >> 7) & 0x7F, tid))
    return ht.frame(f.family if family is None else family, tx4, bytes((idx,)) + ht.enc(piece))


# --- build: byte-exact against the Suite capture ------------------------------------

def test_build_matches_suite_import_capture():
    prst = exported_prst()
    assert prst[0x04] == 0 and prst[0x0A] == 0x58  # the exported file, as Suite saved it
    ours = dw.build_gp150_write_stream(prst, 0, transfer_id=0x24)
    theirs = captured_import()
    assert len(ours) == len(theirs) == 10
    for i, (a, b) in enumerate(zip(ours, theirs)):
        assert a == b, f"chunk {i} differs"
    assert prst[0x0A] == 0x58  # the caller's bytes are not mutated


def test_build_patch_write_stream_dispatches_gp150():
    prst = finger()
    assert dw.build_patch_write_stream(prst, 7) == dw.build_gp150_write_stream(prst, 7)
    assert dw.build_patch_write_stream(exported_prst(), 0) == captured_import()


def test_build_sets_slot_byte_and_validates():
    prst = finger()
    assert prst[4] == 99
    pk = dw.build_gp150_write_stream(prst, 199)
    ok, msg = dw.validate_gp150_stream(pk)
    assert ok, msg
    assert dw.validate_gp150_stream(pk, slot=199) == (True, "ok")
    _tid, payload = ht.assemble_stream([ht.parse_frame(p) for p in pk])
    sent = ht.preset_from_payload(payload)
    assert sent[4] == 199 and sent[0x0A] == ht.IMPORT_BYTE_0A
    diff = [i for i in range(len(prst)) if sent[i] != prst[i]]
    assert diff == [0x04, 0x0A]  # only the index byte and the import marker change
    assert all(len(w) == ht.FULL_WIRE_LEN for w in pk[:-1]) and len(pk[-1]) < ht.FULL_WIRE_LEN
    assert all(w[0] == 0xF0 and w[-1] == 0xF7 and all(b <= 0x7F for b in w[1:-1]) for w in pk)


def test_build_refuses_bad_input():
    prst = finger()
    for bad_slot in (-1, 200, 256):
        with pytest.raises(ValueError):
            dw.build_gp150_write_stream(prst, bad_slot)
    with pytest.raises(ValueError):
        dw.build_gp150_write_stream(open(GP5, "rb").read(), 3)  # a GP-5 file
    broken = bytearray(prst)
    broken[0] ^= 0xFF  # 1128 bytes but no magic
    with pytest.raises(ValueError):
        dw.build_gp150_write_stream(bytes(broken), 3)
    for bad_tid in (0, 0x80, 0xFF):  # transfer ids travel as a SysEx data byte
        with pytest.raises(ValueError):
            dw.build_gp150_write_stream(prst, 3, transfer_id=bad_tid)


# --- validate: every frame re-parsed ---------------------------------------------

def good_stream():
    return dw.build_gp150_write_stream(finger(), 199)


def test_validate_rejects_a_mutated_outer_crc():
    pk = good_stream()
    bad = bytearray(pk[3])
    bad[2] ^= 0x01
    ok, why = dw.validate_gp150_stream(pk[:3] + [bytes(bad)] + pk[4:])
    assert ok is False and "packet 3" in why
    bad = bytearray(pk[3])
    bad[20] ^= 0x01  # a nibble changed, outer CRC left stale
    assert dw.validate_gp150_stream(pk[:3] + [bytes(bad)] + pk[4:])[0] is False


def test_validate_rejects_a_mutated_inner_crc():
    pk = good_stream()
    _off, _tid, _idx, piece = ht.chunk_fields(ht.parse_frame(pk[3]))
    flipped = bytearray(piece)
    flipped[10] ^= 0x01
    tampered = pk[:3] + [reframe(pk[3], piece=bytes(flipped))] + pk[4:]  # outer CRC valid
    ht.parse_frame(tampered[3])
    ok, why = dw.validate_gp150_stream(tampered)
    assert ok is False and "CRC" in why


def test_validate_rejects_a_skipped_chunk_index():
    pk = good_stream()
    ok, why = dw.validate_gp150_stream(pk[:3] + pk[4:])
    assert ok is False and "index" in why
    renumbered = pk[:3] + [reframe(pk[3], idx=5)] + pk[4:]
    assert dw.validate_gp150_stream(renumbered)[0] is False
    one_based = ht.chunk_frames(ht.FAMILY_PATCH, 0x24, ht.logical(ht.import_payload(finger())), host=False)
    assert dw.validate_gp150_stream(one_based)[0] is False  # device->host numbering


def test_validate_rejects_a_bad_offset():
    pk = good_stream()
    ok, why = dw.validate_gp150_stream(pk[:3] + [reframe(pk[3], off=119 * 3 + 1)] + pk[4:])
    assert ok is False and "offset" in why


def test_validate_rejects_a_wrong_slot_byte():
    pk = good_stream()  # index byte 199
    ok, why = dw.validate_gp150_stream(pk, slot=5)
    assert ok is False and "199" in why
    b = bytearray(finger())
    b[4] = 250  # not a GP-150 slot at all
    ok, why = dw.validate_gp150_stream(ht.import_stream(0x24, bytes(b)))
    assert ok is False and "250" in why


def test_validate_rejects_a_stream_without_a_short_final_chunk():
    pk = good_stream()
    ok, why = dw.validate_gp150_stream(pk[:-1])  # last remaining chunk is full-size
    assert ok is False and "final" in why


def test_validate_rejects_non_import_payloads_and_strays():
    prst = finger()
    export_shaped = ht.chunk_frames(ht.FAMILY_PATCH, 0x24, ht.logical(ht.PAYLOAD_HEAD_EXPORT + prst))
    assert dw.validate_gp150_stream(export_shaped)[0] is False
    trailing = ht.chunk_frames(ht.FAMILY_PATCH, 0x24, ht.logical(ht.import_payload(prst) + b"\x00"))
    assert dw.validate_gp150_stream(trailing)[0] is False
    pk = good_stream()
    assert dw.validate_gp150_stream([reframe(w, family=0x2C) for w in pk])[0] is False
    assert dw.validate_gp150_stream(pk[:3] + [reframe(pk[3], tid=0x25)] + pk[4:])[0] is False
    assert dw.validate_gp150_stream([])[0] is False
    assert dw.validate_gp150_stream([ht.hello()])[0] is False
    eight_bit = ht.frame(ht.FAMILY_PATCH, bytes((0x08, 0, 0, 0x80)), b"\x00" + ht.enc(b"\x00"))
    assert dw.validate_gp150_stream([eight_bit])[0] is False


def test_validate_stream_dispatches_ht_streams():
    pk = good_stream()
    assert dw.validate_stream(pk) == (True, "ok")
    assert dw.validate_stream(pk[:-1])[0] is False


# --- R-LEGACY: the legacy payload-length maps never contain the GP-150 -------------

def legacy_stream_with_gp150_body(slot=7):
    """What build_patch_write_stream produced for a GP-150 file before it learned
    the HT transport: a GP-50-shaped 0x1D stream whose payload is 1109 bytes."""
    prst = finger()
    payload = dw.PATCH_HDR + bytes([slot, 0, 0, 0]) + prst[0x19:]
    return [dw.build_packet(dw.PATCH_WRITE_CMD, i // dw.PATCH_BLOCK, payload[i:i + dw.PATCH_BLOCK])
            for i in range(0, len(payload), dw.PATCH_BLOCK)]


def test_legacy_payload_maps_skip_ht_devices():
    lens = dw._legacy_payload_lens()
    assert {p.key for p in lens.values()} == {"gp50", "gp5"}
    assert all(p.transport == "legacy" for p in lens.values())
    assert sorted(lens) == [6 + 507 - 0x19, 6 + 552 - 0x19]
    bogus = legacy_stream_with_gp150_body()
    ok, why = dw.validate_stream(bogus)
    assert ok is False and "1109" in why
    assert dw._infer_device_key(bogus) is None
    assert dw._infer_device_key(good_stream()) == "gp150"
    # a caller claiming validated=True still cannot push it through the legacy sender
    with pytest.raises(RuntimeError, match="unrecognized"):
        dw.send_stream("no-such-port", bogus, confirm=True, validated=True)


# --- send: gated; against a fake pedal only --------------------------------------

class FakeMsg:
    type = "sysex"

    def __init__(self, wire):
        self.raw = bytes(wire)

    def bytes(self):
        return list(self.raw)


class ImportPedal:
    """A scripted GP-150 taking an import: after the final (short) 0x70 chunk it
    ACKs the transfer id, then sends the captured 0x08 notification (tx 0x0D).
    Knobs: ack, notify, notify_raw (another 0x08 frame), dead, extra(pedal, n)
    called on every host chunk (to inject unsolicited frames)."""

    def __init__(self, ack=True, notify=True, notify_raw=None, notify_delay=0.02, dead=False, extra=None):
        self.ack, self.notify, self.dead, self.extra = ack, notify, dead, extra
        self.notify_raw = MSG["import_notify_08"] if notify_raw is None else notify_raw
        self.notify_delay = notify_delay
        self.queue = []
        self.sent = []
        self.sent_at = []
        self.notified_at = None
        self.inp, self.out = _In(self), _Out(self)

    def emit(self, raw, delay=0.001):
        self.queue.append((time.monotonic() + delay, bytes(raw)))

    def due(self):
        now = time.monotonic()
        ready = sorted((q for q in self.queue if q[0] <= now), key=lambda q: q[0])
        self.queue = [q for q in self.queue if q[0] > now]
        if any(raw[3] == ht.FAMILY_IMPORT_DONE for _t, raw in ready):
            self.notified_at = now
        return [raw for _t, raw in ready]

    def receive(self, wire):
        self.sent.append(wire)
        self.sent_at.append(time.monotonic())
        if self.dead:
            return
        f = ht.parse_frame(wire)
        if f.family == ht.FAMILY_ACK and f.tx4[1] == 0x01:
            return self.emit(MSG["hello_reply"])
        if f.family != ht.FAMILY_PATCH or not ht.is_chunk(f):
            return
        if self.extra:
            self.extra(self, f.body[0])
        if len(wire) < ht.FULL_WIRE_LEN:  # the final chunk: the import is complete
            if self.ack:
                self.emit(ht.ack(f.tx4[3]))
            if self.notify:
                self.emit(self.notify_raw, self.notify_delay)

    def acks_for(self, id_):
        return [w for w in self.sent if w == ht.ack(id_)]


class _In:
    def __init__(self, pedal):
        self.p = pedal

    def iter_pending(self):
        for raw in self.p.due():
            yield FakeMsg(raw)


class _Out:
    def __init__(self, pedal):
        self.p = pedal

    def send(self, msg):
        self.p.receive(bytes(msg.bytes()))


FAST = dict(settle=0.005, empty_timeout=0.06, timeout=0.5, idle=0.06, tick=0.001)


def session(pedal):
    return ht_scan.Session(pedal.inp, pedal.out, to_message=FakeMsg, **FAST)


def send(pk, pedal, **kw):
    # happy paths return on the 0x08, so a generous notify_timeout costs nothing;
    # the tests that must time out pass a short one
    opts = dict(confirm=True, validated=True, allow_unverified=True, session=session(pedal),
                pace=0.0, notify_timeout=2.0)
    opts.update(kw)
    return dw.send_stream(None, pk, **opts)


def test_send_gate_refuses_unverified_gp150():
    pk = good_stream()
    assert dw.WRITE_VERIFIED["gp150"] is False
    p = ImportPedal()
    with pytest.raises(RuntimeError, match="not.*verified"):
        dw.send_stream(None, pk, confirm=True, validated=True, session=session(p))
    assert p.sent == []  # refused before a single byte
    # the default path refuses before it ever looks for a MIDI port
    with pytest.raises(RuntimeError, match="not.*verified"):
        dw.send_stream("GP-150", pk, confirm=True, validated=True)
    with pytest.raises(RuntimeError, match="confirm"):
        dw.send_stream(None, pk, validated=True, allow_unverified=True, session=session(p))
    assert p.sent == []


def test_send_refuses_an_invalid_stream_even_when_unverified_is_allowed():
    pk = good_stream()
    p = ImportPedal()
    with pytest.raises(RuntimeError, match="validate"):
        send(pk[:3] + pk[4:], p)
    assert p.sent == []


def test_send_streams_chunks_then_awaits_ack_and_notify():
    pk = good_stream()
    p = ImportPedal(notify_delay=0.05)
    t0 = time.monotonic()
    r = send(pk, p)
    assert r == {"sent": 10, "acks": 1, "notified": True}
    assert p.sent[:10] == pk  # every chunk, in order
    assert p.notified_at is not None and p.notified_at - t0 >= 0.05  # we waited for it
    # exactly ONE ACK for the 0x08: the session's auto-ACK; nothing else from the host
    assert len(p.acks_for(NOTIFY_TX)) == 1
    assert p.sent[10:] == [ht.ack(NOTIFY_TX)]
    assert p.acks_for(0x24) == []


def test_send_paces_the_chunks():
    pk = good_stream()
    p = ImportPedal()
    send(pk, p, pace=0.01)
    gaps = [b - a for a, b in zip(p.sent_at[:10], p.sent_at[1:10])]
    assert len(gaps) == 9 and min(gaps) >= 0.009


def test_send_acks_unsolicited_frames_once_and_keeps_waiting():
    pk = good_stream()

    def extra(pedal, idx):
        if idx == 4:
            pedal.emit(MSG["ident_reply"])  # family 0x10, tx 1, mid-stream
    p = ImportPedal(extra=extra)
    assert send(pk, p)["notified"] is True
    assert len(p.acks_for(1)) == 1 and len(p.acks_for(NOTIFY_TX)) == 1
    assert [w for w in p.sent if w[3] == ht.FAMILY_PATCH] == pk


def test_send_without_notify_raises_after_the_timeout():
    pk = good_stream()
    p = ImportPedal(notify=False)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="0x08"):
        send(pk, p, notify_timeout=0.2)
    assert time.monotonic() - t0 >= 0.2
    assert p.sent == pk  # no stray ACKs, no retries


def test_send_to_a_dead_pedal_raises_not_acked():
    pk = good_stream()
    p = ImportPedal(dead=True)
    with pytest.raises(RuntimeError, match="did not ACK"):
        send(pk, p, notify_timeout=0.1)
    assert p.sent == pk


def test_send_rejects_an_unexpected_notify_payload():
    other = ht.frame(ht.FAMILY_IMPORT_DONE, bytes((0, 0, 0, 0x0E)),
                     b"\x01" + ht.enc(ht.logical(b"\x09\x03\x11\x31")))
    p = ImportPedal(notify_raw=other)
    with pytest.raises(RuntimeError, match="unexpected"):
        send(good_stream(), p)
    assert len(p.acks_for(0x0E)) == 1  # still ACKed once by the session


def test_send_notify_without_ack_still_counts_as_notified():
    p = ImportPedal(ack=False)
    assert send(good_stream(), p) == {"sent": 10, "acks": 0, "notified": True}


def test_notify_payload_is_the_import_done_marker():
    f = ht.parse_frame(MSG["import_notify_08"])
    assert f.family == ht.FAMILY_IMPORT_DONE and f.tx4[3] == NOTIFY_TX
    assert ht.short_payload(f) == ht.IMPORT_DONE_PAYLOAD == b"\x09\x03\x11\x30"
