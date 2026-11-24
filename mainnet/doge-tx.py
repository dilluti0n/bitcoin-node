#!/usr/bin/env python3
# Build and sign a Dogecoin P2PKH -> P2PKH payment from a raw secp256k1 key.
#
# usage: doge-tx.py key.sec <txid>:<vout>:<value_doge> <to_addr> <amount_doge|max> [fee_doge]
#   key.sec : raw 32-byte secret key (bic genkey output)
#   needs the system libsecp256k1 (same one bic links against)
#   max     : send value - fee, no change
#   fee     : default 1 DOGE
# prints the signed raw tx hex to stdout, summary to stderr
import ctypes
import ctypes.util
import hashlib
import sys
from decimal import Decimal

if len(sys.argv) not in (5, 6):
    sys.exit(f'usage: {sys.argv[0]} key.sec <txid>:<vout>:<value_doge> '
             '<to_addr> <amount_doge|max> [fee_doge]')

dsha = lambda b: hashlib.sha256(hashlib.sha256(b).digest()).digest()
p2pkh = lambda pkh: b'\x76\xa9\x14' + pkh + b'\x88\xac'
koinu = lambda s: int(Decimal(s) * 100_000_000)

# system libsecp256k1 via ctypes (no third-party Python deps)
lib = ctypes.CDLL(ctypes.util.find_library('secp256k1') or 'libsecp256k1.so')
lib.secp256k1_context_create.restype = ctypes.c_void_p
ctx = ctypes.c_void_p(lib.secp256k1_context_create(0x301))  # SIGN | VERIFY (NONE on >= 0.2)

seckey = open(sys.argv[1], 'rb').read()
if len(seckey) != 32:
    sys.exit(f'{sys.argv[1]}: expected 32-byte raw secret key, got {len(seckey)} bytes')

pk = ctypes.create_string_buffer(64)  # secp256k1_pubkey
if not lib.secp256k1_ec_pubkey_create(ctx, pk, seckey):
    sys.exit('invalid secret key')
pub = ctypes.create_string_buffer(33)
publen = ctypes.c_size_t(33)
lib.secp256k1_ec_pubkey_serialize(ctx, pub, ctypes.byref(publen), pk, 0x102)  # COMPRESSED
pub = pub.raw

try:
    r = hashlib.new('ripemd160')
except ValueError:
    from Crypto.Hash import RIPEMD160  # pycryptodome
    r = RIPEMD160.new()
r.update(hashlib.sha256(pub).digest())
my_spk = p2pkh(r.digest())

txid_hex, vout, value = sys.argv[2].split(':')
vout, value = int(vout), koinu(value)

A = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
n = 0
for c in sys.argv[3]:
    n = n * 58 + A.index(c)
raw = n.to_bytes(25, 'big')
if raw[0] != 0x1e or dsha(raw[:21])[:4] != raw[21:]:
    sys.exit(f'{sys.argv[3]}: not a valid DOGE P2PKH address')
to_spk = p2pkh(raw[1:21])

fee = koinu(sys.argv[5]) if len(sys.argv) == 6 else 100_000_000  # 1 DOGE
amount = value - fee if sys.argv[4] == 'max' else koinu(sys.argv[4])

change = value - amount - fee
if amount <= 0 or change < 0:
    sys.exit(f'insufficient funds: value {value} koinu, amount {amount}, fee {fee}')

outputs = [(amount, to_spk)]
if change >= 1_000_000:  # 0.01 DOGE; smaller change is left to the fee
    outputs.append((change, my_spk))

def ser(script_sig):
    tx = (1).to_bytes(4, 'little')                       # version
    tx += b'\x01'                                        # vin count
    tx += bytes.fromhex(txid_hex)[::-1] + vout.to_bytes(4, 'little')
    tx += bytes([len(script_sig)]) + script_sig
    tx += b'\xff\xff\xff\xff'                            # sequence
    tx += bytes([len(outputs)])
    for v, spk in outputs:
        tx += v.to_bytes(8, 'little') + bytes([len(spk)]) + spk
    tx += (0).to_bytes(4, 'little')                      # locktime
    return tx

# legacy sighash: scriptSig replaced by the spent output's scriptPubKey, SIGHASH_ALL appended
sighash = dsha(ser(my_spk) + (1).to_bytes(4, 'little'))
csig = ctypes.create_string_buffer(64)  # secp256k1_ecdsa_signature
if not lib.secp256k1_ecdsa_sign(ctx, csig, sighash, seckey, None, None):  # RFC6979, low-S
    sys.exit('signing failed')
assert lib.secp256k1_ecdsa_verify(ctx, csig, sighash, pk) == 1
der = ctypes.create_string_buffer(72)
derlen = ctypes.c_size_t(72)
lib.secp256k1_ecdsa_signature_serialize_der(ctx, der, ctypes.byref(derlen), csig)
sig = der.raw[:derlen.value] + b'\x01'                   # SIGHASH_ALL

tx = ser(bytes([len(sig)]) + sig + bytes([len(pub)]) + pub)
print(tx.hex())
for v, spk in outputs:
    print(f'  out {Decimal(v) / 100_000_000} DOGE  {spk.hex()}', file=sys.stderr)
print(f'  fee {Decimal(value - sum(v for v, _ in outputs)) / 100_000_000} DOGE, '
      f'{len(tx)} bytes, txid {dsha(tx)[::-1].hex()}', file=sys.stderr)
