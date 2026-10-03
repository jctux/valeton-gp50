"""patch/fxid_ring_gp150.json — the GP-150 model catalog keyed by
(slot<<24)|type. Built from patch/gp150_type_map.json (public spec) joined with
Valeton Suite's module150_data.json (never committed)."""
import json
import os

from patch import prst150_format as f150

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RING = json.load(open(os.path.join(ROOT, "patch", "fxid_ring_gp150.json")))
TMAP = json.load(open(os.path.join(ROOT, "patch", "gp150_type_map.json")))


def test_type_map_covers_all_slots():
    assert set(TMAP) == set(f150.SLOTS)
    assert "1" in TMAP["NR"] and TMAP["NR"]["1"]["name"] == "Gate"
    assert TMAP["AMP"]["1"]["name"] == "Tweedy" and [p["name"] for p in TMAP["AMP"]["1"]["params"]][:4] == ["Gain", "Tone", "Volume", "Presence"]


def test_ring_keys_are_slot_shifted_types():
    for k, e in RING.items():
        key = int(k)
        slot, type_ = key >> 24, key & 0xFF
        assert 0 <= slot < 12 and (key & 0x00FFFF00) == 0, k
        assert e["module"] == f150.SLOTS[slot] and e["moduleId"] == slot
        assert e["params"] == sorted(e["params"], key=lambda p: p["algId"])
        assert all(0 <= p["algId"] < 15 for p in e["params"])


def test_every_type_map_entry_is_in_the_ring_and_bypass_entries_exist():
    for slot_name, types in TMAP.items():
        s = f150.SLOTS.index(slot_name)
        for t, e in types.items():
            key = str(f150.model_key(s, int(t)))
            assert key in RING, (slot_name, t)
            assert RING[key]["name"] == e["name"]
    # type 3 with engine 6 is "None"/bypass in every slot of the corpus. The key has no
    # engine, so where the spec defines a real type 3 (AMP/DST/DLY/RVB/VOL) it wins.
    for s, slot_name in enumerate(f150.SLOTS):
        if "3" not in TMAP[slot_name]:
            assert RING[str(f150.model_key(s, 3))]["name"] == "None"


def test_corpus_blocks_resolve_or_fall_back_to_type_label():
    import glob
    files = sorted(glob.glob(os.path.join(ROOT, "re", "gp150", "evidence", "*.prst")))
    seen, hits = 0, 0
    for p in files:
        for type_, slot, low in f150.model_records(open(p, "rb").read()):
            seen += 1
            hits += str(f150.model_key(slot, type_)) in RING
    assert seen == 12 * len(files)
    assert hits >= seen * 0.7  # spec is incomplete for some slots; UI shows "Type N" for the rest


def test_param_bounds_are_ordered():
    for k, e in RING.items():
        for p in e["params"]:
            assert p["min"] <= p["max"], (k, e["name"], p)


def test_param_from_spec_range_parsing():
    from patch import build_ring

    def mm(r):
        q = build_ring._param_from_spec({"name": "X", "index": 0, "range": r, "unit": ""})
        return (q["min"], q["max"]), q["toggle"]

    assert mm("0–100") == ((0, 100), False)
    assert mm("−15–+20") == ((-15, 20), False)
    assert mm("-12–+12") == ((-12, 12), False)
    assert mm("0.10–10.00") == ((0.1, 10.0), False)
    assert mm("200–200") == ((200, 200), False)
    assert mm("Off/On") == ((0, 1), True)
