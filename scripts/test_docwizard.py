# -*- coding: utf-8 -*-
"""文档生成来源边界与格式契约测试（离线）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.service.docwizard import _clean_source_claims, _GENERATE_PROMPT  # noqa: E402

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


text = ("**前置知识**\n"
        "- LLM 基础\n"
        "本计划书完全依据《资料.pdf》整理\n"
        "未引入外部知识\n"
        "资料明确要求学习 Python\n"
        "- 建议实践：建议做一个小项目\n")
clean = _clean_source_claims(text)
check("删除绝对来源声明", "完全依据" not in clean and "未引入" not in clean
      and "资料明确要求" not in clean)
check("保留正常内容", "前置知识" in clean and "建议实践" in clean)
check("生成规则含来源分层", "资料事实" in _GENERATE_PROMPT
      and "建议实践" in _GENERATE_PROMPT)
check("生成规则禁绝对声明", "完全依据" in _GENERATE_PROMPT
      and "未引入外部知识" in _GENERATE_PROMPT)
check("生成规则统一无序列表", "- " in _GENERATE_PROMPT)
check("清理函数幂等", _clean_source_claims(clean) == clean)

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
