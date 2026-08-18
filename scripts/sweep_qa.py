# -*- coding: utf-8 -*-
"""
问法矩阵批量自检（2026-08-17 用户明确要求：不要让用户当人肉测试机）。

每个知识库文档 × 问法模板 → 全链路回答 → 确定性断言 → 失败清单。
用法：python scripts/sweep_qa.py [--docs N] [--only 标题关键词]

断言维度（全部确定性，不依赖人工判断）：
1. 相邻内容误答：问"注意/建议"类而资料无该维度 → 必须兜底，
   有维度 → 必须正常作答（问什么答什么）
2. 无依据术语：答案里资料没有的术语（数字/英文/书名号）→ 失败
3. 题干句式残留：首行是"描写××的方法"类阅读理解题干 → 失败
4. 套话残留：延伸服务句式（欢迎随时…）→ 失败
5. 超短查询锚定：≤2 字查询正常作答时必须以"根据《"开头
6. 兜底混入：答案含"暂无法回答"却还有其他正文 → 失败
"""
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))

from app import guards, milvus_store
from app.service import agent, retrieval

# 问法模板（title 占位）
_TEMPLATES = [
    "{t} 是什么",
    "{t} 应该注意什么",
    "{t} 的注意事项",
    "{t} 的饮食建议",
    "{t} 的用药建议",
    "{t}",
]

_COMPREHENSION_HEADER = re.compile(
    r"^(描写|介绍|说明|阐述|讲述|如何|怎样)[^\n。！？]{1,20}"
    r"(的方法|的方式|的写法|的手法|的途径|的内容|的表达)$")


def _check(query: str) -> list[str]:
    """单条查询的断言结果：返回失败原因列表（空=通过）"""
    fails = []
    t0 = time.time()
    result = agent.answer(query, f"sweep-{abs(hash(query)) % 10**9}")
    seconds = time.time() - t0
    answer = result.get("answer") or ""
    q_norm = re.sub(r"\s+", "", query)

    # 上下文（用于无依据术语与维度判定）
    hits = retrieval.retrieve(query, user="")
    context = retrieval.build_context(hits)
    if answer.startswith("以下内容基于知识图谱关联"):
        # 图谱路径的答案依据是图谱上下文不是检索上下文——曾拿检索上下文
        # 核对图谱答案，Duke/Child/Pugh 全被误判"无依据术语"（实况：这些
        # 术语在图谱关系里，系统自己的校验依据就是图谱上下文）
        from app.service import graph
        gctx = graph.expand(query)
        if gctx:
            context = "## 知识图谱关联\n" + gctx

    # 兜底判定用前缀：兜底句式带"（已记录为知识缺口…）"告知后缀（_NO_ANSWER_GAP），
    # 曾用精确相等——gap 后缀加进来后所有正确兜底被判"应兜底却作答"（sweep 全量首跑
    # 20 条假失败的根因，系统行为是对的）
    is_fallback = answer.startswith(guards.NO_ANSWER)
    expected_fallback = agent._attention_gap(query, context)

    # 1. 相邻内容误答
    if expected_fallback and not is_fallback:
        fails.append("应兜底却作答（资料无该维度）")
    if not expected_fallback and is_fallback:
        fails.append("不应兜底却兜底（资料有该维度）")
    if is_fallback:
        return fails

    # 2. 无依据术语（数字/英文/书名号——资料里没有的出现在答案里）
    missing = guards.find_missing_terms(answer, context)
    if missing:
        fails.append(f"无依据术语: {missing[:4]}")

    # 3. 题干句式残留
    first = next((l.strip() for l in answer.splitlines() if l.strip()), "")
    if _COMPREHENSION_HEADER.match(first):
        fails.append(f"题干句式开头: {first[:20]}")

    # 4. 套话残留
    if guards._EXTENSION_OFFER.search(answer):
        fails.append("延伸服务套话残留")

    # 5. 超短查询锚定
    if len(q_norm) <= 2 and not answer.startswith("根据《"):
        fails.append("超短查询缺来源锚定")

    # 6. 兜底混入（整篇兜底已在前置 is_fallback 分支返回，走到这的都是
    # 实质作答——正文里再出现兜底句式就是半吊子兜底句混入）
    if "暂无法回答" in answer and not is_fallback:
        fails.append("兜底句式混入正文")

    # 7. 时限（生成+校验+闸门，正常 <45s）
    if seconds > 45:
        fails.append(f"超时 {seconds:.0f}s")
    return fails


def main() -> int:
    args = sys.argv[1:]
    doc_limit = 10
    only = ""
    if "--docs" in args:
        doc_limit = int(args[args.index("--docs") + 1])
    if "--only" in args:
        only = args[args.index("--only") + 1]

    docs = [d for d in milvus_store.list_documents()
            if d.get("title") and (not only or only in d["title"])]
    docs = [d for d in docs if "adopt-" not in d.get("docId", "")][:doc_limit]
    if not docs:
        # Milvus 抖动时 list_documents 返回空——曾 0 文档跑出 0 条全绿，
        # 无效自检被当通过（r2 实况：Milvus 短暂不可用，跑分全废）
        print("!! 文档列表为空（Milvus 不可用？）——中止，本次自检无效")
        return 2

    print(f"=== 问法矩阵自检：{len(docs)} 文档 × {len(_TEMPLATES)} 问法 ===")
    failures = []
    total = 0
    for d in docs:
        t = d["title"]
        if t.endswith((".pdf", ".docx", ".txt", ".md")):
            t = t.rsplit(".", 1)[0]
        if not t.strip():
            continue
        for tmpl in _TEMPLATES:
            query = tmpl.format(t=t)
            total += 1
            try:
                fails = _check(query)
            except Exception as e:
                fails = [f"异常: {e!r}"]
            if fails:
                failures.append((query, fails))
                print(f"  ✗ {query}  →  {'; '.join(fails)}")
    print(f"\n=== {total} 条，失败 {len(failures)} 条 ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
