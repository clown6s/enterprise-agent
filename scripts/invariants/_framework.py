# -*- coding: utf-8 -*-
"""dsh 三-5 伴生不变量框架：check 计数器 + 跨模块累计。

每个模块的 run(check) 接收本框架提供的 check 函数；所有 check 调用
写入同一个模块级单例计数器，从而实现跨文件累计 PASS/FAIL。
入口 __init__.run_all() 调 new_session() 重置后顺序跑各模块，最后 totals() 汇总。
"""


class _Counters:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failed_names: list = []

    def reset(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failed_names = []


_counters = _Counters()


def new_session() -> None:
    """重置计数器（每次 run_all 开头调用，防止跨次运行串台）。"""
    _counters.reset()


def totals() -> tuple:
    """返回 (passed, failed)。"""
    return _counters.passed, _counters.failed


def check(name: str, cond: bool, detail: str = "") -> None:
    """单条断言：cond 真则 PASS+1 并打印 ✓；假则 FAIL+1 并打印 ✗+detail。"""
    if cond:
        _counters.passed += 1
        print(f"✓ {name}")
    else:
        _counters.failed += 1
        _counters.failed_names.append(name)
        print(f"✗ {name}  {detail}")
