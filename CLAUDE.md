# CLAUDE.md — enterprise-agent（企业 AI 问答/执行智能体 · FastAPI）

企业 AI 问答/执行智能体（数字员工 + 知识库 + 技能体系 + 管理后台），FastAPI 独立实现。
前端 `D:\project\rag-admin` 复用（协议兼容零改动，账号走同一 MySQL `sys_user` 表）。

## 目录结构

```
run.py                  # 启动入口：python run.py（--reload 热重载）→ 8080
app/
  config.py             # .env 加载（根目录 .env，不是 app/.env）
  logging_config.py     # 统一日志配置 setup()（print→logging 迁移后启动接线）
  llm.py                # LLM 注册表 + 降级链（chat_chain/chat_stream_chain/verify_chat）
                        # 手写 httpx OpenAI 兼容客户端；校验=智谱 GLM-4.6 独立裁判
  prompts.py            # 提示词集中管理（系统/校验/图谱收紧规则；改提示词必须跑回归）
  embeddings.py         # BGE-M3 嵌入 + SiliconFlow bge-reranker-v2-m3 重排
  milvus_store.py       # 混合检索（dense + sparse_bm25，RRF）
  guards.py             # 确定性守卫：NO_ANSWER 兜底 / 格式修复 / 术语核对 / 缺失术语删除 +
                        # 套话拦截 / 鉴伪断言删除 / 空标题清理 / CJK 清理
  demo_data.py          # 员工目录演示数据（8 人）
  employee_store.py     # 员工目录数据层（MySQL employee 表，30s TTL）
  service/agent.py      # 编排核心：answer() + answer_stream() 双路径（只做路由，不做校验）
  service/verify.py     # 答案校验链：小模型核查 + 熔断 + 确定性守卫顺序（勿调换）
  service/retrieval.py  # 预检索 + 上下文构建
  service/graph.py      # Neo4j 图谱-only 兜底（无向两跳遍历）
  service/docwizard.py  # 文档生成：触发词→标题→LLM→飞书云文档直出 + 多轮修改回写
  service/nlsql.py      # NL2SQL 数据面（query_schema 注册表 + 五关校验 + 中文标签）
  plan/sop.py           # SOP 状态机引擎（组件化：SopDefinition steps+handlers；零业务逻辑）
  plan/leave.py         # 请假 ApprovalDef（业务定义从 sop.py 外移）
  plan/approvals.py     # 审批类 SOP 定义（报销/出差/采购/合同用印，ApprovalDef 组件）
  plan/registry.py      # SOP 中央注册表（单一数据源：ALL_DEFS + 派生 TABLE_MAP/COL_LABELS…）
  plan/approval.py      # 审批动作层（通过/拒绝 → 审批记录 + 多级状态机推进 + 对话审批入口）
  learn/                # 自我优化：反馈回流 / 缺口聚类 / 用量追踪 / 校验熔断
  channels/feishu.py    # 飞书：lark-oapi WS + 卡片 + 图片 OCR + 通讯录同步 + 主动推送
  routers/              # auth / chat(SSE) / health / feedback / admin / ingestion
scripts/
  watchdog.py           # 探活 8080/7000，挂了自动拉起（独立 python 进程常驻）
  run_eval.py           # python scripts/run_eval.py regression|retrieval|all
  test_guards.py        # guards 形态测试矩阵 18 用例（改 guards 先跑）
  test_invariants.py    # 不变量套件 60 项（契约锁+入口模糊，全离线——改跨模块契约先跑）
  test_approval.py     # 审批动作层 23 项（真实 MySQL + 测试数据清理）
  test_routers.py      # 路由层 21 项（TestClient + 注入 token，跳过飞书 lifespan）
  test_parse.py        # 文档解析 27 项（离线 + 内存构造 docx）
  test_pipeline.py     # 摄入管线 27 项（切分核心 + mock 嵌入/Milvus）
eval/                   # regression_cases.json（29 用例）+ retrieval_baseline.json
```

## 新增/修改归属表（"我要加 X → 改哪里"）

| 我要加/改… | 改这里 | 不动（零代码） |
|---|---|---|
| 新审批业务（加班/调岗…） | `plan/xxx.py` 一个 `ApprovalDef` + `plan/registry.py` 加一行 import + 后台建表 + `oa_approval_flow` 配审批链 | `sop.py`/`approval.py`/`tools.py`/`admin.py` 引擎（业务解耦，改业务零代码） |
| 新可查询数据表（"问法不碰代码"） | `nlsql.py` `query_schema` 注册表加条目 + 列中文名 | NL2SQL 引擎 |
| 新守卫规则 | `guards.py` `GUARD_REGISTRY`（只读否决注册表） | 守卫顺序 |
| 新 LLM 模型/用途 | `config.py` `MODEL_REGISTRY`（.env 声明式） | 调用点（厂商参数随条目走） |
| 降级链加棒 | `llm.py` `_GEN_CHAIN`/`_VERIFY_CHAIN` + `MODEL_REGISTRY` 条目 | 调用方 |
| 新领域知识 | `app/domains/` 插件（`.env` `DOMAIN_PLUGINS` 开关） | 主流程 |
| 新提示词 | `prompts.py` + `render_prompt` 装配（严格插值 fail-loud） | |
| 新 HTTP 端点 | `routers/` 加路由 | |
| 新测试套件 | `scripts/test_*.py` + `.github/workflows/ci.yml` 加一行 | |
| 诊断日志 | `log.{info/warning/error}`（已迁离 print；`logging_config.setup()` 启动接线） | JSONL 审计日志（`_audit`/`_log` 那套保持不动） |

> 改动循环本身（加新"类"的扩展机制）时，同步更新本表 + 目录树。

## 运行与测试

- 启动：`python run.py`（8080）；`scripts/watchdog.py` 单独常驻（系统里有两个 python 进程是正常的；watchdog 只管理 8080——OA 服务 7000 由用户手动控制）
- 依赖：OA 服务（7000，数据源）、Milvus、Neo4j（bolt://10.211.55.5:7687）、MySQL（sys_user 等共享表）、飞书应用
- 健康检查：`curl localhost:8080/healthz`
- 回归：`python scripts/run_eval.py regression`（基线 15/15）
- 检索评测：`python scripts/run_eval.py retrieval`（基线 100% / MRR 0.92）

## 制度（2026-08-19 借鉴 deepseek-harness 落地，见 docs/dsh-highlights.md）

- **改行为的变更必附决策记录**（docs/decisions/，四段式 Problem→Decision→Alternatives→Consequences）
- **重大实况修复必附 postmortem**（docs/postmortem/，硬性要求：每篇产生至少一条防回归测试或规则）
- **大改后必跑**：`python scripts/smoke_qa.py`（真实链路冒烟+外部验证）→ `python scripts/test_matrix_human.py`（变体矩阵）→ 回归 → 不变量
- **验证外部世界，而非自我报告**：落库/状态类断言用 verify_sql 查真实库，不只用 answer_contain 关键词

## 设计原则（不可违反）

0. **要智能（用户 2026-08-19 暴怒定调，最高优先级）**：
   - 智能体该会的必须会：读=查库问法不碰代码（**含跨表拼接**——"李娜部门的报销"这类要先定位部门再查数）；写=SOP 直连库；多轮追问理解（指代绑定最近实体）；答案全中文口径（列名/字段名不露英文，NULL 显示"未录入"）
   - 交付前穷举同类问法矩阵自测全绿才交付——**用户不是测试员**，每个用户实况=自测没做够
   - 改一类机制时把自己当用户：把该类所有问法形态（含错数据、无记录、跨表、多实体、空值）全部想一遍测一遍，修净一次交付

1. **能确定性算出来的事绝不交给模型**：意图路由用正则规则表，不用路由模型（模型路由已验证不可靠）
2. **无依据内容不出门**：生成 → 小模型校验（缺失术语锚定 + 过度删除回退 + 自动熔断）→ 确定性守卫（guards.py：缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复，graph_only 收紧规则注入校验提示词）
3. **流式/非流式两条路径必须同步接入**：`answer()` 和 `answer_stream()` 都有的逻辑漏接出过三次事（图谱兜底、docwizard、图谱分支的文件上下文）
4. **每个修复的坑写进代码注释**（根因 + 触发场景），git 提交信息写清实况
5. **修改要更高维度看问题（用户 2026-08-18 定调）：改的是"类"不是"实例"——修改是加技能（一类机制），不是打补丁（一个实况）**。实例驱动方案发现，方案必须覆盖一类；新形态进对应语义阶段/词表配置，而非新增散规则（guards 重建引擎、追问统一解析、词汇自学习闭环都是这个模式的产物）

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
- git 提交信息含引号/括号走 `-F` 文件传递（内联多行信息会被 shell 截断，pathspec 报错实况）
- 回归单条挂先判两类再动手：① 外部 API 抖动（重跑该条确认，别急着改代码）；② 断言前提漂移（KB 内容变了/断言锁的是旧错误行为——翻断言锁正确行为，不删用例）
- 不要用"政务/纪检/信访"类措辞，用"合规/企业"等中性词

## 状态（2026-08-19）

- 去 OA 化完成（OA 服务 7000 停用，数据同库 rag_service，零迁移）：
  - 能力总线退役：员工3条+OA报销/经营2条规则全并入 NL2SQL 数据面（biz_expense/biz_metric_daily 注册 query_schema），capabilities/intent 模块删除
  - 身份体系统一：employee 表=员工主数据（dept_id→sys_dept 外键，6 部门），员工-角色绑定指向 employee（sys_employee 演示账号体系退役）
  - 审批三件套：oa_leave_application 复用 + oa_approval_flow 扩 4 业务审批链（expense/travel/purchase/contract）+ oa_approval_record
  - 四审批 SOP：报销/出差/采购/合同用印（ApprovalDef 组件化，加新审批=加一个 def；共享查名册/提取/校验/确认闸门/审批人解析/审计）
  - 审批闭环自建：飞书对话审批（同意/拒绝+单号）+ 飞书通知推送 + 后台审批看板页
  - 飞书通讯录同步：启动自动拉取 upsert feishu_employee（拉得 zzp/张志鹏 2 账号，绑定走后台账号身份页）
- 写业务动作直写本库申请表（业务库即 rag_service），审批人解析=oa_approval_flow 配置（部门负责人/角色Key）；读走 NL2SQL——问法不碰代码
- 回归 26/26、不变量 53/53、guards 矩阵 18/18

## 状态（2026-08-18）

- 功能全部完成：回归 23/23、检索 100%/MRR 0.92，飞书真机验证过（回答卡/数据卡/云文档/OCR）
- 2026-08-18 上午批次：① 跨文档拼接防线（find_mismatched_numbers 句级出处绑定，body 剥全部数字定位 + 校验提示词 spliced 段 + 高置信硬删）；② 校验换智谱 GLM-4.6（探针 scripts/probe_glm.py 取证：反例判定更果断、价格更优）+ LLM 降级链机制（chat_chain/chat_stream_chain，生成链 main→main_fallback 异厂商故障隔离，校验链 verify→verify_fallback 智谱挂了回退 qwen3-max；厂商参数 extra/超时 timeout 随注册表条目声明，调用点不传厂商参数）；③ 领域插件化 app/domains/（DomainPlugin 协议，行业知识与主流程解耦，.env DOMAIN_PLUGINS 开关）；④ 不变量套件 scripts/test_invariants.py 38 项（全离线契约锁+入口模糊）——首跑抓出万/亿单位缺口、route 追问口径、eval 会话残史污染三处并治类；⑤ 回归中修：主力 60s 挂起即降级（曾 120s）、embed 内建重试、图谱兜底确定性相关性闸门（实质词没命中只中短词→不走兜底）
- 校验链已按"兜底句式清理 → 缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复"顺序接线（含延伸服务套话拦截、提示注入拦截、graph_only 收紧规则注入校验提示词）
- 2026-08-17 晚批次（用户全权委托）：文档解析「## 标题」块边界真实现；Milvus 检索失败日志；提示注入防护（查询级拦截+上下文级清洗）；人机协同（低把握答案自动进审核队列 + 请假 SOP 提交前确认）；工具注册表（skill 表开关 TTL 生效）；多轮对话历史（指代消解+历史作答）；检索 ACL 过滤（doc_acl → expr，user 标识从飞书/Web 贯通审计与反馈）
- 2026-08-17 深夜批次：① Milvus 全量迁 MilvusClient API（pymilvus 3.0，requirements 锁 >=3.0.1,<3.1）+ 服务端重启自动重连 + document_id 标量索引 + client 超时 + 检索重试；② 后台首页监控全真实数据（dashboard noAnswer/feishu/adoptions、usage JSONL 多日、kb-health 双计数修复、kb-stats 真实探活/4096）；③ 借鉴 ragflow 落地：LLM 工厂注册表（MODEL_REGISTRY，加用途改 .env）、PDF 版面识别（字号判标题「## 」）、图片 OCR chunk（PDF/docx 内嵌图 → qwen-vl，扫描件可入库）、CSV 支持（编码回退+分隔符自检）、PDF 乱码拒绝、SOP 组件化（steps+handlers+通用执行器）；④ 上传链路四修：upload 端点线程池化（曾阻塞事件循环）、sha256 去重注册表、图谱构建后台化、embed_batch 批量嵌入
- 验收已完成（用户上传文档后）：回归 23/23 + 检索评测 100%/MRR 0.9167 + guards 矩阵 18/18 + 图谱构建正常
- 待办：① 部署上线（用户明确先不用管）；② 飞书真机验证 ACL 身份映射（需给用户账号绑 mobile+employee_id，通讯录 API 手机号匹配链路真机跑一遍）；③ sweep 全量干净结果（r5 挂后台，结果在 logs/sweep_20260818_r5.log）
