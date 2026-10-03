#!/usr/bin/env python3
"""Transcribe Appendix A of the public GP-150 .prst analysis into
patch/gp150_type_map.json: (slot, type code) -> effect name + ordered params.

    curl -sL -o /tmp/GP150_PRST_FORMAT.md \
      https://gist.githubusercontent.com/AlbertoBarba/ec59feecba60ca956eeb6970f0ac0055/raw/445a6b275e0e542b29754f8a3bc09249ca81df8a/GP150_PRST_FORMAT.md
    python3 scripts/gp150_type_map_from_spec.py /tmp/GP150_PRST_FORMAT.md

The markdown has '### <SLOT> slot' sections, '#### type N — Name (Origin)' headings
and a '| # | `Param` | Range | Unit | Description |' table per type.
"""
import json
import os
import re
import sys

SLOT_ALIASES = {"N→S": "N->S", "N->S": "N->S"}
H_SLOT = re.compile(r"^### (.+?) slot\s*$")
H_TYPE = re.compile(r"^#### type (\d+) — (.+?)(?: \((.+)\))?\s*$")
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*([^|]*)\|\s*([^|]*)\|")


def parse(md_lines):
    out, slot, cur = {}, None, None
    for line in md_lines:
        m = H_SLOT.match(line)
        if m:
            slot = SLOT_ALIASES.get(m.group(1).strip(), m.group(1).strip())
            out.setdefault(slot, {})
            cur = None
            continue
        m = H_TYPE.match(line)
        if m and slot:
            cur = {"name": m.group(2).strip(), "origin": (m.group(3) or "").strip(), "params": []}
            out[slot][m.group(1)] = cur
            continue
        m = ROW.match(line)
        if m and cur is not None:
            unit = m.group(4).strip()
            cur["params"].append({"name": m.group(2).strip(), "index": int(m.group(1)),
                                  "range": m.group(3).strip(), "unit": "" if unit in ("—", "-") else unit})
    return out


def main(path):
    out = parse(open(path, encoding="utf8").read().splitlines())
    dst = os.path.join(os.path.dirname(__file__), "..", "patch", "gp150_type_map.json")
    json.dump(out, open(dst, "w"), indent=1, ensure_ascii=False)
    n = sum(len(v) for v in out.values())
    print(f"wrote {dst}: {len(out)} slots, {n} types")
    for s, v in out.items():
        print(f"  {s:5} {len(v)}")


if __name__ == "__main__":
    main(sys.argv[1])
