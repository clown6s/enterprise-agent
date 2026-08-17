# -*- coding: utf-8 -*-
"""
向量集合迁移：documents(1024维) → documents_v2(4096维 + page)。
python scripts/migrate_v2.py [--drop-v2]

流程：建 v2 集合 → 读旧集合全部 chunk → 新模型重嵌入 → 写入 v2。
旧集合查询不可用（Milvus 服务端坏状态）时：提示重启 Milvus 后重跑，
或直接在管理后台重新上传文档（新管线解析更优，重传效果最好）。
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))

from pymilvus import Collection, connections

from app import config, embeddings, milvus_store


def read_legacy() -> list[dict]:
    """读旧集合全部 chunk（id 主键分页）"""
    milvus_store._ensure_database()
    col = Collection(config.LEGACY_COLLECTION)
    col.load()
    rows = []
    offset = 0
    while True:
        batch = col.query(expr="id >= 0",
                          output_fields=["text", "document_id", "title", "source"],
                          offset=offset, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def main() -> int:
    if not milvus_store.create_collection_v2(drop_existing="--drop-v2" in sys.argv):
        print("v2 集合创建失败，终止。")
        return 1

    try:
        rows = read_legacy()
    except Exception as e:
        print(f"旧集合读取失败: {e!r}")
        print("→ 请重启 Milvus 服务后重跑本脚本；或直接在管理后台重新上传文档（推荐，新管线解析更优）。")
        return 1
    print(f"旧集合读出 {len(rows)} 条 chunk，开始重嵌入（4096 维）…")

    ok = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        vec = embeddings.embed(r["text"])
        milvus_store.insert_document(
            text=r["text"], vector=vec,
            document_id=r.get("document_id", ""),
            title=r.get("title", ""), source=r.get("source", ""))
        ok += 1
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(rows)}（{time.time() - t0:.0f}s）")
    print(f"完成：写入 {ok} 条，耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
