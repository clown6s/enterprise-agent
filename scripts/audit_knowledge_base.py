# -*- coding: utf-8 -*-
"""只读知识库审计：不写 Milvus/Neo4j、不调用备份或重建。"""
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, milvus_store

MAX_EXAMPLES = 20

def h(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]

def ex(row, reason):
    return {"reason": reason, "row_id": row.get("id"),
            "document_id": h(row.get("document_id", "")),
            "source": h(row.get("source", "")), "page": row.get("page"),
            "chars": len(row.get("text", "") or "")}

def add(bucket, row, reason):
    if len(bucket) < MAX_EXAMPLES:
        bucket.append(ex(row, reason))

def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try: out.append(json.loads(line))
                except ValueError: pass
    except OSError: pass
    return out

def audit_milvus():
    rows = list(milvus_store._iter_rows(
        milvus_store._get_client(), "id >= 0",
        ["id", "text", "document_id", "title", "source", "page"]))
    findings = defaultdict(list)
    docs = defaultdict(list)
    exact = Counter()
    for row in rows:
        text = row.get("text") or ""
        doc = row.get("document_id") or ""
        docs[doc].append(row)
        exact[h(text)] += 1
        if not text.strip(): add(findings["empty_chunk"], row, "empty")
        if len(text.strip()) < 20: add(findings["short_chunk"], row, "under_20_chars")
        bad = sum(1 for c in text if c == "�" or "" <= c <= "")
        if bad: add(findings["replacement_chars"], row, f"count={bad}")
        if re.search(r"\[\[page:\d+\]\]", text): add(findings["page_marker_in_chunk"], row, "marker")
        if re.search(r"(?:进行|完成|根据|按照|通过|合理)$", text.strip()):
            add(findings["terminal_fragment"], row, "terminal_structure")
        if not doc or not row.get("source") or row.get("page") in (None, 0):
            add(findings["metadata_missing"], row, "document/source/page")
    for digest, count in exact.items():
        if count > 1:
            findings["duplicate_chunk_groups"].append({"text_hash": digest, "count": count})
            if len(findings["duplicate_chunk_groups"]) >= MAX_EXAMPLES: break
    return {"rows": len(rows), "documents": len(docs),
            "chunks_by_document": {h(k): len(v) for k,v in docs.items()},
            "findings": dict(findings), "document_ids": set(docs)}

def audit_graph(document_ids):
    out = {"available": False, "findings": defaultdict(list)}
    try:
        from app.service import graph
        with graph._db().session() as s:
            rels = s.run("MATCH (a:Entity)-[r:RELATES]->(b:Entity) "
                         "RETURN a.name AS src,b.name AS tgt,r.type AS type,r.doc_id AS doc_id").data()
            orphan = s.run("MATCH (n:Entity) WHERE NOT (n)--() RETURN count(n) AS n").single()
            out["available"] = True
            out["relations"] = len(rels)
            out["entities_orphan"] = int((orphan or {}).get("n", 0))
            seen = Counter()
            for r in rels:
                key = (r.get("src"), r.get("type"), r.get("tgt"), r.get("doc_id"))
                seen[key] += 1
                if not r.get("doc_id"): out["findings"]["unscoped_relation"].append({"type": r.get("type")})
                elif r["doc_id"] not in document_ids:
                    out["findings"]["orphan_document_relation"].append({"doc_id": h(r["doc_id"])})
                if r.get("src") == r.get("tgt"): out["findings"]["self_loop"].append({"doc_id": h(r.get("doc_id"))})
            out["duplicate_relations"] = sum(1 for n in seen.values() if n > 1)
    except Exception as e:
        out["error"] = type(e).__name__
    out["findings"] = dict(out["findings"])
    return out

def main():
    milvus = audit_milvus()
    graph = audit_graph(milvus.pop("document_ids"))
    report = {"ts": datetime.now().isoformat(), "collection": config.MILVUS_COLLECTION,
              "milvus": milvus, "graph": graph,
              "ingested_records": len(read_jsonl(os.path.join(config.LOG_DIR, "ingested.jsonl"))),
              "ingestion_events": len(read_jsonl(os.path.join(config.LOG_DIR, "ingestion.jsonl")))}
    path = os.path.join(config.LOG_DIR, "knowledge-audit.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=list)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    print(f"报告已写入: {path}")

if __name__ == "__main__": main()
