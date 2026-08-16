# -*- coding: utf-8 -*-
"""
LLM 用量追踪（成本/延迟看板的数据源）：每次调用记录模型/字符量/耗时 → JSONL。

自我优化的第二个数据源：用量数据回答"钱花在哪、慢在哪"——
延迟异常高时可在上游做模型降级（路线图），现在先把数据攒起来。
"""
import json
import os
import threading
import time
from datetime import datetime

from .. import config

_FILE = os.path.join(config.LOG_DIR, "usage.jsonl")
_lock = threading.Lock()

# 进程内聚合（看板接口用，重启归零；完整历史在 JSONL）
_agg: dict = {"calls": 0, "prompt_chars": 0, "completion_chars": 0, "total_seconds": 0.0,
              "by_model": {}, "recent": []}   # recent 保留最近 20 条明细


def record(model: str, prompt_chars: int, completion_chars: int, seconds: float) -> None:
    """记录一次 LLM 调用（线程安全）"""
    entry = {
        "ts": datetime.now().isoformat(),
        "model": model,
        "prompt_chars": prompt_chars,
        "completion_chars": completion_chars,
        "seconds": round(seconds, 2),
    }
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    with _lock:
        _agg["calls"] += 1
        _agg["prompt_chars"] += prompt_chars
        _agg["completion_chars"] += completion_chars
        _agg["total_seconds"] += seconds
        m = _agg["by_model"].setdefault(model, {"calls": 0, "chars": 0, "seconds": 0.0})
        m["calls"] += 1
        m["chars"] += prompt_chars + completion_chars
        m["seconds"] += seconds
        _agg["recent"].append(entry)
        del _agg["recent"][:-20]


def snapshot() -> dict:
    """看板快照：总量 + 分模型 + 最近明细"""
    with _lock:
        return {
            "calls": _agg["calls"],
            "prompt_chars": _agg["prompt_chars"],
            "completion_chars": _agg["completion_chars"],
            "total_seconds": round(_agg["total_seconds"], 2),
            "by_model": {k: dict(v) for k, v in _agg["by_model"].items()},
            "recent": list(_agg["recent"]),
        }


def timed(model: str, prompt_chars: int) -> "_Timer":
    """计时上下文：with usage.timed(model, n) as t: ... t.done(completion_chars)"""
    return _Timer(model, prompt_chars)


class _Timer:
    def __init__(self, model: str, prompt_chars: int):
        self.model = model
        self.prompt_chars = prompt_chars
        self._start = time.time()

    def done(self, completion_chars: int) -> None:
        record(self.model, self.prompt_chars, completion_chars, time.time() - self._start)
