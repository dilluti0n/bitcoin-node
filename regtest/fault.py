#!/usr/bin/env python3
"""
장애 주입 스크립트 — obs 노드에만 장애를 켜고 끈다.

시나리오
  3) CPU 고갈 (2025 DoS 공지 재현):  inject cpu <퍼센트>   / clear cpu
  1) 정책 분열 × 지연:               inject delay <ms>     / clear net
     (대체안 2: 호스팅 단절)          inject loss <퍼센트>  / clear net

주입·제거에 성공하고, 실제로 적용됐는지 확인까지 통과한 경우에만
fault_on / fault_off 를 JSONL에 기록한다 (탐지 평가의 정답 레이블).

사용 예:
  python3 fault.py inject cpu 25
  python3 fault.py clear cpu
  python3 fault.py inject delay 200
  python3 fault.py clear net
  python3 fault.py clear all
  python3 fault.py status
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time


def parse_args():
    p = argparse.ArgumentParser(description="obs 장애 주입기")
    p.add_argument("--unit", default="btc-obs", help="obs의 systemd 유닛 이름")
    p.add_argument("--dev", default="veth-obs", help="obs 쪽 veth 인터페이스 (호스트 측)")
    p.add_argument("--log", default=os.path.expanduser("~/btc-lab/logs/faults.jsonl"))
    sub = p.add_subparsers(dest="action", required=True)

    inj = sub.add_parser("inject", help="장애 켜기")
    inj.add_argument("type", choices=["cpu", "delay", "loss"])
    inj.add_argument("level", help="cpu: 퍼센트(예: 25) / delay: ms(예: 200) / loss: 퍼센트(예: 5)")

    clr = sub.add_parser("clear", help="장애 끄기")
    clr.add_argument("type", choices=["cpu", "net", "all"])

    sub.add_parser("status", help="현재 적용 상태 보기")
    return p.parse_args()


# ---------------------------------------------------------------- 명령 실행
def run(cmd, need_root=True, check=True):
    """root가 아니면 sudo를 붙여 실행. (로그 파일은 일반 사용자 소유로 남는다)"""
    if need_root and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"명령 실패: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


def log_event(path, event, **fields):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rec = {"ts": time.time_ns(), "event": event, **fields}
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 상태 확인
def cpu_state(unit):
    # CPUQuota=25% 는 CPUQuotaPerSecUSec=250ms 로 보인다. 해제 상태는 infinity
    r = run(["systemctl", "show", unit, "-p", "CPUQuotaPerSecUSec", "--value"], need_root=False)
    return r.stdout.strip()


def net_state(dev):
    r = run(["tc", "qdisc", "show", "dev", dev], need_root=False)
    return r.stdout.strip()


def has_netem(dev):
    return "netem" in net_state(dev)


# ---------------------------------------------------------------- 레벨 검증
def parse_level(ftype, level):
    s = level.strip().rstrip("%").lower().removesuffix("ms")
    try:
        v = float(s)
    except ValueError:
        sys.exit(f"[fault] 강도 형식이 잘못됐습니다: {level}")
    if ftype == "cpu" and not (1 <= v <= 1000):
        sys.exit("[fault] cpu는 1~1000(%) 사이로 주세요 (100% = 코어 1개)")
    if ftype == "delay" and not (0 < v <= 10000):
        sys.exit("[fault] delay는 0 초과 10000(ms) 이하로 주세요")
    if ftype == "loss" and not (0 < v <= 100):
        sys.exit("[fault] loss는 0 초과 100(%) 이하로 주세요")
    return int(v) if v.is_integer() else v


# ---------------------------------------------------------------- 주입 / 제거
def inject(a):
    v = parse_level(a.type, a.level)

    if a.type == "cpu":
        run(["systemctl", "set-property", "--runtime", a.unit, f"CPUQuota={v}%"])
        state = cpu_state(a.unit)
        if state in ("", "infinity"):
            raise RuntimeError(f"CPUQuota가 적용되지 않았습니다 (현재: {state or '없음'})")
        level = f"{v}%"

    else:  # delay / loss → netem
        if has_netem(a.dev):
            sys.exit(f"[fault] {a.dev}에 이미 netem이 걸려 있습니다. 먼저 'clear net' 하세요.\n"
                     f"  현재: {net_state(a.dev)}")
        arg = ["delay", f"{v}ms"] if a.type == "delay" else ["loss", f"{v}%"]
        run(["tc", "qdisc", "add", "dev", a.dev, "root", "netem"] + arg)
        state = net_state(a.dev)
        if "netem" not in state:
            raise RuntimeError(f"netem이 적용되지 않았습니다 (현재: {state})")
        level = f"{v}ms" if a.type == "delay" else f"{v}%"

    log_event(a.log, "fault_on", type=a.type, level=level, applied=state)
    print(f"[fault] ON  {a.type} {level}  (확인: {state})")


def clear_cpu(a):
    if cpu_state(a.unit) in ("", "infinity"):
        print("[fault] cpu: 이미 해제 상태 — 기록 안 함")
        return
    run(["systemctl", "set-property", "--runtime", a.unit, "CPUQuota="])
    state = cpu_state(a.unit)
    if state not in ("", "infinity"):
        raise RuntimeError(f"CPUQuota 해제 실패 (현재: {state})")
    log_event(a.log, "fault_off", type="cpu", applied=state or "infinity")
    print("[fault] OFF cpu")


def clear_net(a):
    before = net_state(a.dev)
    if "netem" not in before:
        print("[fault] net: 이미 해제 상태 — 기록 안 함")
        return
    m = re.search(r"\b(delay|loss)\b", before)
    ftype = m.group(1) if m else "net"
    run(["tc", "qdisc", "del", "dev", a.dev, "root"])
    after = net_state(a.dev)
    if "netem" in after:
        raise RuntimeError(f"netem 해제 실패 (현재: {after})")
    log_event(a.log, "fault_off", type=ftype, applied=after)
    print(f"[fault] OFF {ftype}")


def clear(a):
    if a.type in ("cpu", "all"):
        clear_cpu(a)
    if a.type in ("net", "all"):
        clear_net(a)


def status(a):
    print(f"[fault] {a.unit} CPUQuotaPerSecUSec = {cpu_state(a.unit) or '(없음)'}")
    mem = run(["systemctl", "show", a.unit, "-p", "MemoryCurrent", "--value"], need_root=False)
    print(f"[fault] {a.unit} MemoryCurrent      = {mem.stdout.strip()}")
    print(f"[fault] {a.dev} qdisc              = {net_state(a.dev)}")


def main():
    a = parse_args()
    try:
        {"inject": inject, "clear": clear, "status": status}[a.action](a)
    except RuntimeError as e:
        sys.exit(f"[fault] 실패 — 기록하지 않았습니다.\n{e}")


if __name__ == "__main__":
    main()
