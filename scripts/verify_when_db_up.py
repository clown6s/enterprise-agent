# -*- coding: utf-8 -*-
"""服务器恢复后自动验证：探测 10.211.55.5:3306 恢复 → 跑变体矩阵+回归+不变量
→ 写 data/verify_report.txt。无限探测（30s 间隔），恢复即跑。"""
import socket
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")

REPORT = "data/verify_report.txt"


def probe() -> bool:
    try:
        s = socket.create_connection(("10.211.55.5", 3306), timeout=5)
        s.close()
        return True
    except OSError:
        return False


def run(cmd: list[str], tag: str) -> str:
    print(f"[{datetime.now():%H:%M:%S}] 跑 {tag} ...", flush=True)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=3600)
        return out.stdout[-3000:]
    except subprocess.TimeoutExpired:
        return f"{tag} 超时"


def main():
    waited = 0
    while not probe():
        waited += 30
        if waited % 600 == 0:
            print(f"[{datetime.now():%H:%M:%S}] 等待服务器恢复（已 {waited // 60} 分钟）", flush=True)
        time.sleep(30)
    print(f"[{datetime.now():%H:%M:%S}] 服务器已恢复，开始自动验证", flush=True)
    parts = [f"自动验证报告（{datetime.now():%Y-%m-%d %H:%M}）\n"]
    parts.append("=== 变体矩阵（模拟真人问法，28 项×10 次） ===\n"
                 + run([sys.executable, "scripts/test_matrix_human.py"], "变体矩阵"))
    parts.append("\n=== 回归 ===\n"
                 + run([sys.executable, "scripts/run_eval.py", "regression"], "回归"))
    parts.append("\n=== 不变量 ===\n"
                 + run([sys.executable, "scripts/test_invariants.py"], "不变量"))
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"验证完成，报告在 {REPORT}", flush=True)


if __name__ == "__main__":
    main()
