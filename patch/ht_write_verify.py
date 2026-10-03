#!/usr/bin/env python3
"""GP-150 write verification (plan Task 12) — run ONCE, by hand, with the user
watching the pedal (pedal on USB, Valeton Suite closed, the target slot empty):

  ./.venv-midi/bin/python patch/ht_write_verify.py 199

Six steps, each printed as `N PASS|FAIL <step> — <detail>`. It stops at the first
FAIL, and it never writes unless step 2 proved the target slot EMPTY.

 1. hello; read the active preset (kept for step 6)
 2. read <slot>: must be empty; prints `last_status` (empty-acked = ACK, then
    silence; empty-unacked = no ACK, but the pedal answered a hello)
 3. import a copy of slot 0 renamed "WRITE TEST" into <slot>: the pedal must ACK
    the import and send its 0x08 "import done"
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
WRITE_VERIFIED["gp150"] — that is flipped by hand, and only after six PASS lines.
The MIDI ports are opened only by ht_scan.Session (open_ports is deliberately
never imported by name here, so the test suite's guard on ht_scan.open_ports
stands between any test and the real pedal).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from patch import device_write as dw  # noqa: E402
from patch import ht_proto as ht  # noqa: E402
from patch import ht_scan  # noqa: E402 — ht_scan.Session only
from patch import prst150_format as f150  # noqa: E402

IGNORE = (0x0A, 0x0D, 0x0E, 0x0F)  # import marker (0x5C vs 0x58) + device-written field
SOURCE_SLOT = 0  # the preset copied for the write test (a factory preset)
TEST_NAME = "WRITE TEST"
BLANK_NAME = "New GEN."
STEPS = [
    "hello + read active",
    "target slot is empty",
    "import WRITE TEST",
    "read back == sent",
    "import blank, read back",
    "active preset unchanged",
]
SHOW = 24  # differing offsets listed before "… (+n more)"
READ_CMD = "./.venv-midi/bin/python patch/ht_scan.py read {slot}"

Result = Tuple[str, bool, str]  # (step label, passed, detail)


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


def _without_read_back_hint(msg: str, slot: int) -> str:
    # device_write's errors end with their own read-back hint; ours replaces it
    tail = " — " + dw._read_back(slot)
    return msg[:-len(tail)] if msg.endswith(tail) else msg


def run(slot: int, session=None, log: Callable[[str], None] = print) -> List[Result]:
    """The six-step protocol on ONE session (`session`, or a new ht_scan.Session()
    on the GP-150 ports when None — closed again at the end). Logs one line per
    step and stops at the first FAIL. Returns [(step label, passed, detail)]."""
    _check_slot(slot)
    hint = f"read slot {slot} back before retrying"
    results: List[Result] = []
    state: Dict[str, bytes] = {}

    def record(passed: bool, detail: str) -> None:
        n = len(results) + 1
        label = STEPS[n - 1]
        results.append((label, passed, detail))
        log(f"{n} {'PASS' if passed else 'FAIL'} {label} — {detail}")

    def stopped(n: int) -> None:
        log(f"STOPPED at step {n} — do not retry blindly, and do not flip WRITE_VERIFIED['gp150'].")
        if n >= 3:
            log(f"  read the slot back now: {READ_CMD.format(slot=slot)}")
        else:
            log("  nothing was written to the pedal.")
        log("  power-cycle the pedal if it stopped responding; record this output in re/gp150/DEVICE_WRITE.md")

    log(f"GP-150 write verification: target slot {slot}, source slot {SOURCE_SLOT}, "
        f"WRITE_VERIFIED['gp150'] = {dw.WRITE_VERIFIED.get('gp150')} (this run passes allow_unverified)")
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
        state["active"] = active
        return f"handshake answered; active preset {_preset(active)}, {len(active)} bytes"

    def step2() -> str:
        cur = s.read(slot)
        status = s.last_status
        if cur is not None:
            raise Fail(f"slot {slot} holds a preset ({_preset(cur)}; last_status={status}) — "
                       "refusing to overwrite it; pick an empty slot")
        return f"slot {slot} is empty (last_status={status}: {ht_scan.EMPTY_WHY.get(status or '', status)})"

    def step3() -> str:
        src = s.read(SOURCE_SLOT)
        if src is None:
            raise Fail(f"slot {SOURCE_SLOT} is empty — no source preset to copy; nothing was sent")
        b = bytearray(src)
        f150.write_name(b, TEST_NAME)
        state["src"] = bytes(b)
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
    log("manual checks (record the answers in re/gp150/DEVICE_WRITE.md):")
    log(f"  a. select slot {slot} on the pedal — does it play? (it now holds {BLANK_NAME!r})")
    log("  b. optional, spec §8.2: write the ACTIVE preset back to itself renamed — does the "
        "screen/sound update without a reselect? (procedure in re/gp150/DEVICE_WRITE.md)")
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
              f"{READ_CMD.format(slot=args.slot)}")
        return 130
    return 0 if len(results) == len(STEPS) and all(ok for _label, ok, _detail in results) else 1


if __name__ == "__main__":
    sys.exit(main())
