# 术语表（Glossary）

项目核心概念的唯一规范术语。文档和接口用 canonical term，禁用 alias。

| Canonical Term | 代码字段/模块 | 含义 | 禁用别名 |
|---|---|---|---|
| **会话（Conversation）** | `conversation_id` | 用户与智能体的一次连续对话上下文，跨多轮 | ~~session~~、~~chat session~~ |
| **渠道会话标识（Chat ID）** | `chat_id` | 飞书/IM 渠道分配的会话 ID，用于消息路由和文件上下文 | ~~session_id~~（会话 ID 是 conversation_id） |
| **请求（Request）** | `request_id`（ContextVar） | 单次用户输入到答案返回的处理链路，HTTP 或飞书消息 | ~~trace_id~~（trace_id 是审计记录的 ID，由 request_id 复用或生成） |
| **审批流（Approval Flow）** | `oa_approval_flow` 表 | 按业务类型配置的多级审批链（level → approver） | ~~审批链~~（用"审批流"统一） |
| **审批动作（Approval Action）** | `act_on_application()` | 通过/拒绝某一申请单的具体操作 | ~~审批操作~~ |
| **SOP（业务流程）** | `SopDefinition` | 状态机驱动的业务流程定义（请假/报销/出差等） | ~~skill~~、~~tool~~ |
| **工具（Tool）** | `tools.TOOLS` / `Tool` | 可被 skill 表开关管理的能力（doc_wizard 等） | ~~skill~~（skill 是后台开关记录，不是能力本身） |
| **技能开关记录（Skill Record）** | `skill` 表 | 后台管理界面中工具的启用/禁用状态记录 | ~~tool~~ |
| **守卫（Guard）** | `guards.GUARD_REGISTRY` | 确定性的只读否决规则（格式修复/术语核对/套话拦截等） | ~~校验~~（校验是 LLM 核查，守卫是确定性规则） |
| **校验（Verify）** | `verify.verify()` | 小模型对答案的事实核查（与守卫不同：校验是 LLM，守卫是确定性） | ~~守卫~~ |
| **降级链（Fallback Chain）** | `llm.chat_chain` / `_GEN_CHAIN` | 按用途顺序尝试多个模型条目，前一棒异常接下一棒 | ~~重试链~~（重试是同棒重试，降级是换棒） |
| **待确认单（Pending）** | `sop_pending` 表 | SOP 提交前的人机协同闸门——用户回复"确认"才落库 | ~~草稿~~、~~暂存~~ |
| **审计事件（Audit Event）** | `audit_event` 表 / `_record_audit()` | 每次答案的完整审计记录（query/answer/route/timing/request_id） | ~~日志~~（日志是 stdout，审计是结构化落库） |
| **摄入（Ingestion）** | `pipeline.ingest()` | 文档解析→切分→嵌入→向量库入库的完整链路 | ~~上传~~（上传是 HTTP 端点，摄入是处理链路） |
| **领域插件（Domain Plugin）** | `app/domains/` | 行业知识与主流程解耦的可选插件（.env DOMAIN_PLUGINS 开关） | ~~模块~~ |
