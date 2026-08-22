# -*- coding: utf-8 -*-
"""检索证据预算离线测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.service import retrieval


def check(name, ok):
    print(('✓' if ok else '✗') + ' ' + name)
    if not ok:
        raise SystemExit(1)


hits = [
    {"document_id": "a", "source": "same.txt", "score": .95},
    {"document_id": "a", "source": "same.txt", "score": .94},
    {"document_id": "b", "source": "same.txt", "score": .90},
    {"document_id": "c", "source": "other.txt", "score": .80},
]
out = retrieval._budget_evidence(hits, limit=4, per_document=2)
check("同文档限额", [h["document_id"] for h in out].count("a") == 2)
check("同名来源不同文档不合并", {h["document_id"] for h in out} == {"a", "b", "c"})

low = [h for h in hits if h["score"] >= .9]
out = retrieval._budget_evidence(low, limit=4, per_document=1)
check("阈值后预算", [h["document_id"] for h in out] == ["a", "b"])
print("检索预算测试通过")
