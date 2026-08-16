# enterprise-agent

企业 AI 问答/执行智能体（数字员工 + 知识库 + 技能体系 + 管理后台）。FastAPI 实现，模型链路直连
OpenAI 兼容 API（DeepSeek 主力 + qwen-plus 校验层），向量检索走 Milvus（dense + BM25 混合，RRF）。

## 运行

```bash
pip install -r requirements.txt
python run.py            # http://localhost:8080（加 --reload 热重载）
```

管理前端复用 rag-admin（协议兼容，零改动）：登录 admin / Admin@2026（账号在 MySQL `rag_service.sys_user`）。
依赖：OA 服务（7000，数据源）、Milvus、Neo4j、MySQL；`scripts/watchdog.py` 探活 8080/7000 自动拉起。

## 功能

| 模块 | 说明 |
|---|---|
| 意图路由 | 规则表驱动（员工目录/OA 数据直答、闲聊、追问澄清、知识问答），不调路由模型 |
| 知识问答 | BGE-M3 嵌入 + Milvus 混合召回 + bge-reranker-v2-m3 精排 → LLM 生成 → 小模型校验（缺失术语锚定 + 过度删除回退 + 自动熔断）→ 确定性守卫（缺失术语硬删除/鉴伪断言/空标题清理/格式修复） |
| 图谱兜底 | 检索空手 → Neo4j 无向两跳 → 图谱-only 收紧规则生成 + 校验 |
| 执行层 | 请假 SOP 状态机（查员工 → 提取 → 校验 → 提交 OA → 回执），每步落执行审计 |
| 文档生成 | 一句话直达飞书云文档 + 多轮修改回写 + 表单卡片兜底 |
| 飞书渠道 | lark-oapi WS 长连接 + 回答卡片（👍👎 反馈闭环）+ 数据图表卡（VChart）+ 图片 OCR |
| 摄入管线 | txt/md/docx/pdf 解析 → 切分 → 入库 + 图谱构建，全程落日志 |
| 自我优化 | 反馈回流 / 缺口聚类 / 用量追踪 / 参数自适应（只接受不劣化变更）/ 校验熔断 |
| 管理后台 | 仪表盘/审计/评测/数字员工/技能/SOP/审核/权限/用量等 API（rag-admin 复用） |

## 评测

```bash
python scripts/run_eval.py regression    # 问答回归（基线 15/15）
python scripts/run_eval.py retrieval     # 检索评测（基线 100% / MRR 0.92）
```

## 设计原则

- 能确定性算出来的事绝不交给模型（路由、校验、格式修复全部走代码）
- 无依据内容不出门（生成 → 校验 → 确定性守卫三层）
- 流式/非流式两条路径必须同步接入
- 每个修复的坑写进代码注释（根因 + 触发场景），git 提交信息写清实况
