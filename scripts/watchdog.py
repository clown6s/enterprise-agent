# -*- coding: utf-8 -*-
"""
服务看门狗：探活 enterprise-agent(8080, python) 与 oa-approval(7000, java)，
连续 3 次失败 → 杀端口残留 → 自动拉起 → 飞书 webhook 告警。
（Java 版 watchdog 的移植：8080 的拉起方式从 java -jar 换成 python run.py）

启动：python scripts/watchdog.py（建议 Start-Process 脱离终端）
日志：logs/watchdog.log
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

CHECK_INTERVAL = 30
FAIL_THRESHOLD = 3          # 人工重启停机约 45-60s，3 次=90s 窗口不误触发
COOLDOWN = 90
HTTP_TIMEOUT = 5

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE, ".env")
LOG_FILE = os.path.join(BASE, "logs", "watchdog.log")

SERVICES = [
    {"name": "enterprise-agent", "port": 8080, "workdir": BASE,
     "start": ["python", "run.py"],
     "log": r"D:\dsh-data\temp\py-agent.log", "err": r"D:\dsh-data\temp\py-agent-err.log"},
    {"name": "oa-approval", "port": 7000,
     "workdir": r"D:\project\oa-approval",
     "start": ["java", "-jar", r"D:\project\oa-approval\target\oa-approval-1.0.0.jar"],
     "log": r"D:\dsh-data\temp\oa-service.log", "err": r"D:\dsh-data\temp\oa-service-err.log"},
]

state = {s["name"]: {"fails": 0, "cooldown_until": 0.0} for s in SERVICES}


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [watchdog] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_env(key):
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def check_health(port):
    try:
        req = urllib.request.Request(f"http://localhost:{port}/healthz")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "ignore"))
            return resp.status == 200 and body.get("status") == "UP"
    except Exception:
        return False


def kill_port(port):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
        pids = {line.split()[-1] for line in out.splitlines()
                if f":{port} " in line and "LISTENING" in line and line.split()[-1].isdigit()}
        for pid in pids:
            subprocess.run(["taskkill", "//PID", pid, "//F"],
                           capture_output=True, text=True, timeout=20)
        return list(pids)
    except Exception as e:
        log(f"杀端口 {port} 异常: {e}")
        return []


def start_service(svc):
    # 逐个参数加单引号后逗号连接（"-jar","<jar路径>" 各为独立参数，
    # 不能拼成一个带引号的整体——java 会把整串当类名）
    cmd = svc["start"]
    args = ",".join("'" + a + "'" for a in cmd[1:])
    ps = ("Start-Process -FilePath '" + cmd[0] + "'"
          + (" -ArgumentList " + args if args else "")
          + " -WorkingDirectory '" + svc["workdir"] + "' -WindowStyle Hidden"
          + " -RedirectStandardOutput '" + svc["log"]
          + "' -RedirectStandardError '" + svc["err"] + "'")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, text=True, timeout=60)


def alert_feishu(title, text):
    webhook = read_env("FEISHU_ALERT_WEBHOOK_URL")
    if not webhook:
        return
    payload = json.dumps({"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]}}).encode("utf-8")
    try:
        req = urllib.request.Request(webhook, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def handle(svc):
    st = state[svc["name"]]
    now = time.time()
    if now < st["cooldown_until"]:
        return
    if check_health(svc["port"]):
        st["fails"] = 0
        return
    st["fails"] += 1
    log(f"{svc['name']}:{svc['port']} 探活失败（第 {st['fails']} 次）")
    if st["fails"] < FAIL_THRESHOLD:
        return
    log(f"*** {svc['name']} 连续 {FAIL_THRESHOLD} 次探活失败，执行拉起 ***")
    killed = kill_port(svc["port"])
    if killed:
        log(f"已杀残留进程 PID={killed}")
    start_service(svc)
    st["fails"] = 0
    st["cooldown_until"] = now + COOLDOWN
    alert_feishu(f"⚠️ 服务拉起：{svc['name']}",
                 f"{svc['name']}（端口 {svc['port']}）连续 {FAIL_THRESHOLD} 次探活失败，"
                 f"看门狗已自动拉起（残留进程 {killed or '无'}）。\n"
                 f"时间：{datetime.now().strftime('%m-%d %H:%M:%S')}")


def main():
    log(f"看门狗启动，探活 {[s['name'] for s in SERVICES]}，"
        f"间隔 {CHECK_INTERVAL}s / 阈值 {FAIL_THRESHOLD} 次 / 冷却 {COOLDOWN}s")
    while True:
        for svc in SERVICES:
            try:
                handle(svc)
            except Exception as e:
                log(f"检查 {svc['name']} 异常: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
