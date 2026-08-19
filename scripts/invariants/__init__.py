# -*- coding: utf-8 -*-
"""dsh 三-5 伴生不变量套件入口：顺序跑各模块 + 强制元检查。

结构（per-module 伴生不变量）：
  scripts/invariants/_framework.py  —— check 计数器 + 跨文件累计
  scripts/invariants/guards.py      —— 一、guards 契约
  scripts/invariants/verify.py      —— 二、verify 契约
  scripts/invariants/llm.py         —— 三、LLM 降级链契约
  scripts/invariants/routing.py     —— 四、路由与领域插件契约
  scripts/invariants/fuzz.py        —— 五、入口模糊测试
  scripts/test_invariants.py        —— 薄 runner（sys.path + sys.exit(run_all())）

跑法：python scripts/test_invariants.py
"""
import os

from ._framework import check, new_session, totals

# 段顺序对应原 test_invariants.py 五段（guards→verify→llm→routing→fuzz）
_MODULES = ("guards", "verify", "llm", "routing", "fuzz")


def run_all() -> int:
    """顺序跑各模块 run(check)，累计 PASS/FAIL；返回 0=全绿 / 1=有失败。"""
    new_session()

    # dsh 三-5 强制元检查：每个预期的伴生文件必须存在且非空（缺则 check 失败）。
    # 这是一条覆盖 5 个文件的元检查（不是 5 条），把"伴生文件齐全"本身固化为契约。
    base = os.path.dirname(os.path.abspath(__file__))
    missing = []
    for name in _MODULES:
        path = os.path.join(base, f"{name}.py")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            missing.append(name)
    check("元检查:伴生文件齐全且非空", not missing, repr(missing))

    # 顺序导入并跑每个模块（lazy import：放 run_all 内，
    # 避免 `import invariants` 时触发 app 依赖；runner 已把 project root 入 path）
    from . import guards as _g
    from . import verify as _v
    from . import llm as _l
    from . import routing as _r
    from . import fuzz as _f
    for mod in (_g, _v, _l, _r, _f):
        mod.run(check)

    passed, failed = totals()
    print(f"\n{passed} 通过 / {failed} 失败")
    return 1 if failed else 0
