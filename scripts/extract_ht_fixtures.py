#!/usr/bin/env python3
"""Pick the handful of captured GP-180 SysEx messages our tests pin against.

    python3 scripts/extract_ht_fixtures.py /path/to/Valeton-GP180-Rev-Eng/sysex-corpus.jsonl

Writes app/tests/fixtures/gp150/ht_corpus.json. Only ~40 messages are kept
(hello/ack, one export request + its 0x70 stream, one import stream, two select
requests) — enough to pin framing, CRCs and chunking byte-for-byte.
"""
import json
import os
import sys

WANT = [
    ("hello", "usb-connect.pcapng", 61),
    ("hello_reply", "usb-connect.pcapng", 63),
    ("settings_read", "usb-connect.pcapng", 67),
    ("ack_tx0", "usb-connect.pcapng", 69),
    ("ident_reply", "usb-connect.pcapng", 71),
    ("ack_tx1_host", "usb-connect.pcapng", 73),
    ("read_active", "usb-connect.pcapng", 75),
    ("export_req_slot1", "suite-triggered-patch-file-export-slot-01.pcapng", 13),
    ("export_ack", "suite-triggered-patch-file-export-slot-01.pcapng", 15),
    ("export_final_ack", "suite-triggered-patch-file-export-slot-01.pcapng", 37),
    ("select_slot2", "suite-triggered-edit-patch-select-patch-003-then-004.pcapng", 13),
    ("select_slot3", "suite-triggered-edit-patch-select-patch-003-then-004.pcapng", 47),
    ("import_done_ack", "suite-triggered-import-patch-file-into-slot-01.pcapng", 33),
    ("import_notify_08", "suite-triggered-import-patch-file-into-slot-01.pcapng", 35),
    ("import_notify_ack", "suite-triggered-import-patch-file-into-slot-01.pcapng", 37),
]
STREAMS = [
    ("export_stream_slot1", "suite-triggered-patch-file-export-slot-01.pcapng", "device-to-host"),
    ("import_stream_slot1", "suite-triggered-import-patch-file-into-slot-01.pcapng", "host-to-device"),
]


def main(corpus_path):
    rows = [json.loads(l) for l in open(corpus_path)]
    by = {(r["capture"], r["frame"]): r for r in rows}
    out = []
    for mid, cap, fr in WANT:
        r = by[(cap, fr)]
        out.append({"id": mid, "dir": "H>D" if r["direction"] == "host-to-device" else "D>H",
                    "capture": cap, "frame": fr, "raw": r["raw"]})
    for sid, cap, dirn in STREAMS:
        sel = sorted((r for r in rows if r["capture"] == cap and r["family"] == "0x70"
                      and r["direction"] == dirn), key=lambda r: r["frame"])
        for i, r in enumerate(sel):
            out.append({"id": f"{sid}_{i}", "dir": "H>D" if dirn == "host-to-device" else "D>H",
                        "capture": cap, "frame": r["frame"], "raw": r["raw"]})
    dst = os.path.join(os.path.dirname(__file__), "..", "app", "tests", "fixtures", "gp150", "ht_corpus.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    json.dump({"source": "majabojarska/Valeton-GP180-Rev-Eng sysex-corpus.jsonl", "messages": out},
              open(dst, "w"), indent=1)
    print(f"wrote {dst}: {len(out)} messages")


if __name__ == "__main__":
    main(sys.argv[1])
