# -*- coding: utf-8 -*-
"""
图谱重建工具：从 Milvus 重新构建所有文档的知识图谱。

用法：
  python scripts/rebuild_graph.py                    # 重建所有文档
  python scripts/rebuild_graph.py --doc-id <doc_id>  # 重建指定文档
  python scripts/rebuild_graph.py --force            # 清空图谱后重建（清空已有数据）

说明：图谱构建失败只记日志，不影响文档上传。本工具可重新构建。
"""
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app import config, milvus_store
from app.service import graph


def _get_chunks_by_doc_id(doc_id: str) -> list[str]:
    """按 document_id 获取所有 chunks"""
    chunks: list[str] = []
    try:
        for r in milvus_store._iter_rows(
            milvus_store._get_client(),
            f'document_id == "{doc_id}"',
            ["text"],
        ):
            text = r.get("text", "")
            if text:
                chunks.append(text)
    except Exception as e:
        print(f"  [ERROR] 获取文档 {doc_id} chunks 失败: {e}")
    return chunks


def _clear_graph() -> None:
    """清空 Neo4j 图谱"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        f"bolt://{config._env('NEO4J_HOST', '10.211.55.5')}:7687",
        auth=("neo4j", config.NEO4J_PASSWORD),
    )
    with driver.session() as session:
        result = session.run("MATCH (n) DETACH DELETE n")
        summary = result.consume()
        print(f"已清空图谱：{summary.counters.nodes_deleted} 个节点，{summary.counters.relationships_deleted} 个关系")
    driver.close()


def rebuild_graph(doc_id: str | None = None, force: bool = False) -> None:
    """重建图谱

    Args:
        doc_id: 指定文档 ID，None 表示重建所有文档
        force: 是否先清空图谱后重建
    """
    if force:
        print("清空图谱...")
        _clear_graph()

    # 获取文档列表
    if doc_id:
        docs = [milvus_store._iter_rows(
            milvus_store._get_client(),
            f'document_id == "{doc_id}"',
            ["document_id", "title"],
        )]
        docs = [{"docId": doc_id, "title": "指定文档"}]
    else:
        docs = milvus_store.list_documents()

    if not docs:
        print("没有找到文档")
        return

    print(f"开始重建图谱，共 {len(docs)} 个文档")

    total_rels = 0
    failed = 0

    for i, doc in enumerate(docs, 1):
        doc_id = doc.get("docId", "")
        title = doc.get("title", "")
        print(f"[{i}/{len(docs)}] 重建文档: {doc_id} ({title[:30]})")

        chunks = _get_chunks_by_doc_id(doc_id)
        if not chunks:
            print(f"  [SKIP] 文档无 chunks")
            continue

        rels = graph.build_from_chunks(chunks, doc_id)
        if rels > 0:
            print(f"  [OK] 构建了 {rels} 条关系")
            total_rels += rels
        else:
            print(f"  [SKIP] 未提取到关系（可能没有明确的关系陈述）")

    print(f"\n重建完成：共 {total_rels} 条关系，{failed} 个文档失败")


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    doc_id = None
    force = "--force" in sys.argv

    if "--doc-id" in sys.argv:
        try:
            idx = sys.argv.index("--doc-id")
            if idx + 1 < len(sys.argv):
                doc_id = sys.argv[idx + 1]
        except ValueError:
            pass

    rebuild_graph(doc_id=doc_id, force=force)


if __name__ == "__main__":
    main()