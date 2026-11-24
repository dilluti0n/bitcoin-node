#!/usr/bin/env python3
# Push a raw transaction straight to a node's P2P port (no RPC, no explorer).
#
# usage: p2p-push.py btc|doge <host>[:port] <tx.hex|->
#   btc : magic f9beb4d9, default port 8333
#   doge: magic c0c0c0c0, default port 22556
import hashlib
import os
import socket
import struct
import sys
import time

if len(sys.argv) != 4 or sys.argv[1] not in ('btc', 'doge'):
    sys.exit(f'usage: {sys.argv[0]} btc|doge <host>[:port] <tx.hex|->')

magic, port, proto = {
    'btc':  (bytes.fromhex('f9beb4d9'), 8333, 70016),
    'doge': (bytes.fromhex('c0c0c0c0'), 22556, 70015),
}[sys.argv[1]]
host, _, p = sys.argv[2].rpartition(':') if ':' in sys.argv[2] else (sys.argv[2], '', '')
port = int(p) if p else port
tx = bytes.fromhex((sys.stdin if sys.argv[3] == '-' else open(sys.argv[3])).read().strip())

dsha = lambda b: hashlib.sha256(hashlib.sha256(b).digest()).digest()
print(f'txid {dsha(tx)[::-1].hex()}', file=sys.stderr)

s = socket.create_connection((host, port), timeout=10)

def send(cmd, payload=b''):
    s.sendall(magic + cmd.encode().ljust(12, b'\0') + struct.pack('<I', len(payload))
              + dsha(payload)[:4] + payload)
    print(f'> {cmd}', file=sys.stderr)

def recv():
    def exact(n):
        b = b''
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                sys.exit('connection closed by peer')
            b += c
        return b
    h = exact(24)
    if h[:4] != magic:
        sys.exit(f'bad magic {h[:4].hex()}')
    cmd = h[4:16].rstrip(b'\0').decode()
    payload = exact(struct.unpack('<I', h[16:20])[0])
    print(f'< {cmd}', file=sys.stderr)
    return cmd, payload

netaddr = struct.pack('<Q', 0) + bytes(10) + b'\xff\xff' + bytes(4) + struct.pack('>H', 0)
ua = b'/p2p-push:0.1/'
send('version', struct.pack('<iQq', proto, 0, int(time.time())) + netaddr + netaddr
     + os.urandom(8) + bytes([len(ua)]) + ua + struct.pack('<i', 0) + b'\x01')

got_version = got_verack = False
while not (got_version and got_verack):
    cmd, payload = recv()
    if cmd == 'version':
        got_version = True
        ver, = struct.unpack('<i', payload[:4])
        ualen = payload[80]
        print(f'  peer {payload[81:81 + ualen].decode(errors="replace")} proto {ver}', file=sys.stderr)
        send('verack')
    elif cmd == 'verack':
        got_verack = True
    elif cmd == 'ping':
        send('pong', payload)

send('tx', tx)

# ping/pong round trip: once pong arrives the peer has processed our tx message
nonce = os.urandom(8)
send('ping', nonce)
while True:
    cmd, payload = recv()
    if cmd == 'pong' and payload == nonce:
        break
    if cmd == 'ping':
        send('pong', payload)
    if cmd == 'reject':  # doge 1.14 still sends BIP61 reject; bitcoin core dropped it
        n = payload[0]
        m = payload[1:1 + n].decode()
        code = payload[1 + n]
        rl = payload[2 + n]
        print(f'  REJECT {m} 0x{code:02x} {payload[3 + n:3 + n + rl].decode()}', file=sys.stderr)
        sys.exit(1)

print('sent; peer processed it (no reject)', file=sys.stderr)
s.close()
