# -*- coding: utf-8 -*-
"""
GLM 校验模型探针：GLM-4.6 / GLM-4.5 对比 qwen3-max 基线。
维度：事实核查质量（删编造/保有据/保结构）、相关性评审（正反两视角）、
延迟、参数兼容性（thinking disabled 是否 400）。
用途：换校验模型前取证；python scripts/probe_glm.py
"""
import re
import sys
import time

sys.path.insert(0, ".")
from app import llm, prompts  # noqa: E402

KEY = None
BASE = "https://open.bigmodel.cn/api/paas/v4"
try:
    from app import config
    KEY = config._env("ZHIPU_API_KEY")
except Exception:
    pass

# ---------------- 测试对 ----------------

VERIFY_CTX = (
    "高血压患者每日食盐摄入量应低于 5 克。运动方面，每周进行 150 分钟"
    "中等强度有氧运动。定期监测血压，每 2 周至少测量一次。")
VERIFY_ANS = (
    "**饮食**：\n- 每日食盐摄入量应低于 5 克\n- 应严格禁酒\n"
    "**运动**：\n- 每周进行 150 分钟中等强度有氧运动\n- 每 2 周至少测量一次血压")

REL_QUERY = "高血压应该注意什么"
REL_ANS_OK = ("**饮食注意**：\n- 控制食盐摄入，每日低于 5 克\n- 多吃蔬菜水果\n"
              "**运动建议**：\n- 每周 150 分钟中等强度有氧运动\n")
REL_ANS_BAD = ("高血压的诊断分级如下：\n- 一级高血压为 140/90 mmHg\n"
               "- 二级高血压为 160/100 mmHg\n- 三级高血压为 180/110 mmHg\n")


def timed(fn, *a, **kw):
    t0 = time.time()
    try:
        out = fn(*a, **kw)
        return out, time.time() - t0, None
    except Exception as e:
        return "", time.time() - t0, str(e)[:120]


def check_verify(raw: str) -> list[str]:
    """核查结果质量断言（确定性可判的部分）"""
    notes = []
    if "严格禁酒" in raw:
        notes.append("FAIL 编造句未删（严格禁酒）")
    else:
        notes.append("ok 编造句已删")
    if "5 克" in raw and "150 分钟" in raw:
        notes.append("ok 有据数字保留")
    else:
        notes.append("FAIL 有据数字丢失")
    if "**饮食**" in raw and "**运动**" in raw and "- " in raw:
        notes.append("ok 结构保留")
    else:
        notes.append("FAIL 结构被洗")
    return notes


def check_rel(score: str, expect: str) -> list[str]:
    m = re.search(r"[1-5]", score or "")
    got = int(m.group()) if m else None
    if got is None:
        return ["FAIL 无分数"]
    ok = {"high": got >= 4, "low": got <= 3}[expect]
    return ([f"ok {expect}:{got}"] if ok else [f"FAIL {expect}:{got}"])


def probe_one(name: str, model: str, key: str, base: str, extra) -> dict:
    out = {"model": model, "verify": [], "rel_ok": [], "rel_bad": [], "params": []}
    # 1. 事实核查
    user = prompts.VERIFY_PROMPT.format(missing="（无）", spliced="（无）",
                                        graph_only_rule="", context=VERIFY_CTX,
                                        answer=VERIFY_ANS)
    raw, sec, err = timed(llm.chat, "", user, model=model, base=base, key=key,
                          temperature=0.2, extra=extra)
    if err:
        out["verify"].append(f"FAIL 调用失败: {err}")
    else:
        out["verify"] += check_verify(raw)
    out["lat_verify"] = round(sec, 2)
    # 2. 相关性评审（正 + 反两视角）
    for label, (q, a, expect) in {
        "ok": (REL_QUERY, REL_ANS_OK, "high"),
        "bad": (REL_QUERY, REL_ANS_BAD, "low"),
    }.items():
        # 正面评审
        s_pos, sec, err = timed(
            llm.chat, "", (
                "你是答案相关性评审员。判断【答案】是否**正面回应了问题想问的点**：\n"
                "5 = 正面且充分地回应了问题想问的点\n"
                "4 = 正面回应了问题想问的点（部分细节未覆盖可接受）\n"
                "3 = 只答了相邻内容，没有正面回应问题 → 不合格\n"
                "1 = 答非所问或对用户无用\n"
                "只输出一个 1-5 的整数。\n\n【问题】\n%s\n\n【答案】\n%s"
            ) % (q, a[:800]),
            model=model, base=base, key=key, temperature=0.1, extra=extra)
        if err:
            out[f"rel_{label}"].append(f"FAIL 调用失败: {err}")
        else:
            out[f"rel_{label}"] += check_rel(s_pos, expect)
        out[f"lat_rel_{label}"] = round(sec, 2)
    # 3. 参数兼容性：thinking disabled（Zhipu 语法）
    s, sec, err = timed(llm.chat, "", "只回复两个字：收到", model=model, base=base,
                        key=key, temperature=0.1, extra={"thinking": {"type": "disabled"}})
    out["params"].append("thinking_param=" + ("ok" if not err else f"400/异常 {err}"))
    # 4. 裸调用（不带 extra）
    s, sec, err = timed(llm.chat, "", "只回复两个字：收到", model=model, base=base,
                        key=key, temperature=0.1)
    out["params"].append("no_extra=" + ("ok" if not err else f"异常 {err}"))
    return out


def main():
    if not KEY:
        print("ZHIPU_API_KEY 未配置")
        return
    models = [("glm-4.6", KEY), ("glm-4.5", KEY)]
    extra = {"thinking": {"type": "disabled"}}
    for name, model in [("GLM-4.6", "glm-4.6"), ("GLM-4.5", "glm-4.5")]:
        print(f"\n===== {name} =====")
        r = probe_one(name, model, KEY, BASE, extra)
        for k, v in r.items():
            if k.startswith("lat"):
                print(f"  [{k}] {v}s")
        print("  核查: " + " | ".join(r["verify"]))
        print("  评审正例: " + " | ".join(r["rel_ok"]))
        print("  评审反例: " + " | ".join(r["rel_bad"]))
        print("  参数: " + " | ".join(r["params"]))
    # qwen3-max 基线（同题对照延迟；从注册表 verify_fallback 条目取配置）
    print("\n===== qwen3-max（当前基线） =====")
    try:
        fb = config.MODEL_REGISTRY["verify_fallback"]
        r = probe_one("qwen3-max", fb["model"], fb["key"], fb["base"], fb["extra"])
        for k, v in r.items():
            if k.startswith("lat"):
                print(f"  [{k}] {v}s")
        print("  核查: " + " | ".join(r["verify"]))
        print("  评审正例: " + " | ".join(r["rel_ok"]))
        print("  评审反例: " + " | ".join(r["rel_bad"]))
        print("  参数: " + " | ".join(r["params"]))
    except Exception as e:
        print(f"  基线调用失败: {e!r}")


if __name__ == "__main__":
    main()
