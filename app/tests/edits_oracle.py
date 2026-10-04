"""Emit expected apply-edits results for the in-repo corpus, as a JSON manifest.
The JS port (app/static/prst.js applyEdits) is checked byte-for-byte against this
by app/tests/test_edits_js.mjs.

GP-150 presets (re/gp150/evidence/) are edited by their own codec,
patch/prst150_format.apply_edits, with an edit spec keyed by SLOT index (0..11);
the JS side routes through PRST.codecFor(PRST.detect(base)).applyEdits.
"""

import base64
import copy
import glob
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app import patchlib  # noqa: E402
from patch import prst150_format as f150  # noqa: E402
from patch import prst_format as fmt  # noqa: E402

# A spread of edit specs exercising every branch of apply_edits_bytes.
EDIT_SETS = [
    {
        "label": "params",
        "edits": {"params": {"0": {"0": 42.0, "1": 7.5}, "2": {"3": 100.0}}},
    },
    {"label": "bypass", "edits": {"bypass": {"0": True, "1": False, "3": True}}},
    {"label": "settings", "edits": {"settings": {"patch_vol": 73, "bpm": 132}}},
    {"label": "settings-clamp", "edits": {"settings": {"patch_vol": 250}}},
    {"label": "footswitches", "edits": {"footswitches": {"fs1": [0, 2], "fs2": [5]}}},
    {"label": "models", "edits": {"models": {"0": 0x0A00003C, "4": 0x01000001}}},
    {"label": "name", "edits": {"name": "My Lead Tone"}},
    {"label": "name-long", "edits": {"name": "ThisNameIsWayTooLongForTheField"}},
    # chain reorder: a full permutation of the 10 model records (skipped for
    # presets with no REC_ORDER record; see records()).
    {"label": "order-rvb-first", "edits": {"order": [8, 0, 1, 2, 9, 3, 4, 5, 6, 7]}},
    {
        "label": "order+name",
        "edits": {"order": [0, 7, 1, 2, 9, 3, 4, 5, 6, 8], "name": "Reordered"},
    },
    {
        "label": "name+params",
        "edits": {"name": "Clean 2", "params": {"1": {"0": 33.0}}},
    },
    {
        "label": "combined",
        "edits": {
            "params": {"1": {"0": 12.0, "2": 88.0}},
            "bypass": {"2": False},
            "settings": {"patch_vol": 40, "bpm": 90},
            "footswitches": {"fs1": [1], "fs2": [3, 4]},
            "models": {"5": 0x05000008},
        },
    },
]


# GP-150: one combined edit, keys are slot indexes (Explorer block index == slot):
# AMP param 0, RVB off, DLY model -> type 4, rename, VOL/BPM, RVB moved right after AMP.
GP150_EDIT_SETS = [
    {
        "label": "gp150-combined",
        "edits": {
            "params": {"5": {"0": 33.0}},
            "bypass": {"10": False},
            "models": {"9": f150.model_key(9, 4)},
            "name": "Oracle",
            "settings": {"patch_vol": 60, "bpm": 100},
            "order": [5, 10, 0, 1, 2, 3, 4, 6, 7, 8, 9, 11],
        },
    },
]


def gp150_corpus() -> list[str]:
    return sorted(glob.glob(os.path.join(ROOT, "re", "gp150", "evidence", "*.prst")))


def corpus() -> list[str]:
    paths = sorted(glob.glob(os.path.join(ROOT, "presetExports", "*.prst")))[:12]
    paths += sorted(
        glob.glob(os.path.join(ROOT, "app", "tests", "fixtures", "gp5", "*.prst"))
    )
    return paths


def records() -> list:
    out = []
    for path in corpus():
        with open(path, "rb") as fh:
            base = fh.read()
        src = fmt.detect(base)
        for es in EDIT_SETS:
            # order edits only apply to presets that carry a REC_ORDER record
            if "order" in es["edits"] and fmt.order_offset(base) < 0:
                continue
            b = bytearray(base)
            patchlib.apply_edits_bytes(b, copy.deepcopy(es["edits"]))
            out.append(
                {
                    "path": os.path.relpath(path, ROOT),
                    "srcKey": src.key,
                    "label": es["label"],
                    "edits": es["edits"],
                    "baseB64": base64.b64encode(base).decode(),
                    "editedB64": base64.b64encode(bytes(b)).decode(),
                }
            )
    for path in gp150_corpus():
        with open(path, "rb") as fh:
            base = fh.read()
        src = fmt.detect(base)
        if src.key != "gp150":
            continue
        for es in GP150_EDIT_SETS:
            edited = f150.apply_edits(base, copy.deepcopy(es["edits"]))
            out.append(
                {
                    "path": os.path.relpath(path, ROOT),
                    "srcKey": src.key,
                    "label": es["label"],
                    "edits": es["edits"],
                    "baseB64": base64.b64encode(base).decode(),
                    "editedB64": base64.b64encode(edited).decode(),
                }
            )
    return out


if __name__ == "__main__":
    print(json.dumps(records()))
