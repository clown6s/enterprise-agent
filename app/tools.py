# -*- coding: utf-8 -*-
"""
工具注册表（动态工具注册与发现的地基，第 1-2 层）：

- 统一 Tool 定义（code/name/description）+ 全量登记现有确定性工具
- skill 表接线：status 开关经 TTL 缓存（30s）即时生效——管理后台禁用某技能，
  路由/执行层 30s 内停止使用，不改代码不重启
- skill 表无任何记录时全部工具默认启用（登记了才有开关语义）

第 3-4 层（发现机制：模型选工具 / MCP 客户端）路线图，未做。
"""
import threading
import time
from dataclasses import dataclass

TTL_SECONDS = 30

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "enabled": None}


@dataclass
class Tool:
    code: str
    name: str
    description: str


# 现有确定性工具全量登记（描述供未来发现机制/后台展示用）。
# 2026-08-19 去 OA 化：员工目录/报销/经营三条退役（并入 NL2SQL 数据面）；
# 审批类 SOP 按 code 逐一登记——skill 表空时只有登记过的默认启用，
# 曾新 SOP 未登记 is_enabled 全 False 被整条关死的实况。
TOOLS: list[Tool] = [
    Tool("leave_action", "请假申请", "请假 SOP（查名册→提取→校验→确认→写库）"),
    Tool("expense_action", "报销申请", "报销 SOP（提取→校验→确认→写库→审批流）"),
    Tool("travel_action", "出差申请", "出差 SOP（目的地/日期→校验→确认→写库）"),
    Tool("purchase_action", "采购申请", "采购 SOP（物品/数量/金额→校验→确认→写库）"),
    Tool("contract_action", "合同用印申请", "合同用印 SOP（合同/对方/金额→校验→确认→写库）"),
    Tool("doc_wizard", "文档生成", "一句话生成/多轮修改云文档"),
]


def _load_enabled_from_db() -> set[str]:
    """skill 表 → 启用工具集；表空=全部启用；库/表不可用=全部启用（软依赖）"""
    try:
        import pymysql
        from . import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                               password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                               charset="utf8mb4", connect_timeout=3)
        rows = []
        with conn.cursor() as cur:
            cur.execute("SELECT tool_names, status FROM skill")
            rows = cur.fetchall()
        conn.close()
        if not rows:
            return {t.code for t in TOOLS}
        enabled = set()
        for tool_names, status in rows:
            if not status:
                continue
            for code in (tool_names or "").replace("；", ",").split(","):
                code = code.strip()
                if code:
                    enabled.add(code)
        return enabled
    except Exception as e:
        print(f"[tools] skill 表读取失败（全部启用）: {e!r}")
        return {t.code for t in TOOLS}


def is_enabled(code: str) -> bool:
    """工具开关（TTL 缓存：管理后台变更 30s 内生效）"""
    with _lock:
        now = time.time()
        if _cache["enabled"] is None or now - _cache["ts"] > TTL_SECONDS:
            _cache["enabled"] = _load_enabled_from_db()
            _cache["ts"] = now
        return code in _cache["enabled"]
