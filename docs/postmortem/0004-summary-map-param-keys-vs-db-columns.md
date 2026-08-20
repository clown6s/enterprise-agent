# 事故复盘（postmortem）0004：SUMMARY_MAP 用参数键而非 DB 列名，摘要全空

状态：已解决

## 摘要

业务解耦时 `registry.SUMMARY_MAP` 用 `d.fields.keys()`（camelCase 参数键如
`leaveType`、`startTime`）做摘要列，但 `summary_text()` 拿这些键去查 DB 行
（snake_case 列名如 `leave_type`、`start_time`）——全查不到，审批通知/卡片摘要变空。
同时 `fields.keys()` 是全字段而非精选子集（原 `_SUMMARY_COLS` 是精选 3 列）。
补覆盖率时 `test_approval.py` 的 `summary_text` 测试浮现。

## 影响

飞书审批通知卡片中「📄 来源」段无内容——审批人收到的通知缺少申请摘要
（请假类型/天数/事由等），只有申请人姓名和单号。信息密度下降但不崩溃。

## 时间线

- 2026-08-20：业务解耦 commit（efdc4a2），`registry.py` 的 `SUMMARY_MAP` 定义为
  `{d.business_type: tuple(d.fields.keys()) for d in ALL_DEFS}`。
- 同日：`approval.py` 的 `summary_text()` 改为从 `registry.SUMMARY_MAP` 取列名，
  但 `fields.keys()` 返回的是 camelCase 参数键（`leaveType`），不是 DB 列名（`leave_type`）。
- 同日：补覆盖率时 `test_approval.py` 写 `summary_text` 测试 → 5 条全失败
  （摘要不含期望的中文标签如"请假类型"）。
- 修复：`ApprovalDef` 加 `summary_cols` 字段（精选 DB 列名子集）+ `col_labels`
  （列名→中文）；`SUMMARY_MAP` 改用 `d.summary_cols`；`summary_text()` 改用
  `registry.COL_LABELS`。

## 根因

1. **camelCase vs snake_case 的命名鸿沟**：`ApprovalDef.fields` 的键是**提取参数键**
   （给 LLM 提取用，如 `leaveType`），值是 `(db_column, converter)` 元组——
   参数键和 DB 列名**不是一回事**。`fields.keys()` 返回参数键，但 `summary_text()`
   需要的是 DB 列名（值的第一个元素）。
2. **"派生字典"重构时没验证语义**：从 `approval.py` 硬编码 `_SUMMARY_COLS` 改为
   `registry.SUMMARY_MAP` 派生时，只看了"键名对不对"（business_type），没看
   "值的内容对不对"（参数键 vs DB 列名 vs 精选子集）。
3. **原 `_SUMMARY_COLS` 是精选 3 列，`fields.keys()` 是全 6 列**——不只是键名错，
   语义也变了（精选子集 → 全字段），摘要会变冗长。

## 已添加的防护措施

1. **`ApprovalDef` 加 `summary_cols` + `col_labels` 字段**：显式声明精选 DB 列名子集
   + 列名→中文映射，不再从 `fields.keys()` 隐式派生。
2. **`registry.COL_LABELS`**：从各 `def.col_labels` 合并的全局列名→中文映射，
   `summary_text()` 从此查 registry 不用本地字典。
3. **test_approval.py 的 `summary_text` 测试 6 条**：5 业务 + 未知业务空摘要，
   覆盖精选列正确性 + 中文标签 + 不含未精选列。

## 教训

1. **重构派生字典时要验证值的语义**——不只看键名（`business_type`）对不对，
   要看值的内容（参数键 vs DB 列名）和语义（全字段 vs 精选子集）。
2. **camelCase 参数键和 snake_case DB 列名是两个世界的**——`fields` 的键和值的
   第一个元素不是一回事，不要混用。
3. **"精选子集"不能从"全量"隐式派生**——原 `_SUMMARY_COLS` 是人工精选 3 列，
   `fields.keys()` 是全 6 列，派生丢失了"精选"语义。
