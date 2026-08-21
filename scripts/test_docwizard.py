# -*- coding: utf-8 -*-
"""文档生成来源边界与格式契约测试（离线）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.service.docwizard import (  # noqa: E402
    _clean_source_claims, _GENERATE_PROMPT, parse_request, matches, guess_title,
)

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
check("生成规则含来源分层", "你要写的文档" in _GENERATE_PROMPT
      and "建议实践" in _GENERATE_PROMPT)
check("生成规则禁绝对声明", "完全依据" in _GENERATE_PROMPT
      and "未引入外部知识" in _GENERATE_PROMPT)
check("生成规则统一无序列表", "- " in _GENERATE_PROMPT)
check("清理函数幂等", _clean_source_claims(clean) == clean)

# 文档意图矩阵：覆盖自然口语正例和知识问答/非文档负例。
for query, expected_title in [
    ("帮我这个学习agent的计划书", "学习 Agent 计划书"),
    ("帮我写个学习agent的计划，每天两小时", "学习 Agent 计划"),
    ("帮我写个学习agent 的计划，每天学习2小时", "学习 Agent 计划"),
    ("帮我写个项目计划，目标是通过考试，周期三个月", "项目计划"),
    ("帮我做个学习计划", None),
    ("请帮我写一份学习方案", "学习方案"),
    ("帮我写一份会议纪要", "会议纪要"),
    ("帮我做个北京旅游攻略，预算500元", "北京旅游攻略"),
]:
    parsed = parse_request(query)
    check(f"文档正例：{query}", parsed["matched"])
    check(f"标题规范：{query}", parsed["title"] == expected_title,
          repr(parsed))
    if "每天两小时" in query:
        check("尾部要点保留", parsed["points"] == "每天两小时", repr(parsed))
    if "每天学习2小时" in query:
        check("实际空格数字变体要点", parsed["points"] == "每天学习2小时", repr(parsed))
    if query == "帮我这个学习agent的计划书":
        check("无尾部要点为空", parsed["points"] == "", repr(parsed))
    if "目标是通过考试" in query:
        check("多段要点保留", parsed["points"] == "目标是通过考试，周期三个月", repr(parsed))
    if query == "帮我做个北京旅游攻略，预算500元":
        check("攻略标题保留旅游后缀", parsed["title"] == "北京旅游攻略", repr(parsed))
        check("攻略要点保留", parsed["points"] == "预算500元", repr(parsed))

for query in [
    "学习计划是什么", "如何制定学习计划", "计划书怎么写", "怎么写方案",
    "如何生成通知", "请问关于五一通知", "帮我写代码", "帮我写邮件",
    "帮我看看学习计划", "修改计划书", "帮我写个学习计划每天两小时",
    "请假通知", "做法报告", "来年报告",
]:
    parsed = parse_request(query)
    check(f"文档负例：{query}", not parsed["matched"], repr(parsed))
    check(f"兼容判定：{query}", matches(query) is False and guess_title(query) is None)

# 云文档发布权限契约：editor 身份缺失时必须安全降级，不冒充在线可编辑。
from app.service import docwizard as _dw  # noqa: E402

def _fake_publish_ok(title, content, editor_open_id):
    return "https://feishu.cn/docx/fake" if editor_open_id else None

_orig_publish = _dw.publish_cloud_doc
try:
    _dw.publish_cloud_doc = _fake_publish_ok
    # 无编辑身份 → content_fallback，文案不含“在线可编辑”
    req = parse_request("帮我写个学习agent的计划，每天两小时")
    req["title"] = "学习 Agent 计划"
    out = _dw._generate(req, "", "diag-perm")
    check("无编辑身份不冒充可编辑", out.get("status") == "content_fallback"
          and "在线可编辑" not in out.get("answer", "") and out.get("editable") is False,
          repr(out))
    # 有编辑身份 → published
    out2 = _dw._generate(req, "ou_editor", "diag-perm")
    check("有编辑身份标记可编辑", out2.get("status") == "published"
          and out2.get("editable") is True and "在线可编辑" in out2.get("answer", ""),
          repr(out2))
finally:
    _dw.publish_cloud_doc = _orig_publish

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
