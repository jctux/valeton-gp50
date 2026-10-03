"""Throwaway probe v3: read specific slots (export flag) with IMMEDIATE acks.
Read-only. Verifies: slot addressing, no retransmit with prompt ack, and that the
active preset is unchanged afterwards."""
import json, os, sys, time
import mido
PORT="GP-150"; OUT="probe_out_v3"; os.makedirs(OUT, exist_ok=True)
SLOTS=[int(a) for a in sys.argv[1:]] or [0, 99, 199]
def crc31(d):
    c=0
    for b in d:
        c^=b
        for _ in range(8): c=((c<<1)^0x31)&0xff if c&0x80 else (c<<1)&0xff
    return c&0x7f
def build(after): return b"\xf0\x7f"+bytes([crc31(after)])+after+b"\xf7"
def enc(b): return bytes(x for v in b for x in (v>>4, v&0xf))
def dec(n): return bytes((n[i]<<4)|n[i+1] for i in range(0,len(n)-1,2))
tx=0x10
def preset_request(slot, export=True):
    """fam 0x0f; body = 00 + nibbles(01 CC 0b 00 03 03 11 30 11 30 02 00 <slot u16 LE> <flag>)
    CC = check byte; captured values vary -> computed below as crc31 over payload (hypothesis
    tested offline; if wrong the device will NAK/ignore and we stop)."""
    global tx; tx+=1
    payload=bytes([0x03,0x03,0x11,0x30,0x11,0x30,0x02,0x00, slot&0xff, (slot>>8)&0xff, 0x01 if export else 0x00])
    pre=bytes([0x01, CHECK(payload), len(payload)&0xff, len(payload)>>8])
    return build(bytes([0x0f,0,0,0,tx,0x00])+enc(pre+payload)), tx
CHECK=lambda p: crc31(p)   # overwritten after offline test if needed
def ack(txid4): return build(bytes([0x00])+txid4+b"\x00")
HELLO=bytes.fromhex("f07f51000001030000f7")
ACTIVE=bytes.fromhex("f07f5c0f000000010000010802000b0000000300030101030001010300000200000f0f0f0f0001f7")

# sanity: does our builder reproduce the captured export-slot-01 request (tx 0x23)?
cap=bytes.fromhex("f07f490f000000230000010408000b000000030003010103000101030000020000000000000001f7")
tx=0x22; mine,_=preset_request(0, True)
print("builder vs captured export-slot-01:", "MATCH" if mine==cap else f"DIFF\n  mine={mine.hex()}\n  cap ={cap.hex()}")
tx=0x10

def read_stream(inp, out, req, label, wait=6.0):
    out.send(mido.Message("sysex", data=list(req[1:-1]))); print(f">>> {label}")
    chunks=[]; acked=False; t0=time.time(); last=t0; other=[]
    while time.time()-t0<wait:
        for m in inp.iter_pending():
            if m.type!="sysex": continue
            r=bytes(m.bytes()); last=time.time()
            if r[3]==0x70:
                chunks.append(r)
                if len(r)<248 and not acked:   # final (short) chunk -> ack NOW
                    out.send(mido.Message("sysex", data=list(ack(bytes([0,0,0,r[7]]))[1:-1]))); acked=True; tack=time.time()
            elif r[3]==0x00: pass
            else: other.append(r)
        if acked and time.time()-last>0.8: break
        if not chunks and time.time()-t0>3: break
        time.sleep(0.005)
    extra=0; t1=time.time()
    while time.time()-t1<1.5:
        for m in inp.iter_pending():
            if m.type=="sysex": extra+=1
        time.sleep(0.01)
    data=b"".join(dec(c[9:-1]) for c in chunks)
    i=data.find(b"\x11\x30\x64\x04")
    prst=data[i:i+1128] if i>=0 else b""
    name=prst[0x2c:0x70].split(b"\0")[0].decode("latin1","replace") if prst else "-"
    print(f"    chunks={len(chunks)} acked={acked} extra_after={extra} other={[hex(o[3]) for o in other]} -> index={prst[4] if prst else '-'} name={name!r} prefix={data[:8].hex()}")
    return prst, other

with mido.open_input(PORT) as inp, mido.open_output(PORT) as out:
    time.sleep(0.2)
    for _ in inp.iter_pending(): pass
    out.send(mido.Message("sysex", data=list(HELLO[1:-1]))); time.sleep(0.4)
    for _ in inp.iter_pending(): pass
    results={}
    for s in SLOTS:
        req,t=preset_request(s, True)
        prst,_=read_stream(inp,out,req,f"read slot {s} (tx={t:#x}) req={req.hex()}")
        if prst: open(f"{OUT}/slot{s:03d}.prst","wb").write(prst); results[s]=prst
        time.sleep(0.4)
    prst,_=read_stream(inp,out,ACTIVE,"re-read ACTIVE (should still be index 100)")
    if prst: open(f"{OUT}/active_after.prst","wb").write(prst)
print("DONE")
