# -*- coding: utf-8 -*-
"""四、路由与领域插件契约——搬自原 test_invariants.py 第四段。

24 条 check：route 确定性、领域插件聚合、doc_acl.grants 解析、
术语核对口径（全角/英文碎片）、NL2SQL 五关校验、坏插件跳过。
"""
from app import config, domains, guards  # noqa: F401  llm/语义对称保留（原文件导入）
from app.service import acl, agent, nlsql


def run(check) -> None:
    # 16. route 确定性（不调模型——设计原则 1）
    check("route 闲聊", agent.route("你好") == "CHAT")
    check("route 追问无历史", agent.route("怎么办") == "CLARIFY")
    check("route 追问有历史", agent.route("怎么办", has_history=True) == "KNOWLEDGE_QA")
    check("route 名词查找", agent.route("高血压") == "KNOWLEDGE_QA")

    # 17. 领域插件聚合：医学维度对/注意词/单位参与（插件化后不回归硬编码）
    pairs = domains.dimension_pairs()
    check("领域维度对聚合", any(p[0].search("高血压用药") for p in pairs), str(len(pairs)))
    check("领域注意词聚合", bool(domains.attention_pattern().search("注意事项")))
    check("领域单位聚合", "mmHg" in domains.num_unit_extra())
    check("领域同义词组聚合", bool(domains.synonym_groups()))

    check("泛问注意事项不误杀相关资料",
          not agent._attention_gap("高血压应该注意什么", "高血压诊断标准和控制目标"))
    check("明确饮食维度缺资料拒绝",
          agent._attention_gap("高血压饮食应该注意什么", "高血压诊断标准") )
    check("明确饮食维度有资料放行",
          not agent._attention_gap("高血压饮食应该注意什么", "高血压饮食应低盐"))
    check("多维任一缺失拒绝",
          agent._attention_gap("高血压饮食和用药应该注意什么", "高血压饮食应低盐"))

    # 引号把前缀匹配全带崩，受限文档从未生效过）
    check("grants JSON 数组解析", acl._parse_grants('["employee:3","5"]') == {3, 5})
    check("grants 裸逗号串解析", acl._parse_grants("employee:3,employee:4") == {3, 4})
    check("grants 垃圾串不炸", acl._parse_grants("{not-json") == set())
    check("grants 空/None", acl._parse_grants("") == set() and acl._parse_grants(None) == set())

    # 19. 术语核对口径：全角/半角归一 + 英文碎片豁免（sweep r6 实况）
    check("全角罗马数字归一", guards.find_missing_terms("II 级", "Ⅱ级心衰") == [])
    check("英文文件名碎片豁免", guards.find_missing_terms(
        "AI-Agents-in-Depth-zh-CN.pdf", "[来源: AI-Agents-in-Depth-zh-CN.pdf]") == [])
    check("真缺失英文术语仍报", "Richard" in guards.find_missing_terms(
        "Richard Sutton 提出", "强化学习奠基人之一"))

    # 20. NL2SQL 合规五关校验器（安全扫描抓出过列白名单是死代码——锁死）：
    _nlsql_tables = [{"table": "product",
                      "columns": [{"name": "name"}, {"name": "price"}, {"name": "category"}]}]
    _v = lambda s, p=[]: nlsql._validate(s, p, _nlsql_tables)  # noqa: E731
    check("SQL 校验:正常查询放行", bool(_v("SELECT * FROM product")))
    check("SQL 校验:未注册列拒绝", _v("SELECT secret FROM product") is None)
    check("SQL 校验:写语句拒绝", _v("DELETE FROM product") is None)
    check("SQL 校验:分号多语句拒绝", _v("SELECT * FROM product; DROP TABLE product") is None)
    check("SQL 校验:UNION 越权拒绝", _v("SELECT * FROM product UNION SELECT * FROM sys_user") is None)
    check("SQL 校验:未注册表拒绝", _v("SELECT password_hash FROM sys_user") is None)
    check("SQL 校验:占位不匹配拒绝", _v("SELECT * FROM product WHERE name=%s") is None)
    check("SQL 校验:别名聚合放行", bool(_v("SELECT COUNT(*) AS c FROM product")))

    # 21. 坏插件跳过不崩（插件加载失败 → 空列表，主流程照跑）
    _orig_env = config._env
    try:
        config._env = lambda k, d=None: ("nonexistent_xyz" if k == "DOMAIN_PLUGINS"
                                         else _orig_env(k, d))
        check("坏插件跳过不崩", domains._load() == [])
    finally:
        config._env = _orig_env

    # 22. NL2SQL 明细结果完全重复行折叠契约（不重复展示同一真实行）
    duplicate_rows = [("张伟", "交通费", 356.5), ("张伟", "交通费", 356.5),
                      ("陈杰", "交通费", 100)]
    unique_rows = list(dict.fromkeys(duplicate_rows))
    check("NL2SQL 重复明细行折叠", len(unique_rows) == 2
          and unique_rows[0] == duplicate_rows[0])
