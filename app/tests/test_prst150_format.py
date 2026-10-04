"""patch/prst150_format.py — the 1128-byte GP-150 container. Golden files: the
presets read live from a GP-150 (re/gp150/evidence/) and, when GP180_DUMP_DIR
points at a checkout of majabojarska/Valeton-GP180-Rev-Eng/prst-dump, its 200
factory presets (same container; only header 0x0D-0x0F and footer differ)."""
import glob
import json
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


def _evidence(name):
    return open(os.path.join(ROOT, "re", "gp150", "evidence", name), "rb").read()


def _pedal_reordered():
    """Slot 199 after the PEDAL moved RVB to chain position 6 and saved (2026-10-04)."""
    return _evidence("199-reordered-by-pedal.prst")


def _before_pedal_reorder():
    """The pedal's read-back of slot 199 just before that reorder (DLY = Sweet Echo, on)."""
    return _evidence("199-before-pedal-reorder.prst")


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
        assert sorted(order) == list(range(12)) and order[0] == f150.AMP_SLOT and order[11] == f150.VOL_SLOT, path
        blocks = f150.blocks_by_slot(b)
        assert [blk["pos"] for blk in blocks] == [order.index(s) for s in range(12)]
        assert [blk["rec"] for blk in blocks] == f150.DEFAULT_POS  # records never move
        assert [blk["slot"] for blk in blocks] == list(range(12))
        for blk in blocks:
            assert blk["enabled"] in (0, 1) and len(blk["params"]) == 15


def test_blocks_are_read_from_their_home_records():
    b = _pedal_reordered()
    for s, blk in enumerate(f150.blocks_by_slot(b)):
        raw = b[0x84 + f150.DEFAULT_POS[s] * 0x44:][:0x44]
        assert (blk["enabled"], blk["type"], blk["subtype"], blk["ext"], blk["engine"]) == (raw[0], raw[4], raw[5], raw[6], raw[7])
        assert blk["params"][0] == pytest.approx(struct.unpack_from("<f", raw, 8)[0])
        assert f150.block_of(b, s) == blk
    amp = f150.block_of(b, f150.AMP_SLOT)
    assert amp["rec"] == 0 and amp["pos"] == 0  # AMP: record 0, chain position 0
    with pytest.raises(ValueError):
        f150.block_of(b, 12)


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


def test_write_order_rewrites_only_the_order_table():
    b = bytearray(_active())
    before = bytes(b)
    new = [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11]  # RVB right after AMP
    f150.write_order(b, new)
    assert f150.read_order(b) == new
    assert [i for i in range(len(b)) if b[i] != before[i]] == list(range(0x79, 0x83))
    after = f150.blocks_by_slot(b)
    assert after[10]["pos"] == 1 and after[10]["rec"] == 10
    with pytest.raises(ValueError):
        f150.write_order(b, [0, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11])
    with pytest.raises(ValueError):
        f150.write_order(b, [5, 5, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11])


def test_write_order_requires_vol_last():
    # VOL is at chain position 11 in every corpus file (the user's 200 slots + the
    # GP-180 factory dump); whether the firmware accepts it elsewhere is untested,
    # so VOL is pinned last like AMP first.
    b = bytearray(_active())
    before = bytes(b)
    for bad in ([5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 10], [5, 11, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10]):
        with pytest.raises(ValueError, match="VOL"):
            f150.write_order(b, bad)
        with pytest.raises(ValueError, match="VOL"):
            f150.apply_edits(before, {"order": bad})
    assert bytes(b) == before  # a refused order changes nothing
    assert "VOL" not in f150.MOVABLE and "AMP" not in f150.MOVABLE and len(f150.MOVABLE) == 10


REFUSAL = "no unambiguous engine byte is known for"


def _counts(t, s):
    return {int(k): {int(e): n for e, n in c.items()} for k, c in t["slots"][s]["counts"].items()}


def test_slot_engine_from_the_table():
    # a (slot, type) pair the corpus shows with exactly ONE engine takes that engine
    t = _engine_table()
    for s, row in enumerate(t["slots"]):
        for type_, seen in _counts(t, s).items():
            if len(seen) == 1:
                assert f150.slot_engine(s, type_) == next(iter(seen)), (row["slot"], type_)
                assert f150.pick_allowed(s, type_)
    assert f150.slot_engine(9, 13) == 0x0B  # DLY Sweet Echo
    assert f150.slot_engine(10, 3) == 0x0C and f150.slot_engine(3, 3) == 0x07  # real type 3s
    assert f150.slot_engine(8, 41) == 0x01 and f150.slot_engine(6, 60) == 0x0A
    assert f150.slot_engine(11, 3) == 0x06  # VOL
    with pytest.raises(ValueError, match="WAH"):
        f150.slot_engine(2, 4)  # no preset holds a real wah
    with pytest.raises(ValueError):
        f150.slot_engine(12, 1)
    with pytest.raises(ValueError):
        f150.pick_allowed(12, 1)


@pytest.mark.parametrize("slot,type_", [(5, 1), (1, 11), (3, 117), (5, 3)])
def test_ambiguous_or_unseen_amp_pre_dst_picks_are_refused(slot, type_):
    # AMP type 1 {0x00: 18, 0x01: 2, 0x03: 4}, PRE 11 {0x00: 1, 0x03: 3}, DST 117
    # {0x07: 1, 0x08: 1}: the corpus shows 2-3 engines, so the majority is a guess; AMP
    # type 3 (Bellman 59N) is never a real amp in the corpus. A wrong engine silences the
    # preset on the pedal (hardware), so the pick is refused.
    assert not f150.pick_allowed(slot, type_)
    with pytest.raises(ValueError, match=f"{REFUSAL} {f150.SLOTS[slot]} type {type_}: set it on the pedal, save, rescan"):
        f150.slot_engine(slot, type_)
    with pytest.raises(ValueError, match=REFUSAL):
        f150.apply_edits(f150.blank(3), {"models": {slot: f150.model_key(slot, type_)}})
    msg = ""
    try:
        f150.slot_engine(slot, type_)
    except ValueError as e:
        msg = str(e)
    assert "run scripts/gp150_engines.py and copy the table into prst150.js ENGINES" in msg


def test_single_engine_slots_take_their_engine_for_any_type():
    # NR, EQ, DLY, RVB (and VOL) show one engine for every seen type: an unseen type
    # takes it too
    assert f150.pick_allowed(9, 40) and f150.slot_engine(9, 40) == 0x0B  # DLY, never seen
    assert f150.slot_engine(9, 2) == 0x0B and f150.slot_engine(9, 5) == 0x0B
    for type_ in (0, 1, 2, 3, 4, 6, 8, 9, 13, 18):  # every seen RVB type
        assert f150.slot_engine(10, type_) == 0x0C
    for type_ in (1, 7, 8, 16, 33):  # NR: seen 1/7/8, unseen 16/33
        assert f150.slot_engine(0, type_) == 0x05
    assert f150.slot_engine(7, 25) == 0x01  # EQ, never seen
    assert f150.slot_engine(11, 3) == 0x06  # VOL


def test_multi_engine_slots_refuse_unseen_types():
    # CAB type 60 shows only 0x0A: allowed; a CAB type the corpus never shows: refused
    assert f150.pick_allowed(6, 60) and f150.slot_engine(6, 60) == 0x0A
    for slot, type_ in ((6, 33), (1, 7), (3, 33), (5, 4), (8, 54)):  # CAB, PRE, DST, AMP, MOD
        assert not f150.pick_allowed(slot, type_), (slot, type_)
        with pytest.raises(ValueError, match=REFUSAL):
            f150.slot_engine(slot, type_)
    # N->S shows one engine (0x00), but only for its two NAM types: its SnapTone DST /
    # CAB IR / Bass AMP models are other kinds of effect, so an unseen type is refused
    assert f150.slot_engine(4, 27) == 0x00 and f150.slot_engine(4, 33) == 0x00
    for type_ in (57, 64, 112, 122):
        assert not f150.pick_allowed(4, type_)
    assert not any(f150.pick_allowed(2, t) for t in range(256) if t != f150.NONE_TYPE)  # WAH


def test_pick_allowed_matches_the_committed_counts():
    # the rule, checked over the whole committed table: a seen (slot, type) is allowed iff
    # it has one engine; an unseen one iff its slot is single-engine and not N->S
    t = _engine_table()
    single = set()
    for s in range(f150.N_BLOCKS):
        c = _counts(t, s)
        engines = {e for seen in c.values() for e in seen}
        if len(engines) == 1 and s != 4:
            single.add(f150.SLOTS[s])
        for type_ in range(256):
            if s in f150.NONE_SLOTS and type_ == f150.NONE_TYPE:
                want = True
            elif type_ in c:
                want = len(c[type_]) == 1
            else:
                want = f150.SLOTS[s] in single
            assert f150.pick_allowed(s, type_) == want, (f150.SLOTS[s], type_)
            if want:
                f150.slot_engine(s, type_)
            else:
                with pytest.raises(ValueError, match=REFUSAL):
                    f150.slot_engine(s, type_)
    assert single == {"NR", "EQ", "DLY", "RVB", "VOL"}
    ambiguous = {(f150.SLOTS[s], k) for s in range(f150.N_BLOCKS) for k, v in _counts(t, s).items() if len(v) > 1}
    assert ambiguous == {("AMP", 1), ("PRE", 11), ("DST", 117)}


def test_repick_of_the_stored_type_keeps_the_stored_engine():
    # 100-active: AMP type 1 with engine 0x01 (AMP 1 is ambiguous: the majority is 0x00).
    # Re-picking the model already there is no change: no refusal, no rewrite.
    b = _active()
    amp = f150.blocks_by_slot(b)[5]
    assert (amp["type"], amp["engine"]) == (1, 0x01)
    assert f150.keeps_stored_engine(5, 1, 1, 0x01)
    out = f150.apply_edits(b, {"models": {5: f150.model_key(5, 1, amp["subtype"], amp["ext"])}})
    assert out == b
    # PRE 11 stored with 0x00 (011-Gypsy_of_AT) and with 0x03 (078-Organ_Synth): each kept
    for name, eng in (("011-Gypsy_of_AT.prst", 0x00), ("078-Organ_Synth.prst", 0x03)):
        g = _evidence(name)
        pre = f150.blocks_by_slot(g)[1]
        assert (pre["type"], pre["engine"]) == (11, eng)
        out = f150.apply_edits(g, {"models": {1: f150.model_key(1, 11, pre["subtype"], pre["ext"])}})
        assert out == g, name
    # ... but another ambiguous type is still refused in that slot
    with pytest.raises(ValueError, match=REFUSAL):
        f150.apply_edits(_evidence("099-Finger_AC.prst"), {"models": {1: f150.model_key(1, 11)}})


def test_repick_over_a_none_block_is_a_new_pick():
    # a None block (engine 0x06) is not the real type-3 model of its slot: picking that
    # model takes the table's engine (DST type 3 = Penesas: 0x07), or is refused when
    # none is known (AMP type 3: New GEN.'s AMP is None)
    b = _active()
    dst = f150.blocks_by_slot(b)[3]
    assert (dst["type"], dst["engine"]) == (3, 0x06)
    assert not f150.keeps_stored_engine(3, 3, 3, 0x06)
    out = f150.apply_edits(b, {"models": {3: f150.model_key(3, 3)}})
    assert f150.blocks_by_slot(out)[3]["engine"] == 0x07
    g = _new_gen()
    assert (f150.blocks_by_slot(g)[5]["type"], f150.blocks_by_slot(g)[5]["engine"]) == (3, 0x06)
    with pytest.raises(ValueError, match="AMP type 3"):
        f150.apply_edits(g, {"models": {5: f150.model_key(5, 3)}})
    # VOL's real "Volume" carries 0x06: a re-pick keeps it
    assert f150.keeps_stored_engine(11, 3, 3, 0x06)
    assert f150.apply_edits(g, {"models": {11: f150.model_key(11, 3)}}) == g


def test_slot_engine_none_model():
    # type 3 is the "None" effect in the slots with no real type 3 (the ring's per-slot
    # None entries, WAH included): engine 0x06
    for slot in f150.NONE_SLOTS:
        assert f150.slot_engine(slot, f150.NONE_TYPE) == 0x06, slot
    assert set(f150.NONE_SLOTS) == {0, 1, 2, 4, 6, 7, 8}
    assert not hasattr(f150, "engine_for") and not hasattr(f150, "CANONICAL_ENGINE")


def test_apply_edits_round_trip():
    b = _active()
    out = f150.apply_edits(b, {"name": "Edited", "settings": {"patch_vol": 77, "bpm": 99},
                              "bypass": {0: False}, "params": {5: {0: 42.5}}})
    assert f150.read_name(out) == "Edited" and f150.read_vol_bpm(out) == (77, 99)
    in_blocks, out_blocks = f150.blocks_by_slot(b), f150.blocks_by_slot(out)
    assert out_blocks[0]["enabled"] == 0 and out_blocks[0]["engine"] == in_blocks[0]["engine"]
    assert f150.blocks_by_slot(out)[5]["params"][0] == pytest.approx(42.5)
    assert len(out) == 1128 and f150.apply_edits(b, {}) == b


def test_apply_edits_address_home_records_in_a_reordered_preset():
    # 099-Finger_AC: NR sits at chain position 10, but its record is record 1
    b = open(os.path.join(ROOT, "re", "gp150", "evidence", "099-Finger_AC.prst"), "rb").read()
    assert f150.read_order(b).index(0) == 10
    out = f150.apply_edits(b, {"params": {0: {1: 12.5}}, "bypass": {0: True}})
    diff = [i for i in range(len(b)) if b[i] != out[i]]
    rec1 = 0x84 + 1 * 0x44
    assert diff and all(rec1 <= i < rec1 + 0x44 for i in diff), [hex(i) for i in diff]
    nr = f150.blocks_by_slot(out)[0]
    assert nr["enabled"] == 1 and nr["params"][1] == pytest.approx(12.5) and nr["pos"] == 10


def test_apply_edits_model_change_sets_type_and_engine():
    b = _active()
    key = f150.model_key(9, 4, 0, 0)  # DLY type 4
    out = f150.apply_edits(b, {"models": {9: key}})
    blk = f150.blocks_by_slot(out)[9]
    assert (blk["type"], blk["subtype"], blk["ext"]) == (4, 0, 0)
    assert blk["engine"] == 0x0B
    with pytest.raises(ValueError, match="WAH"):
        f150.apply_edits(b, {"models": {2: f150.model_key(2, 4)}})  # a real wah: engine unknown


def test_apply_edits_reorder_changes_only_the_order_table():
    for path in EVID:
        b = open(path, "rb").read()
        new = [5, 10] + [s for s in f150.read_order(b)[1:11] if s != 10] + [11]
        out = f150.apply_edits(b, {"order": new})
        diff = [i for i in range(len(b)) if b[i] != out[i]]
        assert all(0x78 <= i < 0x84 for i in diff), (os.path.basename(path), diff)
        assert f150.read_order(out) == new
        assert out[0x84:] == b[0x84:]  # every block record + footer untouched (engines too)


def test_apply_edits_model_to_none_and_back():
    b = _active()
    # MOD (slot 8, type 41, on) -> the ring's MOD "None" entry: type 3 + engine 0x06,
    # and OFF (all None blocks in the corpus are disabled) ...
    assert f150.blocks_by_slot(b)[8]["enabled"] == 1
    out = f150.apply_edits(b, {"models": {8: f150.model_key(8, 3)}})
    mod = f150.blocks_by_slot(out)[8]
    assert (mod["type"], mod["engine"], mod["enabled"]) == (3, 0x06, 0)
    assert f150.is_none_model(8, f150.model_key(8, 3)) and not f150.is_none_model(3, f150.model_key(3, 3))
    # ... unless the same edit's bypass says otherwise
    out = f150.apply_edits(b, {"models": {8: f150.model_key(8, 3)}, "bypass": {8: True}})
    mod = f150.blocks_by_slot(out)[8]
    assert (mod["type"], mod["engine"], mod["enabled"]) == (3, 0x06, 1)
    # NR is None; picking Gate (type 1) gives NR's engine 0x05
    out = f150.apply_edits(b, {"models": {0: f150.model_key(0, 1)}})
    nr = f150.blocks_by_slot(out)[0]
    assert (nr["type"], nr["engine"]) == (1, 0x05)
    # DST None -> DST type 3 (Penesas OD, a real effect): DST's engine, not None
    out = f150.apply_edits(b, {"models": {3: f150.model_key(3, 3)}})
    dst = f150.blocks_by_slot(out)[3]
    assert (dst["type"], dst["engine"]) == (3, 0x07)
    # reorder + re-model in one edit: the record stays home, the engine is the slot's
    out = f150.apply_edits(b, {"order": [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11], "models": {0: f150.model_key(0, 1)}})
    nr = f150.blocks_by_slot(out)[0]
    assert (nr["pos"], nr["rec"], nr["engine"]) == (2, 1, 0x05)


def _new_gen():
    return open(os.path.join(ROOT, "re", "gp150", "evidence", "000-New_GEN.prst"), "rb").read()


def test_apply_edits_drag_and_back_round_trips():
    # New GEN.: Pure Delay (DLY type 0, engine 0x0B). Drag it to chain position 1 and
    # back: its record never moves and keeps 0x0B; the file round-trips byte for byte.
    b = _new_gen()
    orig = f150.read_order(b)
    s1 = f150.apply_edits(b, {"order": [5, 9, 0, 1, 2, 3, 4, 6, 7, 8, 10, 11]})
    d1 = f150.blocks_by_slot(s1)[9]
    assert (d1["pos"], d1["rec"], d1["type"], d1["engine"]) == (1, 9, 0, 0x0B)
    assert f150.apply_edits(s1, {"order": orig}) == b


def test_blank_has_index_and_name():
    b = f150.blank(199)
    assert f150.detect(b) and f150.read_index(b) == 199 and f150.read_name(b) == "New GEN."
    assert f150.read_order(b)[0] == f150.AMP_SLOT


# --- block records at fixed home indexes; engines per (slot, type) (hardware 2026-10-04) ---
# Every engine a slot's record carries in the corpus (the user's 200-slot scan + evidence),
# read at the slot's HOME record. 0x06 outside VOL is the "None" effect (type 3).
ENGINE_SETS = {"NR": {0x05}, "PRE": {0x03, 0x00}, "WAH": set(), "DST": {0x07, 0x08}, "N->S": {0x00},
               "AMP": {0x00, 0x01, 0x03}, "CAB": {0x1A, 0x0A}, "EQ": {0x01}, "MOD": {0x04, 0x01},
               "DLY": {0x0B}, "RVB": {0x0C}, "VOL": {0x06}}


def _engine_table():
    return json.load(open(os.path.join(ROOT, "patch", "gp150_engines.json")))


def test_home_record_index_per_slot():
    # record r holds slot DEFAULT_ORDER[r]; slot s lives at record DEFAULT_POS[s]
    assert f150.DEFAULT_ORDER == [5, 0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]
    assert f150.DEFAULT_POS == [1, 2, 3, 4, 5, 0, 6, 7, 8, 9, 10, 11]
    assert all(f150.DEFAULT_ORDER[f150.DEFAULT_POS[s]] == s for s in range(12))


def test_engine_table_shape_and_per_slot_sets():
    t = _engine_table()
    assert [row["slot"] for row in t["slots"]] == f150.SLOTS and t["none_engine"] == 0x06
    for row in t["slots"]:
        allowed = ENGINE_SETS[row["slot"]]
        assert set(row["engines"].values()) <= allowed, row["slot"]
        assert row["default"] in allowed if allowed else row["default"] is None, row["slot"]
        for type_, counts in row["counts"].items():
            assert row["engines"][type_] in [int(e) for e in counts], (row["slot"], type_)
    wah = t["slots"][2]
    assert wah["default"] is None and wah["engines"] == {}  # no preset holds a real wah
    eng = {(row["slot"], int(k)): v for row in t["slots"] for k, v in row["engines"].items()}
    assert eng[("DLY", 13)] == 0x0B and eng[("DLY", 3)] == 0x0B  # Sweet Echo; Dual Echo (real type 3)
    assert eng[("RVB", 3)] == 0x0C and eng[("DST", 3)] == 0x07  # Spring; Penesas
    assert eng[("MOD", 41)] == 0x01 and eng[("MOD", 2)] == 0x04
    assert eng[("CAB", 60)] == 0x0A and eng[("CAB", 80)] == 0x1A
    assert eng[("DST", 122)] == 0x08 and eng[("DST", 4)] == 0x07
    assert eng[("PRE", 26)] == 0x00 and eng[("PRE", 0)] == 0x03
    assert eng[("AMP", 1)] == 0x00 and eng[("AMP", 33)] == 0x01 and eng[("AMP", 9)] == 0x03
    assert eng[("VOL", 3)] == 0x06 and eng[("N->S", 33)] == 0x00
    assert ("NR", 3) not in eng and ("MOD", 3) not in eng  # None is not a real engine


def test_engine_table_covers_the_evidence():
    # every real block of the in-repo evidence is in the table's counts, and carries the
    # table's engine unless its (slot, type) is ambiguous in the corpus
    t = _engine_table()
    checked = 0
    for path in EVID:
        b = open(path, "rb").read()
        for s, row in enumerate(t["slots"]):
            o = f150.BLOCKS_OFF + f150.DEFAULT_POS[s] * f150.BLOCK_LEN
            type_, engine = b[o + 4], b[o + 7]
            if engine == 0x06 and s != f150.VOL_SLOT:
                assert type_ == 3, (path, s)  # None
                continue
            counts = row["counts"][str(type_)]
            assert str(engine) in counts, (os.path.basename(path), row["slot"], type_, engine)
            if len(counts) == 1:
                assert row["engines"][str(type_)] == engine
            checked += 1
    assert checked >= 50


# --- ground truth from the pedal (2026-10-04) ------------------------------------------
DEVICE_OWNED = (0x0E, 0x0F, 0x43C, 0x445)  # device-written field, "saved" flag, enable bits


def test_pedal_reordered_preset_decodes_with_fixed_records():
    b = _pedal_reordered()
    assert f150.read_order(b) == [5, 0, 1, 2, 3, 4, 10, 6, 7, 8, 9, 11]
    blocks = f150.blocks_by_slot(b)
    dly, rvb = blocks[9], blocks[10]
    # the delay is record 9 whatever the order table says; RVB's move put it at chain position 10
    assert (dly["rec"], dly["pos"], dly["type"], dly["engine"], dly["enabled"]) == (9, 10, 13, 0x0B, 1)
    # RVB is None (type 3, engine 0x06, off), moved to chain position 6, record 10
    assert (rvb["rec"], rvb["pos"], rvb["type"], rvb["engine"], rvb["enabled"]) == (10, 6, 3, 0x06, 0)
    raw9 = b[0x84 + 9 * 0x44:][:8]
    assert (raw9[4], raw9[7]) == (13, 0x0B)


def test_pedal_reorder_touched_only_the_order_table():
    a, z = _before_pedal_reorder(), _pedal_reordered()
    diff = [i for i in range(len(a)) if a[i] != z[i]]
    assert diff == [0x0E, 0x0F, 0x7E, 0x7F, 0x80, 0x81, 0x82]
    # reconstructing the pre-reorder file: the pedal's file with the default order table
    rebuilt = bytearray(z)
    rebuilt[0x78:0x84] = bytes(f150.DEFAULT_ORDER)
    assert [i for i in range(len(a)) if a[i] != rebuilt[i]] == [0x0E, 0x0F]


def test_order_edit_reproduces_the_pedals_own_reorder():
    a, z = _before_pedal_reorder(), _pedal_reordered()
    for base in (a, bytes(z[:0x78]) + bytes(f150.DEFAULT_ORDER) + bytes(z[0x84:])):
        out = f150.apply_edits(base, {"order": [5, 0, 1, 2, 3, 4, 10, 6, 7, 8, 9, 11]})
        assert [i for i in range(len(out)) if out[i] != z[i] and i not in DEVICE_OWNED] == []


def test_every_evidence_engine_fits_its_slot():
    # the 8 non-default-order presets included: under the fixed-record mapping every
    # slot's engine is one its slot carries; 0x06 outside VOL only for None (type 3)
    reordered = 0
    for path in EVID:
        b = open(path, "rb").read()
        reordered += f150.read_order(b) != f150.DEFAULT_ORDER
        for s, blk in enumerate(f150.blocks_by_slot(b)):
            name = f150.SLOTS[s]
            if blk["engine"] == 0x06 and name != "VOL":
                assert blk["type"] == f150.NONE_TYPE and blk["enabled"] == 0, (os.path.basename(path), name)
            else:
                assert blk["engine"] in ENGINE_SETS[name], (os.path.basename(path), name, hex(blk["engine"]))
    assert reordered == 8


def test_model_change_on_dly_writes_record_9_whatever_the_order():
    key = f150.model_key(9, 13)  # Sweet Echo
    for base in (_new_gen(), _pedal_reordered(), _evidence("024-Funky_Clean.prst")):
        for order in (None, [5, 9, 10, 0, 1, 2, 3, 4, 6, 7, 8, 11]):
            edits = {"models": {9: key}}
            if order:
                edits["order"] = order
            out = f150.apply_edits(base, edits)
            rec9 = out[0x84 + 9 * 0x44:][:8]
            assert (rec9[4], rec9[7]) == (13, 0x0B)
            assert out[0x84:0x84 + 9 * 0x44] == base[0x84:0x84 + 9 * 0x44]  # other records untouched
            assert out[0x84 + 10 * 0x44:] == base[0x84 + 10 * 0x44:]


def test_none_pick_is_type_3_engine_6_off():
    for slot in f150.NONE_SLOTS:
        for base in (_active(), _new_gen(), _pedal_reordered()):
            out = f150.apply_edits(base, {"models": {slot: f150.model_key(slot, 3)}})
            blk = f150.blocks_by_slot(out)[slot]
            assert (blk["type"], blk["engine"], blk["enabled"]) == (3, 0x06, 0), slot


def test_bpm_is_clamped_to_one_byte_not_truncated():
    # BPM is one byte at 0x24 (0x25 is 0 in all 200 scanned presets); the Explorer's slider
    # used to go to 300, which `& 0xFF` turned into 44.
    b = bytearray(f150.blank(3))
    f150.write_vol_bpm(b, bpm=300); assert f150.read_vol_bpm(b)[1] == 255
    f150.write_vol_bpm(b, bpm=10); assert f150.read_vol_bpm(b)[1] == 40
    f150.write_vol_bpm(b, bpm=120); assert f150.read_vol_bpm(b)[1] == 120
    out = f150.apply_edits(f150.blank(3), {"settings": {"bpm": 300}}); assert f150.read_vol_bpm(out)[1] == 255
