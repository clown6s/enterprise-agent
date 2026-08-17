# CLAUDE.md — enterprise-agent（企业 AI 问答/执行智能体 · FastAPI）

企业 AI 问答/执行智能体（数字员工 + 知识库 + 技能体系 + 管理后台），FastAPI 独立实现。
前端 `D:\project\rag-admin` 复用（协议兼容零改动，账号走同一 MySQL `sys_user` 表）。

## 目录结构

```
run.py                  # 启动入口：python run.py（--reload 热重载）→ 8080
app/
  config.py             # .env 加载（根目录 .env，不是 app/.env）
  llm.py                # qwen3-max 主力 + qwen-plus 校验层（手写 httpx OpenAI 兼容客户端）
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

- 启动：`python run.py`（8080）；`scripts/watchdog.py` 单独常驻（系统里有两个 python 进程是正常的；watchdog 只管理 8080——OA 服务 7000 由用户手动控制）
- 依赖：OA 服务（7000，数据源）、Milvus、Neo4j（bolt://10.211.55.5:7687）、MySQL（sys_user 等共享表）、飞书应用
- 健康检查：`curl localhost:8080/healthz`
- 回归：`python scripts/run_eval.py regression`（基线 15/15）
- 检索评测：`python scripts/run_eval.py retrieval`（基线 100% / MRR 0.92）

## 设计原则（不可违反）

1. **能确定性算出来的事绝不交给模型**：意图路由用正则规则表，不用路由模型（模型路由已验证不可靠）
2. **无依据内容不出门**：生成 → 小模型校验（缺失术语锚定 + 过度删除回退 + 自动熔断）→ 确定性守卫（guards.py：缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复，graph_only 收紧规则注入校验提示词）
3. **流式/非流式两条路径必须同步接入**：`answer()` 和 `answer_stream()` 都有的逻辑漏接出过三次事（图谱兜底、docwizard、图谱分支的文件上下文）
4. **每个修复的坑写进代码注释**（根因 + 触发场景），git 提交信息写清实况

## 关键坑（改代码前必读）

- **不要用 bash heredoc 写 Python 代码**：`\n` 转义和大括号会被 mangle——用 Edit/Write 工具
- `str.format` 模板里 JSON 示例的大括号必须转义 `{{}}`
- SSE 多行数据必须拆成多个 `data:` 行
- OA 接口要 `Bearer ` 前缀
- 飞书卡片 schema 分裂：answer 卡用 1.0 信封（顶层 elements，note+action 有效）；data 卡用 2.0（VChart+表格，不支持 note/action，note 用 markdown）
- docx 块 API 必须带 `document_revision_id=-1`
- guards.py 格式恢复规则要加负向 lookahead 防双重编号（"1. 感知工具：" 被重复编号的实况）
- 校验层会洗掉列表前缀（"**标题**："下的"- 条目"变裸行）还会给条目补句号：guards 阶段5 确定性重建，条目判定=无内部句末标点+行尾至多一个句号或分号+≤59字；散文防线是"行内多句/长行"不是"有句号"（曾误用句号当防线导致实况漏过）
- **fix_output 是格式重建引擎不是恢复器**（用户 2026-08-17 明确：别打补丁）：不信任上游格式，按语义阶段（1-11）重建，幂等；新形态进对应阶段而非新增散规则；形态测试矩阵 scripts/test_guards.py 18 用例护体——改 guards 先跑矩阵再回归
- 列表风格：条目统一 "- "、不要数字编号（用户 2026-08-17 明确；飞书会把多级编号渲染成连续 1-10）——fix_output 4 步确定性归一（编号正则带负向 lookahead 防误伤 1.5倍/日期行），改回编号需同步 prompts.py 规则 4
- 追问是一类问题不是个案：有历史时指代/衔接/省略三类追问统一走 `_resolve_query` 改写补全再检索；检索空手时只有元问题（什么意思/再说一遍）能用历史作答（相关性闸门要豁免），其余一律"答没有"——**不要用历史凑数**（"怎么发生的"被套"发生机制"标题重炒分类内容的实况）
- collapse_empty_headings 判定空标题别把 `\d+[.、]` 编号行加进去（"**标题**：\n1. 内容"是正常结构）
- 凭据（MySQL root 密码含特殊字符）：走文件传递，**绝不内联到 shell 命令**
- 不要用"政务/纪检/信访"类措辞，用"合规/企业"等中性词

## 状态（2026-08-17）

- 功能全部完成：回归 17/17、检索 100%/MRR 0.92，飞书真机验证过（回答卡/数据卡/云文档/OCR）
- 校验链已按"兜底句式清理 → 缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复"顺序接线（含延伸服务套话拦截、提示注入拦截、graph_only 收紧规则注入校验提示词）
- 2026-08-17 晚批次（用户全权委托）：文档解析「## 标题」块边界真实现；Milvus 检索失败日志；提示注入防护（查询级拦截+上下文级清洗）；人机协同（低把握答案自动进审核队列 + 请假 SOP 提交前确认）；工具注册表（skill 表开关 TTL 生效）；多轮对话历史（指代消解+历史作答）；检索 ACL 过滤（doc_acl → expr，user 标识从飞书/Web 贯通审计与反馈）
- 2026-08-17 深夜批次：① Milvus 全量迁 MilvusClient API（pymilvus 3.0，requirements 锁 >=3.0.1,<3.1）+ 服务端重启自动重连 + document_id 标量索引 + client 超时 + 检索重试；② 后台首页监控全真实数据（dashboard noAnswer/feishu/adoptions、usage JSONL 多日、kb-health 双计数修复、kb-stats 真实探活/4096）；③ 借鉴 ragflow 落地：LLM 工厂注册表（MODEL_REGISTRY，加用途改 .env）、PDF 版面识别（字号判标题「## 」）、图片 OCR chunk（PDF/docx 内嵌图 → qwen-vl，扫描件可入库）、CSV 支持（编码回退+分隔符自检）、PDF 乱码拒绝、SOP 组件化（steps+handlers+通用执行器）；④ 上传链路四修：upload 端点线程池化（曾阻塞事件循环）、sha256 去重注册表、图谱构建后台化、embed_batch 批量嵌入
- **Milvus 当前为空**（探针数据已清）。验收待用户上传文档后执行：回归 22/22 + 检索评测 + 图谱构建检查（回归不能提前跑——用户明确：文档没上传跑回归是白跑）
- 遗留（用户明确延期）：① 部署上线；② 飞书通讯录 scopes 需用户在飞书后台勾选发布（身份层暂用 demo_data 8 人；acl.resolve_employee 恒 None 待映射）
