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

输出示例：

```json
{
  "stage": "structure",
  "file": "D:\\datum\\古代诗歌常用意象.txt",
  "profile": "glossary",
  "confidence": 0.95,
  "evidence": ["definition_density"]
}
```

字段说明：

- `stage`：当前测试阶段；`structure` 表示文档结构分类。
- `file`：本次被测试的源文件路径。
- `profile`：分类结果。常见值：`legal` 法规、`qa` 问答、`glossary` 术语定义、`outline` 层级大纲、`medical_mixed` 医学混合、`technical_book` 技术书、`generic` 通用回退。
- `confidence`：分类置信度，范围 `0.0～1.0`；越高表示结构信号越明确。低置信度会回退 `generic`。
- `evidence`：分类依据。常见值说明：
  - `definition_density`：连续出现较多“术语：解释”结构，判为 `glossary` 定义/词条类；
  - `question_answer_pairs`：检测到多组“问题编号/疑问句 + 答案或答案段落”配对，判为 `qa` 问答类；
  - `article_structure`：检测到大量“第 X 条”及编/篇/章/节层级，判为 `legal` 法规类；
  - `outline_lines`：检测到章节、一级/二级编号和说明项，判为 `outline` 层级大纲类；
  - `medical_signals`：检测到诊断、治疗、患者、临床、医学单位等多种医学结构信号，判为 `medical_mixed`；
  - `technical_signals`：检测到目录、章节、API、代码、函数、模块或示例等技术书结构信号，判为 `technical_book`；
  - `dotted_leaders`：检测到大量“标题 + 点线/空格 + 页码”的目录行，判为 `toc` 目录结构；
  - `fallback` 或 `ambiguous`：专用 profile 置信度不足或多个 profile 接近，安全回退 `generic`，不冒险套用特殊切分器。

该阶段只分类，不切分、不向量化、不写入 Milvus/Neo4j。

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

输出示例：

```json
{
  "stage": "full_dry_run",
  "file": "D:\\datum\\古代诗歌常用意象.txt",
  "profile": "glossary",
  "confidence": 0.95,
  "chunks": 8,
  "max_chars": 593,
  "embedding": "mock",
  "milvus": "captured",
  "neo4j": "mock",
  "side_effects": false,
  "real_external_calls": false
}
```

字段详细说明：

- `stage`：测试阶段名称。`full_dry_run` 表示完整流程演练，不是正式上传。
- `file`：被测试的文件路径。
- `profile`：结构分类结果，例如 `legal`、`qa`、`glossary`、`outline`、`medical_mixed`、`technical_book` 或 `generic`。
- `confidence`：结构分类置信度，范围 `0.0～1.0`。数值越高，说明文档结构信号越明显；低置信度通常回退 `generic`。
- `chunks`：真实解析和结构切分后得到的文本单元数量。它不是 Milvus 行数，也不是图谱关系数。
- `max_chars`：本次切分结果中最长单元的字符数，用来检查是否超过配置的切分上限。它不是 token 数；例如 `593` 表示最长文本单元有 593 个 Python 字符。
- `embedding: mock`：embedding 阶段使用确定性假向量，仅验证数量/流程，不调用真实 embedding API、不产生费用、不代表真实向量质量。
- `milvus: captured`：模拟捕获准备写入 Milvus 的 rows，仅查看写入数据形状；不连接、不插入、不删除真实 Milvus 数据。
- `neo4j: mock`：图谱阶段使用模拟实现，仅检查将要传入的 chunks；不连接、不创建节点、不创建关系、不删除图谱。
- `side_effects: false`：本次演练没有产生生产副作用，包括不写 Milvus、Neo4j、生产 ingestion 日志、ingested 登记、源文件归档或会话历史。
- `real_external_calls: false`：本次没有真实外部调用，包括 embedding、rerank、LLM、Milvus 和 Neo4j。解析、质量、分类、结构切分仍使用真实代码。

这个结果只能证明：

```text
真实解析 → 真实质量/分类 → 真实切分 → 模拟外部边界
```

不能证明已经正式入库、向量质量合格或图谱关系正确。

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