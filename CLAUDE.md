# CLAUDE.md — enterprise-agent（企业 AI 问答/执行智能体 · FastAPI）

企业 AI 问答/执行智能体（数字员工 + 知识库 + 技能体系 + 管理后台），FastAPI 独立实现。
前端 `D:\project\rag-admin` 复用（协议兼容零改动，账号走同一 MySQL `sys_user` 表）。

## 目录结构

```
run.py                  # 启动入口：python run.py（--reload 热重载）→ 8080
app/
  config.py             # .env 加载（根目录 .env，不是 app/.env）
  llm.py                # DeepSeek 主力 + qwen-plus 校验层（手写 httpx OpenAI 兼容客户端）
  prompts.py            # 提示词集中管理（系统/校验/图谱收紧规则；改提示词必须跑回归）
  embeddings.py         # BGE-M3 嵌入 + SiliconFlow bge-reranker-v2-m3 重排
  milvus_store.py       # 混合检索（dense + sparse_bm25，RRF）
  guards.py             # 确定性守卫：NO_ANSWER 兜底 / 格式修复 / 术语核对 / 缺失术语删除 +
                        # 套话拦截 / 鉴伪断言删除 / 空标题清理 / CJK 清理
  demo_data.py          # 员工目录演示数据（8 人）
  oa_client.py          # OA 数据源客户端（报销/经营数据；必须 Bearer 前缀）
  intent/rules.py       # 意图规则表：新增确定性意图 = 加一行 IntentRule
  service/agent.py      # 编排核心：answer() + answer_stream() 双路径（只做路由，不做校验）
  service/verify.py     # 答案校验链：小模型核查 + 熔断 + 确定性守卫顺序（勿调换）
  service/retrieval.py  # 预检索 + 上下文构建
  service/graph.py      # Neo4j 图谱-only 兜底（无向两跳遍历）
  service/docwizard.py  # 文档生成：触发词→标题→LLM→飞书云文档直出 + 多轮修改回写
  plan/sop.py           # 请假 SOP 状态机（查员工→提取→校验→提交OA→回执）
  learn/                # 自我优化：反馈回流 / 缺口聚类 / 用量追踪 / 校验熔断
  channels/feishu.py    # 飞书：lark-oapi WS + 卡片 + 图片 OCR + docx
  routers/              # auth / chat(SSE) / health / feedback / admin / ingestion
scripts/
  watchdog.py           # 探活 8080/7000，挂了自动拉起（独立 python 进程常驻）
  run_eval.py           # python scripts/run_eval.py regression|retrieval|all
eval/                   # regression_cases.json（15 用例）+ retrieval_baseline.json
```

## 运行与测试

- 启动：`python run.py`（8080）；`scripts/watchdog.py` 单独常驻（系统里有两个 python 进程是正常的）
- 依赖：OA 服务（7000，数据源）、Milvus、Neo4j（bolt://10.211.55.5:7687）、MySQL（sys_user 等共享表）、飞书应用
- 健康检查：`curl localhost:8080/healthz`
- 回归：`python scripts/run_eval.py regression`（基线 15/15）
- 检索评测：`python scripts/run_eval.py retrieval`（基线 100% / MRR 0.92）

## 设计原则（不可违反）

1. **能确定性算出来的事绝不交给模型**：意图路由用正则规则表，不用路由模型（模型路由已验证不可靠）
2. **无依据内容不出门**：生成 → 小模型校验（缺失术语锚定 + 过度删除回退 + 自动熔断）→ 确定性守卫（guards.py：缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复，graph_only 收紧规则注入校验提示词）
3. **流式/非流式两条路径必须同步接入**：`answer()` 和 `answer_stream()` 都有的逻辑漏接出过两次事（图谱兜底、docwizard）
4. **每个修复的坑写进代码注释**（根因 + 触发场景），git 提交信息写清实况

## 关键坑（改代码前必读）

- **不要用 bash heredoc 写 Python 代码**：`\n` 转义和大括号会被 mangle——用 Edit/Write 工具
- `str.format` 模板里 JSON 示例的大括号必须转义 `{{}}`
- SSE 多行数据必须拆成多个 `data:` 行
- OA 接口要 `Bearer ` 前缀
- 飞书卡片 schema 分裂：answer 卡用 1.0 信封（顶层 elements，note+action 有效）；data 卡用 2.0（VChart+表格，不支持 note/action，note 用 markdown）
- docx 块 API 必须带 `document_revision_id=-1`
- guards.py 格式恢复规则要加负向 lookahead 防双重编号（"1. 感知工具：" 被重复编号的实况）
- 校验层会洗掉列表前缀（"**标题**："下的"- 条目"变裸行）还会给条目补句号：guards 3.5 步确定性恢复，条目判定=无内部句末标点+行尾至多一个句号+≤60字；散文防线是"行内多句/长行"不是"有句号"（曾误用句号当防线导致实况漏过）
- collapse_empty_headings 判定空标题别把 `\d+[.、]` 编号行加进去（"**标题**：\n1. 内容"是正常结构）
- 凭据（MySQL root 密码含特殊字符）：走文件传递，**绝不内联到 shell 命令**
- 不要用"政务/纪检/信访"类措辞，用"合规/企业"等中性词

## 状态（2026-08-17）

- 功能全部完成：回归 15/15、检索 100%/MRR 0.92，飞书真机验证过（回答卡/数据卡/云文档/OCR）
- 校验链已按"缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复"顺序接线（含延伸服务套话拦截）
- 遗留（用户明确延期）：① 部署上线；② 飞书通讯录 scopes 需用户在飞书后台勾选发布（身份层暂用 demo_data 8 人）
