#!/usr/bin/env python3
"""Learn the GP-150 block engine byte per (slot, type) from a corpus of .prst files.

    python3 scripts/gp150_engines.py                 # default corpus -> patch/gp150_engines.json
    python3 scripts/gp150_engines.py DIR [DIR ...]   # another corpus
    python3 scripts/gp150_engines.py --check [DIR]   # exit 1 if the committed table differs

Default corpus: device_scan_gp150/ (the user's full scan, not committed) +
re/gp150/evidence/; byte-identical files are counted once.

The browser codec has its own copy of this table, ENGINES in app/static/prst150.js,
synced BY HAND: after this script rewrites patch/gp150_engines.json, copy the table
into ENGINES (app/tests/test_prst150_js.mjs fails until the two match).

Hardware facts (2026-10-04, re/gp150/evidence/199-reordered-by-pedal.prst): every
block slot's 68-byte record sits at a FIXED index, the slot's home position
(prst150_format.DEFAULT_POS); a reorder rewrites only the order table. The engine
byte (record +7) belongs to the effect in the slot and is never recomputed on a
reorder. Engine 0x06 outside VOL is the "None" effect (always type 3) and is not
counted as a real engine; VOL's own "Volume" carries 0x06.

Table, per slot:
  engines[type] = the most common engine of that (slot, type) in the corpus
                  (a tie goes to the slot's most common real engine, then the lower value)
  default       = the slot's most common real engine, used for a type never seen;
                  null when no preset in the corpus holds a real effect there (WAH)
  counts[type]  = {engine: number of blocks}, for the record
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from patch import prst150_format as f150  # noqa: E402

OUT = os.path.join(ROOT, "patch", "gp150_engines.json")
DEFAULT_DIRS = [os.path.join(ROOT, "device_scan_gp150"), os.path.join(ROOT, "re", "gp150", "evidence")]


def corpus(dirs):
    seen, out = set(), []
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*.prst"))):
            b = open(p, "rb").read()
            h = hashlib.sha256(b).hexdigest()
            if f150.detect(b) and h not in seen:
                seen.add(h)
                out.append(b)
    return out


def home_record(b, slot):
    """(type, engine) of `slot`'s record, read at its fixed home index."""
    o = f150.BLOCKS_OFF + f150.DEFAULT_POS[slot] * f150.BLOCK_LEN
    return b[o + 4], b[o + 7]


def learn(files):
    counts = [collections.defaultdict(collections.Counter) for _ in f150.SLOTS]
    for b in files:
        for s in range(f150.N_BLOCKS):
            type_, engine = home_record(b, s)
            if engine == f150.ENGINE_BYPASS and s != f150.VOL_SLOT:
                continue  # the "None" effect, not a real engine
            counts[s][type_][engine] += 1
    slots = []
    for s, name in enumerate(f150.SLOTS):
        overall = collections.Counter()
        for c in counts[s].values():
            overall.update(c)
        rank = {e: n for e, n in overall.items()}

        def best(c):
            return max(c, key=lambda e: (c[e], rank.get(e, 0), -e))

        engines = {str(t): best(counts[s][t]) for t in sorted(counts[s])}
        slots.append({
            "slot": name,
            "default": best(overall) if overall else None,
            "engines": engines,
            "counts": {str(t): {str(e): n for e, n in sorted(counts[s][t].items())} for t in sorted(counts[s])},
        })
    return {
        "about": "GP-150 engine byte per (slot, type), learned by scripts/gp150_engines.py from the "
                 "user's scan + re/gp150/evidence (block records at fixed home indexes). "
                 "None (type 3 outside VOL) = engine 0x06, not listed.",
        "files": len(files),
        "none_engine": f150.ENGINE_BYPASS,
        "slots": slots,
    }


def dumps(table):
    """One line per slot, so a diff of the table reads per slot."""
    head = {k: v for k, v in table.items() if k != "slots"}
    lines = ["{"] + [f" {json.dumps(k)}: {json.dumps(v)}," for k, v in head.items()] + [' "slots": [']
    rows = [f"  {json.dumps(row)}" for row in table["slots"]]
    return "\n".join(lines + [",\n".join(rows), " ]", "}"]) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dirs", nargs="*", help="directories of .prst files (default: scan + evidence)")
    ap.add_argument("--check", action="store_true", help="compare with patch/gp150_engines.json, write nothing")
    a = ap.parse_args(argv)
    files = corpus(a.dirs or DEFAULT_DIRS)
    if not files:
        print("no GP-150 .prst files found", file=sys.stderr)
        return 2
    text = dumps(learn(files))
    if a.check:
        same = os.path.exists(OUT) and open(OUT).read() == text
        print(f"{OUT}: {'up to date' if same else 'DIFFERS'} ({len(files)} files)")
        return 0 if same else 1
    open(OUT, "w").write(text)
    print(f"wrote {OUT} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
