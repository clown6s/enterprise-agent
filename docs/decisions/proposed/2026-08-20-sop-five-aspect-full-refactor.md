# 决策：SOP 引擎五段式全形式化重构

状态：proposed

## Problem（问题）

deepseek-harness 一-2 的五段式工具执行管线（pre-execute allow/deny/ask → 单调守卫 →
around 分派 → post-execute accept/replace/block → observe 逐监听器容错）是更严谨的
执行切面模型。问：enterprise-agent 的 SOP 引擎（`plan/sop.py` execute + `plan/approvals.py`
_build_sop）要不要全形式化重构为显式五切面（每切面注册表 + hook）？

当前引擎的切面散落（fix_output 在 `agent.py`、graph daemon 裸挂 `_do_insert` 一行、
audit 内联），无显式 `post_hooks`/`observe_hooks` 注册表。当前业务都是测试用，真正
的价值是项目可扩展性——五段式收口后，加新后处理/新旁路只需注册 hook，不改引擎。

## Decision（决定）

**实施 SOP 引擎五段式形式化重构。** 原则：

1. **向后兼容**：`SopDefinition` 加 `post_hooks`/`observe_hooks` 字段，默认空列表；
   旧定义（无 hook）行为完全不变，回归 29 条全绿。
2. **不变的三条契约**：不改执行流水线顺序、不改 `steps` 步骤顺序、不改 handler 签名
   `(state) -> (stop, result)`。
3. **只做增量加法**：hook 在现有步骤结果产生后执行，不干扰主流程。hook 失败不影响主结果。
4. **observe 异步隔离**：每个 observe hook 独立容错（try/except），一个旁路失败不影响
   其他旁路和主结果。
5. **实操顺序**：先加 `SopDefinition` hook 字段 → 迁 `fix_output` 到 post-hook（收口
   agent.py 散落调用）→ 迁 graph-build 到 observe-hook → 加不变量锁契约 → 回归全绿。

## Alternatives considered（考虑过的替代方案）

- **全形式化重构（2026-08-20 上午评估，暂缓 rejected）**：当时判"工作引擎+异步回归抓不到"
  高风险低收益。后续讨论认识到：(1) 当前业务是测试用，可扩展性是项目长期价值；
  (2) 向后兼容的 hook 增量不破契约；(3) 现在功能少路径清晰，改了好测，以后多了难收口。
  故从 rejected 改为 proposed。
- **只做 post-hook 收口**（fix_output 集中化，不碰引擎）：评估后认为不够——fix_output
  只是当前一个实例，observe 的 graph daemon 是另一个裸挂散落，两个一起收口才完整。
- **保持现状，等第二个 observe 需求**：用户判断"重点是项目可扩展性，现在改好时机"，
  否决等待策略。

## Consequences（后果）

- 正面：引擎可扩展性正式化——加新后处理/新旁路只需注册 hook，不改引擎代码。
- 正面：修复 `agent.py` 多条答案路径绕过 `fix_output` 的遗漏缺口。
- 正面：现在功能少路径清晰，改错了回归能立刻发现；以后多了再收口成本高数倍。
- 负面/债：observe hook 的 graph-build 异步路径回归不测，需手动验证（Neo4j 可达时）。
- 跟进：实施后同步更新 `docs/decisions/README.md` 索引 + `CLAUDE.md` 归属表。