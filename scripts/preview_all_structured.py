# -*- coding: utf-8 -*-
"""只读批量结构化切分预览，不调用外部服务。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ingestion import parse
from app.ingestion.structured_chunking import classify_document, chunk_document

SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".csv"}


def preview_file(path, max_chars):
    item = {"file": path, "extension": os.path.splitext(path)[1].lower(),
            "bytes": os.path.getsize(path), "errors": [], "quality_flags": []}
    try:
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)
        if item["extension"] not in SUPPORTED:
            item["quality_flags"].append("unsupported_extension")
            item["profile"] = {"kind": "generic", "confidence": 0,
                               "evidence": ["unsupported_extension"]}
            item["units"] = []
            return item
        diagnostics = None
        if item["extension"] == ".pdf":
            text, diagnostics = parse.parse_pdf_with_diagnostics(data)
            item["diagnostics"] = diagnostics
            if diagnostics.get("reasons"):
                item["quality_flags"].extend(diagnostics["reasons"])
        else:
            text = parse.parse_bytes(name, data)
        if not text.strip():
            item["quality_flags"].append("empty_text")
        classification = classify_document(text, name)
        item["profile"] = {"kind": classification.kind,
                            "confidence": classification.confidence,
                            "evidence": list(classification.evidence),
                            "fallback": classification.fallback}
        if classification.kind == "generic":
            item["quality_flags"].append("generic_fallback")
        units = chunk_document(text, name, max_chars)
        item["units"] = []
        for i, unit in enumerate(units):
            row = {"index": i, "text": unit.get("text", ""),
                   "chars": len(unit.get("text", "")),
                   "page": unit.get("page", 1),
                   "section_path": unit.get("section_path", [])}
            for key in ("kind", "article", "block_id", "part", "parts"):
                if key in unit:
                    row[key] = unit[key]
            if row["chars"] > max_chars:
                item["quality_flags"].append("chunk_over_limit")
            item["units"].append(row)
        item["unit_count"] = len(item["units"])
        item["max_unit_chars"] = max((u["chars"] for u in item["units"]), default=0)
    except Exception as exc:
        item["errors"].append(f"{type(exc).__name__}: {exc}")
        item["quality_flags"].append("processing_error")
        item["units"] = []
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default=r"D:\datum")
    ap.add_argument("--file", default="")
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-chars", type=int, default=600)
    args = ap.parse_args()
    if args.file:
        files = [args.file if os.path.isabs(args.file) else os.path.join(args.input_dir, args.file)]
    else:
        files = sorted((os.path.join(args.input_dir, n) for n in os.listdir(args.input_dir)
                        if os.path.isfile(os.path.join(args.input_dir, n))),
                       key=lambda p: os.path.basename(p).casefold())
    result = {"schema_version": 1, "input_dir": args.input_dir,
              "max_chars": args.max_chars,
              "files": [preview_file(p, args.max_chars) for p in files]}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已写入 {args.output}，文件数={len(files)}")


if __name__ == "__main__":
    main()
