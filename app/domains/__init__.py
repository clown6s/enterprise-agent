# -*- coding: utf-8 -*-
"""
领域插件加载与聚合：主流程只调这里的聚合函数，行业知识全部来自插件。
- .env DOMAIN_PLUGINS=medical,enterprise（缺省全部启用）
- 插件加载失败 → 跳过并记日志（无插件=通用能力照跑，行业增强失效不崩）
"""
import importlib
import re

from .. import config
from .base import DomainPlugin
import logging
log = logging.getLogger(__name__)


def _load() -> list[DomainPlugin]:
    names = [n.strip() for n in config._env("DOMAIN_PLUGINS", "medical,enterprise").split(",")
             if n.strip()]
    plugins: list[DomainPlugin] = []
    for n in names:
        try:
            mod = importlib.import_module(f".{n}", __name__)
            plugins.append(mod.PLUGIN)
        except Exception as e:
            log.warning(f"插件 {n} 加载失败（跳过）: {e!r}")
    return plugins


_PLUGINS = _load()


def dimension_pairs() -> list[tuple[re.Pattern, re.Pattern]]:
    """资料覆盖闸门的维度词对（问句侧, 资料侧）；资料侧模式兼作检索扩展"""
    out = []
    for p in _PLUGINS:
        out.extend((re.compile(q), re.compile(c)) for q, c in p.dimension_pairs)
    return out


def attention_pattern() -> re.Pattern:
    """注意类触发词（无插件时用不匹配任何东西的空模式）"""
    words = [w for p in _PLUGINS for w in p.attention_words]
    return re.compile("|".join(words)) if words else re.compile(r"$^")


def lifestyle_pattern() -> re.Pattern:
    """注意类提问对应的内容词（资料侧）"""
    words = [w for p in _PLUGINS for w in p.lifestyle_terms]
    return re.compile("|".join(words)) if words else re.compile(r"$^")


def synonym_groups() -> list[dict]:
    out = []
    for p in _PLUGINS:
        out.extend(p.synonym_groups)
    return out


def num_unit_extra() -> list[str]:
    """领域数值单位（术语核对/图谱数字硬删的单位表追加部分）"""
    out = []
    for p in _PLUGINS:
        out.extend(p.num_units)
    return out
