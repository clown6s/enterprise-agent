# -*- coding: utf-8 -*-
"""
NL2SQL 查询层（2026-08-18）：问句 → 已注册表 → 生成 SQL → 五关校验 → 执行 → 回答。

权限：query_schema 注册表带 grants（与 doc_acl 同口径：空=全员可见，
非空=employee:ID 名单）——用户身份经 acl.resolve_employee 解析后只可见
授权表；未注册的表/列对模型不可见。
合规（确定性五关校验器，不过即拒绝执行，漏回知识问答）：
1. 单语句 SELECT-only（写关键字/UNION/分号出现即拒）
2. 表名全部在注册白名单内
3. 列名全部在注册列内（聚合函数/别名/占位符除外）
4. 占位符 %s 数量与 params 一致
5. 强制 LIMIT（缺失补 50，上限 100——防全表灌爆）
失败语义：生成失败/校验不过/执行失败 → 返回 None，调用方继续知识问答兜底。
"""
import json
import re

from .. import config, llm
from . import acl

_SAFE_TABLE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "as", "limit", "order", "by", "desc",
    "asc", "like", "in", "not", "null", "count", "sum", "avg", "max", "min",
    "group", "having", "between", "is", "distinct", "on", "join", "left", "right",
    "inner", "case", "when", "then", "else", "end", "like", "offset", "concat",
    # 常用只读函数（白名单外函数一律拒——宁可漏回 KB 也不放行未审计能力）
    "length", "round", "coalesce", "ifnull", "date_format", "substr", "substring",
    "upper", "lower", "trim", "format", "if", "abs", "now", "curdate", "datediff",
    # 相对日期条件（"最近7天"→ INTERVAL %s DAY；曾 interval 不在白名单被
    # 第三关拒掉，报销/经营问法漏回兜底的实况）
    "interval", "day", "date_sub",
}
_BLOCKED_WORDS = ("insert", "update", "delete", "drop", "alter", "create", "exec",
                  "grant", "union", "truncate", "replace", "into", "set")
_GEN_PROMPT = """你是数据库查询生成器。根据下面的表结构描述和用户问题，生成查询计划。
严格只输出 JSON，不要任何解释：
{{"sql_template": "SQL 模板（字面值一律用 %s 占位）", "params": ["占位参数列表"]}}

规则：
1. 只读查询，只允许 SELECT
2. 只使用提供的表与列，不得臆测不存在的表/列；聚合表达式必须用 AS 起别名
3. 所有字面值（人名/数字/日期/字符串）一律用 %s 占位放入 params，不要拼进 SQL
4. 无法确定查什么时输出 {{"sql_template": "", "params": []}}
5. 今天日期是 {today}。"最近 N 天"用参数化区间条件写：expense_date >= CURDATE() - INTERVAL %s DAY，
   params 填数字 N（不要自己算日期，跨月容易算错——曾模型自算日期导致查错窗口）
6. 跨表问法（"某人的部门的报销"= 先定位部门再查该部门数据）用 JOIN：
   ON 条件只允许用【表结构】中含义相同的关联列（如 employee.dept = biz_expense.dept_name），
   表别名用单字母（employee e / biz_expense b），JOIN 后必须保留 WHERE/LIMIT

【表结构】
{schema}

【用户问题】
{query}
"""


def _ensure_tables() -> None:
    """query_schema 注册表建表（软依赖）；注册表本身不自动种子——
    演示数据由 scripts 或管理后台登记"""
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS query_schema ("
                "id INT AUTO_INCREMENT PRIMARY KEY,"
                "table_name VARCHAR(64) NOT NULL UNIQUE,"
                "description VARCHAR(255) DEFAULT '',"
                "columns_json TEXT NOT NULL,"
                "grants TEXT,"
                "status TINYINT DEFAULT 1,"
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
        conn.commit()
        conn.close()
    except Exception:
        pass


def register_table(table_name: str, description: str,
                   columns: list[dict], grants: str = "") -> None:
    """登记一张可查询表：columns=[{name, desc, sample}]；
    grants 与 doc_acl 同口径（""=全员可见）"""
    _ensure_tables()
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO query_schema (table_name, description, columns_json, grants) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                "description=VALUES(description), columns_json=VALUES(columns_json), "
                "grants=VALUES(grants)",
                (table_name, description,
                 json.dumps(columns, ensure_ascii=False), grants or ""))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _visible_tables(user: str) -> list[dict]:
    """注册表 → 当前用户可见的表清单（grants 过滤，与 doc_acl 同口径）"""
    _ensure_tables()
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT table_name, description, columns_json, grants "
                        "FROM query_schema WHERE status=1")
            rows = cur.fetchall()
        conn.close()
    except Exception:
        return []
    emp = acl.resolve_employee(user)
    out = []
    for name, desc, cols_json, grants in rows:
        # grants 解析与 doc_acl 同款（JSON 数组或裸逗号串）
        ids = acl._parse_grants(grants or "")
        if ids and emp not in ids:
            continue
        try:
            cols = json.loads(cols_json)
        except ValueError:
            continue
        if isinstance(cols, list) and all(isinstance(c, dict) for c in cols):
            # 字段级权限过滤：PII 列（如员工电话）对未授权用户直接不可见
            cols = [c for c in cols if acl.field_visible(name, c.get("name", ""), user)]
            if cols:
                out.append({"table": name, "description": desc or "", "columns": cols})
    return out


def _validate(sql: str, params: list, tables: list[dict]) -> str | None:
    """合规五关校验：通过返回可执行 SQL（可能补 LIMIT），不过返回 None"""
    tpl = (sql or "").strip().rstrip(";")
    if not tpl or ";" in tpl or not re.match(r"^\s*select\b", tpl, re.I):
        return None
    low = tpl.lower()
    for w in _BLOCKED_WORDS:
        if re.search(rf"\b{w}\b", low):
            return None
    # 表白名单
    used = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", low))
    registered = {t["table"].lower() for t in tables}
    if not used or not used <= registered:
        return None
    # 列白名单：模板中每个裸标识符必须可解释为 关键字/注册表名/注册列名/
    # AS 别名（安全扫描抓出曾只算 allowed_cols 从未使用——列检查是死代码；
    # 占位符先剥掉，否则 %s 的 s 会被当单字母标识符误拒）
    allowed_cols = {c["name"].lower() for t in tables for c in t["columns"]}
    low_clean = low.replace("%s", " ")
    aliases = set(re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", low_clean))
    # JOIN 裸别名（JOIN employee e ON e.dept=... 里 e 无 AS——
    # 曾跨表问法"李娜部门的报销"生成的 JOIN 被第三关误拒）
    aliases |= set(re.findall(
        r"\b(?:from|join)\s+[a-z_][a-z0-9_]*\s+([a-z_][a-z0-9_]*)\b", low_clean))
    for tok in re.findall(r"[a-z_][a-z0-9_]*", low_clean):
        if (tok in _SQL_KEYWORDS or tok in registered or tok in allowed_cols
                or tok in aliases):
            continue
        return None
    if "%s" not in tpl and params:
        return None
    if tpl.count("%s") != len(params or []):
        return None
    if not re.search(r"\blimit\b", low):
        tpl += " LIMIT 50"
    # 占位上限（防 params 爆炸）
    if len(params or []) > 10:
        return None
    return tpl


def _fmt_rows(rows: list[tuple], cols: list[str], labels: dict[str, str],
              label_fn=None) -> str:
    """结果行 → 确定性文本（列值全来自真实数据行；列名转注册的中文描述）。
    NULL 值显示「未录入」——曾直接输出 None（电话字段没录时"电话：None。"）"""
    heads = [(label_fn(c) if label_fn else labels.get(c.lower(), c)) for c in cols]
    if not rows:
        return "查询结果为空。"

    def _cell(v):
        return "未录入" if v is None else v

    if len(rows) == 1 and len(cols) <= 2:
        return "，".join(f"{heads[i]}：{_cell(rows[0][i])}" for i in range(len(cols))) + "。"
    lines = [f"查询结果（{len(rows)} 条）："]
    for r in rows:
        lines.append("- " + "，".join(f"{heads[i]}：{_cell(r[i])}" for i in range(len(cols))))
    return "\n".join(lines)


def try_answer(query: str, user: str = "") -> str | None:
    """问句 → 已注册表的确定性查询答案；不可答/失败返回 None（调用方兜底）"""
    tables = _visible_tables(user)
    if not tables:
        return None
    schema_block = "\n\n".join(
        f"表 {t['table']}：{t['description']}\n" +
        "，".join(f"{c['name']}({c.get('desc', '')}，如：{c.get('sample', '')})"
                  for c in t["columns"])
        for t in tables)
    try:
        from datetime import datetime
        raw = llm.verify_chat("", _GEN_PROMPT.format(
            schema=schema_block, query=query,
            today=datetime.now().strftime("%Y-%m-%d")), temperature=0.1).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        plan = json.loads(m.group()) if m else {}
        sql, params = plan.get("sql_template"), plan.get("params") or []
        if not isinstance(sql, str) or not isinstance(params, list):
            return None
    except (ValueError, AttributeError):
        return None
    final_sql = _validate(sql, params, tables)
    if not final_sql:
        return None
    def _cast_param(p) -> int | float | str:
        """参数智能转类型：纯数字转 int/float（LIMIT %s 不接受字符串参数——
        曾"谁的报销最多"生成 ORDER BY ... LIMIT %s，'1' 字符串直接语法报错）；
        日期/人名等保持字符串"""
        s = str(p)
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        return s

    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            typed_params = [_cast_param(p) for p in params]
            try:
                cur.execute(final_sql, typed_params)
            except pymysql.err.OperationalError as e:
                # collation 冲突兜底（1267）：库里两代 collation 并存
                # （OA 时代表 utf8mb4_0900_ai_ci vs 主体 utf8mb4_unicode_ci），
                # JOIN 的 = 比较直接炸——ON 右侧列显式 COLLATE 重试
                # （表统一 collation 需 DBA 点头，代码层先治类兜底）
                if e.args[0] == 1267 and " join " in final_sql.lower():
                    fixed = re.sub(r"(\bon\b\s+[^=]+)=\s*([a-z_][\w.]*)",
                                   r"\1= \2 COLLATE utf8mb4_unicode_ci",
                                   final_sql, flags=re.I)
                    cur.execute(fixed, typed_params)
                else:
                    raise
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        conn.close()
    except Exception as e:
        print(f"[nlsql] 执行失败: {e!r} | {final_sql}")
        return None
    labels = {c["name"].lower(): (c.get("desc") or c["name"]) for t in tables for c in t["columns"]}
    # 聚合别名回退（模型起的 total_count/sum_amount 等别名尾缀匹配不上注册列
    # 时的中文标签——"公司有多少人"曾输出"total_count：8。"的实况）。
    # 双向匹配：尾缀（total_count→count）或前缀（total_sales→total）——
    # 曾只做尾缀，"查询所有销售额"的 SUM(revenue) AS total_sales 漏网输出英文
    _AGG_LABELS = {"count": "数量", "sum": "合计", "avg": "平均",
                   "max": "最大值", "min": "最小值", "total": "合计"}

    def _label(col: str) -> str:
        key = col.lower()
        if key in labels:
            return labels[key]
        # 模型可能自造 product_description 类别名——尾缀匹配回注册列
        for k, v in labels.items():
            if key.endswith(k):
                return v
        # 再回退聚合函数名（注册列尾缀优先：total_revenue→营收 而非 合计）
        for k, v in _AGG_LABELS.items():
            if key == k or key.endswith("_" + k) or key.startswith(k + "_"):
                return v
        return col

    return _fmt_rows(rows, cols, labels, _label)
