# 决策：SOP 引擎五段式全形式化重构（暂缓）

状态：rejected（评估后暂缓；引擎已隐含五段式精神，全形式化高风险低收益）

## Problem（问题）

deepseek-harness 一-2 的五段式工具执行管线（pre-execute allow/deny/ask → 单调守卫 →
around 分派 → post-execute accept/replace/block → observe 逐监听器容错）是更严谨的
执行切面模型。问：enterprise-agent 的 SOP 引擎（`plan/sop.py` execute + `plan/approvals.py`
_build_sop）要不要全形式化重构为显式五切面（每切面注册表 + hook）？

## Decision（决定）

**暂缓全形式化重构。** 评估当前引擎，五段式精神已隐含落地，只是切面"散落"非"形式化"：

| dsh 切面 | 当前引擎对应 | 状态 |
|---|---|---|
| pre-execute（allow/deny/ask） | validate 步骤（deny 失败文案）+ confirm 闸门（ask 待确认单） | ✓ 已有 |
| 单调守卫 | `guards.GUARD_REGISTRY` 只读否决注册表（一-1 已落地） | ✓ 已有 |
| around（结果非异常） | 步骤 handler 返回 `(stop, result)` 字典，不抛异常 | ✓ 已有 |
| post-execute（结果改写） | `guards.fix_output` 对全路径答案（含 SOP/DATA/APPROVAL）后处理 | ✓ 已有 |
| observe（异步旁路容错） | `_do_insert` 起 `threading.Thread(_graph_build_bg, daemon=True)` + `_audit` try/except OSError | ✓ 已有（裸 daemon，未形式化为切面） |

唯一缺口是**形式化**：切面散落（fix_output 在 `agent.py`、graph daemon 在 `_do_insert`、
audit 内联），无显式 `post_hooks`/`observe_hooks` 注册表。这是结构清晰度问题，**非行为缺口**——
引擎行为已符合五段式语义。

## Alternatives considered（考虑过的替代方案）

- **全形式化重构**（SopDefinition 加 `pre/guard/around/post/observe` hook 槽 + 注册表，
  把 fix_output/graph-build/audit 迁到切面）：否决。理由：(1) 对工作+回归覆盖的引擎做
  行为不变的结构重构，高风险低收益；(2) observe 切面的 graph-build 是**异步软依赖**，
  重构引入的 bug 回归抓不到（不符"交付前穷举自测"——需飞书真机 + Neo4j 才能验）；(3) 用户
  本身指出"当前引擎能用，这套更稳"，但"更稳"是远期演进，非当下缺口。
- **只形式化 observe 切面**（graph daemon 裸挂 → 注册 observe hook）：部分采纳评估。
  风险仍在（动 `_do_insert` 的 daemon 起点，bug 不易回归捕）。暂缓，待 observe 切面有第二个
  旁路需求（如飞书通知也走 observe）时一并形式化更划算。

## Consequences（后果）

- 正面：避免对工作引擎的高风险重构；五段式映射留档，后人/未来重构有据。
- 负面/债：切面散落（新人读引擎要跨 `agent.py`/`approvals.py`/`sop.py` 拼五段式）；
  observe 无注册表（加第二个旁路时要各自裸挂 daemon）。
- 跟进触发条件：出现 observe 第二个旁路需求、或某切面出现**行为缺口**（非形式化）时，
  重新评估形式化。届时优先 observe 切面（已有 graph daemon 一个实例可基准）。
