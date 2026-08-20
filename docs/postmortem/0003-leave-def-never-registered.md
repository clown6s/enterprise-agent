# 事故复盘（postmortem）0003：LEAVE_DEF 定义了但从未注册进 SOP_REGISTRY

状态：已解决

## 摘要

业务解耦改造中，请假 `LEAVE_DEF` 从 `sop.py` 抽到 `leave.py` 转为 `ApprovalDef` 组件，
但 `leave.py` 底部缺少 `_sop.register(_build_sop(LEAVE_DEF))`——定义了但从未挂进
`SOP_REGISTRY`。`sop_engine.match("帮我请假")` 返回 None → 请假 SOP 从解耦提交后
完全失效，所有请假触发词落入知识问答路径。补覆盖率时 test_approval.py 未直接测
SOP 匹配，但 `from app.service import agent` 后查 `SOP_REGISTRY` 发现只有 4 条
（缺 leave_action）。

## 影响

请假申请功能完全不可用——用户说"帮我请假"不触发 SOP，走知识问答兜底。
因业务都是测试用、无真实用户流量，未被报告；但若上线会造成核心功能缺失。

## 时间线

- 2026-08-20：业务解耦 commit（efdc4a2），leave.py 从 sop.py 抽出。
  `approvals.py` 底部有 `for _def in APPROVAL_DEFS: _sop.register(_build_sop(_def))`
  循环注册 4 个审批类 SOP，但 `LEAVE_DEF` 在 `leave.py` 里定义后**没有等价的注册行**。
- 同日：补覆盖率时跑 `from app.service import agent; sop.SOP_REGISTRY` →
  只有 4 条（contract/expense/travel/purchase），缺 leave_action。
- 修复：`leave.py` 底部加 `_sop.register(_build_sop(LEAVE_DEF))`（与 approvals.py
  底部循环同模式）。

## 根因

1. **注册模式不对称**：`approvals.py` 有模块级循环注册 `APPROVAL_DEFS`（4 条），
   `leave.py` 是单独定义却**没有等价的注册行**——模式迁移时漏了"注册"这一步。
2. **SOP_REGISTRY 是可变全局列表**：`register()` 往列表 append，但没有
   "定义了但没注册"的检测——定义在 `leave.py` 里、注册在别处，两者分离。
3. **解耦重构的盲区**：从 sop.py 抽 leave.py 时，注意力在"逻辑搬对没有"，
   忽略了"注册触发"——注册是隐式副作用（模块级代码执行），不在函数签名里，
   容易漏。

## 已添加的防护措施

1. **注册修复**（`app/plan/leave.py`）：底部加 `_sop.register(_build_sop(LEAVE_DEF))`。
2. **不变量测试**（`scripts/test_invariants.py`）：新增检查
   `from app.service import agent; sop.SOP_REGISTRY` 含 5 条 + leave_action 在内。
3. **补覆盖率发现**：test_approval.py 写真实 apply_no 时浮现了另一个正则 bug
   （见 0002），而 SOP_REGISTRY 缺失是 `from app.service import agent` 后
   查 `len(sop.SOP_REGISTRY)` 发现的——证明"导入后验证状态"是有效的检测手段。

## 教训

1. **模块级副作用注册是脆弱的**——定义和注册不在同一行，迁移时容易漏。
   更好的模式：`registry.py` 遍历 `ALL_DEFS` 统一注册（不是各文件底部各自注册）。
2. **解耦重构的验证清单必须包含"功能等价"**——不只看代码结构对不对，
   要跑一次真实触发（`sop.match("帮我请假")` 不为 None）。
3. **可变全局列表无"漏注册"检测**——`SOP_REGISTRY` 是 list，定义了没注册
   不会报错，静默缺失。
