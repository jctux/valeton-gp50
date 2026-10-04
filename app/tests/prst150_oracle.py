"""Python oracle for app/tests/test_prst150_js.mjs: decode every GP-150 corpus
file with patch/prst150_format.py and emit JSON the JS test compares against."""
import base64
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from patch import prst150_format as f150  # noqa: E402

EDIT = {"name": "JS vs PY", "settings": {"patch_vol": 33, "bpm": 140}, "bypass": {0: False, 10: True},
        "params": {5: {0: 12.5, 1: 77.0}, 9: {2: 300.0}}, "models": {9: f150.model_key(9, 4)},
        "order": [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11]}
# "None" models: MOD -> None (type 3, engine 0x06), NR -> Gate, DST -> type 3 (a real
# drive), plus a reorder (only the order table changes; records stay home).
EDIT_NONE = {"models": {8: f150.model_key(8, 3), 0: f150.model_key(0, 1), 3: f150.model_key(3, 3)},
             "order": [5, 9, 10, 0, 1, 2, 3, 4, 6, 7, 8, 11]}
# a None pick turns the block off unless the same edit's bypass turns it on
EDIT_NONE_ON = {"models": {8: f150.model_key(8, 3)}, "bypass": {8: True}}
# DLY -> Sweet Echo (type 13) with DLY dragged right after AMP: record 9, engine 0x0B
EDIT_DLY = {"models": {9: f150.model_key(9, 13)}, "order": [5, 9, 0, 1, 2, 3, 4, 6, 7, 8, 10, 11]}


def _strkeys(v):
    if isinstance(v, dict):
        return {str(k): _strkeys(x) for k, x in v.items()}
    return v


def _engine(slot, type_):
    try:
        return f150.slot_engine(slot, type_)
    except ValueError:
        return -1  # refused (no engine known)


def _allowed(slot, type_):
    return f150.pick_allowed(slot, type_)


def _repick(b):
    """Every slot re-picks the model stored in it (type, subtype, ext): a stored model
    keeps its engine (keeps_stored_engine); None-stored slots are fresh picks."""
    models = {}
    for blk in f150.blocks_by_slot(b):
        models[blk["slot"]] = f150.model_key(blk["slot"], blk["type"], blk["subtype"], blk["ext"])
    try:
        return base64.b64encode(f150.apply_edits(b, {"models": models})).decode()
    except ValueError:
        return None  # refused


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "re", "gp150", "evidence", "*.prst")))
    files += sorted(glob.glob(os.path.join(os.environ.get("GP180_DUMP_DIR", "/nonexistent"), "*.prst")))
    out = []
    for p in files:
        b = open(p, "rb").read()
        if not f150.detect(b):
            continue
        out.append({
            "path": os.path.relpath(p, ROOT), "prstB64": base64.b64encode(b).decode(),
            "name": f150.read_name(b), "index": f150.read_index(b), "volBpm": list(f150.read_vol_bpm(b)),
            "order": f150.read_order(b), "models": [list(r) for r in f150.model_records(b)],
            "blocks": [{k: v for k, v in blk.items() if k != "params"} for blk in f150.blocks_by_slot(b)],
            "bypass": f150.bypass_mask(b), "floats": f150.param_floats(b),
            "editedB64": base64.b64encode(f150.apply_edits(b, EDIT)).decode(),
            "editedNoneB64": base64.b64encode(f150.apply_edits(b, EDIT_NONE)).decode(),
            "editedNoneOnB64": base64.b64encode(f150.apply_edits(b, EDIT_NONE_ON)).decode(),
            "editedDlyB64": base64.b64encode(f150.apply_edits(b, EDIT_DLY)).decode(),
            "repickB64": _repick(b),
        })
    json.dump({
        "edit": _strkeys(EDIT), "editNone": _strkeys(EDIT_NONE), "editNoneOn": _strkeys(EDIT_NONE_ON),
        "editDly": _strkeys(EDIT_DLY), "blank199B64": base64.b64encode(f150.blank(199)).decode(),
        "defaultOrder": f150.DEFAULT_ORDER, "defaultPos": f150.DEFAULT_POS,
        "slotEngine": [[_engine(s, t) for t in range(256)] for s in range(f150.N_BLOCKS)],
        "pickAllowed": [[_allowed(s, t) for t in range(256)] for s in range(f150.N_BLOCKS)],
        # keeps_stored_engine over every (slot, type, stored type, stored engine) that matters
        "keeps": [[s, t, st, se, f150.keeps_stored_engine(s, t, st, se)]
                  for s in range(f150.N_BLOCKS) for t in (0, 1, 3, 11, 117) for st in (1, 3, 11, 117) for se in (0, 1, 3, 6, 7)],
        "files": out,
    }, sys.stdout)


if __name__ == "__main__":
    main()
