# -*- coding: utf-8 -*-
"""
运行时不变量服务（2026-08-19 借鉴 dsh invariants：检查挂在真实事件流上，
生产持续生效——不变量不是测试期跑一次的断言，是运行时自证）。

设计：
- register(name, check) 注册检查；check 返回 (ok, detail)
- run_all() 在关键调用点执行全部检查；检查必须轻量（O(1)/小常数）
- 失败写 alert_event 告警表（规则引擎输出，后台告警中心可见）+ stderr
- 检查异常不炸主链路（软依赖——不变量失败是信号不是故障源）

与 test_invariants.py（离线契约锁）分工：那个锁「跨模块契约在代码修改后仍成立」；
这个锁「生产运行时每笔请求/事件满足契约」。
"""
import re
import time
from datetime import datetime
import logging
log = logging.getLogger(__name__)

_REGISTRY: dict[str, callable] = {}
# 失败去重窗口：同名检查 5 分钟内只告警一次（防告警风暴）
_FAILED_AT: dict[str, float] = {}


def register(name: str, check: callable) -> None:
    """注册一条运行时检查：check() -> (ok: bool, detail: str)"""
    _REGISTRY[name] = check


def fail(name: str, detail: str) -> None:
    """检查失败的上报入口（告警去重 5 分钟 + 写 alert_event + stderr）。
    挂载点直接调 check 后用本函数上报；run_all 也走这里。"""
    now = time.time()
    if now - _FAILED_AT.get(name, 0.0) <= 300:
        return
    _FAILED_AT[name] = now
    log.warning(f"{name} 失败: {detail}")
    _alert(name, detail)


def run_all(context: str = "") -> int:
    """执行全部检查，返回失败条数。"""
    failures = 0
    for name, check in _REGISTRY.items():
        try:
            ok, detail = check()
            if ok:
                continue
        except Exception as e:
            detail = f"检查异常: {e!r}"
        failures += 1
        fail(name, detail)
    return failures


def _alert(name: str, detail: str) -> None:
    """失败写 alert_event（rule_code=f'invariant:{name}'，一规则一行重触发原地更新）"""
    try:
        from . import config
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_event (rule_code, level, title, detail, status, "
                "first_at, last_at, count) VALUES (%s,'CRITICAL',%s,%s,'OPEN',NOW(),NOW(),1) "
                "ON DUPLICATE KEY UPDATE last_at=NOW(), count=count+1",
                (f"invariant:{name}", f"运行时不变量失败：{name}", detail[:500]))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------- 内置检查 ----------------

_ROUTES = {"GUARD", "KNOWLEDGE_QA", "CHAT", "CLARIFY", "SOP", "APPROVAL",
           "DOC_WIZARD", "ERROR", "RETRIEVAL"}


def check_answer_contract(result: dict = None) -> tuple[bool, str]:
    """answer() 出口契约：route 合法 + answer 是非空 str。
    result=None 时跳过（run_all 无参批量调用与挂载点直调兼容）"""
    if result is None:
        return True, "（无 result，跳过）"
    if not isinstance(result, dict):
        return False, f"answer() 返回非 dict: {type(result)}"
    route = result.get("route", "")
    if route not in _ROUTES:
        return False, f"非法 route: {route!r}"
    ans = result.get("answer")
    if not isinstance(ans, str) or not ans.strip():
        return False, f"route={route} 的 answer 为空/非 str"
    return True, ""


def _load_audit_tail() -> dict:
    """execution.jsonl 尾行（SOP 审计成对性检查用）；文件不可用返回 None"""
    try:
        import os
        import json
        from . import config
        path = os.path.join(config.LOG_DIR, "execution.jsonl")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def check_audit_fields() -> tuple[bool, str]:
    """SOP 审计事件字段完整性：sop/step/ts/output 必填（成对性/可回放的前提）"""
    entry = _load_audit_tail()
    if entry is None:
        return True, "（无审计文件，跳过）"
    for field in ("sop", "step", "ts", "output"):
        if not entry.get(field):
            return False, f"审计事件缺字段 {field}: {entry}"
    return True, ""


def check_validated_sql(final_sql: str, raw_sql: str, tables: list[dict]) -> tuple[bool, str]:
    """NL2SQL 校验先于执行：final_sql 必须是 _validate 产物（曾列白名单
    死代码放行未校验 SQL 的实况——此检查防校验链路被绕过/短路）"""
    if not final_sql or not isinstance(final_sql, str):
        return False, f"final_sql 为空/非 str: {final_sql!r}"
    low = final_sql.lower()
    if not low.strip().startswith("select"):
        return False, f"final_sql 非 SELECT: {final_sql[:80]!r}"
    # 校验器强制 LIMIT——缺失说明没走校验
    if "limit" not in low:
        return False, f"final_sql 缺 LIMIT（未过校验器）: {final_sql[:80]!r}"
    registered = {t["table"].lower() for t in tables}
    used = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", low))
    if not used or not used <= registered:
        return False, f"final_sql 表不在注册白名单: used={used} registered={registered}"
    return True, ""


register("answer_contract", check_answer_contract)
register("audit_fields", check_audit_fields)
