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
4. 无法确定查什么时输出 {{"sql_template": "", "params": []}}。以下问法一律输出空模板：
   提交/申请类写操作（"帮我报销…""提交请假…"）、纯知识问法（"是什么/注意/建议"）、
   与表结构无关的闲聊——你只负责查表，不是查询就不生成 SQL
5. 今天日期是 {today}。"最近 N 天"用参数化区间条件写：expense_date >= CURDATE() - INTERVAL %s DAY，
   params 填数字 N（不要自己算日期，跨月容易算错——曾模型自算日期导致查错窗口）
6. 跨表问法：当问句同时涉及多张表的字段时，查【关联关系】获取表间连接条件
   - 观察问句涉及哪几张表 → 在【关联关系】中找对应的表对 → 直接用其 ON 条件拼 JOIN
   - 不要臆测 JOIN 条件；关联关系里没有的不要硬连
7. 表别名用单字母（如 a、b、c），JOIN 后 WHERE/LIMIT 仍需保留

【表结构】
{schema}

【用户问题】
{query}
"""

# 结果→回答包装层（治类，2026-08-19 用户定调：数据≠答案——
# 曾比较问法甩两行原始数据让用户自己算，答案层加特判加到死也加不完。
# 一个机制收编所有形态：问句+真实数据+确定性衍生信息 → LLM 只做措辞，
# 数字核对兜底防编造，失败回退确定性原文）
_ANSWER_PROMPT = """你是数据问答助手。根据查询结果回答用户问题。
规则：
1. 只能使用【查询结果】与【衍生信息】中的数据，不得编造任何数据外的事实、数字、人名、日期
2. 日期与数字的写法必须与查询结果完全一致（如 2026-08-19 不要写成 2026年8月19日；
   2881.30 不要写成 2881.3 或四舍五入）
3. NULL 表示"未录入"；聚合结果（合计/总数）为 NULL 表示该条件下没有记录，回答"无记录/没有记录"
4. 直接回答用户问的问题（问比较就答谁多、差多少；问单值就直接给值）。
   单值数字回答必须带含义与单位（如"销售额合计 1974100.00 元"，不要只输出裸数字）。
   简明扼要，不要复述整个表格，不要加"根据查询结果"之类的套话

【用户问题】
{query}

【查询结果】
{data_text}
{derived}

请直接输出回答：
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
    """注册表 → 当前用户可见的表清单（grants 过滤，与 doc_acl 同口径）。
    返回的 columns 含非 PII 列；_raw_columns() 额外提供全量列用于 JOIN 校验。"""
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
        ids = acl._parse_grants(grants or "")
        if ids and emp not in ids:
            continue
        try:
            cols = json.loads(cols_json)
        except ValueError:
            continue
        if isinstance(cols, list) and all(isinstance(c, dict) for c in cols):
            cols = [c for c in cols if acl.field_visible(name, c.get("name", ""), user)]
            if cols:
                out.append({"table": name, "description": desc or "", "columns": cols})
    return out


# 表间关联关系（跨表 JOIN 用——模型需要知道怎么连，不只是能连）
# 格式：(源表, 目标表, ON 条件, 语义描述)
# 只用 query_schema 已注册列：employee.dept（部门文本）、biz_expense.dept_name（部门文本）、
# biz_expense.emp_name = employee.name（人名匹配）
_JOINS = [
    ("biz_expense", "employee",
     "biz_expense.dept_name = employee.dept",
     "报销→员工（按部门名匹配）：查某人所在部门的所有报销记录"),
    ("employee", "biz_expense",
     "employee.dept = biz_expense.dept_name",
     "员工→报销（按部门名匹配）：查某部门所有员工的报销汇总"),
]

# 注册表原始列（含 PII）快照——_validate 校验 JOIN ON 条件引用列时需要
_TABLE_COLS_RAW: dict[str, list[str]] = {}


def _raw_columns(table_name: str) -> list[dict]:
    """取一张表的全部注册列（含 PII），不做过滤——校验层用，不给模型看"""
    _ensure_tables()
    if table_name in _TABLE_COLS_RAW:
        return [{"name": n} for n in _TABLE_COLS_RAW[table_name]]
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT columns_json FROM query_schema "
                        "WHERE table_name=%s AND status=1", (table_name,))
            row = cur.fetchone()
        conn.close()
        if row:
            cols = json.loads(row[0])
            _TABLE_COLS_RAW[table_name] = [c["name"] for c in cols]
            return cols
    except Exception:
        pass
    return []


def _build_schema_with_joins(tables: list[dict]) -> str:
    """构建含关联关系的表结构描述：每张表的列 + 表间 JOIN 条件。
    模型看到关联关系后才能生成正确的跨表 JOIN。"""
    table_set = {t["table"] for t in tables}
    # 表结构块
    parts = []
    for t in tables:
        cols_text = "，".join(
            f"{c['name']}({c.get('desc', '')}，如：{c.get('sample', '')})"
            for c in t["columns"])
        parts.append(f"表 {t['table']}：{t['description']}\n{cols_text}")
    # 关联关系块（只注入两端表都已注册的 JOIN）
    join_lines = []
    for src, dst, on, desc in _JOINS:
        if src in table_set and dst in table_set:
            join_lines.append(f"- {desc}：{on}")
    if join_lines:
        parts.append("【关联关系】\n" + "\n".join(join_lines))
    return "\n\n".join(parts)


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
    # 占位符先剥掉，否则 %s 的 s 会被当单字母标识符误拒）。
    # 允许列取全量注册列（含 PII）：JOIN ON 条件可能引用被过滤的列
    # （如 employee.mobile），但模型在 SELECT 中不会引用——
    # 字段级过滤在 schema 层（模型不可见），校验层放行以支持 JOIN
    allowed_cols = {c["name"].lower()
                    for t in tables
                    for c in _raw_columns(t["table"])}
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
    schema_block = _build_schema_with_joins(tables)
    from datetime import datetime
    sql, params = "", []
    # 生成重试 3 次（曾"最近7天"窗口问法偶发生成失败/空模板漏回兜底，9/10 稳定性；
    # verify 模型 ReadTimeout 抖动是"李娜是哪年来的"类变体偶发失败根因，
    # 夜间 API 慢时段连续超时也偶发——重试吸收抖动）
    for _ in range(4):
        try:
            raw = llm.verify_chat("", _GEN_PROMPT.format(
                schema=schema_block, query=query,
                today=datetime.now().strftime("%Y-%m-%d")), temperature=0.1).strip()
            m = re.search(r"\{.*\}", raw, re.S)
            plan = json.loads(m.group()) if m else {}
            sql, params = plan.get("sql_template"), plan.get("params") or []
        except (ValueError, AttributeError):
            sql, params = "", []
        if isinstance(sql, str) and sql.strip() and isinstance(params, list):
            if _validate(sql, params, tables):
                break
        sql, params = "", []
    if not sql:
        return None
    final_sql = _validate(sql, params, tables)
    if not final_sql:
        return None
    # 运行时不变量：校验先于执行（final_sql 必须是 _validate 产物——
    # 曾列白名单死代码放行未校验 SQL 的实况，生产自证防回潮）
    from .. import invariants as _invariants
    ok, detail = _invariants.check_validated_sql(final_sql, sql, tables)
    if not ok:
        _invariants.fail("nlsql_validated", detail)
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

    text = _fmt_rows(rows, cols, labels, _label)
    # 空结果+问句带金额数字 = 提交语义（"王强报销96元交通费"模型偶发生成查询
    # SQL 查无记录）→ 漏回让 SOP 接（写操作不走数据面；空结果语义修正，
    # 不是路由预判——"李娜报销了多少"查空无金额仍答"查询结果为空"）
    if not rows and re.search(r"\d+(\.\d+)?\s*(元|块钱|块)", query or ""):
        return None
    # 多行列表类结果不包装（曾包装层压缩明细列表时丢掉部门/姓名等标识列——
    # 列表本身可读性好，包装只用于单值/两行比较类）
    if len(rows) > 2:
        return text
    return _answer_with_llm(query, text, rows)


# ---------------- 结果→回答包装（治类：数据≠答案） ----------------

def _derive_compare(rows: list[tuple]) -> str:
    """确定性衍生信息：结果恰好 2 行且含数值列时给出差值（算术不让模型算——
    曾比较问法模型需自己算"多多少"，有算错风险；这里算好喂给包装层）"""
    if len(rows or []) != 2:
        return ""
    try:
        from decimal import Decimal, InvalidOperation
        idx = next((i for i, v in enumerate(rows[0])
                    if isinstance(v, (int, float, Decimal))
                    and isinstance(rows[1][i], (int, float, Decimal))), None)
        if idx is None:
            return ""
        a, b = Decimal(str(rows[0][idx])), Decimal(str(rows[1][idx]))
        if a == b:
            return "\n【衍生信息】两者数值相同"
        high_name = str(rows[0][0]) if a > b else str(rows[1][0])
        diff = _fmt_num(abs(a - b))
        return f"\n【衍生信息】{high_name} 的数值更高，差值为 {diff}"
    except (InvalidOperation, TypeError, ValueError):
        return ""


def _fmt_num(v) -> str:
    """数值展示：去多余小数尾零（754.70 不显示成 754.7000）"""
    s = str(v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _numbers_in(text: str) -> set:
    """提取文本中所有数字（含小数），用于核对"""
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _answer_with_llm(query: str, data_text: str, rows: list[tuple]) -> str:
    """数据 → 回答（问句+真实数据+衍生信息 → LLM 只做措辞）。
    数字核对：回答里每个数字必须来自查询结果/衍生信息——模型改写数字
    （2881.30→2881.3 或算错差值）一律回退确定性原文；LLM 失败同样回退。
    确定性原文永远是最差可交付底线，LLM 包装只是让它"像在回答问题"。"""
    derived = _derive_compare(rows)
    source = data_text + derived
    try:
        ans = (llm.verify_chat("", _ANSWER_PROMPT.format(
            query=query, data_text=data_text, derived=derived),
            temperature=0.1).strip() or "")
    except Exception:
        return data_text
    if not ans or ans == data_text:
        return data_text
    # 允许数字集合 = 查询结果/衍生信息 + 用户问句中的数字（"8月营收"答"8月"
    # 的 8 来自问句不算编造——曾把问句数字当越界误杀回退原文）
    if not _numbers_in(ans) <= (_numbers_in(source) | _numbers_in(query)):
        print(f"[nlsql] 包装层数字越界，回退原文: {ans[:80]!r}")
        return data_text
    return ans
