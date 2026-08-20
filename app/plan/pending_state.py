# -*- coding: utf-8 -*-
"""
SOP 待确认单的 state replace 强校验（dsh 一-5）。

原则：replace 必须声明被遮蔽的全部节点（provenance）；改写只能动允许字段；
候选先 validate 生成 plan 通过才进日志/写库。

第一版：纯函数校验 + dry-run。save_pending 调用前过一遍 plan 校验，
非法字段变更直接拒绝（返回错误而非静默覆盖）。不改 sop_pending 表结构，
向后兼容现有首次 save/confirm 流程。
"""
from dataclasses import dataclass, field
from typing import Any

# 用户可修改的字段白名单（来自 ApprovalDef.fields 的提取参数键）。
# 审批状态、员工身份等不可由用户随意替换——由审批动作层/确认闸门管理。
_USER_MUTABLE_KEYS = {
    # 通用提取参数（各业务共享）
    "userName", "reason",
    # 请假
    "startTime", "endTime", "days", "leaveType",
    # 报销
    "expenseType", "amount", "expenseDate",
    # 出差
    "destination", "startDate", "endDate",
    # 采购
    "itemName", "quantity",
    # 合同用印
    "contractName", "counterparty",
}

# 系统字段（不可由用户消息修改，由引擎/审批层管理）
_SYSTEM_KEYS = {
    "sop_code", "apply_no", "status", "current_approver_id",
    "user_id", "user_name", "dept_id", "create_time",
}


@dataclass
class ChangePlan:
    """候选变更计划：旧值/新值/来源/允许字段/版本。"""
    conversation_id: str
    old: dict
    new: dict
    source: str = "user"          # user / system / admin
    rejected_keys: list = field(default_factory=list)
    approved: bool = True


def validate_change(old: dict, new: dict, source: str = "user") -> ChangePlan:
    """校验候选变更：用户源只能动白名单字段，系统源可动系统字段。

    用户源改系统字段（即使 old 中已有）也被拒绝——防止用户消息篡改审批状态。
    返回 ChangePlan：approved=True 可提交，approved=False + rejected_keys 拒绝。
    """
    rejected = []
    allowed = _USER_MUTABLE_KEYS if source == "user" else (_USER_MUTABLE_KEYS | _SYSTEM_KEYS)
    for key in new:
        if key not in allowed:
            rejected.append(key)
    return ChangePlan(
        conversation_id=old.get("conversation_id", ""),
        old=old, new=new, source=source,
        rejected_keys=rejected,
        approved=not rejected,
    )


def apply_plan(plan: ChangePlan) -> dict:
    """从 plan 生成最终 params（只含 approved 字段，拒绝字段被剔除）。

    如果 plan 不 approved，抛 ValueError（调用方决定降级还是拒绝）。
    """
    if not plan.approved:
        raise ValueError(
            f"变更计划被拒绝：以下字段不允许由 {plan.source} 修改: {plan.rejected_keys}")
    merged = dict(plan.old)
    merged.update(plan.new)
    return merged
