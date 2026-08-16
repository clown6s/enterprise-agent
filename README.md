# enterprise-agent

WorkBuddy 内网版的 Python 重写（原 Java 版在 `D:\project\rag-service`，本工程逐模块迁移）。

## 运行

```bash
pip install -r requirements.txt
python run.py            # http://localhost:8080（加 --reload 热重载）
```

管理前端复用现有 rag-admin（`http://localhost:5174`，协议兼容，零改动）：
登录 admin / Admin@2026（账号在 MySQL `rag_service.sys_user`，与原版共用）。

## 已迁移（阶段 1）

| 模块 | 说明 |
|---|---|
| 配置 | .env → app/config.py（与 Java 版同一份 .env） |
| LLM | DeepSeek 对话/流式（OpenAI 兼容，httpx 手写） |
| 向量检索 | BGE-M3 嵌入 + Milvus 召回 + bge-reranker-v2-m3 精排（直接调硅基流动，不再需要 sidecar） |
| 确定性守卫 | 员工目录直查、OA 报销/经营数据直查（带图表数据）、流程词排除 |
| 防编造 | 小模型校验（qwen-plus）+ 鉴伪断言删除 + 图谱回声剥除 + 格式修复 |
| 端点 | /healthz、/api/rag/admin/auth/*、/api/rag/agent/chat(+stream SSE，前端兼容) |

## 已迁移（全路线图）

| 阶段 | 状态 |
|---|---|
| P2 评测基座 | ✅ 回归 15/15 + 参数自适应 |
| P3 反馈环 | ✅ 采纳回流 + 负反馈锁用例 |
| 混合检索 | ✅ dense+BM25，检索 87.5% 超 Java 基线 |
| 自我优化收口 | ✅ 缺口发现 + 用量追踪 + 校验熔断 |
| P4 执行层 | ✅ 请假 SOP + 执行审计 + 防编造成功 |
| P5 飞书渠道 | ✅ WS 长连接 + 文本问答 + 图片 OCR（真机验证） |
| 摄入管线 | ✅ txt/md/docx/pdf 解析 + 切分 + 入库 + 文档管理 API |
| Neo4j 图谱 | ✅ 图谱-only 兜底（检索空时图作答） |
| 看门狗 | ✅ 适配 Python 服务，实测自动拉起 |

## 待补（遗留小项）

- 摄入时图谱构建（Python 摄入的文档暂不建图节点；现有 35k 实体来自 Java 版摄入）
- 飞书卡片回复（当前文本回复；👍👎 按钮卡片后续加）
- 文档生成云文档（攻略/通知直出，依赖飞书 docx API）

## 与原版的关键差异（为什么更快）

- 无编译/无 jar 锁：改完 `--reload` 秒生效
- AI 组件直连 HTTP：省掉 sidecar 进程与 LangChain4j 两层间接
- 确定性守卫全部在 `app/guards.py` + `app/service/agent.py`，改一处即全局生效
