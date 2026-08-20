# -*- coding: utf-8 -*-
"""
真实入口路径测试（dsh 三-12）：验证 FastAPI 应用装配路径与离线可运行性。

模拟 `uvicorn.run("app.main:app")` 的导入路径，验证：
- 应用能成功装配（无 import 崩溃）
- 路由表包含预期端点
- /healthz 能正常响应
- 飞书 lifespan 不崩溃（全局跳过）
- config 模块级常量有合理的默认值

全离线运行，不依赖 .env 和任何外部服务。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 全局跳过飞书 lifespan（避免 WS 挂起/连接失败噪声）
from app.channels import feishu as feishu_mod
_ORIG_ENABLED = feishu_mod.channel.enabled
feishu_mod.channel.enabled = lambda: False

from app.main import app  # noqa: E402
from app import config  # noqa: E402
from app.logging_config import setup as setup_logging  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("缺少 fastapi 测试依赖（httpx）；pip install httpx 后重跑")
    sys.exit(2)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


# 1. 应用装配——无 import 崩溃
check("app 装配成功", isinstance(app, type(app)),
      f"app 类型={type(app).__name__}")

# 2. 路由表——包含预期端点
expected_paths = {"/healthz", "/api/rag/agent/chat",
                  "/api/rag/agent/chat/stream", "/api/rag/admin/approvals",
                  "/api/rag/admin/sops", "/api/rag/admin/auth/login"}
# FastAPI 新版将 include_router 保留为懒展开的 _IncludedRouter；
# OpenAPI 是真实装配后的路由投影，比直接遍历 app.routes 更稳定。
actual_paths = set(app.openapi().get("paths", {}))
missing = expected_paths - actual_paths
check("路由表含预期路径", not missing, f"缺: {sorted(missing)}")

# 3. /healthz 正常响应
c = TestClient(app)
r = c.get("/healthz")
check("healthz 200", r.status_code == 200)
check("healthz UP", r.json().get("status") == "UP")
check("healthz 含 time", "time" in r.json())

# 4. config 模块级常量——有合理默认值（非空）
check("MILVUS_HOST 有默认值", bool(config.MILVUS_HOST),
      f"host={config.MILVUS_HOST!r}")
check("MYSQL_HOST 有默认值", bool(config.MYSQL_HOST),
      f"host={config.MYSQL_HOST!r}")
check("MAIN_MODEL 有默认值", bool(config.MAIN_MODEL),
      f"model={config.MAIN_MODEL!r}")
check("EMBED_DIM 合理", config.EMBED_DIM == 4096,
      f"dim={config.EMBED_DIM}")
check("PORT 合理", config.PORT == 8080)

# 5. 日志配置幂等
setup_logging()
setup_logging()  # 第二次调用不崩
check("logging 幂等", True)

# 6. 模型注册表完整性
for purpose, entry in config.MODEL_REGISTRY.items():
    has_model = bool(entry.get("model"))
    has_base = bool(entry.get("base"))
    has_key = "key" in entry  # 允许空 key（CI 无 .env）
    check(f"注册表 {purpose} 含 model", has_model, f"entry={entry}")
    check(f"注册表 {purpose} 含 base", has_base, f"entry={entry}")

# 7. 飞书 channel 已禁用（无副作用）
check("飞书 channel 已禁用", not feishu_mod.channel.enabled(),
      "enabled() 应为 False")

# 恢复
feishu_mod.channel.enabled = _ORIG_ENABLED

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)