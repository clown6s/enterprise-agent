# -*- coding: utf-8 -*-
"""
SOP 中央注册表——所有业务类型的单一数据源。
新增业务 = 本文件加一个 ApprovalDef 实例，其余模块自动感知。
"""
from .leave import LEAVE_DEF
from .approvals import APPROVAL_DEFS

# 全量定义列表（顺序 = 匹配优先级）
ALL_DEFS = [LEAVE_DEF] + APPROVAL_DEFS

# 从定义派生的查找表（approval.py / tools.py / admin.py 消费）
TABLE_MAP = {d.business_type: d.table for d in ALL_DEFS}
SUMMARY_MAP = {d.business_type: tuple(d.fields.keys()) for d in ALL_DEFS}
BIZ_NAME_MAP = {d.business_type: d.name for d in ALL_DEFS}
PREFIX_MAP = {d.prefix: d.business_type for d in ALL_DEFS}


def get_tools():
    """tools.py 消费：注册表 → Tool 列表"""
    from ..tools import Tool
    return [Tool(d.code, d.name, f"{d.name} SOP（{d.code}）") for d in ALL_DEFS]
