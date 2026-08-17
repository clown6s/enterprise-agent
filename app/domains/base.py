# -*- coding: utf-8 -*-
"""
领域插件协议：行业知识（医学/企业/法律…）以插件形式挂载，
主流程零行业硬编码——换行业 KB = 改 .env 一行 + 写一个新插件文件。
（用户 2026-08-18 定调：针对性强的东西不做强依赖，做成插件类型。）
"""
from dataclasses import dataclass, field


@dataclass
class DomainPlugin:
    code: str                       # 插件标识（.env DOMAIN_PLUGINS 用）
    name: str                       # 展示名
    dimension_pairs: list = field(default_factory=list)
    # [(问句侧模式, 资料侧模式)]——资料覆盖闸门的"问什么答什么"维度词对，
    # 资料侧模式同时用作检索扩展短语
    attention_words: list = field(default_factory=list)
    # 注意类触发词（问句含任一 → 资料须含 lifestyle_terms 内容）
    lifestyle_terms: list = field(default_factory=list)
    # 注意类提问对应的"生活方式内容"词（资料侧）
    synonym_groups: list = field(default_factory=list)
    # [{terms: [查询侧触发词...], expand: "资料侧扩展短语"}]——检索词汇对齐
    num_units: list = field(default_factory=list)
    # 领域数值单位（数值+单位的单位部分，用于术语核对/图谱数字硬删）
