# -*- coding: utf-8 -*-
"""独立测试：只判断上传文档类型，不调用 OCR task/query。"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from app.ingestion import parse
from app.ingestion.structured_chunking import classify_document

# 直接修改这里填写待测试文档路径；命令行 --file 可覆盖。
FILE_PATH = r"D:\datum\AI-Agents-in-Depth-zh-CN.pdf"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=FILE_PATH, help="待判断的文档路径")
    args = parser.parse_args()
    path = os.path.abspath(args.file)
    with open(path, "rb") as f:
        data = f.read()

    # 分类阶段只使用本地解析入口，绝不触发 OCR task/query。
    text = parse.parse_local_bytes(os.path.basename(path), data)
    profile = classify_document(text, path)
    result = {
        "stage": "classification",
        "file": path,
        "profile": profile.kind,
        "confidence": profile.confidence,
        "evidence": list(profile.evidence),
        "chars": len(text),
        "ocr_called": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
