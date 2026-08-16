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

## 待迁移（路线图）

1. 飞书桥（WS 长连接 + 卡片回复 + 图片 OCR + 文档生成云文档）
2. MCP 技能注册/工具装配 + 请假 SOP
3. 管理 API 全量（审计查询/看板/告警/评测/ACL）+ 审计落库
4. Neo4j 图谱（graph-only 场景）
5. 文档摄入管线（上传/切分/嵌入）
6. 看门狗适配 + 回归评测集

## 与原版的关键差异（为什么更快）

- 无编译/无 jar 锁：改完 `--reload` 秒生效
- AI 组件直连 HTTP：省掉 sidecar 进程与 LangChain4j 两层间接
- 确定性守卫全部在 `app/guards.py` + `app/service/agent.py`，改一处即全局生效
