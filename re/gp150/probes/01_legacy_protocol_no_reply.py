"""Throwaway read-only probe: send selector 0x40 (names) then 0x41 (active body)
to the GP-150 exactly as live_read.py does for the GP-50. One persistent port,
one request at a time, settle between. Saves raw replies to probe_out/."""
import json, os, sys, time, struct
import mido

PORT = "GP-150"
SETTLE = 0.6
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)

def crc8(data, init=0):
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c

def build_request(sel):
    buf = [0, 0x01, 0x00, 0x02, 0x12, sel]; buf[0] = crc8(buf); return buf
def to_wire(buf):
    o = []
    for b in buf: o += [b >> 4, b & 0xF]
    return o
def nib_decode(m): return [(m[i] << 4) | m[i+1] for i in range(0, len(m)-1, 2)]

def reassemble(replies):
    from collections import defaultdict
    by = defaultdict(list)
    for b in replies:
        if len(b) >= 4: by[b[1]].append((b[2], b[4:]))
    out = {}
    for cmd, ch in by.items():
        ch.sort(); d = []
        for _, x in ch: d += x
        out[cmd] = bytes(d)
    return out

def split_names(blob, hdr=2, rec=20):
    names = []; i = hdr
    while i + rec <= len(blob):
        idx = struct.unpack_from("<I", blob, i)[0]
        nm = blob[i+4:i+rec].split(b"\0")[0].decode("latin1","replace").strip()
        names.append((idx, nm)); i += rec
    return names

def exchange(inp, out, sel, wait=3.0):
    for _ in inp.iter_pending(): pass
    buf = build_request(sel)
    print(f"\n>>> selector {sel:#04x}  wire: F0 {' '.join(f'{x:02x}' for x in to_wire(buf))} F7")
    out.send(mido.Message("sysex", data=to_wire(buf)))
    replies = []; t0 = time.time(); last = t0
    while time.time() - t0 < wait:
        got = False
        for m in inp.iter_pending():
            if m.type == "sysex":
                replies.append(nib_decode(list(m.bytes())[1:-1])); got = True
        if got: last = time.time()
        elif replies and time.time() - last > 0.5: break
        time.sleep(0.02)
    print(f"    {len(replies)} reply frames in {time.time()-t0:.2f}s")
    json.dump(replies, open(f"{OUT}/raw_{sel:02x}.json","w"))
    banks = reassemble(replies)
    for cmd, blob in banks.items():
        open(f"{OUT}/blob_{sel:02x}_cmd{cmd:02x}.bin","wb").write(blob)
        print(f"    reply cmd={cmd:#04x}  {len(blob)} bytes  head: {blob[:24].hex(' ')}")
    time.sleep(SETTLE)
    return banks

with mido.open_input(PORT) as inp, mido.open_output(PORT) as out:
    time.sleep(0.2)
    b40 = exchange(inp, out, 0x40)
    for cmd, blob in b40.items():
        names = split_names(blob)
        print(f"    cmd {cmd:#04x}: ~{len(names)} name records")
        for s, n in names[:12]: print(f"       slot {s:3}: {n!r}")
        if len(names) > 12: print(f"       ... last: {names[-1]}")
    b41 = exchange(inp, out, 0x41)
    for cmd, blob in b41.items():
        print(f"    cmd {cmd:#04x}: body {len(blob)} bytes (incl. 2-byte selector echo -> {len(blob)-2} payload)")
        print("    hexdump head:"); 
        for i in range(0, min(len(blob), 96), 16): print("      %04x  %s" % (i, blob[i:i+16].hex(' ')))
print("\nDONE — pedal still responsive? (check screen). Raw in", OUT)
