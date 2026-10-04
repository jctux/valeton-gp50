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
# drive), plus a move so None blocks shift position and must stay None.
EDIT_NONE = {"models": {8: f150.model_key(8, 3), 0: f150.model_key(0, 1), 3: f150.model_key(3, 3)},
             "order": [5, 9, 10, 0, 1, 2, 3, 4, 6, 7, 8, 11]}
# a None pick turns the block off unless the same edit's bypass turns it on
EDIT_NONE_ON = {"models": {8: f150.model_key(8, 3)}, "bypass": {8: True}}


def _two_step(b, slot):
    """Drag `slot` to chain position 1, then back to the file's own order."""
    orig = f150.read_order(b)
    moved = [orig[0], slot] + [s for s in orig[1:] if s != slot]
    s1 = f150.apply_edits(b, {"order": moved})
    return moved, orig, f150.apply_edits(s1, {"order": orig})


def _strkeys(v):
    if isinstance(v, dict):
        return {str(k): _strkeys(x) for k, x in v.items()}
    return v


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
            "bypass": f150.bypass_mask(b), "floats": f150.param_floats(b),
            "editedB64": base64.b64encode(f150.apply_edits(b, EDIT)).decode(),
            "editedNoneB64": base64.b64encode(f150.apply_edits(b, EDIT_NONE)).decode(),
            "editedNoneOnB64": base64.b64encode(f150.apply_edits(b, EDIT_NONE_ON)).decode(),
            # DLY (a real effect or None, per file) and WAH dragged to position 1 and back
            "twoStep": [
                {"slot": slot, "moved": m, "orig": o, "outB64": base64.b64encode(out).decode()}
                for slot in (9, 2) for (m, o, out) in [_two_step(b, slot)]
            ],
        })
    json.dump({"edit": _strkeys(EDIT), "editNone": _strkeys(EDIT_NONE), "editNoneOn": _strkeys(EDIT_NONE_ON), "blank199B64": base64.b64encode(f150.blank(199)).decode(), "files": out}, sys.stdout)


if __name__ == "__main__":
    main()
