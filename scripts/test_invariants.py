# -*- coding: utf-8 -*-
"""不变量套件薄 runner（dsh 三-5 伴生 per-module 结构）。

实际检查在 scripts/invariants/{guards,verify,llm,routing,fuzz}.py，
框架与计数在 scripts/invariants/_framework.py，
编排在 scripts/invariants/__init__.py:run_all()。

跑法：python scripts/test_invariants.py
（直接当脚本跑时，Python 自动把脚本所在 scripts/ 入 sys.path[0]，
故 `from invariants import run_all` 可解析到 scripts/invariants 包；
下面再插入 project root 让包内 `from app import ...` 也能解析。）
"""
import os
import sys

# project root（让包内 `from app import ...` 可解析）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invariants import run_all  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_all())
