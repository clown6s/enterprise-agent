# 决策：去 OA 化——审批业务直连本库

状态：implemented

## Problem

请假/报销等审批动作原走 OA 服务（7000 接口）。用户 2026-08-19 定调「从现在开始就不用 oa」：OA 是多余的中间层——业务数据本来就在智能体的 rag_service 库（oa_leave_application/oa_approval_flow/oa_approval_record/biz_expense 全同库），且「读也走接口，一个问题要写一个接口」违背「数据在表里问法就不该碰代码」。

## Decision

- **读**：员工/报销/经营问答全部并入 NL2SQL 数据面（能力总线退役，5 条种子规则删除）
- **写**：审批申请直写本库申请表（SOP 提交=INSERT，审批人按 oa_approval_flow 配置确定性解析）；审批闭环自建（飞书对话审批「同意/拒绝 单号」+ 后台看板）
- **身份统一**：employee 表=员工主数据（dept_id→sys_dept 外键），sys_employee 演示体系退役
- 审批三件套扩展：4 张申请表（报销/出差/采购/合同用印）+ 4 条审批链配置

## Alternatives considered

1. OA 接口保留、新问法写新接口（用户否决：「一个问题要写一个接口」）
2. 同步 OA 数据到本地表（发现同库后无需同步）
3. 通用申请表+JSON 字段（否决：字段可查性差，NL2SQL 查不了 JSON 内字段）

## Consequences

+ 问法不碰代码：新数据源=后台注册表；新审批类型=ApprovalDef+审批链配置
+ 数据零迁移、零同步
− 审批界面/通知需自建（已建：飞书推送+对话审批+后台页）
− 申请单状态机与审批人解析的正确性依赖审批链配置数据（配置错=流程错）
