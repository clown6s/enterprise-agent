# 上传文档分阶段测试

本目录中的脚本用于**逐阶段、单文档、无副作用**检查上传流程。

```text
解析 → 质量 → 结构分类 → 切分 → mock 向量 → mock Milvus → mock 图谱
```

## 重要说明

- 默认文件在 `upload_test_common.py` 的 `FILE_PATH` 中填写；
- 也可以通过 `--file` 临时指定文件；
- 所有脚本都不调用真实 embedding、Milvus、Neo4j；
- 不写 `logs/ingestion.jsonl`、`logs/ingested.jsonl`；
- 不写 `data/uploads`；
- 不修改现有知识库。

## 使用方式

进入项目根目录执行。

### 1. 解析阶段

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_parse.py
```

或指定文件：

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_parse.py --file D:/datum/内科学.pdf
```

输出文件大小、解析字符数和 PDF 诊断信息。

### 2. 质量阶段

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_quality.py --file D:/datum/内科学.pdf
```

查看：

- `allow / warn / reject`
- 质量原因
- PDF 页数和字符统计

### 3. 结构分类阶段

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_structure.py --file D:/datum/导游面试问答.docx
```

查看：

- profile 类型
- 置信度
- 分类依据

### 4. 切分阶段

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_chunk.py --file D:/datum/中华人工共和国民法典.pdf
```

切分结果写入项目根目录：

```text
outputs/upload_stages/<文件名>.chunk.json
```

该目录已加入 `.gitignore`，不会提交到 Git。JSON 包含每个切分单元的文本、页码、章节路径、条号/结构类型和字符数。

### 5. 向量阶段（只使用 mock）

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_embedding.py --file D:/datum/古代诗歌常用意象.txt
```

只输出确定性 mock 向量的数量和维度，绝不会请求外部 embedding API。

### 6. Milvus 阶段（只捕获 rows）

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_milvus.py --file D:/datum/古代诗歌常用意象.txt
```

查看准备写入 Milvus 的行结构，但不会连接或写入 Milvus。

### 7. 图谱阶段（只捕获输入）

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_graph.py --file D:/datum/古代诗歌常用意象.txt
```

查看准备交给图谱阶段的 chunks，但不会连接或写入 Neo4j。

### 8. 全链路 dry-run

```bash
PYTHONUTF8=1 python scripts/upload_stages/test_upload_full_dry_run.py --file D:/datum/内科学.pdf
```

输出：

```text
解析 → 质量 → profile → chunks → mock embedding → captured Milvus → mock Neo4j
```

## 推荐验收顺序

每次只测一个文件，按以下顺序执行：

```bash
# 例：先测一个法规文件
python scripts/upload_stages/test_upload_parse.py --file D:/datum/中华人工共和国民法典.pdf
python scripts/upload_stages/test_upload_quality.py --file D:/datum/中华人工共和国民法典.pdf
python scripts/upload_stages/test_upload_structure.py --file D:/datum/中华人工共和国民法典.pdf
python scripts/upload_stages/test_upload_chunk.py --file D:/datum/中华人工共和国民法典.pdf
python scripts/upload_stages/test_upload_full_dry_run.py --file D:/datum/中华人工共和国民法典.pdf
```

前一阶段发现问题时，先停止，不继续测后续阶段。