# -*- coding: utf-8 -*-
"""查询阶段测试公共配置；真实读取/调用，结果只写 outputs。"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
QUERY = "高血压一日三餐应该注意什么"
USER = ""
OUTPUT_DIR = os.path.join(ROOT, "outputs", "query_stages")


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("--query", default=QUERY)
    p.add_argument("--output", default="")
    return p.parse_args()


def output_path(query, stage, explicit=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return explicit or os.path.join(
        OUTPUT_DIR, f"{hashlib.sha256(query.encode()).hexdigest()[:12]}.{stage}.json")


def write_result(query, stage, result, explicit=""):
    path = output_path(query, stage, explicit)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"输出: {path}")
    return path


def hit_view(hit):
    return {k: hit.get(k) for k in
            ("document_id", "title", "source", "page", "score", "_expanded")}
