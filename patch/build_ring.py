#!/usr/bin/env python3
"""Regenerate the fxid -> model metadata rings from Valeton Suite's asset data.
Builds one ring per device: fxid_ring.json from module50_data.json (GP-50) and
fxid_ring_gp5.json from module_data.json (GP-5). The GP-5 catalog is a strict
subset of the GP-50's. `origin` = the official gear reference (e.g. Green OD ->
"Ibanez TS808"), used by the explorer's "official names" toggle."""

import json
import os
import re

SUITE_DIRS = [
    # Valeton Suite 2.x (Mac Catalyst build)
    "/Applications/Runner.app/Wrapper/Runner.app/Frameworks/App.framework/flutter_assets/assets/data",
    # Valeton Suite 1.x (desktop build)
    "/Applications/Valeton Suite.app/Contents/Frameworks/App.framework/Versions/A/Resources/flutter_assets/assets/data",
]


def suite_file(name: str) -> str:
    for d in SUITE_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"{name} not found in any Valeton Suite install ({SUITE_DIRS})")


# (source asset filename, output ring filename) per device
RINGS = [
    ("module50_data.json", "fxid_ring.json"),  # GP-50
    ("module_data.json", "fxid_ring_gp5.json"),  # GP-5
]
GP150_SOURCE = "module150_data.json"
GP150_RING = "fxid_ring_gp150.json"
GP150_TYPE_MAP = "gp150_type_map.json"
# GP-150 .prst block slots (patch/prst150_format.SLOTS) -> Suite catalog module name
GP150_SLOTS = ["NR", "PRE", "WAH", "DST", "N->S", "AMP", "CAB", "EQ", "MOD", "DLY", "RVB", "VOL"]


def clean_origin(o: str) -> str:
    if not o:
        return ""
    o = o.split("\n")[0].strip()  # drop "XTOMP/Ampero Name: ..." 2nd line
    o = re.sub(r"^Original:\s*", "", o).strip()  # newer Suite data prefixes "Original:"
    o = re.sub(r"\s*\(MIJ\)\s*$", "", o).strip()  # drop trailing "(MIJ)" noise
    if o.lower() in ("original", "n/a", "none", "-"):  # not a real gear reference
        return ""
    return o


def resolve_origin(entry: dict) -> str:
    """Official gear name. Some origins drop the channel (e.g. Foxy 30N and
    Foxy 30TB are both 'VOX AC30'); recover it from the description's
    '(... channel)' hint when the origin has no parenthetical of its own."""
    o = clean_origin(entry.get("origin"))
    if not o or "(" in o:
        return o
    desc = re.sub(r"<[^>]+>", "", entry.get("descriptionEn") or "")
    m = re.search(r"\(([^)]*?)\s*channel\)", desc, re.I)
    if m:
        chan = m.group(1).strip().title()  # "normal" -> "Normal", "Top Boost"
        return f"{o} ({chan})"
    return o


def unit_from_range(rng: str) -> str:
    """Extract a display unit from a valueRange like '0.10Hz-10.00Hz' -> 'Hz',
    '0-1000ms' -> 'ms'. Returns '' for plain numeric or Off/On ranges."""
    if not rng or "/" in rng:
        return ""
    m = re.search(r"[0-9.]([A-Za-z%]+)\s*$", rng)  # trailing unit on the last number
    return m.group(1) if m else ""


def params_of(entry: dict) -> list:
    """Param definitions in model order: name + algId (float-slot index) + toggle
    flag + unit. Value at runtime = float[block*8 + algId]."""
    out = []
    for p in entry.get("alg") or []:
        try:
            algid = int(p.get("algId", "-1"))
        except (TypeError, ValueError):
            algid = -1
        if algid < 0:
            continue

        def _num(x, default):
            try:
                return float(x)
            except (TypeError, ValueError):
                return default

        out.append(
            {
                "name": p.get("name"),
                "algId": algid,
                "toggle": p.get("widgetType") == 1 or (p.get("valueRange") == "Off/On"),
                "unit": unit_from_range(p.get("valueRange") or ""),
                # slider bounds are in display units == the stored float value
                "min": _num(p.get("min"), 0),
                "max": _num(p.get("max"), 100),
                "step": _num(p.get("step"), 1),
                "default": _num(p.get("defaultValue"), 0),  # applied on model change
            }
        )
    return out


def build_ring(src_name: str) -> dict:
    d = json.load(open(suite_file(src_name)))
    ring = {}
    for m in d["modules"]:
        for e in m["module"]:
            fx = e.get("fxid")
            if fx is None:
                continue
            ring[fx] = {
                "module": m["name"],
                "moduleId": m.get("moduleId"),
                "name": e.get("name"),
                "fxtitle": e.get("fxtitle"),
                "type": e.get("type"),
                "origin": resolve_origin(e),
                "params": params_of(e),
            }
    return ring


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _param_from_spec(p: dict) -> dict:
    rng = p.get("range") or ""
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", rng.replace("–", "-"))]
    toggle = rng.lower() in ("off/on", "on/off", "0/1")
    return {"name": p["name"], "algId": p["index"], "toggle": toggle, "unit": p.get("unit") or "",
            "min": nums[0] if nums else 0.0, "max": nums[1] if len(nums) > 1 else (1.0 if toggle else 100.0),
            "step": 1.0, "default": 0.0}


def build_ring_gp150() -> dict:
    """(slot<<24)|type -> entry. Names/params come from the public spec's type map;
    origin/fxtitle/type/defaults are enriched from Suite's module150_data.json by
    effect name. Entries the spec lacks but the corpus shows are added by the
    scan tool later (see re/gp150/DEVICE_READ.md)."""
    here = os.path.dirname(__file__)
    tmap = json.load(open(os.path.join(here, GP150_TYPE_MAP)))
    suite = json.load(open(suite_file(GP150_SOURCE)))
    by_mod = {}
    for m in suite["modules"]:
        for e in m["module"]:
            by_mod.setdefault(m["name"], {})[_norm(e.get("name"))] = e
            by_mod[m["name"]][_norm(e.get("fxtitle"))] = e
    ring = {}
    for slot_idx, slot in enumerate(GP150_SLOTS):
        for type_s, spec in tmap.get(slot, {}).items():
            key = (slot_idx << 24) | int(type_s)
            suite_e = by_mod.get(slot, {}).get(_norm(spec["name"]))
            params = [_param_from_spec(p) for p in spec["params"]]
            if suite_e:
                by_name = {_norm(p["name"]): p for p in params_of(suite_e)}
                for p in params:
                    sp = by_name.get(_norm(p["name"]))
                    if sp:
                        p.update({k: sp[k] for k in ("toggle", "unit", "min", "max", "step", "default")})
            ring[key] = {
                "module": slot, "moduleId": slot_idx, "name": spec["name"],
                "fxtitle": (suite_e or {}).get("fxtitle") or spec["name"],
                "type": (suite_e or {}).get("type") or "",
                "origin": resolve_origin(suite_e) if suite_e else spec.get("origin", ""),
                "params": params,
            }
        ring.setdefault((slot_idx << 24) | 3, {"module": slot, "moduleId": slot_idx, "name": "None",
                                              "fxtitle": "None", "type": "", "origin": "", "params": []})
    return ring


def main(only: str = "all"):
    """Rebuild the model rings. `only` = 'gp50' | 'gp5' | 'gp150' | 'all'. NOTE: a fresh
    GP-50 build reflects the *currently installed* Valeton Suite data, which can
    drift from the committed fxid_ring.json (origin coverage changes between Suite
    versions) — rebuild it deliberately, not as a side effect."""
    targets = {"gp50": RINGS[0:1], "gp5": RINGS[1:2], "all": RINGS, "gp150": []}.get(only)
    if targets is None:
        raise SystemExit(f"unknown target {only!r} (gp50|gp5|gp150|all)")
    if only in ("gp150", "all"):
        try:
            ring = build_ring_gp150()
        except FileNotFoundError as e:
            print(f"skip gp150: {e}")
        else:
            out = os.path.join(os.path.dirname(__file__), GP150_RING)
            json.dump({str(k): v for k, v in sorted(ring.items())}, open(out, "w"))
            print(f"wrote {out}: {len(ring)} models")
    for src_name, out_name in targets:
        out = os.path.join(os.path.dirname(__file__), out_name)
        ring = build_ring(src_name)
        json.dump({str(k): v for k, v in ring.items()}, open(out, "w"))
        withorigin = sum(1 for v in ring.values() if v["origin"])
        withparams = sum(1 for v in ring.values() if v["params"])
        print(
            f"wrote {out}: {len(ring)} models, {withorigin} origins, "
            f"{withparams} with params"
        )


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "all")
