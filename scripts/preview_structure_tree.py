# -*- coding: utf-8 -*-
"""只读结构树预览：不调用 embedding、Milvus 或 Neo4j。

用法：
  python scripts/preview_structure_tree.py D:/datum/中华人工共和国民法典.pdf
  python scripts/preview_structure_tree.py D:/datum/导游面试问答.docx --json
"""
import argparse
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ingestion import parse

# 手动填写文件地址；留空时使用命令行参数
FILE_PATH = r"D:\datum\古代诗歌常用意象.txt"
# 手动填写输出 JSON 文件路径；留空时输出到同目录同名 .structure.json
OUTPUT_FILE = ""


LEGAL_LEVEL_RE = re.compile(r"^第\s*[一二三四五六七八九十百千万0-9]+\s*(编|篇|章|节)\b")
ARTICLE_RE = re.compile(r"^第\s*[一二三四五六七八九十百千万0-9]+\s*条\b")
QA_RE = re.compile(r"^\d+[、.．]\s*.+")
SUBITEM_RE = re.compile(r"^(?:[⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽]|（\d+）|\(\d+\)|\d+\.)\s*.*")
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
PAGE_RE = re.compile(r"^\[\[page:(\d+)\]\]$")
TOC_RE = re.compile(r"^(.+?)(?:\.{2,}|…{2,}|\s{3,})(\d+)\s*$")

def node(kind, title, parent, page, order, text=""):
    return {"node_id": uuid.uuid4().hex[:12], "kind": kind,
            "title": title[:200], "parent_id": parent,
            "page_start": page, "page_end": page, "order": order,
            "text_chars": len(text), "children": []}

def extract_tree(text, filename):
    root = node("document", os.path.basename(filename), None, 1, 0)
    nodes = [root]
    stack = [(0, root)]
    order = 1
    page = 1
    current = root

    def add(kind, title, level, content=""):
        nonlocal order, current
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]
        n = node(kind, title, parent["node_id"], page, order, content)
        order += 1
        parent["children"].append(n)
        nodes.append(n)
        stack.append((level, n))
        current = n

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        pm = PAGE_RE.match(line)
        if pm:
            page = int(pm.group(1))
            continue
        m = MD_HEADING_RE.match(line)
        if m:
            add("heading", m.group(2), len(m.group(1)))
            continue
        if line in {"目录", "目 录"}:
            add("toc", line, 1)
            continue
        if TOC_RE.match(line):
            add("toc_item", TOC_RE.match(line).group(1).strip(), 2, line)
            continue
        if LEGAL_LEVEL_RE.match(line):
            add("legal_level", line, 2)
            continue
        if ARTICLE_RE.match(line):
            add("article", line.split()[0] if line.split() else line, 3, line)
            continue
        if QA_RE.match(line) and ("？" in line or "?" in line or
                                  re.search(r"什么|哪里|如何|为什么|哪些|多少|谁|何时|吗", line)):
            add("question", line, 3, line)
            continue
        if SUBITEM_RE.match(line):
            add("subitem", line[:120], 4, line)
            continue
        # 正文挂到最近结构节点，累计字符/页范围，不把正文伪造为关系节点。
        current["text_chars"] += len(line)
        current["page_end"] = page
    return root, nodes

def compact(n):
    return {k: ( [compact(x) for x in v] if k == "children" else v )
            for k, v in n.items()}

def print_tree(n, prefix="", last=True):
    if n["kind"] != "document":
        print(prefix + ("└── " if last else "├── ") +
              f"[{n['kind']}] {n['title']} (page {n['page_start']}-{n['page_end']}, chars={n['text_chars']})")
    children = n.get("children", [])
    next_prefix = prefix + ("    " if last else "│   ") if n["kind"] != "document" else ""
    for i, child in enumerate(children):
        print_tree(child, next_prefix, i == len(children) - 1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--json", action="store_true", default=True)
    args = ap.parse_args()
    file_path = FILE_PATH or args.file
    if not file_path:
        ap.error("请填写 FILE_PATH 或提供文件路径")
    with open(file_path, "rb") as f:
        data = f.read()
    text = parse.parse_bytes(os.path.basename(file_path), data)
    root, nodes = extract_tree(text, file_path)
    result = {"file": file_path, "node_count": len(nodes), "tree": root}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    output_path = OUTPUT_FILE or os.path.splitext(file_path)[0] + ".structure.json"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"结构树 JSON 已写入: {output_path}")
    if args.json:
        print(output)
        print_tree(root)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
