# -*- coding: utf-8 -*-
"""
检索参数自适应（闭环 B）：top_k × 阈值 网格搜索，只接受不劣化变更。

跑法：python -m app.learn.tune_retrieval
产出：data/tuned_params.json（retrieval 模块自动加载）+ 各组合得分表。
原则：只有当更优参数组合全面不劣于当前参数时才写入。
"""
import io
import json
import os
import sys

from ..service.retrieval import current_params, retrieve
from . import eval_engine

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OUT = os.path.join(_BASE, "data", "tuned_params.json")

# 网格：top_k ∈ [4, 8, 12]，阈值 ∈ [0.2, 0.3, 0.4, 0.5]
GRID_TOP_K = [4, 8, 12]
GRID_MIN_SCORE = [0.2, 0.3, 0.4, 0.5]


def tune() -> dict:
    current = current_params()
    print(f"当前参数: {current}")
    results = []
    best = None
    for top_k in GRID_TOP_K:
        for min_score in GRID_MIN_SCORE:
            report = eval_engine.run_retrieval_set(
                top_k=top_k, min_score=min_score)
            results.append({
                "top_k": top_k,
                "min_score": min_score,
                "hit_rate": report["hit_rate"],
                "mrr": report["mrr"],
            })
            print(f"top_k={top_k:>2} min_score={min_score} → "
                  f"hitRate={report['hit_rate']:.2%} MRR={report['mrr']}")
            if best is None or (report["hit_rate"], report["mrr"]) > (best[1], best[2]):
                best = (top_k, report["hit_rate"], report["mrr"], min_score)

    best_top_k, best_hr, best_mrr, best_th = best
    current_hr = None
    # 当前参数组合的得分（在网格里或单独跑一次）
    for r in results:
        if r["top_k"] == current["top_k"] and r["min_score"] == current["min_score"]:
            current_hr = r["hit_rate"]
    if current_hr is None:
        report = eval_engine.run_retrieval_set(
            top_k=current["top_k"], min_score=current["min_score"])
        current_hr = report["hit_rate"]

    payload = {
        "top_k": best_top_k,
        "min_score": best_th,
        "hit_rate": best_hr,
        "mrr": best_mrr,
        "previous": current,
        "grid_results": results,
    }
    # 只接受严格更优：平局不换参（缩小 top_k 在评测集上无差异，但会削弱未见查询的召回面）
    if best_hr <= current_hr:
        print(f"[保持] 最优组合 {best_top_k}/{best_th}（{best_hr:.2%}）不优于当前"
              f"（{current_hr:.2%}），参数不变。网格平局说明评测集内参数已达最优"
              f"（瓶颈在未见查询的召回面）。")
        payload["top_k"] = current["top_k"]
        payload["min_score"] = current["min_score"]
    else:
        os.makedirs(os.path.dirname(_OUT), exist_ok=True)
        with open(_OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[OK] 写入 data/tuned_params.json: top_k={best_top_k} min_score={best_th}"
              f"（hitRate {best_hr:.2%}, MRR {best_mrr}）")
    return payload


if __name__ == "__main__":
    # stdout 重配只在直接运行时生效（曾放模块顶层——被 import 时会劫持
    # 导入方的标准输出，副作用炸弹）
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    tune()
