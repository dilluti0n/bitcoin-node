#!/usr/bin/env python3
import hashlib
import sys

pub = open(sys.argv[1], 'rb').read()
if len(pub) != 33 or pub[0] not in (2, 3):
    sys.exit(f'{sys.argv[0]}: expected 33-byte compressed pubkey, got {len(pub)} bytes')

try:
    r = hashlib.new('ripemd160')
except ValueError:
    from Crypto.Hash import RIPEMD160  # pycryptodome
    r = RIPEMD160.new()
r.update(hashlib.sha256(pub).digest())

d = b'\x1e' + r.digest()  # Dogecoin mainnet P2PKH version byte
d += hashlib.sha256(hashlib.sha256(d).digest()).digest()[:4]

A = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
n, s = int.from_bytes(d, 'big'), ''
while n:
    n, m = divmod(n, 58)
    s = A[m] + s
print(s)
