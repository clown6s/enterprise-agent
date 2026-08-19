# -*- coding: utf-8 -*-
"""数据库服务器恢复探测：每 30s 探 10.211.55.5:3306，恢复即退出。"""
import socket
import time

for i in range(240):  # 最多 2 小时
    try:
        s = socket.create_connection(("10.211.55.5", 3306), timeout=5)
        s.close()
        print(f"MySQL 已恢复（第 {i + 1} 次探测）", flush=True)
        break
    except OSError:
        pass
    if i % 4 == 0:
        print(f"10.211.55.5 仍不可达（已等 {i * 30}s）", flush=True)
    time.sleep(30)
else:
    print("2 小时未恢复，放弃等待", flush=True)
