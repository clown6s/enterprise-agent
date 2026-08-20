# -*- coding: utf-8 -*-
"""
最小变异测试（dsh 三-6/三-11）：证明 guards 测试不是"跑绿即算"。

不修改工作区：把 app/guards.py 复制到临时项目目录，做一个确定性坏变异，
在临时目录运行 test_guards.py，必须失败；finally 清理临时目录并校验原文件哈希。
这是 opt-in 质量验证，不纳入每次 CI 门控（运行时间和维护成本高于普通测试）。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "app", "guards.py")
TEST = os.path.join(ROOT, "scripts", "test_guards.py")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    original_hash = sha(TARGET)
    tmp = tempfile.mkdtemp(prefix="enterprise-agent-mutation-")
    try:
        # 临时项目保持 app 包和测试脚本结构；复制 guards 及其包标记。
        os.makedirs(os.path.join(tmp, "app"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "scripts"), exist_ok=True)
        shutil.copy2(TARGET, os.path.join(tmp, "app", "guards.py"))
        shutil.copy2(os.path.join(ROOT, "app", "__init__.py"), os.path.join(tmp, "app", "__init__.py"))
        shutil.copy2(TEST, os.path.join(tmp, "scripts", "test_guards.py"))

        mutated = os.path.join(tmp, "app", "guards.py")
        text = open(mutated, encoding="utf-8").read()
        # 变异：禁止 fix_output 的所有处理，原始 guards 测试必有多条失败。
        marker = "def fix_output(text: str) -> str:"
        if marker not in text:
            print("✗ 变异目标函数签名不存在，测试脚本需更新")
            return 1
        # 在函数体第一条执行语句前返回原文；只改临时副本。
        needle = marker + "\n"
        mutated_text = text.replace(needle, needle + "    return text\n", 1)
        if mutated_text == text:
            print("✗ 变异未生效")
            return 1
        with open(mutated, "w", encoding="utf-8") as f:
            f.write(mutated_text)

        result = subprocess.run(
            [sys.executable, os.path.join(tmp, "scripts", "test_guards.py")],
            cwd=tmp, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode == 0:
            print("✗ 变异后 test_guards 仍全绿：测试没有拦住 fix_output 失效")
            print(result.stdout[-2000:])
            return 1
        print("✓ 变异被 test_guards 拦住（测试确实有拦截能力）")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if sha(TARGET) != original_hash:
            print("✗ 原始 guards.py 哈希改变，变异测试污染了工作区")
            return 1
        print("✓ 原始 guards.py 哈希恢复/未改变")


if __name__ == "__main__":
    sys.exit(main())
