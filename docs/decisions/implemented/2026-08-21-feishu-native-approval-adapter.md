# 决策：以本地审批为事实源接入飞书原生审批适配层

状态：implemented

## Problem

现有审批申请、审批链和状态机全部在本地数据库，飞书仅发送纯文本通知，审批人通过“同意/拒绝 + 单号”操作。用户希望在飞书原生审批界面处理申请，但仓库没有原生实例 API、tenant token、审批模板 code 或状态事件接线。直接复制一套飞书状态机会造成后台、本地对话和飞书原生三套状态互相覆盖。

## Decision

先建立默认关闭的飞书原生审批适配层，保持本地审批为最终事实源：

1. 新增 `app/integrations/feishu_approval.py`，独立封装 `tenant_access_token` 获取/缓存、原生实例创建、模板 code 配置、状态映射和 SDK 状态事件解析；不复用 app access token，不在 provider 内复制本地状态机。
2. 新增 `FEISHU_APPROVAL_ENABLED`、各业务 `FEISHU_APPROVAL_CODE_*`、verification token/encrypt key 配置。开关关闭或模板 code 缺失时不请求飞书，原有本地审批继续工作。
3. 本地申请提交成功后，在 `app/plan/approvals.py` 以旁路方式尝试创建飞书实例；外部失败不回滚本地申请，也不伪造“原生审批已创建”。成功返回 instance code/link，后续映射表和补偿机制独立落地。
4. 在飞书 WS 启动时，开关开启才注册 SDK 的审批实例状态变更事件；当前 handler 只做安全解析和日志，不直接改本地状态。待租户模板、外部实例映射、event_id 幂等和统一审批命令层完成后，再打开状态同步。
5. 飞书普通通道显式消费 `FEISHU_ENABLED`；原生审批使用独立开关和凭据边界。离线 provider 契约测试纳入 CI，真实飞书租户验证为显式发布门。

## Alternatives considered

1. 继续只发飞书文本并要求回复单号：无需外部配置，但不能提供飞书原生审批体验。
2. 让飞书原生审批成为事实源：需要把本地申请表/审批记录改为投影，冲击后台和对话审批，且当前系统已有成熟本地状态机，不采用。
3. 直接在当前 `act_on_application()` 中消费飞书回调：该函数没有外部 event_id、instance code、显式级别和并发幂等，容易重复写记录或错级，因此先保留适配层并把统一命令/映射作为后续接入门。
4. 使用现有 app access token 调审批 API：原生审批创建要求 tenant token，token 类型混用会在权限/接口层失败，因此不采用。

## Consequences

+ 未配置飞书模板或开关关闭时，本地审批行为保持兼容；不会因新增外部依赖阻塞提交。
+ 已具备原生审批 API 的安全边界、tenant token 缓存、模板 code 入口、状态枚举解析和 SDK 事件注册骨架。
+ 外部 API 成功返回的实例信息可以作为后续映射/补偿链的输入。
− 当前版本尚未将原生状态事件写回本地状态，不能宣称飞书原生审批闭环已上线。
− 仍需在飞书测试租户确认五类 approval_code、表单字段、节点策略、应用权限和事件订阅；未配置时 provider 会 fail-closed。
− 外部实例/事件持久化映射、统一审批命令的显式级别与 event_id 幂等、回调验签和多级同步通知是后续必须完成的交付项。
