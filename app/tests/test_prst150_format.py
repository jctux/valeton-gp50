"""patch/prst150_format.py — the 1128-byte GP-150 container. Golden files: the
presets read live from a GP-150 (re/gp150/evidence/) and, when GP180_DUMP_DIR
points at a checkout of majabojarska/Valeton-GP180-Rev-Eng/prst-dump, its 200
factory presets (same container; only header 0x0D-0x0F and footer differ)."""
import glob
import os
import struct

import pytest

from patch import prst150_format as f150

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVID = sorted(glob.glob(os.path.join(ROOT, "re", "gp150", "evidence", "*.prst")))
DUMP = sorted(glob.glob(os.path.join(os.environ.get("GP180_DUMP_DIR", "/nonexistent"), "*.prst")))
CORPUS = EVID + DUMP


def _active():
    return open(os.path.join(ROOT, "re", "gp150", "evidence", "100-active.prst"), "rb").read()


def test_corpus_has_live_files():
    assert len(EVID) >= 3


def test_detect_and_header_fields():
    b = _active()
    assert f150.detect(b)
    assert f150.read_index(b) == 100
    assert f150.read_name(b) == "It's GP-150"
    assert f150.read_vol_bpm(b) == (50, 120)
    assert f150.read_order(b) == [5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]


def test_every_corpus_file_is_sane():
    for path in CORPUS:
        b = open(path, "rb").read()
        assert f150.detect(b), path
        order = f150.read_order(b)
        assert sorted(order) == list(range(12)) and order[0] == f150.AMP_SLOT, path
        blocks = f150.blocks_by_slot(b)
        assert [blk["pos"] for blk in blocks] == [order.index(s) for s in range(12)]
        for blk in blocks:
            assert blk["enabled"] in (0, 1) and len(blk["params"]) == 15


def test_blocks_by_slot_matches_raw_layout():
    b = _active()
    amp = f150.block_at(b, 0)  # position 0 is AMP in every file
    raw = b[0x84:0x84 + 0x44]
    assert amp["enabled"] == raw[0] and amp["type"] == raw[4] and amp["engine"] == raw[7]
    assert amp["params"][0] == pytest.approx(struct.unpack_from("<f", raw, 8)[0])


def test_model_records_and_bypass_follow_slot_order():
    b = _active()
    recs = f150.model_records(b)
    assert len(recs) == 12
    blocks = f150.blocks_by_slot(b)
    for s, (type_, slot, low) in enumerate(recs):
        assert slot == s and type_ == blocks[s]["type"]
        assert low == (blocks[s]["ext"] << 16) | (blocks[s]["subtype"] << 8) | blocks[s]["type"]
    mask = f150.bypass_mask(b)
    for s in range(12):
        assert ((mask >> s) & 1) == blocks[s]["enabled"]
    floats = f150.param_floats(b)
    assert len(floats) == 12 * 15 and floats[5 * 15:5 * 15 + 15] == blocks[5]["params"]


def test_name_write_round_trip_and_cap():
    b = bytearray(_active())
    f150.write_name(b, "Hello")
    assert f150.read_name(b) == "Hello" and b[0x2C + 5] == 0
    f150.write_name(b, "x" * 100)
    assert len(f150.read_name(b)) == 0x44 - 1  # NUL-terminated inside the field


def test_write_order_moves_blocks_and_requires_amp_first():
    b = bytearray(_active())
    before = f150.blocks_by_slot(b)
    new = [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11]  # RVB right after AMP
    f150.write_order(b, new)
    assert f150.read_order(b) == new
    after = f150.blocks_by_slot(b)
    for s in range(12):  # every slot keeps its own bytes, only the position changes
        a, z = dict(before[s]), dict(after[s]); a.pop("pos"); z.pop("pos")
        assert a == z, s
    assert after[10]["pos"] == 1
    with pytest.raises(ValueError):
        f150.write_order(b, [0, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11])
    with pytest.raises(ValueError):
        f150.write_order(b, [5, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11])


def test_write_order_requires_vol_last():
    # VOL sits at chain position 11 in every corpus file (203/203) and no other block
    # ever does; position 11's canonical engine is 0x06, the "None" engine, so a real
    # effect there would be indistinguishable from None. VOL is pinned last like AMP first.
    b = bytearray(_active())
    before = bytes(b)
    for bad in ([5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 10], [5, 11, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10]):
        with pytest.raises(ValueError, match="VOL"):
            f150.write_order(b, bad)
        with pytest.raises(ValueError, match="VOL"):
            f150.apply_edits(before, {"order": bad})
    assert bytes(b) == before  # a refused order changes nothing
    assert "VOL" not in f150.MOVABLE and "AMP" not in f150.MOVABLE and len(f150.MOVABLE) == 10


def test_engine_rules_from_spec():
    # canonical by position; type overrides; AMP by type/params
    assert f150.engine_for(3, 2, 4, [0.0] * 15) == 0x07
    assert f150.engine_for(1, 0, 1, [0.0] * 15) == 0x05  # NR gate at pos 1
    assert f150.engine_for(6, 6, 16, [0.0] * 15) == 0x1A  # CAB at pos 6
    assert f150.engine_for(6, 6, 60, [0.0] * 15) == 0x0A  # acoustic cab override
    assert f150.engine_for(0, 5, 1, [15, 50, 50, 10, 0, 0] + [0] * 9) == 0x00  # Tweedy base
    assert f150.engine_for(0, 5, 1, [15, 50, 50, 60, 0, 0] + [0] * 9) == 0x03  # Tweedy presence>=50
    assert f150.engine_for(0, 5, 1, [15, 50, 50, 10, 1, 0] + [0] * 9) == 0x01  # extended
    assert f150.engine_for(0, 5, 33, [0.0] * 15) == 0x01
    assert f150.engine_for(0, 5, 9, [0.0] * 15) == 0x03


def test_engine_rules_reproduce_corpus():
    bad = []
    checked = 0
    for path in CORPUS:
        b = open(path, "rb").read()
        order = f150.read_order(b)
        for pos in range(12):
            blk = f150.block_at(b, pos)
            if not blk["enabled"] or pos == 0:
                continue  # firmware keeps engines of disabled blocks; AMP is best effort
            checked += 1
            want = f150.engine_for(pos, order[pos], blk["type"], blk["params"])
            if want != blk["engine"]:
                bad.append((os.path.basename(path), pos, order[pos], blk["type"], blk["engine"], want))
    assert len(bad) <= checked // 100 + 1, bad[:10]


def test_apply_edits_round_trip():
    b = _active()
    out = f150.apply_edits(b, {"name": "Edited", "settings": {"patch_vol": 77, "bpm": 99},
                              "bypass": {0: False}, "params": {5: {0: 42.5}}})
    assert f150.read_name(out) == "Edited" and f150.read_vol_bpm(out) == (77, 99)
    in_blocks, out_blocks = f150.blocks_by_slot(b), f150.blocks_by_slot(out)
    assert out_blocks[0]["enabled"] == 0 and out_blocks[0]["engine"] == in_blocks[0]["engine"]
    assert f150.blocks_by_slot(out)[5]["params"][0] == pytest.approx(42.5)
    assert len(out) == 1128 and f150.apply_edits(b, {}) == b


def test_apply_edits_model_change_sets_type_and_defaults_untouched():
    b = _active()
    key = f150.model_key(9, 4, 0, 0)  # DLY type 4
    out = f150.apply_edits(b, {"models": {9: key}})
    blk = f150.blocks_by_slot(out)[9]
    assert (blk["type"], blk["subtype"], blk["ext"]) == (4, 0, 0)
    assert blk["engine"] == 0x0B


def test_apply_edits_reorder_recomputes_moved_engines():
    b = _active()
    before = f150.blocks_by_slot(b)
    old = f150.read_order(b)
    new = [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11]
    out = f150.apply_edits(b, {"order": new})
    after = f150.blocks_by_slot(out)
    for s in range(12):
        if old.index(s) == new.index(s):
            assert after[s]["engine"] == before[s]["engine"], s
    rvb = after[10]
    assert rvb["pos"] == 1 and rvb["engine"] == f150.engine_for(1, 10, rvb["type"], rvb["params"])


def test_engine_for_none_model():
    # type 3 is the "None" effect in the slots whose spec has no real type 3 (the
    # ring's per-slot None entries): engine 0x06 at any position (corpus encoding).
    for slot in f150.NONE_SLOTS:
        for pos in range(1, 12):
            assert f150.engine_for(pos, slot, f150.NONE_TYPE, [0.0] * 15) == 0x06, (slot, pos)
    assert set(f150.NONE_SLOTS) == {0, 1, 2, 4, 6, 7, 8}
    # DST/DLY/RVB/VOL have a REAL type 3 (Penesas OD, Dual Echo, Spring, Volume)
    assert f150.engine_for(4, 3, 3, [0.0] * 15) == 0x07
    assert f150.engine_for(9, 9, 3, [0.0] * 15) == 0x0B
    assert f150.engine_for(10, 10, 3, [0.0] * 15) == 0x0C


def test_apply_edits_moved_none_blocks_stay_none():
    # "It's GP-150" (the factory placeholder): NR PRE WAH DST N->S CAB are None
    # (type 3, engine 0x06). Moving RVB right after AMP shifts all of them; they must
    # stay None, not pick up the new position's engine (DST type 3 + engine 0x00 is a
    # real drive). Real blocks that moved are still recomputed.
    b = _active()
    before = f150.blocks_by_slot(b)
    out = f150.apply_edits(b, {"order": [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11]})
    after = f150.blocks_by_slot(out)
    for s in (0, 1, 2, 3, 4, 6):
        assert before[s]["engine"] == 0x06 and after[s]["pos"] == before[s]["pos"] + 1, s
        assert (after[s]["type"], after[s]["engine"]) == (before[s]["type"], 0x06), s
    for s in (7, 8, 9, 10):
        blk = after[s]
        assert blk["engine"] == f150.engine_for(blk["pos"], s, blk["type"], blk["params"]), s
    assert after[11] == before[11]  # VOL did not move


def test_apply_edits_model_to_none_and_back():
    b = _active()
    # MOD (slot 8, type 41, on) -> the ring's MOD "None" entry: type 3 + engine 0x06,
    # and OFF (all 1,573 None blocks in the corpus are disabled) ...
    assert f150.blocks_by_slot(b)[8]["enabled"] == 1
    out = f150.apply_edits(b, {"models": {8: f150.model_key(8, 3)}})
    mod = f150.blocks_by_slot(out)[8]
    assert (mod["type"], mod["engine"], mod["enabled"]) == (3, 0x06, 0)
    assert f150.is_none_model(8, f150.model_key(8, 3)) and not f150.is_none_model(3, f150.model_key(3, 3))
    # ... unless the same edit's bypass says otherwise
    out = f150.apply_edits(b, {"models": {8: f150.model_key(8, 3)}, "bypass": {8: True}})
    mod = f150.blocks_by_slot(out)[8]
    assert (mod["type"], mod["engine"], mod["enabled"]) == (3, 0x06, 1)
    # NR is None; picking Gate (type 1) gives the position's real engine
    out = f150.apply_edits(b, {"models": {0: f150.model_key(0, 1)}})
    nr = f150.blocks_by_slot(out)[0]
    assert (nr["type"], nr["engine"]) == (1, f150.engine_for(nr["pos"], 0, 1, nr["params"])) and nr["engine"] != 0x06
    # DST None -> DST type 3 (Penesas OD, a real effect): real engine, not None
    out = f150.apply_edits(b, {"models": {3: f150.model_key(3, 3)}})
    dst = f150.blocks_by_slot(out)[3]
    assert (dst["type"], dst["engine"]) == (3, 0x07)
    # a None block that is both moved and re-modelled gets the new model's engine
    out = f150.apply_edits(b, {"order": [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11], "models": {0: f150.model_key(0, 1)}})
    nr = f150.blocks_by_slot(out)[0]
    assert nr["pos"] == 2 and nr["engine"] == f150.engine_for(2, 0, 1, nr["params"])


def _new_gen():
    return open(os.path.join(ROOT, "re", "gp150", "evidence", "000-New_GEN.prst"), "rb").read()


def test_apply_edits_two_step_drag_restores_engines():
    # New GEN.: Pure Delay (DLY type 0) at position 9, engine 0x0B. Drag it to
    # position 1 and back: every moved real block gets its position engine and gets
    # the original one back; the file round-trips byte for byte.
    b = _new_gen()
    orig = f150.read_order(b)
    assert orig == [5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]
    dly0 = f150.blocks_by_slot(b)[9]
    assert (dly0["pos"], dly0["type"], dly0["engine"]) == (9, 0, 0x0B)
    s1 = f150.apply_edits(b, {"order": [5, 9, 0, 1, 2, 3, 4, 6, 7, 8, 10, 11]})
    d1 = f150.blocks_by_slot(s1)[9]
    assert (d1["pos"], d1["type"], d1["engine"]) == (1, 0, 0x05)
    s2 = f150.apply_edits(s1, {"order": orig})
    d2 = f150.blocks_by_slot(s2)[9]
    assert (d2["pos"], d2["type"], d2["engine"]) == (9, 0, 0x0B)
    assert s2 == b
    # WAH is None (type 3, engine 0x06): moved twice, it stays None
    w1 = f150.apply_edits(b, {"order": [5, 2, 0, 1, 3, 4, 6, 7, 8, 9, 10, 11]})
    assert (f150.blocks_by_slot(w1)[2]["pos"], f150.blocks_by_slot(w1)[2]["engine"]) == (1, 0x06)
    w2 = f150.apply_edits(w1, {"order": orig})
    wah = f150.blocks_by_slot(w2)[2]
    assert (wah["pos"], wah["type"], wah["engine"]) == (3, 3, 0x06)
    assert w2 == b


def test_moved_none_skip_needs_type_3():
    # engine 0x06 alone is not "None": a moved block of another type is recomputed
    b = bytearray(_new_gen())
    pos = f150.read_order(b).index(9)  # DLY (type 0)
    f150.set_block(b, pos, engine=0x06)
    out = f150.apply_edits(bytes(b), {"order": [5, 9, 0, 1, 2, 3, 4, 6, 7, 8, 10, 11]})
    dly = f150.blocks_by_slot(out)[9]
    assert (dly["pos"], dly["engine"]) == (1, 0x05)


def test_blank_has_index_and_name():
    b = f150.blank(199)
    assert f150.detect(b) and f150.read_index(b) == 199 and f150.read_name(b) == "New GEN."
    assert f150.read_order(b)[0] == f150.AMP_SLOT
