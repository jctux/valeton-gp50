"""patch/prst_format.py — the single source of truth for the .prst layout.

Golden-file tests against the real exported patch set: if any offset, the CRC,
or the name codec drifts, these fail before a device write ever could.
"""

import glob
import os

from patch import prst_format as fmt

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
EXPORTS = sorted(glob.glob(os.path.join(PROJECT_ROOT, "presetExports", "*.prst")))


def _all_prsts():
    out = [(os.path.basename(p), open(p, "rb").read()) for p in EXPORTS]
    return [(n, b) for n, b in out if len(b) == fmt.PRST_LEN]


def test_crc8_known_vector():
    # CRC-8/SMBUS check value: crc8(b"123456789") == 0xF4
    assert fmt.crc8(b"123456789") == 0xF4


def test_layout_constants_hold_across_all_exports():
    prsts = _all_prsts()
    assert len(prsts) >= 100
    for name, b in prsts:
        assert b[: fmt.CRC_OFF] == fmt.HEADER, f"{name}: header drift"
        assert b[0x15:0x19] == fmt.SENTINEL, f"{name}: sentinel drift"
        # stored CRC matches a recompute over prst[0x15:]
        assert b[fmt.CRC_OFF] == fmt.crc8(b[fmt.CRC_OFF + 1 :]), f"{name}: CRC"


def test_rebuild_round_trips_every_export():
    prsts = _all_prsts()
    ok = sum(fmt.rebuild(fmt.read_name(b), b[fmt.BODY_OFF :]) == b for _, b in prsts)
    assert ok == len(prsts), f"round-trip {ok}/{len(prsts)}"


def test_name_codec_round_trip_and_truncation():
    _, b = _all_prsts()[0]
    buf = bytearray(b)
    fmt.write_name(buf, "Test Name")
    assert fmt.read_name(buf) == "Test Name"
    # 16-char cap: longer names truncate, never spill into the body
    body_before = bytes(buf[fmt.BODY_OFF :])
    fmt.write_name(buf, "A" * 40)
    assert fmt.read_name(buf) == "A" * 16
    assert bytes(buf[fmt.BODY_OFF :]) == body_before


def test_read_name_stops_at_body_boundary():
    # a full 16-char name has no null terminator; read_name must not run into
    # the body (this was a live bug: one reader used 0x30 as the end)
    _, b = _all_prsts()[0]
    buf = bytearray(b)
    fmt.write_name(buf, "B" * 16)
    assert fmt.read_name(buf) == "B" * 16


def test_model_records_shape_and_refix():
    _, b = _all_prsts()[0]
    recs = fmt.model_records(b)
    assert len(recs) == fmt.N_BLOCKS
    assert all(len(r) == 3 for r in recs)
    assert len(fmt.param_floats(b)) == fmt.N_PARAM_SLOTS
    # refix_crc restores a valid CRC after an edit
    buf = bytearray(b)
    off = fmt.models_offset(buf)
    buf[off] ^= 0x01
    fmt.refix_crc(buf)
    assert buf[fmt.CRC_OFF] == fmt.crc8(buf[fmt.CRC_OFF + 1 :])


def test_check_length_rejects_short_bodies():
    try:
        fmt.check_length(b"\0" * 100)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


EVIDENCE = sorted(glob.glob(os.path.join(PROJECT_ROOT, "re", "gp150", "evidence", "*.prst")))


def test_gp150_profile_fields():
    p = fmt.GP150
    assert (p.key, p.name, p.prst_len, p.slots, p.transport, p.n_blocks) == (
        "gp150", "GP-150", 1128, 200, "ht", 12)
    assert p.usb_pid == 0x0186 and p.midi_port == "GP-150"
    assert fmt.GP50.slots == 100 and fmt.GP50.transport == "legacy" and fmt.GP50.n_blocks == 10
    assert fmt.GP5.slots == 100 and fmt.GP5.transport == "legacy"


def test_detect_gp150_by_magic_then_length():
    assert len(EVIDENCE) >= 3
    for path in EVIDENCE:
        b = open(path, "rb").read()
        assert len(b) == 1128
        assert fmt.detect(b).key == "gp150"
    # length-only fallback (magic damaged) still lands on gp150 — nothing else is 1128
    assert fmt.detect(b"\x00" * 1128).key == "gp150"
    # a 507-byte file is still a GP-5
    assert fmt.detect(open(os.path.join(PROJECT_ROOT, "app", "tests", "fixtures", "gp5", "65-Puppy.prst"), "rb").read()).key == "gp5"
