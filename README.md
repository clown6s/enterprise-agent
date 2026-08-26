# enterprise-agent

企业 AI 问答/执行智能体（数字员工 + 知识库 + 技能体系 + 管理后台）。FastAPI 实现，模型链路直连
OpenAI 兼容 API（qwen3-max 主力 + glm-4.6 独立裁判校验），向量检索走 Milvus（dense + BM25 混合，RRF）。

## 运行

```bash
pip install -r requirements.txt
python run.py            # http://localhost:8080（加 --reload 热重载）
```

管理前端复用 rag-admin（协议兼容，零改动，账号在 MySQL `rag_service.sys_user`）。
依赖：Milvus、Neo4j、MySQL；`scripts/watchdog.py` 探活 8080 自动拉起。

## 功能

| 模块 | 说明                                                                                                                                                                                      |
|---|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 确定性路由 | 正则路由表（NL2SQL 数据面/闲聊/追问澄清/知识问答/SOP），不调路由模型                                                                                                                      |
| 知识问答 | Qwen3-Embedding 嵌入 + Milvus 混合召回 + Qwen3-Reranker 精排 → LLM 生成 → 小模型校验（缺失术语锚定 + 过度删除回退 + 自动熔断）→ 确定性守卫（缺失术语硬删除/鉴伪断言/空标题清理/格式修复） |
| 图谱兜底 | 检索空手 → Neo4j 无向两跳 → 图谱-only 收紧规则生成 + 校验                                                                                                                                 |
| 审批 SOP | 五类审批（请假/报销/出差/采购/合同用印）状态机，直连本库，飞书对话审批闭环                                                                                                                |
| 文档生成 | 一句话直达飞书云文档 + 多轮修改回写 + 表单卡片兜底                                                                                                                                        |
| 飞书渠道 | lark-oapi WS 长连接 + 回答卡片（👍👎 反馈闭环）+ 数据图表卡（VChart）+ 图片 OCR                                                                                                           |
| 摄入管线 | txt/md/docx/pdf 解析 → 切分 → 入库 + 图谱构建，全程落日志                                                                                                                                 |
| 自我优化 | 反馈回流 / 缺口聚类 / 用量追踪 / 参数自适应（只接受不劣化变更）/ 校验熔断                                                                                                                 |
| 管理后台 | 仪表盘/审计/评测/数字员工/技能/SOP/审核/权限/用量等 API                                                                                                                                   |

## 文档向量化最终格式

技术文档经过百度 PaddleOCR-VL 的 Markdown 结构解析后，最终送入嵌入和 Milvus 的每个 chunk 使用统一 JSON 字段：

```json
{
  "text": "正文或结构内容",
  "kind": "prose",
  "section_path": ["第2章 上下文工程", "2.2 Agent 如何调用大模型"],
  "continuation": false,
  "continuation_of": "prose:12",
  "continuation_index": 0
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `text` | string | 实际用于嵌入、检索和 LLM 上下文的内容。正文保持 Markdown 语义；表格使用 Markdown；代码、流程、图题保持各自完整内容。 |
| `kind` | string | 内容类型：`prose` 普通正文、`heading` 正式章节、`subheading` 章节内小标题、`experiment` 实验/思考题/小结栏目、`code` 代码、`table` 表格、`procedure` 流程、`figure` 图片或图示。 |
| `section_path` | string[] | 当前内容所属的正式章节路径。Markdown 的 `##` 是章节第一级，`###` 是第二级；`#` 文档标题不进入路径；实验、思考题、流程步骤等栏目只继承路径，不新增路径层级。 |
| `continuation` | boolean | 当前 chunk 是否为一个较长结构的后续分片。首片或未拆分内容为 `false`，后续分片为 `true`。 |
| `continuation_of` | string | 原始结构块 ID。同一段正文、代码、表格或流程被拆成多个 chunk 时保持不变，例如 `prose:12`。 |
| `continuation_index` | integer | 当前结构块内的分片序号，从 `0` 开始递增，用于恢复原始顺序。 |

明确不进入最终向量 chunk 的字段：

- `page` / `page_end`：不保存页码，不把页边界写入正文，也不使用页码切断跨页语义；
- `table_html` / `cells` / `matrix`：属于 OCR 原始解析和展示/结构旁路数据，不直接替代 `text`；
- OCR 的 `task_id`、`parse_result_url`、原始 `parse_result`：保存在 OCR 任务数据库/原始缓存，不进入向量文本。

核心规则：`text` 用于检索，`kind` 和 `section_path` 用于结构归属，`continuation_*` 用于长内容顺序恢复；最终嵌入只对 `text` 生成向量。

## 评测

```bash
python scripts/run_eval.py regression    # 问答回归（基线 29/29，支持 --replay 零成本回放）
python scripts/run_eval.py retrieval     # 检索评测（基线 100% / MRR 0.92）
```

## 设计原则

- 能确定性算出来的事绝不交给模型（路由、校验、格式修复全部走代码）
- 无依据内容不出门（生成 → 校验 → 确定性守卫三层）
- 流式/非流式两条路径必须同步接入
- 每个修复的坑写进代码注释（根因 + 触发场景），git 提交信息写清实况
