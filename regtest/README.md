# 작업량·장애 스크립트

`~/btc-lab/scripts/`에 두고 쓰는 것을 기준으로 함. Python 3.9+ (WSL Ubuntu 기본 3.12), 외부 패키지 없음.

| 파일 | 대상 | 역할 |
| --- | --- | --- |
| `workload.py` | miner (10.10.0.1) | 초당 R건 거래 + T초마다 블록 1개, 저수수료/일반 수수료 섞기 |
| `fault.py` | obs (`btc-obs`, `veth-obs`) | CPU 제한, 네트워크 지연·손실 주입/해제 |

로그는 모두 `~/btc-lab/logs/*.jsonl`, 타임스탬프는 ns 단위 epoch(`time.time_ns()`).

## workload.py

```bash
python3 workload.py --rate 5 --block-interval 30 --duration 600 --seed 1
```

| 인자 | 기본값 | 의미 |
| --- | --- | --- |
| `--rate` | (필수) | 초당 거래 수 R |
| `--block-interval` | (필수) | 블록 간격 T(초) |
| `--duration` | 0 | 실행 시간(초). 0이면 Ctrl+C까지 |
| `--low-ratio` | 0.3 | 저수수료 거래 비율 (시나리오 1 강도) |
| `--low-fee-rates` | `0.2,0.5` | 저수수료 후보(sat/vB) — obs가 거부할 거래 |
| `--high-fee-rates` | `2,5,10` | 일반 수수료 후보(sat/vB) |
| `--seed` | 없음 | 같은 값이면 수수료 순서가 매번 같음 |
| `--amount` | 0.0001 | 거래당 금액(BTC). 자기 지갑 주소로 보냄 |

기록 이벤트: `workload_start`, `tx_sent`(txid, fee_rate, low_fee, rpc_ms), `tx_error`, `block_mined`(height, hash), `block_error`, `schedule_lag`, `workload_stop`(실제 속도 포함).

- RPC는 `bitcoin-cli`를 띄우지 않고 JSON-RPC(HTTP keep-alive 연결 1개)로 직접 호출.
- `--send-mode inputs`(기본): 시작할 때 0.001 BTC UTXO 목록을 txid:vout 순으로 정렬해 두고, 거래마다 1개를 지정해 `send` → 지갑 코인 선택을 건너뛰어 빠름(테스트 PC 약 20건/s). 같은 기반 상태(restore.sh base)면 매번 같은 UTXO 순서. UTXO가 떨어지면 종료(`utxo_exhausted`).
- `--send-mode wallet`: 예전 방식 `sendtoaddress`(지갑이 코인 선택, 테스트 PC 약 6건/s).
- `--send-mode replay`: **CPU 시나리오용 고속 부하.** 서명이 필요 없는 표준 출력(P2WSH, witnessScript `<체인번호> OP_DROP OP_TRUE`)으로 1입력-1출력 거래 체인을 여러 개 만들고, 거래를 그 자리에서 계산해 `sendrawtransaction` (테스트 PC 150건/s 안정, 상한 약 190건/s).
  - 시작 전 준비: 체인마다 0.01 BTC 자금 거래 + 블록 1개 (`replay_prepared` 기록, `workload_start` 전에 끝남)
  - 체인 수는 R×T로 자동(`--replay-chains`로 지정 가능), 체인당 미확정 거래 20개까지
  - `--low-ratio`는 '저수수료 체인' 비율. 저수수료 체인은 부모부터 obs가 거부하므로 그 체인 전체가 obs에 없음 → CPU 실험은 `--low-ratio 0` 권장
  - R×T ≤ 8000 (블록 1개에 다 들어가야 함)
  - miner→obs/control 거래 릴레이는 Bitcoin Core의 전파 속도 제한(초당 수십 건) 때문에 대부분 블록 도착 때 `getblocktxn`으로 한꺼번에 받음 → obs의 CPU 사용이 블록 도착 순간에 몰림
- 그래도 호출이 순차라 R이 너무 높으면 못 따라감 → `workload_stop`의 `actual_rate`와 `schedule_lag`를 확인.
- 백그라운드(`&`)로 돌릴 땐 `python3 -u`로 실행하면 출력이 바로 보임.
- 1초 이상 밀리면 몰아서 보내지 않고 건너뜀(부하가 갑자기 몰려 장애처럼 보이는 것 방지).

## fault.py

```bash
python3 fault.py inject cpu 25      # 시나리오 3: CPUQuota=25%
python3 fault.py clear cpu
python3 fault.py inject delay 200   # 시나리오 1: netem delay 200ms
python3 fault.py inject loss 5      # 대체안(호스팅 단절): netem loss 5%
python3 fault.py clear net
python3 fault.py clear all
python3 fault.py status             # 현재 CPUQuota, MemoryCurrent, qdisc
```

- root가 아니면 내부에서 `sudo`를 붙임 (비밀번호 1회 입력).
- **적용 확인까지 통과해야만** `fault_on`/`fault_off`를 기록함. 실패하면 기록 없이 종료 → 정답 레이블이 틀어지지 않음.
- netem은 한 번에 하나만. 이미 걸려 있으면 `clear net` 먼저.
- `veth-obs`(호스트 쪽)의 root qdisc는 **호스트 → obs 방향** 패킷에 적용됨. obs가 받는 거래·블록·응답이 지연되고, 왕복 1회마다 지연 1회가 더해짐.

## 시나리오

### 3. CPU 고갈 (2025 Bitcoin Core DoS 공지의 '결과' 재현)
실제 익스플로잇이 아니라 "CPU를 빼앗긴 상태"를 `CPUQuota`로 흉내 냄.

**수정 (테스트 PC 측정 근거)** — 처음 안(`inputs` R=5 + `cpu 25`)은 효과 없음.
obs가 평소 쓰는 CPU가 코어 1개의 0.5~2%라서, 25%로 제한해도 throttling이 0회.
→ 부하를 `replay` R=150, T=10으로 바꾸고 강도를 **2~5%** 로 낮춤.

replay R=150, T=10, 조건당 60초(블록 6개), 노드별 0.2초 폴링:

| CPUQuota | obs 블록 지연 중앙/최대 | control 지연 중앙/최대 | obs RPC p90 | throttled 횟수 / 시간 |
| --- | --- | --- | --- | --- |
| 없음 | 0.14 / 0.21 s | 0.09 / 0.20 s | 0.5 ms | 0 / 0 ms |
| 10% | 0.38 / 0.48 s | 0.14 / 0.20 s | 0.6 ms | 39 / 3.1 s |
| 5% | 0.92 / 0.97 s | 0.12 / 0.18 s | 3.1 ms | 84 / 7.6 s |
| 2% | 2.64 / 3.19 s | 0.13 / 0.19 s | 500 ms | 270 / 31.4 s |
| 1% | 5.71 / 8.26 s | 0.12 / 0.17 s | 5,301 ms | 590 / 87.5 s |

- control은 모든 조건에서 0.1초대 유지 → 차이는 obs 장애 때문
- 강도 후보: **5%(약함, 블록 지연 ~1s)**, **2%(강함, ~2.6s + RPC 지연)**. 1%는 RPC가 수 초씩 멈춰 측정 자체가 흔들림
- 레이블 확인: 장애 구간에 `cpu.stat`의 `nr_throttled`가 늘지 않은 run은 무효 처리
- 한계: 테스트 PC 1회 측정. 팀원 PC에서 같은 표를 먼저 다시 뽑아 강도 확정 (CPU가 다르면 숫자가 달라짐)

```bash
~/btc-lab/scripts/restore.sh base
python3 -u workload.py --send-mode replay --rate 150 --block-interval 10 --low-ratio 0 \
    --duration 900 --seed 1 &
sleep 300                          # 워밍업 + 정상 구간
python3 fault.py inject cpu 2
sleep 300                          # 장애 구간
python3 fault.py clear cpu
wait                               # 회복 구간 후 workload 종료
```

### 1. 멤풀 정책 분열 × 네트워크 지연
obs만 `minrelaytxfee`를 높여 띄운 상태(실험 조건) + 작업량의 저수수료 거래 + `netem delay`.
obs 멤풀에 없는 거래가 블록에 들어오면 컴팩트 블록 재조립 때 빠진 거래를 다시 요청 → 지연만큼 늦어짐.

**Go / No-go 확인 (본 실험 전)**
1. obs만 `-minrelaytxfee=0.00001`(1 sat/vB)로 재시작, `debug=cmpctblock` 켜기
2. `python3 workload.py --rate 1 --block-interval 9999 --duration 5 --low-ratio 1` (저수수료 거래만 5건, 블록 없음)
3. `bcli miner getrawmempool` / `bcli control getrawmempool` / `bcli obs getrawmempool` → miner·control엔 있고 obs엔 없어야 함
4. `bcli miner generatetoaddress 1 <miner 주소>` 후 obs `debug.log`에서 `txn requested` 확인
- 2번에서 "lower than the minimum fee rate"가 나오면 miner(·control)의 `minrelaytxfee`와 지갑 `mintxfee`를 0.1 sat/vB 이하로 낮춰야 함
- 효과가 없으면 대체안 `inject loss`로 전환 (같은 netem 사용)

## 실험 1회 예시

CPU 시나리오는 위 3번의 명령을 사용.
