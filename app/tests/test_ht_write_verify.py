"""patch/ht_write_verify.py — the supervised GP-150 write verification (plan Task 12),
driven end to end against a scripted fake pedal.

No test here can reach a real MIDI port: conftest's autouse midi_port_guard fails
any call of ht_scan.open_ports (every test asserts nothing reached it), _no_mido
fails any use of mido, and once a test's own fake-port Session exists,
ht_scan.Session is replaced by a tripwire, so the script can only use the ONE
session it was handed (a second session on the port would double-ACK the 0x08).
WRITE_VERIFIED["gp150"] is never touched: the script passes allow_unverified=True.
Python 3.9-compatible (also run under .venv-midi).
"""
from __future__ import annotations

import os
import sys

import pytest

from app.tests.test_device_write_gp150 import _BlockedMido
from app.tests.test_ht_scan import CORPUS_PRST, EVID, FakeMsg, FakePedal
from patch import device_write as dw
from patch import ht_proto as ht
from patch import ht_scan
from patch import ht_write_verify as hwv
from patch import prst150_format as f150

SLOT = 199
ACTIVE = ht.SLOT_ACTIVE
HINT = "read slot 199 back before retrying"
NOTIFY_TX = 0x0D  # tx id of the captured 0x08; the fake pedal counts up from it per import
# empty_timeout short (an empty slot costs 60 ms); timeout/idle generous so a loaded
# box cannot expire a stream that is still flowing
OPTS = dict(settle=0.005, empty_timeout=0.06, timeout=5.0, idle=1.0, tick=0.001)


def evidence(name):
    with open(os.path.join(EVID, name), "rb") as fh:
        return fh.read()


ACTIVE_PRST = evidence("100-active.prst")  # the pedal's active preset (index 100)


@pytest.fixture(autouse=True)
def _no_mido(monkeypatch):
    monkeypatch.setitem(sys.modules, "mido", _BlockedMido("mido"))


def import_done(tx):
    """The pedal's family-0x08 'import done' (flag 01, payload 09 03 11 30) with tx id `tx`."""
    return ht.frame(ht.FAMILY_IMPORT_DONE, bytes((0, 0, 0, tx)), b"\x01" + ht.enc(ht.logical(ht.IMPORT_DONE_PAYLOAD)))


def flip(prst, *offsets):
    b = bytearray(prst)
    for off in offsets:
        b[off] ^= 0x55
    return bytes(b)


class WritePedal(FakePedal):
    """test_ht_scan's FakePedal (hello; ACK + stream for a filled slot, slot 0 = the
    captured corpus stream; ACK + silence for an empty one) that also takes an
    import, like test_device_write_gp150's ImportPedal: host 0x70 chunks are
    collected; on the final (short) one it stores the preset under its index byte
    (passed through `store`, default: exactly as received, 0x0A = 0x5C), ACKs the
    transfer id and sends a 0x08 'import done' with its own tx id (0x0D, 0x0E, …).
    Later reads of that slot return the stored bytes. Knobs: ack_import, notify,
    store(prst) -> bytes, import_sets_active (the pedal also loads the import)."""

    def __init__(self, presets=None, ack_import=True, notify=True, store=None, import_sets_active=False, **kw):
        super().__init__(presets={0: None, ACTIVE: ACTIVE_PRST} if presets is None else presets, **kw)
        self.ack_import, self.notify = ack_import, notify
        self.store = store or (lambda prst: prst)
        self.import_sets_active = import_sets_active
        self.chunks = []
        self.imports = []  # every imported .prst, as received
        self.notify_tx = []  # tx id of every 0x08 sent

    def receive(self, wire):
        super().receive(wire)  # logs the frame; hello / reads / host ACKs
        f = ht.parse_frame(wire)
        if self.dead or f.family != ht.FAMILY_PATCH or not ht.is_chunk(f):
            return
        self.chunks.append(f)
        if len(wire) >= ht.FULL_WIRE_LEN:
            return
        tid, payload = ht.assemble_stream(self.chunks)  # the final chunk: the import is complete
        self.chunks = []
        assert payload[:4] == ht.PAYLOAD_HEAD_IMPORT
        prst = ht.preset_from_payload(payload)
        self.imports.append(prst)
        self.presets[prst[f150.IDX_OFF]] = self.store(prst)
        if self.import_sets_active:
            self.presets[ACTIVE] = self.store(prst)
        if self.ack_import:
            self.emit(ht.ack(tid))
        if self.notify:
            tx = NOTIFY_TX + len(self.notify_tx)
            self.notify_tx.append(tx)
            self.emit(import_done(tx), 0.02)


def wire_log(pedal):
    """The host's frames, compacted: hello / read <slot> / import <n chunks> / ack <id>."""
    out = []
    for w in pedal.sent:
        f = ht.parse_frame(w)
        if w == ht.hello():
            out.append("hello")
        elif f.family == ht.FAMILY_PRESET_REQ:
            p = ht.parse_logical(ht.dec(f.body[1:]))
            out.append(f"read {p[8] | (p[9] << 8)}")
        elif f.family == ht.FAMILY_PATCH:
            if out and out[-1].startswith("import "):
                out[-1] = f"import {int(out[-1].split()[1]) + 1}"
            else:
                out.append("import 1")
        elif f.family == ht.FAMILY_ACK:
            out.append(f"ack {f.tx4[3]:#04x}")
        else:
            out.append(f"family {f.family:#04x}")
    return out


def _second_session(*a, **k):
    pytest.fail("ht_write_verify constructed a second Session (it must reuse the one it was handed)")


def run_on(pedal, monkeypatch, slot=SLOT, **opts):
    o = dict(OPTS)
    o.update(opts)
    s = ht_scan.Session(pedal.inp, pedal.out, to_message=FakeMsg, **o)
    monkeypatch.setattr(ht_scan, "Session", _second_session)  # R-ONE-SESSION
    lines = []
    results = hwv.run(slot, session=s, log=lines.append)
    return results, lines, s


def oks(results):
    return [ok for _label, ok, _detail in results]


def step_lines(lines):
    return [ln for ln in lines if ln[:1].isdigit()]


READS_BEFORE_WRITE = ["hello", "read 65535", "ack 0x21", "read 199", "read 0", "ack 0x0c"]


# --- the whole protocol --------------------------------------------------------------

def test_all_six_steps_pass_with_exactly_one_ack_per_0x08(monkeypatch, midi_port_guard):
    p = WritePedal()
    calls = []
    real_send = dw.send_stream

    def spy(*a, **k):
        calls.append((a, k))
        return real_send(*a, **k)

    monkeypatch.setattr(dw, "send_stream", spy)
    results, lines, s = run_on(p, monkeypatch)

    assert oks(results) == [True] * 6, "\n".join(lines)
    assert [ln.split(" ", 2)[:2] for ln in step_lines(lines)] == [[str(n), "PASS"] for n in range(1, 7)]
    assert [label for label, _ok, _d in results] == hwv.STEPS

    # the wire, in order: one hello, reads, two 10-chunk imports, each 0x08 ACKed once
    assert wire_log(p) == READS_BEFORE_WRITE + [
        "import 10", "ack 0x0d",   # step 3: WRITE TEST -> 199; the session ACKs the 0x08
        "read 199", "ack 0x21",    # step 4: read back
        "import 10", "ack 0x0e",   # step 5: blank -> 199
        "read 199", "ack 0x21",    # step 5: read back
        "read 65535", "ack 0x21",  # step 6: active preset again
    ]
    assert p.notify_tx == [0x0D, 0x0E]
    assert len(p.acks_for(0x0D)) == 1 and len(p.acks_for(0x0E)) == 1  # exactly one ACK per 0x08
    assert p.acks_for(dw.GP150_TRANSFER_ID) == []  # the host never ACKs its own import

    # what was written: slot 0 (the corpus preset) renamed, then the blank, both to 199
    want = bytearray(CORPUS_PRST)
    f150.write_name(want, "WRITE TEST")
    want[f150.IDX_OFF] = SLOT
    want[0x0A] = ht.IMPORT_BYTE_0A
    blank = bytearray(f150.blank(SLOT))
    blank[0x0A] = ht.IMPORT_BYTE_0A
    assert p.imports == [bytes(want), bytes(blank)]
    assert f150.read_name(p.presets[SLOT]) == "New GEN."

    # both writes went through the gated sender on the ONE session, gate untouched
    assert len(calls) == 2
    for a, k in calls:
        assert a[0] is None and k["session"] is s
        assert k["confirm"] is True and k["validated"] is True and k["allow_unverified"] is True
    assert dw.WRITE_VERIFIED["gp150"] is False

    # what the user records
    assert "empty-acked" in results[1][2]
    assert "back[0x0A] = 0x5c" in results[3][2]
    assert "ignored differences: 0x00a" in results[3][2]  # 0x5C stored vs the export's 0x58
    assert "New GEN." in results[4][2]
    text = "\n".join(lines)
    assert "select slot 199 on the pedal" in text and "8.2" in text
    assert midi_port_guard == []


def test_an_occupied_slot_fails_step_2_and_nothing_is_sent_after_it(monkeypatch, midi_port_guard):
    user = bytearray(evidence("099-Finger_AC.prst"))
    user[f150.IDX_OFF] = SLOT
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: bytes(user)})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True, False], "\n".join(lines)
    detail = results[1][2]
    assert f150.read_name(bytes(user)) in detail and "refus" in detail and "last_status=preset" in detail
    assert HINT not in detail  # nothing was written: no read-back needed
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "ack 0x21"]  # then nothing at all
    assert p.imports == [] and not any(w[3] == ht.FAMILY_PATCH for w in p.sent)
    assert p.presets[SLOT] == bytes(user)
    assert step_lines(lines)[-1].startswith("2 FAIL")
    assert midi_port_guard == []


def test_a_write_without_0x08_fails_step_3_with_the_read_back_hint_and_stops(monkeypatch, midi_port_guard):
    p = WritePedal(notify=False)  # ACKs the import, never sends the 0x08 (waits the real 3 s)
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True, True, False], "\n".join(lines)
    detail = results[2][2]
    assert "0x08" in detail and detail.endswith(HINT)
    assert detail.count("read slot 199 back") == 1  # device_write's own hint is not doubled
    assert step_lines(lines)[-1].startswith("3 FAIL") and step_lines(lines)[-1].endswith(HINT)
    assert wire_log(p) == READS_BEFORE_WRITE + ["import 10"]  # no read-back, no blank, no retry
    assert len(p.imports) == 1
    assert midi_port_guard == []


def test_a_0x08_without_an_ack_still_passes_step_3_and_says_so(monkeypatch, midi_port_guard):
    p = WritePedal(ack_import=False)  # send_stream counts the 0x08 alone as done ({"acks": 0})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 6, "\n".join(lines)
    assert "NO ACK" in results[2][2] and "NO ACK" in results[4][2]
    assert len(p.acks_for(0x0D)) == 1 and len(p.acks_for(0x0E)) == 1
    assert midi_port_guard == []


def test_a_read_back_that_differs_fails_step_4_with_the_offsets_and_stops(monkeypatch, midi_port_guard):
    p = WritePedal(store=lambda prst: flip(prst, 0x30, 0x200))
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True, True, True, False], "\n".join(lines)
    detail = results[3][2]
    assert "0x030" in detail and "0x200" in detail and "back[0x0A] = 0x5c" in detail
    assert detail.endswith(HINT)
    assert wire_log(p) == READS_BEFORE_WRITE + ["import 10", "ack 0x0d", "read 199", "ack 0x21"]  # no blank
    assert midi_port_guard == []


def test_the_read_back_compare_ignores_0x0a_and_the_device_field(monkeypatch, midi_port_guard):
    def device_written(prst):  # the pedal stores 0x58 like an export and rewrites 0x0D..0x0F
        b = bytearray(flip(prst, 0x0D, 0x0E, 0x0F))
        b[0x0A] = 0x58
        return bytes(b)

    p = WritePedal(store=device_written)
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 6, "\n".join(lines)
    detail = results[3][2]
    assert "back[0x0A] = 0x58" in detail  # == the source export's 0x0A, so no difference there
    assert "ignored differences: 0x00d 0x00e 0x00f" in detail  # recorded, but ignored
    assert midi_port_guard == []


def test_a_changed_active_preset_fails_step_6(monkeypatch, midi_port_guard):
    p = WritePedal(import_sets_active=True)  # as if the pedal loaded the imported preset
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 5 + [False], "\n".join(lines)
    assert "changed" in results[5][2] and results[5][2].endswith(HINT)
    assert "select slot 199 on the pedal" not in "\n".join(lines)  # no manual checks after a FAIL
    assert midi_port_guard == []


def test_no_handshake_fails_step_1_and_sends_nothing_else(monkeypatch, midi_port_guard):
    p = WritePedal(dead=True)
    results, _lines, _s = run_on(p, monkeypatch, timeout=0.2)
    assert oks(results) == [False] and "handshake" in results[0][2]
    assert wire_log(p) == ["hello"]
    assert midi_port_guard == []


# --- pieces ---------------------------------------------------------------------------

def test_same_except_ignores_only_0x0a_and_0x0d_to_0x0f():
    a = bytes(CORPUS_PRST)
    assert hwv.IGNORE == (0x0A, 0x0D, 0x0E, 0x0F)
    assert hwv.same_except(a, a)
    assert hwv.same_except(a, flip(a, 0x0A, 0x0D, 0x0E, 0x0F))
    for off in (0x00, 0x04, 0x09, 0x0B, 0x0C, 0x10, 0x2C, 0x3B4, len(a) - 1):
        assert not hwv.same_except(a, flip(a, off)), hex(off)
    assert not hwv.same_except(a, a[:-1])
    assert hwv.same_except(a, flip(a, 0x30), ignore=(0x30,))
    assert hwv.diff_offsets(a, flip(a, 0x0A, 0x30)) == [0x0A, 0x30]


def test_slot_is_checked_before_any_port_is_opened(midi_port_guard):
    for bad in (0, -1, 200, ACTIVE, "199", None):  # 0 is the source preset's slot
        with pytest.raises(ValueError):
            hwv.run(bad)
    for argv in (["0"], ["200"], ["abc"], []):
        with pytest.raises(SystemExit):
            hwv.main(argv)
    assert midi_port_guard == []


def test_the_default_session_opens_ports_only_through_the_guarded_open_ports(midi_port_guard):
    # R-GUARD: never bind open_ports by name, so the guard (and nothing else) is in the way
    assert "open_ports" not in vars(hwv)
    assert not any(getattr(v, "__name__", "") == "open_ports" for v in vars(hwv).values())
    with pytest.raises(pytest.fail.Exception, match="real MIDI port"):
        hwv.run(SLOT)
    assert midi_port_guard == [ht_scan.PORT]
    with pytest.raises(pytest.fail.Exception, match="real MIDI port"):
        hwv.main([str(SLOT)])
    assert midi_port_guard == [ht_scan.PORT, ht_scan.PORT]


def test_the_cli_uses_one_session_of_its_own_and_closes_it(monkeypatch, midi_port_guard, capsys):
    p = WritePedal()
    real_session = ht_scan.Session
    made = []

    def factory(*a, **k):
        assert not a and set(k) <= {"log"}  # the default GP-150 session, nothing else
        s = real_session(p.inp, p.out, to_message=FakeMsg, log=k.get("log"), **OPTS)
        made.append(s)
        return s

    monkeypatch.setattr(ht_scan, "Session", factory)
    assert hwv.main([str(SLOT)]) == 0
    out = capsys.readouterr().out
    assert len(made) == 1 and p.inp.closed and p.out.closed
    assert [ln.split(" ", 2)[:2] for ln in out.splitlines() if ln[:1].isdigit()] == [
        [str(n), "PASS"] for n in range(1, 7)]
    assert midi_port_guard == []


def test_the_cli_exits_nonzero_on_a_fail_and_on_a_missing_port(monkeypatch, midi_port_guard, capsys):
    user = bytearray(evidence("099-Finger_AC.prst"))
    user[f150.IDX_OFF] = SLOT
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: bytes(user)})
    real_session = ht_scan.Session
    monkeypatch.setattr(ht_scan, "Session",
                        lambda **k: real_session(p.inp, p.out, to_message=FakeMsg, log=k.get("log"), **OPTS))
    assert hwv.main([str(SLOT)]) == 1
    assert "2 FAIL" in capsys.readouterr().out

    def no_port(**k):
        raise RuntimeError("no MIDI port named like 'GP-150' (found: none)")

    monkeypatch.setattr(ht_scan, "Session", no_port)
    results = hwv.run(SLOT, log=lambda _ln: None)
    assert oks(results) == [False] and "no MIDI port" in results[0][2]
    assert hwv.main([str(SLOT)]) == 1
    assert midi_port_guard == []
