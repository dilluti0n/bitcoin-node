작업량·장애 스크립트

~/btc-lab/scripts/에 두고 쓰는 것을 기준으로 함. Python 3.9+ (WSL Ubuntu 기본 3.12), 외부 패키지 없음.

파일	대상	역할
workload.py	miner (10.10.0.1)	초당 R건 거래 + T초마다 블록 1개, 저수수료/일반 수수료 섞기
fault.py	obs (btc-obs, veth-obs)	CPU 제한, 네트워크 지연·손실 주입/해제

로그는 모두 ~/btc-lab/logs/*.jsonl, 타임스탬프는 ns 단위 epoch(time.time_ns()).

노드와 스크립트의 연결

obs = start-nodes.sh가 systemd-run으로 띄운 bitcoind 프로세스(유닛 btc-obs, netns 안 10.10.0.2).

workload.py ──RPC(10.10.0.1:18443)──▶ miner ──P2P──▶ obs / control
                                                     ▲
fault.py ── systemctl set-property btc-obs ──────────┤ (obs 프로세스의 cgroup: CPU)
         └─ tc qdisc ... dev veth-obs ───────────────┘ (obs로 들어가는 veth: 네트워크)
workload.py는 miner에만 RPC를 보냄. obs는 miner에게서 거래·블록을 P2P로 받아 처리함.
fault.py는 RPC를 쓰지 않고 obs를 바깥에서 건드림. CPU는 btc-obs 유닛(cgroup), 네트워크는 호스트 쪽 veth-obs.
control은 어느 스크립트도 건드리지 않음.
기본값(btc-obs, veth-obs, 10.10.0.x)은 서버의 start-nodes.sh/setup-net.sh 기준. 다르면 fault.py --unit/--dev, workload.py --rpcconnect로 바꿔서 실행.
workload.py
bash
python3 workload.py --rate 5 --block-interval 30 --duration 600 --seed 1
인자	기본값	의미
--rate	(필수)	초당 거래 수 R
--block-interval	(필수)	블록 간격 T(초)
--duration	0	실행 시간(초). 0이면 Ctrl+C까지
--low-ratio	0.3	저수수료 거래 비율 (시나리오 1 강도)
--low-fee-rates	0.2,0.5	저수수료 후보(sat/vB) — obs가 거부할 거래
--high-fee-rates	2,5,10	일반 수수료 후보(sat/vB)
--seed	없음	같은 값이면 수수료 순서가 매번 같음
--amount	0.0001	거래당 금액(BTC). 자기 지갑 주소로 보냄

기록 이벤트: workload_start, tx_sent(txid, fee_rate, low_fee, rpc_ms), tx_error, block_mined(height, hash), block_error, schedule_lag, workload_stop(실제 속도 포함).

거래 1건 = bitcoin-cli 1회 호출이라 R을 너무 높이면 못 따라감 → workload_stop의 actual_rate와 schedule_lag를 확인.
1초 이상 밀리면 몰아서 보내지 않고 건너뜀(부하가 갑자기 몰려 장애처럼 보이는 것 방지).
fault.py
bash
python3 fault.py inject cpu 25      # 시나리오 3: CPUQuota=25%
python3 fault.py clear cpu
python3 fault.py inject delay 200   # 시나리오 1: netem delay 200ms
python3 fault.py inject loss 5      # 대체안(호스팅 단절): netem loss 5%
python3 fault.py clear net
python3 fault.py clear all
python3 fault.py status             # 현재 CPUQuota, MemoryCurrent, qdisc
root가 아니면 내부에서 sudo를 붙임 (비밀번호 1회 입력).
적용 확인까지 통과해야만 fault_on/fault_off를 기록함. 실패하면 기록 없이 종료 → 정답 레이블이 틀어지지 않음.
netem은 한 번에 하나만. 이미 걸려 있으면 clear net 먼저.
veth-obs(호스트 쪽)의 root qdisc는 호스트 → obs 방향 패킷에 적용됨. obs가 받는 거래·블록·응답이 지연되고, 왕복 1회마다 지연 1회가 더해짐.
시나리오
3. CPU 고갈 (2025 Bitcoin Core DoS 공지의 '결과' 재현)

실제 익스플로잇이 아니라 "CPU를 빼앗긴 상태"를 CPUQuota로 흉내 냄.

restore.sh base → workload 시작 → 워밍업 → inject cpu 25 → (유지) → clear cpu → 회복 → workload 종료
1. 멤풀 정책 분열 × 네트워크 지연

obs만 minrelaytxfee를 높여 띄운 상태(실험 조건) + 작업량의 저수수료 거래 + netem delay. obs 멤풀에 없는 거래가 블록에 들어오면 컴팩트 블록 재조립 때 빠진 거래를 다시 요청 → 지연만큼 늦어짐.

Go / No-go 확인 (본 실험 전)

obs만 -minrelaytxfee=0.00001(1 sat/vB)로 재시작, debug=cmpctblock 켜기
python3 workload.py --rate 1 --block-interval 9999 --duration 5 --low-ratio 1 (저수수료 거래만 5건, 블록 없음)
bcli miner getrawmempool / bcli control getrawmempool / bcli obs getrawmempool → miner·control엔 있고 obs엔 없어야 함
bcli miner generatetoaddress 1 <miner 주소> 후 obs debug.log에서 txn requested 확인
2번에서 "lower than the minimum fee rate"가 나오면 miner(·control)의 minrelaytxfee와 지갑 mintxfee를 0.1 sat/vB 이하로 낮춰야 함
효과가 없으면 대체안 inject loss로 전환 (같은 netem 사용)
실험 1회 예시
bash
~/btc-lab/scripts/restore.sh base
python3 workload.py --rate 5 --block-interval 30 --duration 900 --seed 1 &
sleep 300                          # 워밍업 + 정상 구간
python3 fault.py inject cpu 25
sleep 300                          # 장애 구간
python3 fault.py clear cpu
wait                               # 회복 구간 후 workload 종료
