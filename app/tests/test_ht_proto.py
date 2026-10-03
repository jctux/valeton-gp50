"""patch/ht_proto.py — the GP-150/GP-180 'HT' SysEx protocol, pinned against
Valeton Suite captures (app/tests/fixtures/gp150/ht_corpus.json) and the presets
read live from a GP-150 (re/gp150/evidence/)."""
import json
import os

import pytest

from patch import ht_proto as ht

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = json.load(open(os.path.join(ROOT, "app", "tests", "fixtures", "gp150", "ht_corpus.json")))
MSG = {m["id"]: bytes.fromhex(m["raw"]) for m in FIX["messages"]}
EVID = os.path.join(ROOT, "re", "gp150", "evidence")


def stream(prefix):
    return [MSG[k] for k in sorted((k for k in MSG if k.startswith(prefix)),
                                   key=lambda k: int(k.rsplit("_", 1)[1]))]


def test_crc_known_vectors():
    # live values: outer crc of the hello body, inner crc of the export payload
    assert ht.ocrc(bytes.fromhex("000001030000")) == 0x51
    assert ht.icrc(bytes.fromhex("0303113011300200000001")) == 0x48


def test_every_fixture_frame_round_trips():
    for m in FIX["messages"]:
        wire = bytes.fromhex(m["raw"])
        f = ht.parse_frame(wire)  # raises on a bad outer CRC
        assert ht.frame(f.family, f.tx4, f.body) == wire, m["id"]


def test_builders_reproduce_suite_bytes():
    assert ht.hello() == MSG["hello"]
    assert ht.ack(0x01) == MSG["ack_tx1_host"]
    assert ht.ack(0x0C) == MSG["export_final_ack"]
    assert ht.preset_request(0x23, 0) == MSG["export_req_slot1"]
    assert ht.preset_request(0x63, 2, select=True) == MSG["select_slot2"]
    assert ht.preset_request(0x64, 3, select=True) == MSG["select_slot3"]
    assert ht.preset_request(0x01, ht.SLOT_ACTIVE) == MSG["read_active"]


def test_export_stream_assembles_to_a_preset():
    frames = [ht.parse_frame(w) for w in stream("export_stream_slot1_")]
    assert all(ht.is_chunk(f) for f in frames)
    tid, payload = ht.assemble_stream(frames)
    assert tid == 0x0C
    prst = ht.preset_from_payload(payload)
    assert len(prst) == 1128 and prst[:4] == b"\x11\x30\x64\x04" and prst[4] == 0
    assert prst[0x2C:0x2C + 7] == b"New GEN"


def test_assemble_rejects_gap():
    frames = [ht.parse_frame(w) for w in stream("export_stream_slot1_")]
    with pytest.raises(ValueError):
        ht.assemble_stream(frames[:3] + frames[4:])
    with pytest.raises(ValueError):
        ht.assemble_stream(frames[:-1])  # no final (short) chunk


def test_import_stream_reproduces_suite_bytes():
    # Suite imported the exported slot-1 file; rebuild its stream from that file.
    frames = [ht.parse_frame(w) for w in stream("export_stream_slot1_")]
    prst = ht.preset_from_payload(ht.assemble_stream(frames)[1])
    ours = ht.import_stream(0x24, prst)
    theirs = stream("import_stream_slot1_")
    assert len(ours) == len(theirs) == 10
    for i, (a, b) in enumerate(zip(ours, theirs)):
        assert a == b, f"chunk {i} differs"


def test_live_gp150_stream_decodes():
    data = open(os.path.join(EVID, "100-active_stream_decoded.bin"), "rb").read()
    payload = ht.parse_logical(data)
    prst = ht.preset_from_payload(payload)
    assert prst == open(os.path.join(EVID, "100-active.prst"), "rb").read()
