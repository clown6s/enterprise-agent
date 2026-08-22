# -*- coding: utf-8 -*-
import json
import os
from upload_test_common import parsed, classify_document, chunk_document

# 输出到项目根目录 outputs/upload_stages/，已在 .gitignore 排除
OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "outputs", "upload_stages"))

if __name__ == '__main__':
    path, data, text, diagnostics = parsed()
    c = classify_document(text, path)
    chunks = chunk_document(text, path, 600)
    normalized = []
    for unit in chunks:
        row = {"text": unit.get("text", ""), "page": unit.get("page", 1)}
        if unit.get("section_path"):
            row["section_path"] = unit["section_path"]
        kind = unit.get("kind") or ("qa" if c.kind == "qa" else c.kind)
        if kind:
            row["kind"] = kind
        if c.kind == "qa":
            import re
            m = re.match(r"\s*(\d+)", row["text"])
            row["question_number"] = int(m.group(1)) if m else None
            row["qa_status"] = "paired" if re.search(r"答\s*[:：]", row["text"]) else "unknown"
        elif c.kind == "legal" and "article" in unit:
            row["article"] = unit["article"]
        for key in ("title", "category", "term", "definition", "definition_part", "continuation", "continuation_of", "continuation_index", "quality_flag"):
            if key in unit:
                row[key] = unit[key]
        normalized.append(row)
    result = {
        'stage': 'chunk',
        'file': path,
        'profile': c.kind,
        'confidence': c.confidence,
        'evidence': list(c.evidence),
        'count': len(chunks),
        'max_chars': max((len(x['text']) for x in chunks), default=0),
        'chunks': normalized,
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0] + '.chunk.json'
    output_path = os.path.join(OUTPUT_DIR, name)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'切分结果已写入: {output_path}')
