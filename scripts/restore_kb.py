# -*- coding: utf-8 -*-
"""
知识库恢复：backup_kb.py 的 JSONL → Milvus 重新入库（批量 100/批）。
跑法：python scripts/restore_kb.py <备份文件> [--force]
说明：恢复目标是"重建知识库"——集合非空时需 --force（避免重复 chunk）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, milvus_store


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/restore_kb.py <备份文件> [--force]")
        sys.exit(1)
    path = sys.argv[1]
    force = "--force" in sys.argv
    client = milvus_store._get_client()
    stats = client.get_collection_stats(config.MILVUS_COLLECTION)
    if stats.get("row_count", 0) > 0 and not force:
        print(f"集合非空（{stats.get('row_count')} 条）——恢复会重复入库。"
              f"确认重建请加 --force（先清空：管理后台逐文档删除，或联系运维重建集合）")
        sys.exit(1)
    rows = []
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            rows.append({
                "text": r.get("text", ""),
                "dense": r.get("dense", []),
                "document_id": r.get("document_id", ""),
                "title": r.get("title", ""),
                "source": r.get("source", ""),
                "page": int(r.get("page", 0) or 0),
            })
            if len(rows) >= 100:
                if not milvus_store.insert_documents(rows):
                    print(f"[restore] 批次写入失败，中止（已恢复 {count} 条）")
                    sys.exit(1)
                count += len(rows)
                rows = []
                print(f"[restore] 已恢复 {count} 条")
    if rows and milvus_store.insert_documents(rows):
        count += len(rows)
    print(f"[restore] 完成，共恢复 {count} 条 chunk")
    print("[restore] 提示：图谱关系不在备份内——文档重新摄入时会自动重建")


if __name__ == "__main__":
    main()
