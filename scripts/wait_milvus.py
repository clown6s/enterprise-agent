# -*- coding: utf-8 -*-
"""Milvus 恢复探测：每 30s 探一次 19530，恢复即退出（供测试前等待用）。"""
import time
import httpx

for i in range(120):  # 最多 60 分钟
    try:
        r = httpx.get("http://10.211.55.5:19530/healthz", timeout=5)
        if r.status_code == 200:
            print(f"Milvus 已恢复（第 {i + 1} 次探测）")
            break
    except httpx.HTTPError:
        pass
    if i % 4 == 0:
        print(f"Milvus 仍不可达（已等 {i * 30}s）")
    time.sleep(30)
else:
    print("60 分钟未恢复，放弃等待")
