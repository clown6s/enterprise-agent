# -*- coding: utf-8 -*-
"""上传文档完整调试入口：OCR 状态完成后输出统一 chunk JSON。"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.ingestion import parse
from app.ingestion.structured_chunking import classify_document, chunk_technical_book
from app.ingestion import ocr_paddle

# 直接修改这里填写文件地址；--file 会覆盖它。
FILE_PATH = r"D:\datum\AI-Agents-in-Depth-zh-CN.pdf"
OUTPUT_DIR = os.path.join(ROOT, "outputs", "upload_stages")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=FILE_PATH)
    args = parser.parse_args()
    path = os.path.abspath(args.file)
    print("[1/8] 读取文件开始", flush=True)
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.basename(path)
    print(f"[1/8] 读取文件完成 size={len(data)}", flush=True)

    print("[2/8] 本地解析开始", flush=True)
    local_text = parse.parse_local_bytes(name, data)
    print(f"[2/8] 本地解析完成 chars={len(local_text)}", flush=True)
    print("[3/8] 文档分类开始", flush=True)
    profile = classify_document(local_text, name)
    print(f"[3/8] 文档分类完成 profile={profile.kind} evidence={list(profile.evidence)}", flush=True)
    if profile.kind == "technical_book":
        print("[4/8] 查询 OCR 任务状态开始", flush=True)
        task = ocr_paddle.create_document_task(data, name)
        print(f"[4/8] OCR 任务状态 status={task.get('status')} task_id={task.get('task_id', '')}", flush=True)
        if task.get("status") != "success" and task.get("reused"):
            print("[5/8] 请求 OCR query 开始", flush=True)
            task = ocr_paddle.query_document_task(data)
            print(f"[5/8] OCR query 完成 status={task.get('status')}", flush=True)
        if task.get("status") != "success":
            print("[6/8] OCR 尚未完成，停止后续解析", flush=True)
            result = {"stage": "chunk", "file": path, "profile": profile.kind,
                      "ocr_status": task.get("status", "pending"),
                      "task_id": task.get("task_id", ""), "pending": True, "chunks": []}
            _write(result, name)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("[6/8] parse_result 解析开始", flush=True)
        content, _ = parse.parse_document_with_vl(name, data)
        print(f"[6/8] parse_result 解析完成 chars={len(content)}", flush=True)
    else:
        content = local_text

    print("[7/8] chunk 开始", flush=True)
    chunks = chunk_technical_book(content, 600)
    print(f"[7/8] chunk 完成 count={len(chunks)}", flush=True)
    normalized = []
    for index, unit in enumerate(chunks):
        row = dict(unit)
        row.pop("page", None)
        row.pop("page_end", None)
        row.setdefault("kind", "prose")
        row.setdefault("section_path", [])
        row.setdefault("continuation", False)
        row.setdefault("continuation_of", f"{row['kind']}:{index}")
        row.setdefault("continuation_index", 0)
        normalized.append(row)
    result = {"stage": "chunk", "file": path, "profile": profile.kind,
              "confidence": profile.confidence, "evidence": list(profile.evidence),
              "count": len(normalized), "max_chars": max((len(x["text"]) for x in normalized), default=0),
              "chunks": normalized}
    print("[8/8] 写出结果开始", flush=True)
    _write(result, name)
    print("[8/8] 写出结果完成", flush=True)
    print(f"切分结果已写入 outputs/upload_stages/{os.path.splitext(name)[0]}.chunk.json")
    return 0


def _write(result: dict, name: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, os.path.splitext(name)[0] + ".chunk.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
