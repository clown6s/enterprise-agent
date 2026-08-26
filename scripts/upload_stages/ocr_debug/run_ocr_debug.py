# -*- coding: utf-8 -*-
"""百度 PaddleOCR-VL 异步调试入口。

用法：
    python scripts/upload_stages/ocr_debug/run_ocr_debug.py --file D:\\datum\\file.pdf

流程由生产 ingestion pipeline 负责：数据库已有 parse_result 直接解析；
已有 task_id 只 query；没有任务才创建 task。调试产物统一写 outputs/。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from app.ingestion.pipeline import ingest

OUTPUT_DIR = os.path.join(ROOT, "outputs", "upload_stages")
# 直接修改这里即可运行；命令行 --file 会覆盖此默认值。
FILE_PATH = r"D:\datum\AI-Agents-in-Depth-zh-CN.pdf"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=FILE_PATH, help="待上传文件路径")
    parser.add_argument("--title", default="", help="文档标题")
    args = parser.parse_args()
    path = os.path.abspath(args.file)
    with open(path, "rb") as f:
        data = f.read()
    result = ingest(os.path.basename(path), data, title=args.title)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0] + ".ocr-debug.json"
    output = os.path.join(OUTPUT_DIR, name)
    with open(output, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"调试结果已写入: {output}")
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
