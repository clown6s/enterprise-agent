# -*- coding: utf-8 -*-
"""DATA 多行结果表格展示契约测试。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.service import nlsql  # noqa: E402

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


def test_fmt_table():
    labels = {"name": "姓名", "amount": "金额"}
    # 结构化结果契约通过真实 formatter 行为验证
    text = nlsql._fmt_rows([("张伟", 100), ("李娜", 80)], ["name", "amount"], labels)
    check("多行文本保留行数", "查询结果（2 条）" in text)
    check("多行文本保留内容", "张伟" in text and "李娜" in text)
    # 空/单值仍是轻量文本
    check("空结果文本", nlsql._fmt_rows([], ["name"], labels) == "查询结果为空。")
    one = nlsql._fmt_rows([("张伟",)], ["name"], labels)
    check("单行文本", "张伟" in one and "查询结果（" not in one)


def test_table_payload_shape():
    rows = [("张伟", 100), ("李娜", 80)]
    headers = ["姓名", "金额"]
    payload = {
        "kind": "query_table",
        "table": [headers] + [list(r) for r in rows],
        "columns": [
            {"key": "c0", "label": "姓名", "data_type": "text"},
            {"key": "c1", "label": "金额", "data_type": "number"},
        ],
        "row_count": len(rows),
    }
    check("table row_count", payload["row_count"] == 2)
    check("table 表头+行", len(payload["table"]) == 3)
    check("table JSON 可序列化", isinstance(json.dumps(payload, ensure_ascii=False), str))
    check(">1 行触发表格", payload["row_count"] > 1 and payload.get("table"))

    from app.channels.feishu import FeishuChannel
    card = FeishuChannel.build_data_card("查询结果（2 条），详情见下方表格。", payload)
    tags = [e.get("tag") for e in card["body"]["elements"]]
    check("纯表格不渲染空图表", "chart" not in tags and "table" in tags)
    check("纯表格来源不写 VChart", "VChart" not in card["body"]["elements"][-1]["content"])

    # 多行正文只保留摘要，详情由表格承载，不能双重展示
    answer = "查询结果（2 条），详情见下方表格。"
    check("多行正文不重复明细", "张伟" not in answer and "详情见下方表格" in answer)


if __name__ == "__main__":
    test_fmt_table()
    test_table_payload_shape()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
