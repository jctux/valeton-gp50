#!/usr/bin/env python3
"""GP-150 write verification (plan Task 12) — run ONCE, by hand, with the user
watching the pedal (pedal on USB, Valeton Suite closed, the target slot empty).
REQUIRED first: a completed `./.venv-midi/bin/python patch/ht_scan.py scan` (all 200
slots saved under device_scan_gp150/) — that is the real backup; step 0 enforces it.

  ./.venv-midi/bin/python -u patch/ht_write_verify.py 199

Seven steps, 0..6, each printed as `N PASS|FAIL <step> — <detail>`. It stops at the
first FAIL, and it never writes unless steps 0 and 2 showed the target slot EMPTY.

 0. scan precondition (no port is opened): device_scan_gp150/scan_summary.json from
    `ht_scan.py scan` exists, was not aborted, covers all 200 slots, and records
    <slot> as `empty-acked`
 1. hello; read the active preset (kept for step 6); refuse if it IS <slot>
 2. prove <slot> empty: two reads, each with >= EMPTY_PROOF_TIMEOUT s of silence
    after the ACK and LATE_STREAM_PUMP s of watching the input afterwards. FAIL if
    either read returns a preset, if a preset chunk arrives late (the session
    discards and logs it), if any exchange of the read saw a chunk frame (a partial
    stream the read retried past), if corrupt frames arrive, or if the pedal did
    not ACK the request (`empty-unacked`: an empty slot and a lost request look the
    same)
 3. back up the active preset and the write-test preset under
    device_scan_gp150/write_verify_backup/; import a copy of slot 0 renamed
    "WRITE TEST" into <slot>: the pedal must ACK the import and send its 0x08
    "import done"
 4. read <slot> back: byte-identical to what was sent, except 0x0A (the import
    sends 0x5C, exports carry 0x58; `back[0x0A]` is printed) and 0x0D..0x0F
    (device-written); the differing offsets are printed
 5. import blank(<slot>) ("New GEN.") the same way; read back: name "New GEN."
 6. read the active preset again: byte-identical to step 1's (reads and writes to
    another slot must not touch it)

From step 3 on, every FAIL ends with "read slot <slot> back before retrying": the
slot may or may not hold the import (`./.venv-midi/bin/python patch/ht_scan.py read <slot>`).

ONE ht_scan.Session carries every read and both writes (a second session on the
port would ACK the pedal's 0x08 twice). The writes go through the gated sender,
device_write.send_stream(None, pk, confirm=True, validated=ok,
allow_unverified=True, session=s); this script never changes
WRITE_VERIFIED["gp150"] — that is flipped by hand, and only after seven PASS lines.
The MIDI ports are opened only by ht_scan.Session (open_ports is deliberately
never imported by name here, so the test suite's guard on ht_scan.open_ports
stands between any test and the real pedal).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BACKUP_DIR = os.path.join(ROOT, "device_scan_gp150", "write_verify_backup")  # gitignored
SCAN_SUMMARY = os.path.join(ROOT, "device_scan_gp150", "scan_summary.json")  # written by `ht_scan.py scan`
SCAN_CMD = "./.venv-midi/bin/python patch/ht_scan.py scan"
from patch import device_write as dw  # noqa: E402
from patch import ht_proto as ht  # noqa: E402
from patch import ht_scan  # noqa: E402 — ht_scan.Session only
from patch import prst150_format as f150  # noqa: E402

IGNORE = (0x0A, 0x0D, 0x0E, 0x0F)  # import marker (0x5C vs 0x58) + device-written field
SOURCE_SLOT = 0  # the preset copied for the write test (a factory preset)
TEST_NAME = "WRITE TEST"
BLANK_NAME = "New GEN."
STEPS = [  # index == step number
    "scan precondition",
    "hello + read active",
    "target slot is empty",
    "import WRITE TEST",
    "read back == sent",
    "import blank, read back",
    "active preset unchanged",
]
SHOW = 24  # differing offsets listed before "… (+n more)"
READ_CMD = "./.venv-midi/bin/python patch/ht_scan.py read {slot}"
# Step 2 (proving the slot empty). A single read(slot) -> None is not proof: a stream
# that starts after the session's empty_timeout reads as "empty-acked" (its chunks
# are then ACKed and discarded by Session.pump, which logs LATE_CHUNK_LOG), and two
# lost requests plus an answered hello read as "empty-unacked".
EMPTY_PROOF_TIMEOUT = 3.0  # s of silence after the ACK before the slot counts as empty
LATE_STREAM_PUMP = 2.0  # s the input is watched after each empty read for a late stream
LATE_CHUNK_LOG = "unsolicited frame family 0x70"  # ht_scan.Session.pump's line for a discarded preset chunk

Result = Tuple[str, bool, str]  # (step label, passed, detail)
_UNSET = object()


class Fail(Exception):
    """A step's FAIL; the message is the detail."""


def diff_offsets(a: bytes, b: bytes) -> List[int]:
    """Every offset where a and b differ (a length difference counts as differing)."""
    n = min(len(a), len(b))
    return [i for i in range(n) if a[i] != b[i]] + list(range(n, max(len(a), len(b))))


def same_except(a: bytes, b: bytes, ignore: Sequence[int] = IGNORE) -> bool:
    return len(a) == len(b) and all(i in ignore for i in diff_offsets(a, b))


def _offsets(offs: Sequence[int]) -> str:
    if not offs:
        return "none"
    more = f" … (+{len(offs) - SHOW} more)" if len(offs) > SHOW else ""
    return " ".join(f"{i:#05x}" for i in offs[:SHOW]) + more


def _preset(prst: bytes) -> str:
    return f"index {f150.read_index(prst)} {f150.read_name(prst)!r}"


def _check_slot(slot) -> None:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < ht_scan.N_SLOTS:
        raise ValueError(f"slot must be an int 1..{ht_scan.N_SLOTS - 1}, not {slot!r}")
    if slot == SOURCE_SLOT:
        raise ValueError(f"slot {SOURCE_SLOT} holds the source of the test preset; pick an empty slot (199)")


def display_number(slot: int) -> int:
    """The preset number the pedal's display (and Suite) shows: 001-based."""
    return slot + 1


def scan_precondition(slot: int, path: Optional[str] = None) -> str:
    """Step 0, before any port is opened: the full backup scan (`ht_scan.py scan`)
    must have completed and recorded `slot` as "empty-acked". Same schema as
    ht_scan.scan(): {"slots": {"<slot>": {"status": "preset" | "empty-acked" |
    "empty-unacked" | "error", ...}, ...}, "aborted": "<why>" when it gave up}.
    Raises Fail (naming the scan command); returns the PASS detail."""
    path = SCAN_SUMMARY if path is None else path
    redo = f"run the full backup scan first: {SCAN_CMD}"
    try:
        with open(path) as fh:
            summary = json.load(fh)
    except FileNotFoundError:
        raise Fail(f"no scan summary at {path} — {redo}")
    except (OSError, ValueError) as e:
        raise Fail(f"cannot read the scan summary {path} ({e}) — {redo}")
    slots = summary.get("slots") if isinstance(summary, dict) else None
    if not isinstance(slots, dict):
        raise Fail(f"{path} is not an ht_scan.py scan summary (no 'slots') — {redo}")
    if summary.get("aborted"):
        raise Fail(f"the scan in {path} was aborted ({summary['aborted']}) — {redo}")
    missing = [i for i in range(ht_scan.N_SLOTS) if str(i) not in slots]
    if missing:
        raise Fail(f"the scan in {path} covers {ht_scan.N_SLOTS - len(missing)} of {ht_scan.N_SLOTS} slots "
                   f"(missing e.g. {missing[:5]}) — {redo}")
    entry = slots[str(slot)]
    status = entry.get("status") if isinstance(entry, dict) else None
    if status != "empty-acked":
        what = f" ({entry.get('name')!r})" if status == "preset" and isinstance(entry, dict) else ""
        raise Fail(f"the scan in {path} recorded slot {slot} as {status!r}{what}, not 'empty-acked' — pick a slot "
                   f"that a full scan ({SCAN_CMD}) lists as empty-acked")
    counts = {k: sum(1 for e in slots.values() if isinstance(e, dict) and e.get("status") == k)
              for k in ("preset", "empty-acked", "empty-unacked", "error")}
    errors = sorted(int(k) for k, e in slots.items() if isinstance(e, dict) and e.get("status") == "error")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    return (f"{path} ({when}): slot {slot} recorded empty-acked; {counts['preset']} presets, "
            f"{counts['empty-acked']} empty-acked, {counts['empty-unacked']} empty-unacked, {counts['error']} errors"
            + (f" (slots {errors} could not be read and are NOT in the backup)" if errors else ""))


def _without_read_back_hint(msg: str, slot: int) -> str:
    # device_write's errors end with their own read-back hint; ours replaces it
    tail = " — " + dw._read_back(slot)
    return msg[:-len(tail)] if msg.endswith(tail) else msg


def run(slot: int, session=None, log: Callable[[str], None] = print) -> List[Result]:
    """The protocol: step 0 (the scan summary; no port), then steps 1..6 on ONE
    session (`session`, or a new ht_scan.Session() on the GP-150 ports when None —
    opened only after step 0 passed, closed again at the end). Logs one line per
    step and stops at the first FAIL. Returns [(step label, passed, detail)],
    indexed by step number."""
    _check_slot(slot)
    hint = f"read slot {slot} back before retrying"
    results: List[Result] = []
    state: Dict[str, bytes] = {}

    def record(passed: bool, detail: str) -> None:
        n = len(results)  # step 0 first
        label = STEPS[n]
        results.append((label, passed, detail))
        log(f"{n} {'PASS' if passed else 'FAIL'} {label} — {detail}")

    def stopped(n: int) -> None:
        log(f"STOPPED at step {n} — do not retry blindly, and do not flip WRITE_VERIFIED['gp150'].")
        if n >= 3:
            log(f"  read the slot back now: {READ_CMD.format(slot=slot)}")
        elif n == 0:
            log("  no MIDI port was opened; nothing was written to the pedal.")
        else:
            log("  nothing was written to the pedal.")
        log("  power-cycle the pedal if it stopped responding; record this output in re/gp150/DEVICE_WRITE.md")

    log(f"GP-150 write verification: target slot {slot} (display {display_number(slot):03d}), "
        f"source slot {SOURCE_SLOT}, WRITE_VERIFIED['gp150'] = {dw.WRITE_VERIFIED.get('gp150')} "
        "(this run passes allow_unverified)")
    try:
        detail, passed = scan_precondition(slot), True
    except Fail as e:
        detail, passed = str(e), False
    except Exception as e:  # noqa: BLE001 — a malformed summary: FAIL, never a traceback
        detail, passed = f"{type(e).__name__} reading {SCAN_SUMMARY}: {e} — run the full backup scan first: {SCAN_CMD}", False
    record(passed, detail)
    if not passed:
        stopped(0)
        return results
    own = session is None
    s = session
    if own:
        try:
            s = ht_scan.Session(log=lambda level, msg: log(f"  [{level}] {msg}"))
        except Exception as e:  # noqa: BLE001 — no port / no mido: a step-1 FAIL, not a traceback
            record(False, f"could not open the GP-150 MIDI ports: {e}")
            stopped(1)
            return results

    def write(prst: bytes) -> Dict:
        pk = dw.build_gp150_write_stream(prst, slot)
        ok, why = dw.validate_gp150_stream(pk, slot=slot)
        if not ok:
            raise Fail(f"the import stream did not validate ({why}); nothing was sent")
        try:
            res = dw.send_stream(None, pk, confirm=True, validated=ok, allow_unverified=True, session=s)
        except RuntimeError as e:
            raise Fail(f"{_without_read_back_hint(str(e), slot)}; the slot may or may not have been written")
        if not (res.get("notified") and res.get("sent") == len(pk)):
            raise Fail(f"unexpected send result {res!r} (expected {len(pk)} chunks sent and the 0x08)")
        return res

    def sent_note(res: Dict) -> str:
        acks = "ACK" if res["acks"] else "NO ACK of the transfer id (note it)"
        return f"{res['sent']} chunks sent, {acks}, 0x08 'import done' received"

    def step1() -> str:
        if not s.hello():
            raise Fail("no handshake reply from the GP-150 (USB cable? Valeton Suite open?)")
        active = s.read(ht.SLOT_ACTIVE)
        if active is None or not f150.detect(active):
            got = "nothing" if active is None else f"{len(active)} bytes"
            raise Fail(f"the active-preset read returned {got}, not a 1128-byte GP-150 preset")
        if f150.read_index(active) == slot:
            raise Fail(f"the active preset is slot {slot} ({_preset(active)}) — refusing to write the slot "
                       "the pedal has loaded; select another preset on the pedal or pick another empty slot")
        state["active"] = active
        return f"handshake answered; active preset {_preset(active)}, {len(active)} bytes"

    def empty_read(n: int) -> str:
        """One guarded read of `slot`: a long empty_timeout, then LATE_STREAM_PUMP s
        of watching for a late stream, with the session's log tapped for discarded
        preset chunks. Raises Fail unless it shows the slot empty; nothing is written."""
        late: List[str] = []
        partial: List[int] = []  # chunk frames per exchange that saw any
        log0, timeout0, bad0 = s.log, s.empty_timeout, s.bad_frames
        own_exchange = vars(s).get("exchange", _UNSET)  # normally unset: the class method
        exchange0 = s.exchange

        def tap(level: str, msg: str) -> None:
            if LATE_CHUNK_LOG in msg:
                late.append(msg)
            log0(level, msg)

        def tapped_exchange(*a, **k):
            # read() retries past a headless / stalled stream; a chunk frame in ANY
            # of its exchanges means the slot sent preset data
            r = exchange0(*a, **k)
            chunks = sum(1 for f in r.frames if ht.is_chunk(f))
            if chunks:
                partial.append(chunks)
            return r

        s.log, s.empty_timeout, s.exchange = tap, max(timeout0, EMPTY_PROOF_TIMEOUT), tapped_exchange
        try:
            cur = s.read(slot)
            status = s.last_status
            if cur is None:
                s.pump(LATE_STREAM_PUMP)
        finally:
            s.log, s.empty_timeout = log0, timeout0
            if own_exchange is _UNSET:
                del s.exchange
            else:
                s.exchange = own_exchange
        if cur is not None:
            raise Fail(f"read {n}: slot {slot} holds a preset ({_preset(cur)}; last_status={status}) — "
                       "refusing to overwrite it; pick an empty slot")
        if late:
            raise Fail(f"read {n}: a preset stream arrived late ({len(late)} discarded 0x70 chunk(s)) — "
                       f"slot {slot} is probably NOT empty; nothing was written")
        if partial:
            raise Fail(f"read {n}: a partial preset stream was seen ({sum(partial)} chunk frame(s)) — "
                       f"slot {slot} is probably NOT empty; nothing was written")
        if s.bad_frames != bad0:
            raise Fail(f"read {n}: {s.bad_frames - bad0} corrupt frame(s) arrived — cannot prove slot {slot} "
                       "empty; nothing was written")
        if status == "empty-unacked":
            raise Fail(f"read {n}: slot {slot} gave no ACK — cannot distinguish empty from lost request; "
                       "answer DEVICE_READ question (a) with a full scan first")
        if status != "empty-acked":
            raise Fail(f"read {n}: unexpected read status {status!r}; nothing was written")
        return status

    def step2() -> str:
        statuses = [empty_read(1), empty_read(2)]
        return (f"slot {slot} read empty twice (last_status={', '.join(statuses)}: "
                f"{ht_scan.EMPTY_WHY['empty-acked']}; {max(s.empty_timeout, EMPTY_PROOF_TIMEOUT):g} s of silence "
                f"after each ACK, no late stream in the {LATE_STREAM_PUMP:g} s after)")

    def step3() -> str:
        src = s.read(SOURCE_SLOT)
        if src is None:
            raise Fail(f"slot {SOURCE_SLOT} is empty — no source preset to copy; nothing was sent")
        b = bytearray(src)
        f150.write_name(b, TEST_NAME)
        state["src"] = bytes(b)
        sent = bytearray(b)
        f150.write_index(sent, slot)
        try:  # before the first write: the active preset + exactly what goes to <slot>
            for what, prst, n in (("active preset", state["active"], f150.read_index(state["active"])),
                                  (f"write-test preset (for slot {slot})", bytes(sent), slot)):
                log(f"  backup: {what} -> {ht_scan.save(prst, n, BACKUP_DIR)}")
        except OSError as e:
            raise Fail(f"could not save the backups under {BACKUP_DIR} ({e}); nothing was sent")
        res = write(state["src"])
        return f"copy of slot {SOURCE_SLOT} ({_preset(src)}) renamed {TEST_NAME!r} -> slot {slot}: {sent_note(res)}"

    def step4() -> str:
        back = s.read(slot)
        if back is None:
            raise Fail(f"slot {slot} reads back EMPTY (last_status={s.last_status}) after the import")
        sent = bytearray(state["src"])
        f150.write_index(sent, slot)
        offs = diff_offsets(back, bytes(sent))
        real = [i for i in offs if i not in IGNORE]
        ignored = f"ignored differences: {_offsets([i for i in offs if i in IGNORE])}"
        b0a = f"back[0x0A] = {back[0x0A]:#04x}"
        if not same_except(back, bytes(sent)):
            raise Fail(f"read back {_preset(back)} differs from what was sent at {len(real)} offset(s): "
                       f"{_offsets(real)}; {b0a}; {ignored}")
        return f"read back {_preset(back)} == sent; {b0a} (the import sent {ht.IMPORT_BYTE_0A:#04x}); {ignored}"

    def step5() -> str:
        blank = f150.blank(slot)
        res = write(blank)
        back = s.read(slot)
        if back is None:
            raise Fail(f"slot {slot} reads back EMPTY (last_status={s.last_status}) after the blank import")
        name = f150.read_name(back)
        if name != BLANK_NAME:
            raise Fail(f"slot {slot} reads back {name!r}, expected {BLANK_NAME!r}")
        other = [i for i in diff_offsets(back, blank) if i not in IGNORE]
        return (f"blank({slot}): {sent_note(res)}; read back {_preset(back)}; "
                f"differences from blank({slot}) outside the ignored offsets: {_offsets(other)}")

    def step6() -> str:
        now = s.read(ht.SLOT_ACTIVE)
        before = state["active"]
        if now != before:
            if now is None:
                raise Fail("the active preset changed: the read returned nothing")
            offs = diff_offsets(now, before)
            raise Fail(f"the active preset changed: now {_preset(now)}, was {_preset(before)}; "
                       f"{len(offs)} offset(s) differ: {_offsets(offs)}")
        return f"identical to step 1's read ({_preset(now)})"

    try:
        for n, step in enumerate((step1, step2, step3, step4, step5, step6), 1):
            try:
                detail, passed = step(), True
            except Fail as e:
                detail, passed = str(e), False
            except Exception as e:  # noqa: BLE001 — pedal/port errors: a FAIL line, then stop
                detail, passed = f"{type(e).__name__}: {e}", False
            if not passed and n >= 3:
                detail = f"{detail} — {hint}"
            record(passed, detail)
            if not passed:
                stopped(n)
                return results
    finally:
        if own:
            s.close()
    log(f"all {len(STEPS)} steps PASS — copy this whole output into re/gp150/DEVICE_WRITE.md")
    log("manual check (record the answer in re/gp150/DEVICE_WRITE.md):")
    log(f"  a. select preset {display_number(slot):03d} on the pedal's display (internal slot {slot}; Suite "
        f"numbers presets from 001) and confirm the screen shows {BLANK_NAME!r}; play a few notes: does it sound?")
    log("  note: §8.2 (does the pedal refresh the active preset on rewrite?) will be a scripted, tested mode "
        "added in Task 13 — do not improvise it.")
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ht_write_verify.py",
        description="GP-150 supervised write verification (writes ONLY to an empty slot; stops at the first FAIL).")
    ap.add_argument("slot", type=int, help=f"an EMPTY slot 1..{ht_scan.N_SLOTS - 1} (use 199)")
    args = ap.parse_args(argv)
    try:
        _check_slot(args.slot)
    except ValueError as e:
        ap.error(str(e))
    try:
        results = run(args.slot)
    except KeyboardInterrupt:
        print(f"interrupted — if a write had started, read slot {args.slot} back before retrying: "
              f"{READ_CMD.format(slot=args.slot)}; power-cycle the pedal if hello gets no answer; "
              "do not flip the gate")
        return 130
    return 0 if len(results) == len(STEPS) and all(ok for _label, ok, _detail in results) else 1


if __name__ == "__main__":
    sys.exit(main())
