"""Throwaway probe v2: replay Valeton Suite's USB connect sequence (from the GP-180
corpus) against the GP-150 over USB MIDI. Read-only: hello, one settings read,
one 'read active preset' request, acks. One port, one request at a time."""
import json, os, sys, time
import mido
PORT="GP-150"; OUT="probe_out_v2"; os.makedirs(OUT, exist_ok=True)
MAX_SENDS=10; sent=0

def crc31(d):
    c=0
    for b in d:
        c^=b
        for _ in range(8): c=((c<<1)^0x31)&0xff if c&0x80 else (c<<1)&0xff
    return c&0x7f
def build(after_crc: bytes) -> bytes:   # after_crc = fam + hdr + body
    return b"\xf0\x7f"+bytes([crc31(after_crc)])+after_crc+b"\xf7"
def dec(nib):
    return bytes((nib[i]<<4)|nib[i+1] for i in range(0,len(nib)-1,2))

# captured Suite messages (usb-connect.pcapng); self-check our CRC reproduces them
HELLO   = bytes.fromhex("f07f51000001030000f7")
SETTING = bytes.fromhex("f07f250c0000000000000105030008000000030001070001000700010000020000f7")
PRESET  = bytes.fromhex("f07f5c0f000000010000010802000b0000000300030101030001010300000200000f0f0f0f0001f7")
for m in (HELLO,SETTING,PRESET):
    assert build(m[3:-1])==m, ("crc self-check failed", m.hex())
print("CRC self-check vs captured Suite messages: OK")
def ack(txid4: bytes) -> bytes:
    return build(bytes([0x00])+txid4+b"\x00")
assert ack(bytes.fromhex("00000001"))==bytes.fromhex("f07f74000000000100f7")

log=[]
def send(out, wire, label):
    global sent
    if sent>=MAX_SENDS: raise RuntimeError("send cap reached")
    sent+=1
    print(f">>> {label}: {wire.hex(' ')[:90]}{'...' if len(wire)>45 else ''}")
    out.send(mido.Message("sysex", data=list(wire[1:-1])))
    log.append({"dir":"H>D","label":label,"raw":wire.hex()})

def collect(inp, wait=3.0, idle=0.8):
    got=[]; t0=time.time(); last=t0
    while time.time()-t0<wait:
        for m in inp.iter_pending():
            if m.type=="sysex":
                r=bytes(m.bytes()); got.append(r); last=time.time()
                log.append({"dir":"D>H","raw":r.hex()})
                fam=r[3] if len(r)>3 else -1
                print(f"<<<   fam=0x{fam:02x} len={len(r):4} hdr={r[4:9].hex(' ')} body={r[9:40].hex(' ')}{'...' if len(r)>40 else ''}")
        if got and time.time()-last>idle: break
        time.sleep(0.01)
    if not got: print("<<<   (no reply)")
    return got

with mido.open_input(PORT) as inp, mido.open_output(PORT) as out:
    time.sleep(0.2)
    for _ in inp.iter_pending(): pass
    # 1. hello
    send(out, HELLO, "hello"); r=collect(inp); time.sleep(0.3)
    if not r:
        print("\nRESULT: no response to hello. (Suite BLE session may hold the pedal; or GP-150 differs.)")
        json.dump(log, open(f"{OUT}/log.json","w"), indent=1); sys.exit(0)
    # 2. settings read -> expect ack (fam 00) + fam 0x10 identification -> ack it
    send(out, SETTING, "settings-read"); r=collect(inp); time.sleep(0.3)
    for m in r:
        if m[3]!=0x00: send(out, ack(m[4:8]), f"ack tx={m[4:8].hex()}"); time.sleep(0.2)
    # 3. read active preset (slot 0xffff, export flag) -> ack + 0x70 stream
    send(out, PRESET, "read-active-preset"); r=collect(inp, wait=6.0, idle=1.2); time.sleep(0.3)
    chunks=[m for m in r if m[3]==0x70]
    if chunks:
        tid=chunks[-1][7]
        send(out, ack(bytes([0,0,0,tid])), f"ack transfer id={tid:#04x}")
        data=b"".join(dec(c[9:-1]) for c in chunks)
        i=data.find(b"\x11\x30\x64\x04")
        print(f"\n0x70 stream: {len(chunks)} chunks, decoded {len(data)} bytes, magic at {i}, prefix={data[:i].hex() if i>=0 else '-'}")
        if i>=0:
            prst=data[i:i+1128]
            name=prst[0x2c:0x2c+0x44].split(b"\0")[0].decode("latin1","replace")
            print(f"PRESET: index={prst[4]} name={name!r} bpm={prst[0x24]} vol={prst[0x26]} chain={list(prst[0x78:0x84])}")
            open(f"{OUT}/active.prst","wb").write(prst)
            open(f"{OUT}/active_with_prefix.bin","wb").write(data)
    time.sleep(0.5); extra=collect(inp, wait=2.0, idle=2.0)
json.dump(log, open(f"{OUT}/log.json","w"), indent=1)
print("\nDONE. log:", f"{OUT}/log.json")
