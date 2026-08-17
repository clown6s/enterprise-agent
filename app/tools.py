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


# 现有确定性工具全量登记（描述供未来发现机制/后台展示用）
TOOLS: list[Tool] = [
    Tool("employee_directory", "员工目录", "公司人数统计与员工信息查询"),
    Tool("oa_expenses", "报销数据", "按人/按天查询报销明细与汇总"),
    Tool("oa_metrics", "经营数据", "营收/成本/利润经营数据查询"),
    Tool("leave_action", "请假申请", "请假 SOP（查名册→提取→校验→确认→提交 OA）"),
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
