# -*- coding: utf-8 -*-
"""只读查询某个文档对应的 Neo4j 图谱关系。

用法：
  python scripts/query_document_graph.py --doc-id <document_id>
  python scripts/query_document_graph.py --list

只执行 MATCH/RETURN，不会修改 Neo4j。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from neo4j import GraphDatabase

from app import config


def _driver():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    host = config._env("NEO4J_HOST", "10.211.55.5")
    return GraphDatabase.driver(
        f"bolt://{host}:7687",
        auth=("neo4j", config.NEO4J_PASSWORD),
    )


def query_document(driver, doc_id: str) -> None:
    query = (
        "MATCH (a:Entity)-[r:RELATES {doc_id: $doc_id}]->(b:Entity) "
        "RETURN a.name AS source, r.type AS relation, b.name AS target "
        "ORDER BY source, relation, target"
    )
    with driver.session() as session:
        rows = session.run(query, doc_id=doc_id).data()
    print(json.dumps({
        "doc_id": doc_id,
        "relation_count": len(rows),
        "relations": rows,
    }, ensure_ascii=False, indent=2))


def list_documents(driver) -> None:
    query = (
        "MATCH ()-[r:RELATES]->() "
        "RETURN r.doc_id AS doc_id, count(r) AS relation_count "
        "ORDER BY relation_count DESC, doc_id"
    )
    with driver.session() as session:
        rows = session.run(query).data()
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="只读查询文档图谱")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id", help="文档 document_id")
    group.add_argument("--list", action="store_true", help="列出图谱中的文档 ID 和关系数")
    args = parser.parse_args()

    driver = _driver()
    try:
        if args.list:
            list_documents(driver)
        else:
            query_document(driver, args.doc_id)
    finally:
        driver.close()
    return 0


# 手动填写文档 document_id；留空时使用命令行 --doc-id
DOCUMENT_ID = "523c7115adb54bbe887ec4ff793ffe2f"


if __name__ == "__main__":
    if DOCUMENT_ID:
        query_document(_driver(), DOCUMENT_ID)
    else:
        raise SystemExit(main())
