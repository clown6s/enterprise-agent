# -*- coding: utf-8 -*-
"""
Type-equiv 防漂移检查（dsh 二-7）。

从 docs/type-equiv.md 提取 ```python type-equiv 围栏中的签名声明，
用 inspect 从源码提取真实签名比对，漂移即红。

第一版只锁函数名 + dataclass 字段名，不锁类型注解细节（避免误报）。
"""
import inspect
import os
import re
import sys
import ast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


def _extract_blocks(md_path: str) -> list[tuple[str, str]]:
    """从 type-equiv.md 提取 (模块路径, 声明文本) 对。"""
    text = open(md_path, encoding="utf-8").read()
    blocks = []
    for m in re.finditer(r"```python type-equiv\n(.*?)```", text, re.S):
        body = m.group(1).strip()
        # 第一行是 # 模块路径 注释
        lines = body.splitlines()
        mod_line = next((l for l in lines if l.strip().startswith("#")), "")
        mod_path = mod_line.strip().lstrip("# ").strip()
        blocks.append((mod_path, body))
    return blocks


def _check_function(mod_path: str, func_name: str, decl: str) -> bool:
    """检查函数是否存在且名字一致。"""
    try:
        parts = mod_path.split("/")
        mod_import = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
        if mod_import.startswith("app/"):
            mod_import = mod_import.replace("/", ".")
        # 只取最后一级作为模块名
        mod_name = parts[-1].replace(".py", "")
        full_mod = f"app.{mod_name}" if mod_name != "request_context" else "app.request_context"
        if mod_path == "app/prompts.py":
            full_mod = "app.prompts"
        elif mod_path == "app/config.py":
            full_mod = "app.config"
        elif mod_path == "app/plan/sop.py":
            full_mod = "app.plan.sop"
        elif mod_path == "app/plan/approvals.py":
            full_mod = "app.plan.approvals"
        elif mod_path == "app/llm.py":
            full_mod = "app.llm"
        elif mod_path == "app/request_context.py":
            full_mod = "app.request_context"
        mod = __import__(full_mod, fromlist=["_"])
        return hasattr(mod, func_name)
    except Exception as e:
        check(f"type-equiv {mod_path}.{func_name}", False, f"导入失败: {e!r}")
        return False


def _check_dataclass_fields(mod_path: str, class_name: str, expected_fields: list[str]) -> bool:
    """检查 dataclass 字段名集合是否匹配。"""
    try:
        mod_map = {
            "app/plan/sop.py": "app.plan.sop",
            "app/plan/approvals.py": "app.plan.approvals",
        }
        full_mod = mod_map.get(mod_path, mod_path.replace("/", ".").replace(".py", ""))
        mod = __import__(full_mod, fromlist=["_"])
        cls = getattr(mod, class_name)
        import dataclasses
        actual = [f.name for f in dataclasses.fields(cls)]
        return set(expected_fields) <= set(actual)
    except Exception as e:
        check(f"type-equiv {mod_path}.{class_name}", False, f"导入失败: {e!r}")
        return False


def main() -> int:
    md_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "type-equiv.md")
    if not os.path.exists(md_path):
        print(f"✗ type-equiv.md 不存在: {md_path}")
        return 1
    blocks = _extract_blocks(md_path)
    if not blocks:
        print("✗ type-equiv.md 无 type-equiv 围栏")
        return 1

    for mod_path, decl in blocks:
        # 提取函数名
        for fm in re.finditer(r"def (\w+)\s*\(", decl):
            func_name = fm.group(1)
            ok = _check_function(mod_path, func_name, decl)
            check(f"type-equiv {mod_path}.{func_name} 存在", ok)

        # 提取 dataclass 字段
        cm = re.search(r"class (\w+):", decl)
        if cm:
            class_name = cm.group(1)
            fields = re.findall(r"^\s+(\w+)\s*:", decl, re.M)
            fields = [f for f in fields if f not in ("object", "str", "dict", "list", "tuple")]
            ok = _check_dataclass_fields(mod_path, class_name, fields)
            check(f"type-equiv {mod_path}.{class_name} 字段集", ok,
                  f"expected={fields}" if not ok else "")

    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
