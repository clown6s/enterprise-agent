# -*- coding: utf-8 -*-
"""
知识库全量备份：Milvus 全量导出（含向量）→ data/backup/kb-<时间戳>.jsonl。
跑法：python scripts/backup_kb.py
恢复：python scripts/restore_kb.py <备份文件>
建议：原始文档已自动留底 data/uploads/（根备份）；本脚本备份的是
"含向量的快照"，恢复免重新嵌入。
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, milvus_store

_OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backup")


def main() -> None:
    client = milvus_store._get_client()
    out_path = os.path.join(_OUT_DIR, f"kb-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    os.makedirs(_OUT_DIR, exist_ok=True)
    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        it = client.query_iterator(config.MILVUS_COLLECTION, batch_size=500,
                                   filter="id >= 0",
                                   output_fields=["text", "dense", "document_id",
                                                  "title", "source", "page"])
        try:
            while True:
                batch = it.next()
                if not batch:
                    break
                for r in batch:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    count += 1
        finally:
            try:
                it.close()
            except Exception:
                pass
    meta = {"exported_at": datetime.now().isoformat(), "count": count,
            "collection": config.MILVUS_COLLECTION}
    with open(out_path + ".meta", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[backup] 已导出 {count} 条 chunk → {out_path}")
    print(f"[backup] 原始文档留底: data/uploads/（{len(os.listdir(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'uploads'))
        if os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'uploads')) else 0)} 份）")


if __name__ == "__main__":
    main()
