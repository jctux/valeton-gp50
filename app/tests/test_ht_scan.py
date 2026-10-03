"""patch/ht_scan.py — the GP-150 read CLI's Session, driven against a scripted fake
in/out MIDI port pair (no pedal, no mido needed). Mirrors the rules the browser
session follows (app/static/ht_transport.js, test_ht_transport_js.mjs): one request
at a time, tx ids 1..0x7F, the final stream chunk ACKed immediately, a silent slot
is empty only after an ACK (or, un-ACKed twice, when the pedal still answers a
hello), a lost request is re-sent once, a stream missing its first chunk is an
error that is retried — never "empty".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from patch import ht_proto as ht
from patch import ht_scan
from patch import prst150_format as f150

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = json.load(open(os.path.join(ROOT, "app", "tests", "fixtures", "gp150", "ht_corpus.json")))
MSG = {m["id"]: bytes.fromhex(m["raw"]) for m in FIX["messages"]}
EVID = os.path.join(ROOT, "re", "gp150", "evidence")
EXPORT_CHUNKS = [MSG[f"export_stream_slot1_{i}"] for i in range(10)]
CORPUS_PRST = ht.preset_from_payload(ht.assemble_stream([ht.parse_frame(w) for w in EXPORT_CHUNKS])[1])
CORPUS_TID = 0x0C  # transfer id of the captured slot-1 export stream

FAST = dict(settle=0.005, empty_timeout=0.06, timeout=0.5, idle=0.06, tick=0.001)
# for pedals that do answer: a generous hard cap so a loaded CI box can't expire a
# stream between polls (timeout only bounds a stream that keeps flowing)
LIVE = dict(FAST, timeout=5.0, idle=1.0)


def evidence(name):
    return open(os.path.join(EVID, name), "rb").read()


def device_stream(prst, tid=0x21):
    """Device->host 0x70 chunk frames for `prst`, as the pedal would send them
    (chunk_frames(host=False) reproduces the captured stream byte-for-byte)."""
    return ht.chunk_frames(ht.FAMILY_PATCH, tid, ht.logical(ht.PAYLOAD_HEAD_EXPORT + prst), host=False)


class FakeMsg:
    """The slice of mido.Message the Session uses: .type and .bytes()."""

    type = "sysex"

    def __init__(self, wire):
        self.raw = bytes(wire)

    def bytes(self):
        return list(self.raw)


class FakePedal:
    """A scripted GP-150 behind a fake (inp, out) port pair.

    Answers hello; answers a read of a slot in `presets` with ACK + that preset's
    stream (slot 0 = the captured corpus chunks); ACKs and stays silent for other
    slots. Knobs: ack_silent=False (no ACK for a silent slot either), dead (never
    answers), drop_chunk, on_read(pedal, n, slot, tx) and on_hello(pedal) to script
    replies. Inbound frames are queued with a release time; `inp.iter_pending()`
    yields the due ones, like mido's non-blocking input."""

    def __init__(self, presets=None, ack_silent=True, dead=False, drop_chunk=-1, on_read=None, on_hello=None):
        self.presets = {0: None} if presets is None else presets  # None -> the corpus stream
        self.ack_silent, self.dead, self.drop_chunk = ack_silent, dead, drop_chunk
        self.on_read, self.on_hello = on_read, on_hello
        self.queue = []  # [(release_at, raw)]
        self.sent = []  # host wire frames, in order
        self.sent_at_poll = []  # poll counter at each host send
        self.polls = 0
        self.final_yield_poll = []  # poll counter whenever a final (short) chunk was handed over
        self.reads = 0
        self.inp, self.out = _In(self), _Out(self)

    # -- device -> host
    def emit(self, raw, delay=0.001):
        self.queue.append((time.monotonic() + delay, bytes(raw)))

    def stream(self, frames=None, delay=0.003, mutate=None):
        frames = EXPORT_CHUNKS if frames is None else frames
        for i, c in enumerate(frames):
            if i == self.drop_chunk:
                continue
            self.emit(mutate(c, i) if mutate else c, delay + i * 0.001)

    def stream_for(self, slot):
        prst = self.presets.get(slot)
        return None if prst is None else device_stream(prst)

    def due(self):
        now = time.monotonic()
        ready = sorted((q for q in self.queue if q[0] <= now), key=lambda q: q[0])
        self.queue = [q for q in self.queue if q[0] > now]
        return [raw for _t, raw in ready]

    # -- host -> device
    def receive(self, wire):
        self.sent.append(wire)
        self.sent_at_poll.append(self.polls)
        if self.dead:
            return
        f = ht.parse_frame(wire)
        if f.family == ht.FAMILY_ACK and f.tx4[1] == 0x01:  # hello
            if self.on_hello:
                return self.on_hello(self)
            return self.emit(MSG["hello_reply"])
        if f.family == ht.FAMILY_ACK:
            return  # an ACK from the host
        if f.family == ht.FAMILY_PRESET_REQ:
            payload = ht.parse_logical(ht.dec(f.body[1:]))
            slot = payload[8] | (payload[9] << 8)
            self.reads += 1
            if self.on_read:
                return self.on_read(self, self.reads, slot, f.tx4[3])
            if slot not in self.presets:
                if self.ack_silent:
                    self.emit(ht.ack(f.tx4[3]))
                return  # empty slot: silence (after the ACK, or with none at all)
            self.emit(ht.ack(f.tx4[3]))
            self.stream(self.stream_for(slot))

    def reqs(self):
        return [w for w in self.sent if w[3] == ht.FAMILY_PRESET_REQ]

    def hellos(self):
        return [w for w in self.sent if w == ht.hello()]

    def acks_for(self, id_):
        return [w for w in self.sent if w == ht.ack(id_)]


class _In:
    def __init__(self, pedal):
        self.p = pedal
        self.closed = False

    def iter_pending(self):
        self.p.polls += 1
        for raw in self.p.due():
            if len(raw) >= 9 and raw[3] != 0 and raw[4] != 0 and len(raw) < ht.FULL_WIRE_LEN:
                self.p.final_yield_poll.append(self.p.polls)
            yield FakeMsg(raw)

    def close(self):
        self.closed = True


class _Out:
    def __init__(self, pedal):
        self.p = pedal
        self.closed = False

    def send(self, msg):
        self.p.receive(bytes(msg.bytes()))

    def close(self):
        self.closed = True


def session(pedal, **kw):
    opts = dict(LIVE)
    opts.update(kw)
    return ht_scan.Session(pedal.inp, pedal.out, to_message=FakeMsg, **opts)


def seven_bit(wires):
    return all(w[0] == 0xF0 and w[-1] == 0xF7 and all(b <= 0x7F for b in w[1:-1]) for w in wires)


# --- tx ids / framing -------------------------------------------------------------

def test_tx_ids_stay_7_bit_and_wrap():
    s = session(FakePedal())
    ids = [s.next_tx() for _ in range(300)]
    assert all(1 <= v <= 0x7F for v in ids)
    assert all(a != b for a, b in zip(ids, ids[1:]))
    assert any(a == 0x7F and b == 1 for a, b in zip(ids, ids[1:]))  # wraps 0x7F -> 1


def test_hello_reply_is_a_frame_not_an_ack():
    p = FakePedal()
    s = session(p, **FAST)
    r = s.exchange(ht.hello())
    ack, frames = r  # unpacks like the brief's (ack, frames)
    assert ack is False and len(frames) == 1
    assert frames[0].family == ht.FAMILY_ACK and frames[0].tx4[1] == 0x02
    assert s.hello() is True


def test_hello_survives_an_unsolicited_frame_first():
    def on_hello(p):
        p.emit(MSG["ident_reply"])  # family 0x10, tx 1
        p.emit(MSG["hello_reply"], delay=0.01)
    p = FakePedal(on_hello=on_hello)
    s = session(p)
    assert s.hello() is True
    assert p.acks_for(1), "unsolicited device message with a tx id must be ACKed"


def test_dead_pedal_hello_is_false():
    s = session(FakePedal(dead=True), **FAST)
    assert s.hello() is False


# --- reads ------------------------------------------------------------------------

def test_read_slot_streams_the_corpus_chunks():
    p = FakePedal()
    s = session(p)
    t0 = time.monotonic()
    prst = s.read(0)
    assert prst == CORPUS_PRST and len(prst) == 1128
    assert f150.read_name(prst) == "New GEN." and f150.read_index(prst) == 0
    assert s.last_status == "preset"
    assert len(p.reqs()) == 1 and not p.hellos()
    req = ht.parse_logical(ht.dec(ht.parse_frame(p.reqs()[0]).body[1:]))
    assert req[8:11] == b"\x00\x00\x01"  # slot 0, flag 01 = read without selecting
    assert len(p.acks_for(CORPUS_TID)) == 1, "final chunk ACKed exactly once"
    assert time.monotonic() - t0 < 2.0
    assert seven_bit(p.sent)


def test_final_chunk_acked_immediately_before_assembly(monkeypatch):
    p = FakePedal()
    s = session(p)
    seen = {}
    real = ht_scan.ht.assemble_stream

    def spy(frames):
        seen["last_sent"] = p.sent[-1]
        seen["polls"] = p.polls
        return real(frames)

    monkeypatch.setattr(ht_scan.ht, "assemble_stream", spy)
    assert s.read(0) == CORPUS_PRST
    final_ack = ht.ack(CORPUS_TID)
    assert seen["last_sent"] == final_ack, "the ACK must go out before the stream is assembled"
    i = p.sent.index(final_ack)
    assert p.final_yield_poll, "the fake never handed over a final chunk"
    # sent during the same iter_pending() pass that handed the final chunk over
    assert p.sent_at_poll[i] == p.final_yield_poll[0]


def test_read_active_uses_slot_ffff():
    def on_read(p, n, slot, tx):
        assert slot == ht.SLOT_ACTIVE
        p.emit(ht.ack(tx))
        p.stream(device_stream(evidence("100-active.prst")))
    p = FakePedal(on_read=on_read)
    s = session(p)
    prst = s.read(ht.SLOT_ACTIVE)
    assert prst == evidence("100-active.prst")  # index 100 != 0xFFFF is fine for ACTIVE
    assert len(p.reqs()) == 1


def test_silent_slot_after_ack_is_empty():
    p = FakePedal()
    s = session(p, **FAST)
    t0 = time.monotonic()
    assert s.read(199) is None
    assert s.last_status == "empty-acked"
    assert len(p.reqs()) == 1 and not p.hellos()
    assert time.monotonic() - t0 < 1.0


def test_lost_request_is_resent_once_with_a_new_tx():
    def on_read(p, n, slot, tx):
        if n == 1:
            return  # request lost: no ACK, no stream
        p.emit(ht.ack(tx))
        p.stream()
    p = FakePedal(on_read=on_read)
    s = session(p)
    assert s.read(0) == CORPUS_PRST
    reqs = p.reqs()
    assert len(reqs) == 2 and not p.hellos()
    assert reqs[0][7] != reqs[1][7], "the re-send carries a new tx id"


def test_late_unacked_stream_is_kept_not_empty():
    # read #1's stream only shows up after read #1 was judged silent
    def on_read(p, n, slot, tx):
        if n == 2:
            p.stream()
    p = FakePedal(on_read=on_read)
    s = session(p)
    assert s.read(0) == CORPUS_PRST
    assert len(p.reqs()) == 2 and not p.hellos()


def test_unacked_silence_twice_then_hello_decides_empty():
    p = FakePedal(presets={}, ack_silent=False)
    s = session(p, **FAST)
    assert s.read(199) is None
    assert s.last_status == "empty-unacked"
    assert len(p.reqs()) == 2 and len(p.hellos()) == 1
    assert p.sent[-1] == ht.hello()


def test_dead_pedal_read_raises_not_responding():
    p = FakePedal(dead=True)
    s = session(p, **FAST)
    with pytest.raises(ht_scan.NotResponding):
        s.read(5)
    assert len(p.reqs()) == 2 and len(p.hellos()) == 1


def test_stream_during_the_hello_probe_reads_again():
    def on_read(p, n, slot, tx):
        if n >= 3:
            p.emit(ht.ack(tx))
            p.stream()

    def on_hello(p):
        p.stream(delay=0.001)
        p.emit(MSG["hello_reply"], delay=0.04)
    p = FakePedal(on_read=on_read, on_hello=on_hello)
    s = session(p)
    assert s.read(0) == CORPUS_PRST
    assert len(p.reqs()) == 3


def _corrupt_first(c, i):
    if i != 0:
        return c
    w = bytearray(c)
    w[2] ^= 0x01  # outer CRC wrong -> the offset-0 chunk is dropped
    return bytes(w)


def test_missing_first_chunk_is_retried_never_empty():
    def on_read(p, n, slot, tx):
        p.emit(ht.ack(tx))
        p.stream(mutate=_corrupt_first if n == 1 else None)
    p = FakePedal(on_read=on_read)
    s = session(p)
    assert s.read(0) == CORPUS_PRST
    assert len(p.reqs()) == 2
    assert len(p.acks_for(CORPUS_TID)) == 2, "each stream's final chunk ACKed once"
    assert s.bad_frames == 1


def test_exchange_reports_a_headless_stream_as_an_error_not_silence():
    def on_read(p, n, slot, tx):
        p.emit(ht.ack(tx))
        p.stream(mutate=_corrupt_first)
    p = FakePedal(on_read=on_read)
    s = session(p)
    r = s.exchange(ht.preset_request(s.next_tx(), 0), stream=True)
    assert r.ack and not r.silent and r.payload is None
    assert isinstance(r.error, ht_scan.StreamError) and "first chunk" in str(r.error)


def test_first_chunk_always_missing_raises_stream_error():
    def on_read(p, n, slot, tx):
        p.emit(ht.ack(tx))
        p.stream(mutate=_corrupt_first)
    p = FakePedal(on_read=on_read)
    s = session(p)
    with pytest.raises(ht_scan.StreamError, match="first chunk"):
        s.read(0)
    assert len(p.reqs()) == 2


def test_gapped_stream_retried_once_then_error():
    p = FakePedal(drop_chunk=3)
    s = session(p)
    with pytest.raises(ht_scan.StreamError):
        s.read(0)
    assert len(p.reqs()) == 2
    assert len(p.acks_for(CORPUS_TID)) == 2


def test_index_mismatch_retried_then_kept_with_a_warning():
    warnings = []

    def on_read(p, n, slot, tx):
        p.emit(ht.ack(tx))
        p.stream()  # always the index-0 preset
    p = FakePedal(on_read=on_read)
    s = session(p, log=lambda level, msg: warnings.append((level, msg)))
    prst = s.read(5)
    assert prst == CORPUS_PRST and len(p.reqs()) == 2
    assert [m for lvl, m in warnings if lvl == "warn"], warnings


def test_unsolicited_message_acked_and_bad_crc_dropped():
    p = FakePedal()
    s = session(p, **FAST)
    p.emit(MSG["ident_reply"], delay=0)
    s.pump(0.02)
    assert p.sent == [ht.ack(1)]
    bad = bytearray(MSG["ident_reply"])
    bad[2] ^= 0x01
    p.emit(bytes(bad), delay=0)
    s.pump(0.02)
    assert p.sent == [ht.ack(1)] and s.bad_frames == 1


def test_read_rejects_out_of_range_slots():
    s = session(FakePedal(), **FAST)
    for bad in (-1, 0x10000):
        with pytest.raises(ValueError):
            s.read(bad)


# --- ports ------------------------------------------------------------------------

def test_session_without_ports_opens_them_lazily(monkeypatch):
    p = FakePedal()
    opened = []

    def fake_open(port):
        opened.append(port)
        return p.inp, p.out
    monkeypatch.setattr(ht_scan, "open_ports", fake_open)
    s = ht_scan.Session(to_message=FakeMsg, open_delay=0, **FAST)
    assert opened == [ht_scan.PORT] and s.inp is p.inp and s.out is p.out
    assert s.hello() is True
    s.close()
    assert p.inp.closed and p.out.closed


def test_pick_port():
    assert ht_scan.pick_port(["AudioBox USB 96", "GP-150"], "GP-150") == "GP-150"
    assert ht_scan.pick_port(["GP-150 MIDI 1", "GP-50"], "GP-150") == "GP-150 MIDI 1"
    with pytest.raises(RuntimeError, match="AudioBox"):
        ht_scan.pick_port(["AudioBox USB 96"], "GP-150")


def test_module_imports_without_mido():
    code = "import sys; sys.modules['mido'] = None; import patch.ht_scan as m; print(m.PORT)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "GP-150"


def test_mido_sysex_round_trip():
    pytest.importorskip("mido")
    for wire in (ht.hello(), ht.ack(0x0C), ht.preset_request(0x23, 0)):
        assert bytes(ht_scan.mido_sysex(wire).bytes()) == wire


# --- CLI helpers ------------------------------------------------------------------

def test_scan_saves_presets_and_records_empty_slots(tmp_path):
    p = FakePedal(presets={0: CORPUS_PRST, 99: evidence("099-Finger_AC.prst"), 100: evidence("100-active.prst")})
    s = session(p, **dict(LIVE, empty_timeout=0.06))
    lines = []
    summary = ht_scan.scan(s, slots=[0, 1, 99, 100], out_dir=str(tmp_path), printer=lines.append)
    assert summary["presets"] == 3 and summary["empty"] == 1 and summary["errors"] == 0
    assert summary["empty_slots"] == [1] and summary["empty_acked"] == [1] and summary["empty_unacked"] == []
    assert summary["index_mismatch"] == []
    names = sorted(os.listdir(tmp_path))
    assert "000-New_GEN.prst" in names and "099-Finger_AC.prst" in names and "scan_summary.json" in names
    assert open(tmp_path / "099-Finger_AC.prst", "rb").read() == evidence("099-Finger_AC.prst")
    saved = json.load(open(tmp_path / "scan_summary.json"))
    assert saved["empty_slots"] == [1] and saved["slots"]["1"]["status"] == "empty-acked"
    assert lines[-1].startswith("done: 3 presets, 1 empty")


def test_scan_stops_after_three_straight_errors(tmp_path):
    p = FakePedal(dead=True)
    s = session(p, **FAST)
    with pytest.raises(ht_scan.NotResponding):
        ht_scan.scan(s, slots=range(10), out_dir=str(tmp_path), printer=lambda _l: None)
    assert len(p.reqs()) == 6  # 3 slots x (read + re-send), then abort


def test_cli_read_and_scan(tmp_path, monkeypatch, capsys):
    p = FakePedal(presets={0: CORPUS_PRST, 2: f150.blank(2)})
    monkeypatch.setattr(ht_scan, "N_SLOTS", 3)
    make = lambda: session(p, **dict(LIVE, empty_timeout=0.06))  # noqa: E731
    assert ht_scan.main(["hello"], session_factory=make) == 0
    assert "answered the handshake" in capsys.readouterr().out
    assert ht_scan.main(["read", "0", "--out", str(tmp_path)], session_factory=make) == 0
    out = capsys.readouterr().out
    assert "New GEN." in out and (tmp_path / "000-New_GEN.prst").exists()
    assert ht_scan.main(["read", "1", "--out", str(tmp_path)], session_factory=make) == 0
    assert "empty" in capsys.readouterr().out
    assert ht_scan.main(["scan", "--out", str(tmp_path / "s")], session_factory=make) == 0
    assert "done: 2 presets, 1 empty" in capsys.readouterr().out
    dead = FakePedal(dead=True)
    with pytest.raises(SystemExit, match="handshake"):
        ht_scan.main(["hello"], session_factory=lambda: session(dead, **FAST))


def test_watch_prints_byte_diffs():
    a = bytearray(CORPUS_PRST)
    b = bytearray(CORPUS_PRST)
    pos = 3  # chain position 3 holds slot order[3]
    off = f150.BLOCKS_OFF + pos * f150.BLOCK_LEN + 4  # the block's type byte
    b[off] ^= 0x01
    answers = [bytes(a), bytes(b)]

    def on_read(p, n, slot, tx):
        p.emit(ht.ack(tx))
        p.stream(device_stream(answers[min(n, len(answers)) - 1]))
    p = FakePedal(on_read=on_read)
    s = session(p)
    lines = []
    ht_scan.watch(s, interval=0, count=2, printer=lines.append)
    changed = [ln for ln in lines if "bytes changed" in ln]
    assert len(changed) == 1 and changed[0].split()[1] == "1"
    slot_name = f150.SLOTS[f150.read_order(a)[pos]]
    assert any(f"pos {pos} ({slot_name}) block byte +0x04" in ln for ln in lines), lines

