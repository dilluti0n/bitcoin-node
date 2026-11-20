#!/usr/bin/env python3
"""
작업량(workload) 스크립트 — miner 노드에 일정한 속도로 거래와 블록을 만든다.

- 초당 R건 거래, T초마다 블록 1개(generatetoaddress)
- 수수료율을 '저수수료(1 sat/vB 미만)'와 '일반' 구간에서 섞어 보낸다
  → 시나리오 1(정책 분열): 저수수료 거래는 minrelaytxfee가 높은 obs가 거부한다
- 모든 이벤트를 ns 단위 epoch 타임스탬프로 JSONL에 기록한다

전송 방식(--send-mode)
  inputs  (기본) 기반 체인의 0.001 BTC UTXO를 1개씩 지정해 지갑 send  (~20건/s)
  wallet          sendtoaddress, 지갑이 코인 선택                       (~6건/s)
  replay          서명 없는 거래 체인을 미리 계산해 sendrawtransaction  (수백 건/s)
                  → CPU 시나리오처럼 obs에 많은 거래 처리를 시키고 싶을 때

사용 예:
  python3 workload.py --rate 5 --block-interval 30 --duration 600
  python3 workload.py --rate 5 --block-interval 30 --low-ratio 0.5 --seed 1
  python3 workload.py --send-mode replay --rate 200 --block-interval 15 --duration 300 --low-ratio 0 --seed 1
"""
import argparse
import base64
import hashlib
import http.client
import json
import math
import os
import random
import sys
import time
import urllib.parse


# ---------------------------------------------------------------- 인자
def parse_args():
    p = argparse.ArgumentParser(description="regtest 작업량 생성기 (miner 대상)")
    # 작업량
    p.add_argument("--rate", type=float, required=True, help="초당 거래 수 R")
    p.add_argument("--block-interval", type=float, required=True, help="블록 간격 T (초)")
    p.add_argument("--duration", type=float, default=0, help="실행 시간(초). 0이면 Ctrl+C까지")
    p.add_argument("--amount", type=float, default=0.0001,
                   help="거래당 보낼 금액 (BTC, inputs/wallet 모드)")
    # 수수료 (sat/vB)
    p.add_argument("--low-fee-rates", default="0.2,0.5",
                   help="저수수료 구간 후보 (쉼표 구분, sat/vB). obs가 거부할 거래")
    p.add_argument("--high-fee-rates", default="2,5,10",
                   help="일반 수수료 구간 후보 (쉼표 구분, sat/vB)")
    p.add_argument("--low-ratio", type=float, default=0.3,
                   help="저수수료 거래 비율 0~1 (시나리오 1의 강도 손잡이). "
                        "replay 모드에서는 '저수수료 체인'의 비율")
    p.add_argument("--seed", type=int, default=None, help="난수 시드 (같은 값이면 같은 수수료 순서)")
    # 전송 방식
    p.add_argument("--send-mode", choices=["inputs", "wallet", "replay"], default="inputs",
                   help="inputs: 0.001 UTXO 1개씩 지정해 send / wallet: sendtoaddress / "
                        "replay: 서명 없는 거래 체인을 sendrawtransaction (고속)")
    p.add_argument("--utxo-amount", type=float, default=0.001,
                   help="inputs 모드에서 쓸 UTXO 금액 (BTC, 기반 체인의 0.001 UTXO)")
    p.add_argument("--replay-chains", type=int, default=0,
                   help="replay 체인 수 (0이면 R×T로 자동 계산)")
    # 노드 접속
    p.add_argument("--rpcconnect", default="10.10.0.1", help="miner IP")
    p.add_argument("--rpcport", default="18443")
    p.add_argument("--rpcuser", default="lab")
    p.add_argument("--rpcpassword", default="lab")
    p.add_argument("--wallet", default="miner")
    p.add_argument("--rpc-timeout", type=float, default=30, help="RPC 1회 제한 시간(초)")
    # 기록
    p.add_argument("--log", default=os.path.expanduser("~/btc-lab/logs/workload.jsonl"))
    a = p.parse_args()

    if a.rate <= 0 or a.block_interval <= 0:
        p.error("--rate, --block-interval은 0보다 커야 합니다")
    if not 0 <= a.low_ratio <= 1:
        p.error("--low-ratio는 0~1 사이여야 합니다")
    a.low_fee_rates = [float(x) for x in a.low_fee_rates.split(",") if x.strip()]
    a.high_fee_rates = [float(x) for x in a.high_fee_rates.split(",") if x.strip()]
    if a.low_ratio > 0 and not a.low_fee_rates:
        p.error("--low-ratio > 0 이면 --low-fee-rates가 필요합니다")
    if a.low_ratio < 1 and not a.high_fee_rates:
        p.error("--low-ratio < 1 이면 --high-fee-rates가 필요합니다")
    if a.send_mode == "replay" and a.rate * a.block_interval > 8000:
        p.error("replay: R×T가 8000을 넘으면 블록 1개(4M WU)에 다 안 들어갑니다. T를 줄이세요")
    return a


# ---------------------------------------------------------------- RPC
class RPCError(Exception):
    pass


_conn = None   # keep-alive HTTP 연결 1개를 재사용 (호출마다 프로세스를 띄우지 않음)
_rpc_id = 0


def _connection(args):
    global _conn
    if _conn is None:
        _conn = http.client.HTTPConnection(args.rpcconnect, int(args.rpcport),
                                           timeout=args.rpc_timeout)
    return _conn


def rpc(args, method, use_wallet=False, **params):
    """bitcoind JSON-RPC 1회 호출 (named 인자). 결과(result)를 그대로 반환."""
    global _conn, _rpc_id
    _rpc_id += 1
    path = "/wallet/" + urllib.parse.quote(args.wallet, safe="") if use_wallet else "/"
    body = json.dumps({"jsonrpc": "1.0", "id": _rpc_id, "method": method, "params": params})
    auth = base64.b64encode(f"{args.rpcuser}:{args.rpcpassword}".encode()).decode()
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}

    for attempt in (1, 2):  # 끊긴 keep-alive 연결이면 한 번만 다시 연결
        try:
            conn = _connection(args)
            conn.request("POST", path, body, headers)
            resp = conn.getresponse()
            raw = resp.read()
            break
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            if _conn is not None:
                _conn.close()
            _conn = None
            if attempt == 2:
                raise RPCError(f"{args.rpcconnect}:{args.rpcport} 연결 실패: {e}") from None

    if resp.status == 401:
        raise RPCError("인증 실패 (rpcuser/rpcpassword 확인)")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RPCError(f"HTTP {resp.status}: {raw[:200].decode(errors='replace')}") from None
    if data.get("error"):
        err = data["error"]
        raise RPCError(f"error code: {err.get('code')} — {err.get('message')}")
    return data["result"]


# ---------------------------------------------------------------- 기록
def log_event(f, event, **fields):
    rec = {"ts": time.time_ns(), "event": event, **fields}
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()


def pick_fee(args, rng):
    low = rng.random() < args.low_ratio
    return rng.choice(args.low_fee_rates if low else args.high_fee_rates), low


class SendError(RPCError):
    """전송 실패 + 그 거래의 수수료 정보 (tx_error 기록용)"""

    def __init__(self, err, fee, low):
        super().__init__(str(err))
        self.fee, self.low = fee, low


# ---------------------------------------------------------------- 전송 방식 1: wallet
class WalletSender:
    """sendtoaddress — 지갑이 매번 코인 선택 (느림)"""

    def __init__(self, args, addr):
        self.args, self.addr = args, addr

    def prepare(self, f):
        return {}

    def send(self, rng):
        fee, low = pick_fee(self.args, rng)
        try:
            txid = rpc(self.args, "sendtoaddress", use_wallet=True,
                       address=self.addr, amount=self.args.amount, fee_rate=fee)
        except RPCError as e:
            raise SendError(e, fee, low) from None
        return txid, fee, low

    def on_block(self, block_hash):
        pass

    def exhausted(self):
        return False


# ---------------------------------------------------------------- 전송 방식 2: inputs
class InputsSender(WalletSender):
    """기반 체인의 0.001 UTXO를 txid:vout 순으로 1개씩 지정해 send.
    같은 기반 상태(restore.sh base)면 매번 같은 UTXO를 같은 순서로 쓴다."""

    def prepare(self, f):
        found = rpc(self.args, "listunspent", use_wallet=True, minconf=1,
                    query_options={"minimumAmount": self.args.utxo_amount,
                                   "maximumAmount": self.args.utxo_amount})
        self.stack = sorted((u["txid"], u["vout"]) for u in found if u.get("spendable", True))
        self.stack.reverse()   # pop()이 정렬 순서의 앞쪽부터 꺼내도록
        if not self.stack:
            sys.exit(f"[workload] {self.args.utxo_amount} BTC UTXO가 없습니다 "
                     f"(restore.sh base 했는지 확인, 또는 --send-mode wallet)")
        return {"utxos": len(self.stack)}

    def send(self, rng):
        fee, low = pick_fee(self.args, rng)
        txid, vout = self.stack.pop()
        try:
            r = rpc(self.args, "send", use_wallet=True, outputs={self.addr: self.args.amount},
                    fee_rate=fee,
                    options={"inputs": [{"txid": txid, "vout": vout}], "add_inputs": False})
        except RPCError as e:
            raise SendError(e, fee, low) from None
        return r["txid"], fee, low

    def exhausted(self):
        return not self.stack


# ---------------------------------------------------------------- 전송 방식 3: replay
def _dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_p2wsh(hrp, program):
    """segwit v0 주소 (BIP173)"""
    def polymod(values):
        gen = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
        chk = 1
        for v in values:
            top = chk >> 25
            chk = (chk & 0x1ffffff) << 5 ^ v
            for i in range(5):
                chk ^= gen[i] if (top >> i) & 1 else 0
        return chk
    acc = bits = 0
    data = [0]
    for byte in program:
        acc, bits = (acc << 8) | byte, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = polymod(exp + data + [0] * 6) ^ 1
    return hrp + "1" + "".join(_B32[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


class ReplaySender:
    """서명이 필요 없는 P2WSH 출력(witnessScript = <체인번호> OP_DROP OP_TRUE)으로
    1입력-1출력 거래 체인을 여러 개 만들고, 거래를 그 자리에서 계산해 sendrawtransaction.

    - 서명이 없어서 miner 쪽 비용이 거의 없음 → 높은 R을 낼 수 있음 (obs에 처리 부하)
    - 표준 거래라서 obs/control도 정상적으로 릴레이·검증함
    - 체인별 수수료율은 시드로 정해짐. 저수수료 체인은 부모부터 obs가 거부하므로
      그 체인의 거래가 통째로 obs에 없음 (시나리오 1과 섞어 쓸 때 주의)
    - 준비(자금 거래 + 블록 1개)는 workload_start 전에 끝남
    """
    VSIZE = 97          # 1입력(P2WSH, witness 9바이트) + 1출력(P2WSH)
    FUND_SAT = 1_000_000  # 체인당 0.01 BTC
    MAX_DEPTH = 20      # 체인당 미확정 거래 수 상한 (멤풀 클러스터 제한보다 충분히 작게)

    def __init__(self, args, addr):
        self.args, self.addr = args, addr

    def prepare(self, f):
        a = self.args
        rng = random.Random(a.seed)
        n = a.replay_chains or max(20, math.ceil(a.rate * a.block_interval / (self.MAX_DEPTH / 2)))
        self.chains = []
        outputs = []
        for i in range(n):
            ws = bytes([4]) + i.to_bytes(4, "little") + b"\x75\x51"   # <i> OP_DROP OP_TRUE
            spk = b"\x00\x20" + hashlib.sha256(ws).digest()
            fee, low = pick_fee(a, rng)
            self.chains.append({"ws": ws, "spk": spk, "fee_rate": fee, "low": low,
                                "fee": math.ceil(fee * self.VSIZE),
                                "prev": None, "value": self.FUND_SAT,
                                "unconf": [], "dead": False})
            outputs.append({_bech32_p2wsh("bcrt", spk[2:]): self.FUND_SAT / 1e8})
        # 주소 계산이 맞는지 노드에 한 번 확인
        chk = rpc(a, "validateaddress", address=list(outputs[0])[0])
        if chk.get("scriptPubKey") != self.chains[0]["spk"].hex():
            sys.exit(f"[workload] replay 주소 계산 오류: {chk}")

        fund_txids = []
        by_spk = {c["spk"].hex(): c for c in self.chains}
        for i in range(0, n, 1000):   # 자금 거래 1개당 출력 1000개까지
            r = rpc(a, "send", use_wallet=True, outputs=outputs[i:i + 1000], fee_rate=2)
            tx = rpc(a, "getrawtransaction", txid=r["txid"], verbosity=1)
            for vo in tx["vout"]:
                c = by_spk.get(vo["scriptPubKey"]["hex"])
                if c:
                    c["prev"] = (r["txid"], vo["n"])
            fund_txids.append(r["txid"])
        rpc(a, "generatetoaddress", nblocks=1, address=self.addr)
        self.order = 0
        info = {"chains": n, "fund_txids": fund_txids,
                "low_chains": sum(c["low"] for c in self.chains)}
        log_event(f, "replay_prepared", **info)
        return info

    def _next_chain(self):
        n = len(self.chains)
        for _ in range(n):
            c = self.chains[self.order % n]
            self.order += 1
            if not c["dead"] and len(c["unconf"]) < self.MAX_DEPTH and c["value"] > 10 * c["fee"]:
                return c
        return None

    def send(self, rng):
        c = self._next_chain()
        if c is None:
            raise RPCError("replay: 보낼 수 있는 체인이 없음 (모두 미확정 상한 도달 — T를 줄이거나 체인 수를 늘리세요)")
        txid_prev, vout = c["prev"]
        value_out = c["value"] - c["fee"]
        txin = bytes.fromhex(txid_prev)[::-1] + vout.to_bytes(4, "little") + b"\x00" \
            + (0xfffffffd).to_bytes(4, "little")
        txout = value_out.to_bytes(8, "little") + bytes([len(c["spk"])]) + c["spk"]
        ver, lock = (2).to_bytes(4, "little"), b"\x00\x00\x00\x00"
        txid = _dsha(ver + b"\x01" + txin + b"\x01" + txout + lock)[::-1].hex()
        wit = b"\x01" + bytes([len(c["ws"])]) + c["ws"]
        raw = ver + b"\x00\x01" + b"\x01" + txin + b"\x01" + txout + wit + lock
        try:
            rpc(self.args, "sendrawtransaction", hexstring=raw.hex())
        except RPCError as e:
            c["dead"] = True   # 이 체인의 다음 거래는 부모가 없으므로 체인 중단
            raise SendError(e, c["fee_rate"], c["low"]) from None
        c["prev"], c["value"] = (txid, 0), value_out
        c["unconf"].append(txid)
        return txid, c["fee_rate"], c["low"]

    def on_block(self, block_hash):
        mined = set(rpc(self.args, "getblock", blockhash=block_hash, verbosity=1)["tx"])
        for c in self.chains:
            if c["unconf"]:
                c["unconf"] = [t for t in c["unconf"] if t not in mined]

    def exhausted(self):
        return all(c["dead"] or c["value"] <= 10 * c["fee"] for c in self.chains)


SENDERS = {"wallet": WalletSender, "inputs": InputsSender, "replay": ReplaySender}


# ---------------------------------------------------------------- 메인
def main():
    args = parse_args()
    rng = random.Random(args.seed)
    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)

    try:
        if args.wallet not in rpc(args, "listwallets"):
            try:
                rpc(args, "loadwallet", filename=args.wallet)
            except RPCError as e:
                sys.exit(f"[workload] 지갑 '{args.wallet}' 로드 실패: {e}")
        addr = rpc(args, "getnewaddress", use_wallet=True)
        sender = SENDERS[args.send_mode](args, addr)
        with open(args.log, "a") as f:
            prep = sender.prepare(f)
        height = rpc(args, "getblockcount")
        bal = rpc(args, "getbalance", use_wallet=True)
    except RPCError as e:
        sys.exit(f"[workload] miner 접속/준비 실패: {e}")

    tx_gap = 1.0 / args.rate
    sent = errors = blocks = skipped = 0

    with open(args.log, "a") as f:
        log_event(f, "workload_start",
                  rate=args.rate, block_interval=args.block_interval,
                  duration=args.duration, low_ratio=args.low_ratio,
                  low_fee_rates=args.low_fee_rates, high_fee_rates=args.high_fee_rates,
                  seed=args.seed, height=height, balance=bal, send_mode=args.send_mode, **prep)
        extra = ", ".join(f"{k}={v}" for k, v in prep.items() if k != "fund_txids")
        print(f"[workload] 시작: R={args.rate}/s, T={args.block_interval}s, "
              f"low_ratio={args.low_ratio}, height={height}, mode={args.send_mode}"
              + (f", {extra}" if extra else ""))
        if args.send_mode == "inputs" and args.duration > 0 and prep["utxos"] < args.rate * args.duration:
            print(f"[workload] 주의: UTXO {prep['utxos']}개 < 필요 {int(args.rate * args.duration)}건 "
                  f"— 도중에 떨어지면 종료합니다", file=sys.stderr)

        t0 = time.monotonic()
        next_tx = t0
        next_block = t0 + args.block_interval
        end = t0 + args.duration if args.duration > 0 else float("inf")

        try:
            while True:
                now = time.monotonic()
                if now >= end:
                    break

                # 1) 블록 차례가 먼저
                if now >= next_block:
                    try:
                        hashes = rpc(args, "generatetoaddress", nblocks=1, address=addr)
                        h = rpc(args, "getblockcount")
                        blocks += 1
                        log_event(f, "block_mined", height=h, hash=hashes[0])
                        sender.on_block(hashes[0])
                    except RPCError as e:
                        log_event(f, "block_error", error=str(e))
                        print(f"[workload] 블록 생성 실패: {e}", file=sys.stderr)
                    next_block += args.block_interval
                    continue

                # 2) 거래 차례
                if now >= next_tx:
                    if sender.exhausted():
                        log_event(f, "utxo_exhausted")
                        print("[workload] 보낼 UTXO/체인 소진 — 종료합니다", file=sys.stderr)
                        break
                    # 1초 넘게 밀리면 몰아서 보내지 않고 건너뛴다(부하가 갑자기 몰리는 것 방지)
                    behind = now - next_tx
                    if behind > 1.0:
                        n = int(behind / tx_gap)
                        skipped += n
                        next_tx += n * tx_gap
                        log_event(f, "schedule_lag", skipped=n, behind_s=round(behind, 3))

                    t_call = time.monotonic()
                    try:
                        txid, fee, low = sender.send(rng)
                        sent += 1
                        log_event(f, "tx_sent", txid=txid, fee_rate=fee, low_fee=low,
                                  rpc_ms=round((time.monotonic() - t_call) * 1000, 1))
                    except RPCError as e:
                        errors += 1
                        log_event(f, "tx_error", fee_rate=getattr(e, "fee", None),
                                  low_fee=getattr(e, "low", None), error=str(e))
                        if errors <= 3:
                            print(f"[workload] 거래 실패: {e}", file=sys.stderr)
                    next_tx += tx_gap
                    continue

                # 3) 할 일이 없으면 가장 가까운 예정 시각까지 대기
                time.sleep(max(0.0, min(next_tx, next_block, end) - time.monotonic()))

        except KeyboardInterrupt:
            print("\n[workload] Ctrl+C — 종료합니다")

        elapsed = time.monotonic() - t0
        log_event(f, "workload_stop", sent=sent, errors=errors, blocks=blocks,
                  skipped=skipped, elapsed_s=round(elapsed, 2),
                  actual_rate=round(sent / elapsed, 3) if elapsed > 0 else 0)
        print(f"[workload] 종료: 거래 {sent}건, 실패 {errors}건, 블록 {blocks}개, "
              f"건너뜀 {skipped}건, 실제 속도 {sent / elapsed if elapsed else 0:.2f}/s")
        print(f"[workload] 로그: {args.log}")


if __name__ == "__main__":
    main()
