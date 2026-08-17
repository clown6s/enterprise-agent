# -*- coding: utf-8 -*-
"""企业/法律领域插件（从 agent.py 原样迁出，内容零改动）"""
from .base import DomainPlugin

PLUGIN = DomainPlugin(
    code="enterprise",
    name="企业法律",
    synonym_groups=[
        # terms=查询侧触发词；expand=资料侧扩展短语（独立检索词，只放
        # 桥接词——曾放"处罚/罚款"类高频词把主查询稀释的实况）
        {"terms": ["报销", "报账"], "expand": "报账 差旅 费用报销"},
        {"terms": ["请假", "休假", "调休"], "expand": "年假 事假 调休 休假"},
        {"terms": ["工资", "薪资", "薪酬"], "expand": "薪水 薪酬 待遇"},
        {"terms": ["斗殴", "打架"], "expand": "结伙 殴打"},
        {"terms": ["制度", "规定", "规章"], "expand": "办法 条例 规范 规章"},
        {"terms": ["员工", "同事"], "expand": "职工 人员"},
        {"terms": ["智能体", "Agent"], "expand": "智能体 Agent"},
    ],
)
