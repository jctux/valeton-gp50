#!/usr/bin/env python3
"""GP-150 read tool over USB MIDI (HT protocol). READ-ONLY: it never writes a preset.

  ./.venv-midi/bin/python patch/ht_scan.py hello
  ./.venv-midi/bin/python patch/ht_scan.py read 0          # name + saves device_scan_gp150/000-<name>.prst
  ./.venv-midi/bin/python patch/ht_scan.py read active     # the preset currently loaded on the pedal
  ./.venv-midi/bin/python patch/ht_scan.py scan            # all 200 slots (+ scan_summary.json)
  ./.venv-midi/bin/python patch/ht_scan.py scan --out DIR
  ./.venv-midi/bin/python patch/ht_scan.py watch           # re-read the active preset every 2 s and
                                                           # print byte diffs (switch a model on the
                                                           # pedal to learn its type code)

Wire rules (spec §2/§3.4, same as app/static/ht_transport.js): one persistent port
pair, one request at a time, 300 ms settle after each, tx ids 1..0x7F, the final
(short) chunk of every stream ACKed the moment it arrives (a late ACK makes the
pedal resend the whole stream), device short messages carrying a tx id ACKed.

`Session` is importable without mido or a pedal: it opens the "GP-150" ports
lazily, and tests inject fake ports (anything with `iter_pending()` yielding
objects with `.type`/`.bytes()`, and `send(msg)`) plus `to_message`.
patch/ht_write_verify.py (Task 12) uses ONE Session's hello/read/pump for its reads;
to prove a slot empty it temporarily raises `empty_timeout` and taps `log` for
pump()'s "unsolicited frame family 0x70" line (a late preset stream; keep that
text). Its writes go through the gated import in patch/device_write.py
(send_stream(..., session=s)), which drives `Session.exchange(lead=...)` so the
session's ACK duties stay the only ones. This CLI itself never writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from patch import ht_proto as ht  # noqa: E402
from patch import prst150_format as f150  # noqa: E402

PORT = "GP-150"
OUT = os.path.join(ROOT, "device_scan_gp150")  # gitignored
N_SLOTS = 200
SETTLE = 0.3  # quiet gap after each request (the pedal has a shallow input queue)
TIMEOUT = 3.0  # per-request reply timeout; a stream's hard cap adds EMPTY_TIMEOUT
EMPTY_TIMEOUT = 1.5  # no chunk this long after the ACK (or the send, if no ACK) = silent
IDLE = 0.8  # a stream with no new chunk for this long is incomplete
TICK = 0.005  # input poll interval (bounds the final-chunk ACK latency)
MAX_STRAIGHT_ERRORS = 3  # scan: this many failed slots in a row = the pedal is gone
NOT_RESPONDING = "pedal not responding — close Valeton Suite if it's open, and check the USB cable"
EMPTY_WHY = {"empty-acked": "ACK, then silence", "empty-unacked": "no ACK; pedal answered hello"}

Log = Callable[[str, str], None]


class HtError(RuntimeError):
    """A read that could not be completed (never means "empty slot")."""


class NotResponding(HtError):
    pass


class StreamError(HtError):
    pass


# --- MIDI ports (mido is imported only when real ports are needed) ----------------

def _mido():
    try:
        import mido  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "mido is not installed in this Python — use the MIDI venv: "
            "python3 -m venv .venv-midi && ./.venv-midi/bin/python -m pip install mido python-rtmidi") from e
    return mido


def mido_sysex(wire: bytes):
    """F0 .. F7 wire bytes -> a mido sysex Message (mido adds the F0/F7 itself)."""
    return _mido().Message("sysex", data=list(wire[1:-1]))


def pick_port(names: Iterable[str], want: str) -> str:
    names = list(names)
    for n in names:
        if n == want:
            return n
    for n in names:
        if want in n:
            return n
    raise RuntimeError(f"no MIDI port named like {want!r} (found: {', '.join(names) or 'none'}) — "
                       "is the GP-150 on USB and Valeton Suite closed?")


def open_ports(port: str = PORT):
    mido = _mido()
    inp = mido.open_input(pick_port(mido.get_input_names(), port))
    try:
        out = mido.open_output(pick_port(mido.get_output_names(), port))
    except Exception:
        inp.close()
        raise
    return inp, out


def _default_log(level: str, msg: str) -> None:
    if level == "warn":
        print(f"[ht] {msg}", file=sys.stderr)


def _is_handshake_reply(f: ht.Frame) -> bool:
    # host `00 01 03 00` -> pedal `00 02 03 00`: a family-0x00 frame, NOT an ACK
    return f.family == ht.FAMILY_ACK and not ht.is_chunk(f) and f.tx4[1] == 0x02


class Reply:
    """What one exchange saw. Unpacks as (ack, frames). Exactly one outcome is set
    for a stream: payload (+transfer_id) | silent | error | timeout."""

    __slots__ = ("ack", "frames", "chunks", "payload", "transfer_id", "silent", "error", "timeout", "before_last")

    def __init__(self) -> None:
        self.ack = False
        self.frames: List[ht.Frame] = []  # every non-ACK frame, chunks included
        self.chunks: List[ht.Frame] = []  # the stream being assembled
        self.payload: Optional[bytes] = None
        self.transfer_id: Optional[int] = None
        self.silent = False
        self.error: Optional[Exception] = None
        self.timeout = False
        self.before_last = 0  # frames that arrived before the last frame went out (exchange(lead=...))

    def __iter__(self) -> Iterator:
        return iter((self.ack, self.frames))

    def __repr__(self) -> str:
        return (f"Reply(ack={self.ack}, frames={len(self.frames)}, chunks={len(self.chunks)}, "
                f"payload={None if self.payload is None else len(self.payload)}, silent={self.silent}, "
                f"error={self.error!r}, timeout={self.timeout})")


class Session:
    """One HT session over a MIDI in/out pair. Single-threaded: every request
    finishes (and settles) before the next one starts."""

    def __init__(self, inp=None, out=None, *, port: str = PORT, to_message: Optional[Callable] = None,
                 settle: float = SETTLE, timeout: float = TIMEOUT, empty_timeout: float = EMPTY_TIMEOUT,
                 idle: float = IDLE, tick: float = TICK, open_delay: float = 0.2, log: Optional[Log] = None):
        if (inp is None) != (out is None):
            raise ValueError("pass both inp and out, or neither (then the GP-150 ports are opened)")
        opened = inp is None
        if opened:
            inp, out = open_ports(port)
        self.port = port
        self.inp, self.out = inp, out
        self.to_message = to_message or mido_sysex
        self.settle, self.timeout, self.empty_timeout, self.idle, self.tick = settle, timeout, empty_timeout, idle, tick
        self.log: Log = log or _default_log
        self.tx = 0x10
        self.bad_frames = 0
        self.last_status: Optional[str] = None  # "preset" | "empty-acked" | "empty-unacked" after read()
        if opened and open_delay:
            time.sleep(open_delay)
        self.pump(0)  # whatever was queued before us belongs to no request

    def close(self) -> None:
        for port in (self.inp, self.out):
            close = getattr(port, "close", None)
            if close:
                close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, wire: bytes) -> None:
        self.out.send(self.to_message(bytes(wire)))

    def next_tx(self) -> int:
        # tx ids travel as one SysEx data byte: 1..0x7F (0 = "no id", 0x80+ = status byte)
        self.tx = (self.tx % 0x7F) + 1
        return self.tx

    def _ack_now(self, id_: int) -> None:
        try:
            self.send(ht.ack(id_))
        except Exception as e:  # noqa: BLE001 — a failed ACK must not kill the read loop
            self.log("warn", f"could not ACK id {id_}: {e}")

    def _frames(self) -> Iterator[Tuple[ht.Frame, bytes]]:
        """Every pending inbound HT frame. ACK duties come FIRST, as each frame is
        pulled and before the caller sees it: the final (short) chunk of any stream
        is ACKed right now, and a device short message with a tx id (0x10 reply,
        0x18 status, 0x08 import notify ...) is ACKed with that id. A frame with a
        bad CRC is dropped unACKed (counted; warned about at 3)."""
        for m in self.inp.iter_pending():
            if getattr(m, "type", None) != "sysex":
                continue
            raw = bytes(m.bytes())
            if len(raw) < 9 or raw[0] != 0xF0 or raw[1] != 0x7F:
                continue
            try:
                f = ht.parse_frame(raw)
            except ValueError as e:
                self.bad_frames += 1
                if self.bad_frames == 3:
                    self.log("warn", f"dropped {self.bad_frames} corrupt frames from the pedal ({e})")
                continue
            chunk = ht.is_chunk(f)
            if chunk and len(raw) < ht.FULL_WIRE_LEN:
                self._ack_now(f.tx4[3])
            elif not chunk and f.family != ht.FAMILY_ACK and f.tx4[3] != 0:
                self._ack_now(f.tx4[3])
            yield f, raw

    def pump(self, seconds: float) -> None:
        """Service the input for `seconds` with no request in flight: ACK duties
        only; the frames themselves belong to no request and are dropped."""
        end = time.monotonic() + seconds
        while True:
            for f, _raw in self._frames():
                if f.family != ht.FAMILY_ACK or ht.is_chunk(f):
                    self.log("debug", f"unsolicited frame family {f.family:#04x}")
            if time.monotonic() >= end:
                return
            time.sleep(self.tick)

    def exchange(self, wire: bytes, stream: bool = False, until: Optional[Callable[[ht.Frame], bool]] = None,
                 timeout: Optional[float] = None, *, lead: Sequence[bytes] = (), pace: float = 0.0,
                 ack_id: Optional[int] = None, idle: Optional[float] = None) -> Reply:
        """Send `wire`, collect frames until done, settle. Never raises for the
        pedal's behaviour; the Reply says what happened.
          non-stream: done on the first non-ACK, non-chunk frame (or handshake
            reply) that `until(frame)` accepts (default: any), or `idle` after the ACK.
          stream: done on the final (short) chunk (assembled -> payload, or error),
            `silent` when no chunk arrived `empty_timeout` after the ACK (after the
            send if there was no ACK), error when only a tail (no offset-0 chunk)
            came or the stream stalled `idle` before its final chunk.
        Multi-frame requests (the gated GP-150 import, device_write.send_stream):
        `lead` frames go out first, `pace` s apart, with the input serviced (ACK
        duties, frames collected) in between; `wire` is the last frame and every
        timer starts when it is sent. Until then nothing ends the exchange or
        counts as its ACK (`before_last` = how many of `frames` arrived before the
        last frame went out). `ack_id` names the ACK that counts (default: the
        request's tx id; any ACK for raw bytes / chunk frames); `idle` overrides
        how long to wait after that ACK."""
        timeout = self.timeout if timeout is None else timeout
        idle = self.idle if idle is None else idle
        want = ack_id  # the ACK id that counts; by default the request's tx id
        if want is None:
            try:
                rf = ht.parse_frame(bytes(wire))
                if rf.family != ht.FAMILY_ACK and not ht.is_chunk(rf):
                    want = rf.tx4[3]
            except ValueError:
                pass  # raw bytes: any ACK counts
        self.pump(0)
        r = Reply()
        saw_tail = False  # non-zero-offset chunks seen with no offset-0 chunk before them
        ack_at = last_chunk_at = 0.0
        queue = list(lead) + [wire]  # frames still to send; timers start with the last one
        t0 = time.monotonic()
        try:
            self.send(queue.pop(0))
        except Exception as e:  # noqa: BLE001
            r.error = e
            return r
        next_send_at = time.monotonic() + pace
        done = False
        while not done:
            for f, raw in list(self._frames()):  # duties for the whole batch first
                if done:
                    continue  # after the verdict: ACKed above, belongs to no request
                if f.family == ht.FAMILY_ACK and not ht.is_chunk(f):
                    if f.tx4[1] != 0:  # `00 0x 03 00` handshake reply: a frame, not an ACK
                        r.frames.append(f)
                        if not stream and not queue and (until is None or until(f)):
                            done = True
                    elif not r.ack and not queue and (want is None or f.tx4[3] == want):
                        r.ack, ack_at = True, time.monotonic()
                    continue
                r.frames.append(f)
                if not ht.is_chunk(f):  # reply / status message
                    if not stream and not queue and (until is None or until(f)):
                        done = True
                    continue
                if not stream:
                    continue
                # offset 0 opens a stream: drop partial leftovers (e.g. a resend); a
                # tail with no head is an older stream's, or ours with its first chunk
                # lost -> a stream error at the silence verdict, never "empty".
                offset = (f.tx4[1] & 0x7F) | ((f.tx4[2] & 0x7F) << 7)
                if offset == 0:
                    r.chunks = []
                elif not r.chunks:
                    saw_tail = True
                    continue
                r.chunks.append(f)
                last_chunk_at = time.monotonic()
                if len(raw) < ht.FULL_WIRE_LEN:  # the final chunk (already ACKed in _frames)
                    try:
                        r.transfer_id, r.payload = ht.assemble_stream(r.chunks)
                    except ValueError as e:
                        r.error = StreamError(f"preset stream rejected: {e}")
                    done = True
            if done:
                break
            now = time.monotonic()
            if queue:  # still sending the lead: no verdicts yet
                if now >= next_send_at:
                    if len(queue) == 1:
                        r.before_last = len(r.frames)
                        t0 = time.monotonic()
                    try:
                        self.send(queue.pop(0))
                    except Exception as e:  # noqa: BLE001
                        r.error = e
                        break
                    next_send_at = time.monotonic() + pace
                time.sleep(self.tick)
                continue
            if stream:
                if r.chunks:
                    if now - last_chunk_at > self.idle:
                        r.error = StreamError("preset stream stopped before its final chunk")
                        break
                elif now - (ack_at if r.ack else t0) > self.empty_timeout:
                    if saw_tail:
                        r.error = StreamError("preset stream arrived without its first chunk")
                    else:
                        r.silent = True
                    break
            elif r.ack and now - ack_at > idle:
                break
            if now - t0 > timeout + (self.empty_timeout if stream else 0):
                r.timeout = True
                break
            time.sleep(self.tick)
        self.pump(self.settle)
        return r

    def hello(self) -> bool:
        """The handshake; True when the pedal answers `00 02 03 00`."""
        r = self.exchange(ht.hello(), until=_is_handshake_reply)  # an unsolicited 0x18 must not end it
        return any(_is_handshake_reply(f) for f in r.frames)

    def read(self, slot: int) -> Optional[bytes]:
        """Read one preset without selecting it (flag 01). Returns the 1128-byte
        .prst, or None for an empty slot (`last_status` says how that was decided):
          ACK, then empty_timeout of silence            -> None ("empty-acked", spec §2)
          no ACK and no stream                          -> re-send once (new tx); if
            that is silent and un-ACKed too, a hello probe decides: answered -> None
            ("empty-unacked"), else NotResponding
          a chunk arrives during the probe              -> a late stream: read again
            (at most 2 more reads), never None on that path
          garbled / stalled / timed-out stream, or one  -> one retry (StreamError if
            missing its first chunk                         it fails again); never None
          preset index != slot (a stale stream)         -> one retry, then kept + warning
        Same rules as app/static/ht_transport.js readPreset()."""
        if slot != ht.SLOT_ACTIVE and not (isinstance(slot, int) and 0 <= slot < ht.SLOT_ACTIVE):
            raise ValueError(f"slot out of range: {slot}")
        self.last_status = None
        last_err: Optional[Exception] = None
        mismatched: Optional[bytes] = None
        unacked, budget, late_stream = 0, 2, False
        while budget > 0:
            budget -= 1
            r = self.exchange(ht.preset_request(self.next_tx(), slot), stream=True)
            if r.payload is not None:
                try:
                    prst = ht.preset_from_payload(r.payload)
                except ValueError as e:
                    last_err = StreamError(str(e))
                    continue
                if slot == ht.SLOT_ACTIVE or prst[f150.IDX_OFF] == slot:
                    self.last_status = "preset"
                    return prst
                if mismatched is not None:
                    self.log("warn", f"slot {slot} answered with preset index {prst[f150.IDX_OFF]} twice; keeping it")
                    self.last_status = "preset"
                    return prst
                mismatched = prst
                last_err = StreamError(f"slot {slot} answered with preset index {prst[f150.IDX_OFF]}")
                continue
            if r.silent and any(ht.is_chunk(f) for f in r.frames):  # chunks came, no usable stream
                last_err = StreamError("preset stream arrived without its first chunk")
                continue
            if r.silent and r.ack:
                if not late_stream:
                    self.last_status = "empty-acked"
                    return None
                last_err = StreamError(f"slot {slot}: a late preset stream, then silence — read it again")
                continue
            if r.silent:  # neither an ACK nor a stream: a lost / late request
                unacked += 1
                if budget > 0 or late_stream or unacked < 2:
                    last_err = NotResponding(NOT_RESPONDING)
                    continue
                # both reads un-ACKed and silent: is the pedal there at all?
                p = self.exchange(ht.hello(), until=_is_handshake_reply)
                if any(ht.is_chunk(f) for f in p.frames):  # the stream was just late
                    late_stream, budget = True, 2
                    last_err = NotResponding(NOT_RESPONDING)
                    continue
                if any(_is_handshake_reply(f) for f in p.frames):
                    self.last_status = "empty-unacked"
                    return None
                raise NotResponding(NOT_RESPONDING)
            last_err = r.error or StreamError("preset read timed out" if r.timeout else "preset stream incomplete")
        raise last_err or NotResponding(NOT_RESPONDING)


# --- CLI ----------------------------------------------------------------------------

def save(prst: bytes, slot: int, out_dir: str = OUT) -> str:
    os.makedirs(out_dir, exist_ok=True)
    name = "".join(c if c.isalnum() else "_" for c in f150.read_name(prst)).strip("_") or "unnamed"
    path = os.path.join(out_dir, f"{slot:03d}-{name}.prst")
    with open(path, "wb") as fh:
        fh.write(prst)
    return path


def _avg(xs: List[int]) -> str:
    return f"{sum(xs) / len(xs):.0f} ms" if xs else "-"


def scan(session: Session, slots: Optional[Iterable[int]] = None, out_dir: str = OUT,
         printer: Callable[[str], None] = print) -> Dict:
    """Read every slot, save each preset, and write `scan_summary.json` (per-slot
    status, timing, name, index byte) to out_dir. Raises after MAX_STRAIGHT_ERRORS
    failed slots in a row (the summary so far is still written)."""
    slots = list(range(N_SLOTS)) if slots is None else list(slots)
    summary: Dict = {"presets": 0, "empty": 0, "errors": 0, "seconds": 0.0, "empty_slots": [], "empty_acked": [],
                     "empty_unacked": [], "index_mismatch": [], "error_slots": [], "slots": {}}
    ms_preset: List[int] = []
    ms_empty: List[int] = []
    t_start = time.monotonic()
    straight = 0

    def write_summary(aborted: Optional[str] = None) -> None:
        summary["seconds"] = round(time.monotonic() - t_start, 1)
        if aborted:
            summary["aborted"] = aborted
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "scan_summary.json"), "w") as fh:
            json.dump(summary, fh, indent=1)

    for slot in slots:
        t0 = time.monotonic()
        try:
            p = session.read(slot)
            if p is not None and not f150.detect(p):
                raise StreamError(f"slot {slot}: not a GP-150 preset")
        except HtError as e:
            ms = int((time.monotonic() - t0) * 1000)
            summary["errors"] += 1
            summary["error_slots"].append(slot)
            summary["slots"][str(slot)] = {"status": "error", "error": str(e), "ms": ms}
            printer(f"{slot:3d}: ERROR {e}  ({ms} ms)")
            straight += 1
            if straight >= MAX_STRAIGHT_ERRORS:
                write_summary(aborted=str(e))
                raise
            continue
        straight = 0
        ms = int((time.monotonic() - t0) * 1000)
        if p is None:
            status = session.last_status or "empty-acked"
            summary["empty"] += 1
            summary["empty_slots"].append(slot)
            summary["empty_acked" if status == "empty-acked" else "empty_unacked"].append(slot)
            summary["slots"][str(slot)] = {"status": status, "ms": ms}
            ms_empty.append(ms)
            printer(f"{slot:3d}: (empty — {EMPTY_WHY.get(status, status)})  {ms} ms")
            continue
        idx, name = f150.read_index(p), f150.read_name(p)
        path = save(p, slot, out_dir)
        if idx != slot:
            summary["index_mismatch"].append([slot, idx])
        summary["presets"] += 1
        summary["slots"][str(slot)] = {"status": "preset", "ms": ms, "name": name, "index": idx,
                                       "file": os.path.basename(path)}
        ms_preset.append(ms)
        printer(f"{slot:3d}: {name!r} -> {path}  {ms} ms" + (f"  (index byte {idx} != slot)" if idx != slot else ""))
    write_summary()
    printer(f"empty slots: {summary['empty_slots'] or 'none'}")
    printer(f"index byte 0x04 == slot for {summary['presets'] - len(summary['index_mismatch'])}/{summary['presets']} "
            f"presets" + (f"; mismatches (slot, index): {summary['index_mismatch']}" if summary["index_mismatch"] else ""))
    printer(f"done: {summary['presets']} presets, {summary['empty']} empty "
            f"({len(summary['empty_acked'])} ACKed, {len(summary['empty_unacked'])} un-ACKed), "
            f"{summary['errors']} errors, {summary['seconds']:.0f}s "
            f"(avg {_avg(ms_preset)} per preset, {_avg(ms_empty)} per empty slot)")
    return summary


def watch(session: Session, interval: float = 2.0, count: Optional[int] = None,
          printer: Callable[[str], None] = print) -> None:
    """Re-read the active preset every `interval` s and print what changed; block
    bytes are named by chain position and slot (+0x04 = the model type code)."""
    printer("watching the active preset — change something on the pedal; Ctrl-C to stop")
    last: Optional[bytes] = None
    n = 0
    while count is None or n < count:
        n += 1
        p = session.read(ht.SLOT_ACTIVE)
        if p and last and p != last:
            diffs = [i for i in range(len(p)) if p[i] != last[i]]
            more = f" … (+{len(diffs) - 24})" if len(diffs) > 24 else ""
            printer(f"{time.strftime('%H:%M:%S')} {len(diffs)} bytes changed: "
                    + " ".join(f"{i:#05x}:{last[i]:02x}->{p[i]:02x}" for i in diffs[:24]) + more)
            order = f150.read_order(p)
            for i in diffs:
                if f150.BLOCKS_OFF <= i < f150.FOOTER_OFF:
                    pos, off = divmod(i - f150.BLOCKS_OFF, f150.BLOCK_LEN)
                    slot = f150.SLOTS[order[pos]] if order[pos] < len(f150.SLOTS) else f"slot {order[pos]}"
                    what = f" (type {last[i]} -> {p[i]})" if off == 4 else ""
                    printer(f"   pos {pos} ({slot}) block byte +{off:#04x}{what}")
        if p:
            last = p
        if count is None or n < count:
            session.pump(interval)


def main(argv: Optional[List[str]] = None, session_factory: Optional[Callable[[], Session]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ht_scan.py", description="GP-150 read tool (HT protocol, read-only).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("hello", help="handshake only")
    rp = sub.add_parser("read", help="read one slot (0..199) or 'active'")
    rp.add_argument("slot")
    rp.add_argument("--out", default=OUT)
    sp = sub.add_parser("scan", help=f"read all {N_SLOTS} slots")
    sp.add_argument("--out", default=OUT)
    wp = sub.add_parser("watch", help="print byte diffs of the active preset")
    wp.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)
    cmd = args.cmd or "hello"
    slot = None
    if cmd == "read":
        if args.slot == "active":
            slot = ht.SLOT_ACTIVE
        else:
            try:
                slot = int(args.slot)
            except ValueError:
                ap.error(f"slot must be 0..{N_SLOTS - 1} or 'active', not {args.slot!r}")
            if not 0 <= slot < N_SLOTS:
                ap.error(f"slot must be 0..{N_SLOTS - 1} or 'active'")
    try:
        s = (session_factory or Session)()
    except RuntimeError as e:
        raise SystemExit(str(e))
    try:
        if not s.hello():
            raise SystemExit("no handshake reply from the GP-150 (USB cable? Valeton Suite open?)")
        if cmd == "hello":
            print("GP-150 answered the handshake")
        elif cmd == "read":
            label = "active" if slot == ht.SLOT_ACTIVE else str(slot)
            p = s.read(slot)
            if p is None:
                print(f"slot {label}: empty ({EMPTY_WHY.get(s.last_status or '', s.last_status)})")
            else:
                idx = f150.read_index(p)
                note = f"  (index byte {idx} != slot)" if slot != ht.SLOT_ACTIVE and idx != slot else ""
                print(f"slot {label}: index={idx} name={f150.read_name(p)!r} -> {save(p, idx, args.out)}{note}")
        elif cmd == "scan":
            scan(s, out_dir=args.out)
        elif cmd == "watch":
            try:
                watch(s, interval=args.interval)
            except KeyboardInterrupt:
                print("stopped")
    except HtError as e:
        raise SystemExit(f"error: {e}")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
