# -*- coding: utf-8 -*-
"""无副作用文档切分预览：不调用 embedding/Milvus/Neo4j。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) )
from app.ingestion import parse
from app.ingestion.structured_chunking import chunk_legal, is_legal_confident


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--profile", default="legal_article", choices=["legal_article"])
    p.add_argument("--max-chars", type=int, default=600)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    with open(args.file, "rb") as f:
        data = f.read()
    text = parse.parse_bytes(os.path.basename(args.file), data)
    if args.profile == "legal_article" and is_legal_confident(text):
        chunks = chunk_legal(text, args.max_chars)
    else:
        chunks = [{"text": text, "page": 1, "article": None, "section_path": []}]
    result = {"file": args.file, "profile": args.profile,
              "legal_confident": is_legal_confident(text),
              "chunks": chunks}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else "\n\n".join(f"[{i+1}] page={c['page']} article={c['article']}\n{c['text']}"
                              for i, c in enumerate(chunks)))


if __name__ == "__main__":
    main()
