"""patch/ht_write_verify.py — the supervised GP-150 write verification (plan Task 12),
driven end to end against a scripted fake pedal.

No test here can reach a real MIDI port: conftest's autouse midi_port_guard fails
any call of ht_scan.open_ports (every test asserts nothing reached it), _no_mido
fails any use of mido, and once a test's own fake-port Session exists,
ht_scan.Session is replaced by a tripwire, so the script can only use the ONE
session it was handed (a second session on the port would double-ACK the 0x08).
WRITE_VERIFIED["gp150"] is never touched: the script passes allow_unverified=True.
Backups and the step-0 scan summary live in a per-test tmp dir, never the repo's
device_scan_gp150/; the summary is built from one written by the real
ht_scan.scan(). Step 2's hardware timings (3 s / 2 s) are scaled down here and
pinned separately.
Python 3.9-compatible (also run under .venv-midi).
"""
from __future__ import annotations

import copy
import json
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
REAL_STEP2 = (hwv.EMPTY_PROOF_TIMEOUT, hwv.LATE_STREAM_PUMP)  # read at import, before any fixture


@pytest.fixture(autouse=True)
def _no_mido(monkeypatch):
    monkeypatch.setitem(sys.modules, "mido", _BlockedMido("mido"))


@pytest.fixture(autouse=True)
def _fast_step2(monkeypatch):
    monkeypatch.setattr(hwv, "EMPTY_PROOF_TIMEOUT", OPTS["empty_timeout"])
    monkeypatch.setattr(hwv, "LATE_STREAM_PUMP", 0.05)


@pytest.fixture(autouse=True)
def backup_dir(monkeypatch, tmp_path):
    d = str(tmp_path / "write_verify_backup")
    monkeypatch.setattr(hwv, "BACKUP_DIR", d)
    return d


@pytest.fixture(scope="module")
def summary_template(tmp_path_factory):
    """scan_summary.json as the real ht_scan.scan() writes it (fake ports): slot 0 a
    preset, the target slot empty-acked."""
    d = str(tmp_path_factory.mktemp("scan"))
    p = FakePedal()  # slot 0 = the corpus preset; any other slot: ACK, then silence
    ht_scan.scan(ht_scan.Session(p.inp, p.out, to_message=FakeMsg, **OPTS), slots=[0, SLOT], out_dir=d,
                 printer=lambda _line: None)
    with open(os.path.join(d, "scan_summary.json")) as fh:
        summary = json.load(fh)
    assert summary["slots"]["0"]["status"] == "preset" and summary["slots"][str(SLOT)]["status"] == "empty-acked"
    return summary


def full_summary(template, target=None, drop_target=False, aborted=None):
    """A completed 200-slot scan in the real schema: every slot a preset except the
    target (empty-acked, or `target`)."""
    summary = copy.deepcopy(template)
    preset = template["slots"]["0"]
    summary["slots"] = {str(i): dict(preset, index=i) for i in range(ht_scan.N_SLOTS)}
    summary["slots"][str(SLOT)] = dict(template["slots"][str(SLOT)]) if target is None else target
    if drop_target:
        del summary["slots"][str(SLOT)]
    if aborted:
        summary["aborted"] = aborted
    return summary


@pytest.fixture(autouse=True)
def scan_summary(monkeypatch, tmp_path, summary_template):
    """Step 0's file: a completed scan listing the target slot empty-acked. Call the
    returned writer to replace it (a summary dict, or raw text)."""
    path = str(tmp_path / "scan_summary.json")
    monkeypatch.setattr(hwv, "SCAN_SUMMARY", path)

    def write(summary=None, raw=None):
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(summary or full_summary(summary_template)))
        return path

    write()
    return write


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
    store(prst) -> bytes, import_sets_active (the pedal also loads the import),
    script={slot: [how, ...]}: the next reads of that slot, one `how` each —
    "drop" (no ACK, no stream: a lost request), "empty" (ACK, then silence),
    "corrupt" (ACK, then the slot's stream with every outer CRC broken),
    "tail" (ACK, then the slot's stream without its offset-0 chunk, valid CRCs),
    ("late", s) (ACK, then the slot's stream s seconds later); then the default."""

    def __init__(self, presets=None, ack_import=True, notify=True, store=None, import_sets_active=False,
                 script=None, **kw):
        super().__init__(presets={0: None, ACTIVE: ACTIVE_PRST} if presets is None else presets, **kw)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.ack_import, self.notify = ack_import, notify
        self.store = store or (lambda prst: prst)
        self.import_sets_active = import_sets_active
        self.chunks = []
        self.imports = []  # every imported .prst, as received
        self.notify_tx = []  # tx id of every 0x08 sent

    def scripted_read(self, wire, f):
        if self.dead or f.family != ht.FAMILY_PRESET_REQ:
            return False
        p = ht.parse_logical(ht.dec(f.body[1:]))
        slot = p[8] | (p[9] << 8)
        if not self.script.get(slot):
            return False
        how = self.script[slot].pop(0)
        self.sent.append(wire)
        self.sent_at_poll.append(self.polls)
        self.reads += 1
        if how == "drop":
            return True
        self.emit(ht.ack(f.tx4[3]))
        if how == "corrupt":
            for i, c in enumerate(self.stream_for(slot)):
                b = bytearray(c)
                b[2] ^= 0x01  # outer CRC wrong: the session drops (and counts) the frame
                self.emit(bytes(b), 0.003 + i * 0.001)
        elif how == "tail":
            self.stream(self.stream_for(slot)[1:])
        elif how != "empty":
            _late, delay = how
            self.stream(self.stream_for(slot), delay=delay)
        return True

    def receive(self, wire):
        f = ht.parse_frame(wire)
        if self.scripted_read(wire, f):
            return
        super().receive(wire)  # logs the frame; hello / reads / host ACKs
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
        elif w == ht.session_open() or w == ht.ack(1):
            continue  # the session open + its ident ACK are part of hello() (power-cycled pedal fix, 2026-10-04)
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


def run_on(pedal, monkeypatch, slot=SLOT, placeholder=False, **opts):
    o = dict(OPTS)
    o.update(opts)
    s = ht_scan.Session(pedal.inp, pedal.out, to_message=FakeMsg, **o)
    monkeypatch.setattr(ht_scan, "Session", _second_session)  # R-ONE-SESSION
    lines = []
    results = hwv.run(slot, session=s, log=lines.append, placeholder=placeholder)
    return results, lines, s


def oks(results):
    return [ok for _label, ok, _detail in results]


def step_lines(lines):
    return [ln for ln in lines if ln[:1].isdigit()]


READS_BEFORE_WRITE = ["hello", "read 65535", "ack 0x21", "read 199", "read 199", "read 0", "ack 0x0c"]
DISPLAY_CHECK = ("select preset 200 on the pedal's display (internal slot 199; Suite numbers presets from 001) "
                 "and confirm the screen shows 'New GEN.'")


def user_preset(index=SLOT):
    b = bytearray(evidence("099-Finger_AC.prst"))
    b[f150.IDX_OFF] = index
    return bytes(b)


def no_write(p):
    return p.imports == [] and not any(w[3] == ht.FAMILY_PATCH for w in p.sent)


# --- the whole protocol --------------------------------------------------------------

def test_all_six_steps_pass_with_exactly_one_ack_per_0x08(monkeypatch, midi_port_guard, backup_dir):
    p = WritePedal()
    calls = []
    real_send = dw.send_stream

    def spy(*a, **k):
        calls.append((a, k))
        return real_send(*a, **k)

    monkeypatch.setattr(dw, "send_stream", spy)
    results, lines, s = run_on(p, monkeypatch)

    assert oks(results) == [True] * 7, "\n".join(lines)
    assert [ln.split(" ", 2)[:2] for ln in step_lines(lines)] == [[str(n), "PASS"] for n in range(0, 7)]
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
    assert dw.WRITE_VERIFIED["gp150"] is True  # flipped after the 2026-10-04 hardware run; the script still passes allow_unverified

    # backed up before the first write: the active preset + exactly what went to 199
    sent = bytearray(want)
    sent[0x0A] = CORPUS_PRST[0x0A]  # the import marker is set by the builder, not in the file
    files = {n: open(os.path.join(backup_dir, n), "rb").read() for n in os.listdir(backup_dir)}
    assert files == {"100-It_s_GP_150.prst": ACTIVE_PRST, "199-WRITE_TEST.prst": bytes(sent)}
    backups = [ln for ln in lines if ln.startswith("  backup: ")]
    assert backups == [f"  backup: active preset -> {os.path.join(backup_dir, '100-It_s_GP_150.prst')}",
                       f"  backup: write-test preset (for slot 199) -> {os.path.join(backup_dir, '199-WRITE_TEST.prst')}"]
    assert lines.index(backups[-1]) < lines.index(step_lines(lines)[3])  # before step 3's write

    # step 2's proof left the session as it found it
    assert s.empty_timeout == OPTS["empty_timeout"] and s.log is ht_scan._default_log
    assert "exchange" not in vars(s) and s.exchange.__func__ is type(s).exchange

    # what the user records
    assert "slot 199 recorded empty-acked; 199 presets, 1 empty-acked, 0 empty-unacked, 0 errors" in results[0][2]
    assert "read empty twice (last_status=empty-acked, empty-acked" in results[2][2]
    assert "back[0x0A] = 0x5c" in results[4][2]
    assert "ignored differences: 0x00a" in results[4][2]  # 0x5C stored vs the export's 0x58
    assert "New GEN." in results[5][2]
    text = "\n".join(lines)
    assert DISPLAY_CHECK in text
    assert "§8.2 answered 2026-10-04: an import into the active slot is not heard/shown until the preset is re-selected on the pedal" in text
    assert midi_port_guard == []


def test_an_occupied_slot_fails_step_2_and_nothing_is_sent_after_it(monkeypatch, midi_port_guard, backup_dir):
    user = user_preset()
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 2 + [False], "\n".join(lines)
    detail = results[2][2]
    assert f150.read_name(user) in detail and "refus" in detail and "last_status=preset" in detail
    assert HINT not in detail  # nothing was written: no read-back needed
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "ack 0x21"]  # then nothing at all
    assert no_write(p) and p.presets[SLOT] == user
    assert step_lines(lines)[-1].startswith("2 FAIL")
    assert not os.path.exists(backup_dir)  # backups come right before the first write only
    assert midi_port_guard == []


# --- step 2: a None read is not proof (fix round 1) ---------------------------------

@pytest.mark.parametrize("settle, late_pump, where", [
    (0.08, 0.05, "the read's own settle"),  # the reviewer's case: chunks ACKed + discarded while settling
    (0.005, 0.3, "the trailing watch"),  # chunks that come even later
])
def test_a_late_stream_fails_step_2_scenario_a(monkeypatch, midi_port_guard, settle, late_pump, where):
    # ACK, then the full slot's stream 0.09 s later, past the (scaled) 0.06 s empty timeout:
    # read() returns None with last_status=empty-acked, the session discards the chunks
    monkeypatch.setattr(hwv, "LATE_STREAM_PUMP", late_pump)
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()}, script={SLOT: [("late", 0.09)]})
    results, lines, s = run_on(p, monkeypatch, settle=settle)
    assert oks(results) == [True] * 2 + [False], where + "\n" + "\n".join(lines)
    assert "arrived late" in results[2][2] and "probably NOT empty" in results[2][2]
    assert no_write(p), where
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "ack 0x21"]  # the ACK = session duty
    assert s.empty_timeout == OPTS["empty_timeout"]  # restored after the proof
    assert s.log is ht_scan._default_log and "exchange" not in vars(s)
    assert midi_port_guard == []


def test_the_long_empty_timeout_reads_a_slow_stream_as_a_preset(monkeypatch, midi_port_guard):
    monkeypatch.setattr(hwv, "EMPTY_PROOF_TIMEOUT", 0.3)  # >= the stream's delay: the read waits for it
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()}, script={SLOT: [("late", 0.09)]})
    results, lines, s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 2 + [False], "\n".join(lines)
    assert "holds a preset" in results[2][2]
    assert no_write(p) and s.empty_timeout == OPTS["empty_timeout"]
    assert midi_port_guard == []


def test_lost_requests_fail_step_2_scenario_b(monkeypatch, midi_port_guard):
    # the first two requests for the (full) slot are lost; the hello probe is answered:
    # read() returns None with last_status=empty-unacked — no override exists
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()}, script={SLOT: ["drop", "drop"]})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 2 + [False], "\n".join(lines)
    assert results[2][2].endswith("slot 199 gave no ACK — cannot distinguish empty from lost request; "
                                  "answer DEVICE_READ question (a) with a full scan first")
    assert no_write(p)
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "read 199", "hello"]
    assert midi_port_guard == []


def test_the_second_read_must_confirm_the_first(monkeypatch, midi_port_guard):
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()}, script={SLOT: ["empty"]})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 2 + [False], "\n".join(lines)
    assert results[2][2].startswith("read 2: slot 199 holds a preset")
    assert no_write(p)
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "read 199", "ack 0x21"]
    assert midi_port_guard == []


def test_corrupt_frames_during_the_empty_check_fail_step_2(monkeypatch, midi_port_guard):
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()}, script={SLOT: ["corrupt"]})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 2 + [False], "\n".join(lines)
    assert "corrupt frame" in results[2][2] and "cannot prove slot 199 empty" in results[2][2]
    assert no_write(p)
    assert midi_port_guard == []


def test_a_partial_stream_the_read_retried_past_fails_step_2(monkeypatch, midi_port_guard):
    # first exchange: ACK + a headless tail (valid CRCs) -> read() retries; second:
    # ACK + silence -> read() returns None, empty-acked. The exchange tap catches it.
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: user_preset()},
                   script={SLOT: ["tail", "empty", "empty"]})
    results, lines, s = run_on(p, monkeypatch)
    assert oks(results) == [True, True, False], "\n".join(lines)
    detail = results[2][2]
    assert detail.startswith("read 1: a partial preset stream was seen (9 chunk frame(s))")
    assert "slot 199 is probably NOT empty" in detail
    assert no_write(p)
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21", "read 199", "ack 0x21", "read 199"]
    assert "exchange" not in vars(s) and s.log is ht_scan._default_log
    assert midi_port_guard == []


# --- step 0: the full backup scan is required (no port before it passes) -----------------

def test_no_scan_summary_fails_step_0_and_opens_no_port(midi_port_guard, capsys):
    os.remove(hwv.SCAN_SUMMARY)
    results = hwv.run(SLOT)  # no session: a real one would be opened right after step 0
    assert oks(results) == [False]
    assert "no scan summary" in results[0][2] and results[0][2].endswith(hwv.SCAN_CMD)
    assert hwv.SCAN_CMD == "./.venv-midi/bin/python patch/ht_scan.py scan"
    assert hwv.main([str(SLOT)]) == 1
    out = capsys.readouterr().out
    assert "0 FAIL scan precondition" in out and "no MIDI port was opened" in out
    assert midi_port_guard == []


@pytest.mark.parametrize("case, want", [
    ("preset", "recorded slot 199 as 'preset' ('New GEN.'), not 'empty-acked'"),
    ("empty-unacked", "recorded slot 199 as 'empty-unacked', not 'empty-acked'"),
    ("error", "recorded slot 199 as 'error', not 'empty-acked'"),
    ("missing", "covers 199 of 200 slots"),
    ("aborted", "was aborted (pedal not responding)"),
    ("garbled", "cannot read the scan summary"),
    ("not-a-summary", "is not an ht_scan.py scan summary"),
])
def test_a_scan_summary_that_does_not_show_the_slot_empty_fails_step_0(
        case, want, scan_summary, summary_template, midi_port_guard):
    t = summary_template
    if case == "preset":
        scan_summary(full_summary(t, target=dict(t["slots"]["0"], index=SLOT)))
    elif case in ("empty-unacked", "error"):
        scan_summary(full_summary(t, target={"status": case, "ms": 3000}))
    elif case == "missing":
        scan_summary(full_summary(t, drop_target=True))
    elif case == "aborted":
        scan_summary(full_summary(t, aborted="pedal not responding"))
    elif case == "garbled":
        scan_summary(raw='{"slots": {')
    else:
        scan_summary(raw="[]")
    lines = []
    results = hwv.run(SLOT, log=lines.append)
    assert oks(results) == [False], "\n".join(lines)
    assert want in results[0][2] and hwv.SCAN_CMD in results[0][2]
    assert midi_port_guard == []  # step 0 stops before any port


def test_a_scan_with_unreadable_slots_still_passes_step_0_but_says_so(scan_summary, summary_template):
    summary = full_summary(summary_template)
    summary["slots"]["5"] = {"status": "error", "error": "preset read timed out", "ms": 6000}
    scan_summary(summary)
    detail = hwv.scan_precondition(SLOT)
    assert "198 presets, 1 empty-acked, 0 empty-unacked, 1 errors" in detail
    assert "slots [5] could not be read and are NOT in the backup" in detail


def test_step_2_timings_on_hardware_and_the_late_chunk_log_line(monkeypatch):
    assert REAL_STEP2[0] >= 3.0 and REAL_STEP2[1] >= 2.0
    # the tap matches exactly what Session.pump logs for a discarded preset chunk
    p = WritePedal()
    seen = []
    s = ht_scan.Session(p.inp, p.out, to_message=FakeMsg, log=lambda lvl, msg: seen.append(msg), **OPTS)
    p.stream(None)
    s.pump(0.05)
    assert seen and all(hwv.LATE_CHUNK_LOG in m for m in seen)


def test_the_active_slot_is_refused_at_step_1(monkeypatch, midi_port_guard):
    p = WritePedal(presets={0: None, ACTIVE: user_preset(SLOT)})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True, False], "\n".join(lines)
    assert "the active preset is slot 199" in results[1][2]
    assert wire_log(p) == ["hello", "read 65535", "ack 0x21"] and no_write(p)
    assert midi_port_guard == []


def test_a_failed_backup_fails_step_3_before_anything_is_sent(monkeypatch, midi_port_guard, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"")
    monkeypatch.setattr(hwv, "BACKUP_DIR", str(blocker / "write_verify_backup"))
    p = WritePedal()
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 3 + [False], "\n".join(lines)
    assert "could not save the backups" in results[3][2] and "nothing was sent" in results[3][2]
    assert results[3][2].endswith(HINT)
    assert no_write(p) and wire_log(p) == READS_BEFORE_WRITE
    assert midi_port_guard == []


def test_a_write_without_0x08_fails_step_3_with_the_read_back_hint_and_stops(monkeypatch, midi_port_guard):
    p = WritePedal(notify=False)  # ACKs the import, never sends the 0x08 (waits the real 3 s)
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 3 + [False], "\n".join(lines)
    detail = results[3][2]
    assert "0x08" in detail and detail.endswith(HINT)
    assert detail.count("read slot 199 back") == 1  # device_write's own hint is not doubled
    assert step_lines(lines)[-1].startswith("3 FAIL") and step_lines(lines)[-1].endswith(HINT)
    assert wire_log(p) == READS_BEFORE_WRITE + ["import 10"]  # no read-back, no blank, no retry
    assert len(p.imports) == 1
    assert midi_port_guard == []


def test_a_0x08_without_an_ack_still_passes_step_3_and_says_so(monkeypatch, midi_port_guard):
    p = WritePedal(ack_import=False)  # send_stream counts the 0x08 alone as done ({"acks": 0})
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 7, "\n".join(lines)
    assert "NO ACK" in results[3][2] and "NO ACK" in results[5][2]
    assert len(p.acks_for(0x0D)) == 1 and len(p.acks_for(0x0E)) == 1
    assert midi_port_guard == []


def test_a_read_back_that_differs_fails_step_4_with_the_offsets_and_stops(monkeypatch, midi_port_guard):
    p = WritePedal(store=lambda prst: flip(prst, 0x30, 0x200))
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 4 + [False], "\n".join(lines)
    detail = results[4][2]
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
    assert oks(results) == [True] * 7, "\n".join(lines)
    detail = results[4][2]
    assert "back[0x0A] = 0x58" in detail  # == the source export's 0x0A, so no difference there
    assert "ignored differences: 0x00d 0x00e 0x00f" in detail  # recorded, but ignored
    assert midi_port_guard == []


def test_a_changed_active_preset_fails_step_6(monkeypatch, midi_port_guard):
    p = WritePedal(import_sets_active=True)  # as if the pedal loaded the imported preset
    results, lines, _s = run_on(p, monkeypatch)
    assert oks(results) == [True] * 6 + [False], "\n".join(lines)
    assert "changed" in results[6][2] and results[6][2].endswith(HINT)
    assert "select preset 200" not in "\n".join(lines)  # no manual check after a FAIL
    assert midi_port_guard == []


def test_no_handshake_fails_step_1_and_sends_nothing_else(monkeypatch, midi_port_guard):
    p = WritePedal(dead=True)
    results, _lines, _s = run_on(p, monkeypatch, timeout=0.2)
    assert oks(results) == [True, False] and "handshake" in results[1][2]
    assert wire_log(p) == ["hello"]
    assert midi_port_guard == []


# --- pieces ---------------------------------------------------------------------------

def test_same_except_ignores_only_the_device_owned_bytes():
    # 0x0A import marker (0x5C sent, 0x58 stored), 0x0D-0x0F device-written field,
    # 0x43C "saved on the pedal" flag, 0x445 enable bits (bit0 MOD .. bit3 VOL; the pedal
    # set bit 1 when a delay became active, Task 13 checklist 2026-10-04)
    a = bytes(CORPUS_PRST)
    assert hwv.IGNORE == (0x0A, 0x0D, 0x0E, 0x0F, 0x43C, 0x445)
    assert hwv.same_except(a, a)
    assert hwv.same_except(a, flip(a, 0x0A, 0x0D, 0x0E, 0x0F, 0x43C, 0x445))
    for off in (0x00, 0x04, 0x09, 0x0B, 0x0C, 0x10, 0x2C, 0x78, 0x84, 0x3B4, 0x43B, 0x43D, 0x444, 0x446, len(a) - 1):
        assert not hwv.same_except(a, flip(a, off)), hex(off)
    assert not hwv.same_except(a, a[:-1])
    assert hwv.same_except(a, flip(a, 0x30), ignore=(0x30,))
    assert hwv.diff_offsets(a, flip(a, 0x0A, 0x30)) == [0x0A, 0x30]


def test_slot_is_checked_before_any_port_is_opened(midi_port_guard):
    assert (hwv.display_number(0), hwv.display_number(199)) == (1, 200)  # the pedal/Suite count from 001
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
        [str(n), "PASS"] for n in range(0, 7)]
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
    assert oks(results) == [True, False] and "no MIDI port" in results[1][2]
    assert hwv.main([str(SLOT)]) == 1
    assert midi_port_guard == []


# --- placeholder mode: a pedal with no empty slot (seen 2026-10-04: slots 111..199 hold
# 89 byte-identical factory "It's GP-150" presets) --------------------------------------
PH_NAME = "It's GP-150"


def placeholder_bytes(slot):
    b = bytearray(f150.blank(slot))
    f150.write_name(b, PH_NAME)
    return bytes(b)


@pytest.fixture
def placeholder_scan(scan_summary, summary_template):
    """A completed scan where SLOT and 20 twins hold the same factory placeholder, with
    the scanned .prst files on disk next to the summary (as ht_scan.scan() leaves them)."""
    summary = full_summary(summary_template)
    d = os.path.dirname(hwv.SCAN_SUMMARY)
    for i in range(SLOT - 20, SLOT + 1):
        fn = f"{i:03d}-It_s_GP_150.prst"
        with open(os.path.join(d, fn), "wb") as fh:
            fh.write(placeholder_bytes(i))
        summary["slots"][str(i)] = {"status": "preset", "ms": 300, "name": PH_NAME, "index": i, "file": fn}

    def rewrite(mutate=None):
        if mutate:
            mutate(summary, d)
        scan_summary(summary)

    rewrite()
    return rewrite


def test_placeholder_mode_writes_then_restores_the_original(placeholder_scan, monkeypatch, midi_port_guard):
    ph = placeholder_bytes(SLOT)
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: ph})
    results, lines, _s = run_on(p, monkeypatch, placeholder=True)
    assert oks(results) == [True] * 7, "\n".join(lines)
    assert [label for label, _ok, _d in results] == hwv.PLACEHOLDER_STEPS
    assert len(p.imports) == 2 and f150.read_name(p.imports[0]) == hwv.TEST_NAME
    assert f150.read_name(p.presets[SLOT]) == PH_NAME and hwv.same_except(p.presets[SLOT], ph)
    assert any(PH_NAME in ln for ln in lines if ln.startswith("  a.")), "manual check names the restored preset"
    assert midi_port_guard == []


def test_placeholder_mode_refuses_a_slot_with_too_few_twins(placeholder_scan, monkeypatch, midi_port_guard):
    def unique(summary, d):  # SLOT's bytes differ from every other scanned preset
        fn = summary["slots"][str(SLOT)]["file"]
        with open(os.path.join(d, fn), "wb") as fh:
            fh.write(flip(placeholder_bytes(SLOT), 0x30, 0x31))
    placeholder_scan(unique)
    lines = []
    results = hwv.run(SLOT, log=lines.append, placeholder=True)  # no session: step 0 must stop it
    assert oks(results) == [False] and "placeholder" in results[0][2] and midi_port_guard == []


def test_placeholder_mode_refuses_when_the_pedal_no_longer_holds_the_scanned_bytes(placeholder_scan, monkeypatch, midi_port_guard):
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST, SLOT: flip(placeholder_bytes(SLOT), 0x30)})
    results, lines, _s = run_on(p, monkeypatch, placeholder=True)
    assert oks(results) == [True, True, False], "\n".join(lines)
    assert p.imports == [] and midi_port_guard == []


def test_placeholder_mode_refuses_an_empty_slot(placeholder_scan, monkeypatch, midi_port_guard):
    p = WritePedal(presets={0: None, ACTIVE: ACTIVE_PRST})  # SLOT reads empty now
    results, lines, _s = run_on(p, monkeypatch, placeholder=True)
    assert oks(results) == [True, True, False] and p.imports == []


def test_placeholder_mode_is_off_by_default(placeholder_scan, midi_port_guard):
    results = hwv.run(SLOT, log=lambda _l: None)
    assert oks(results) == [False] and "not 'empty-acked'" in results[0][2] and midi_port_guard == []
