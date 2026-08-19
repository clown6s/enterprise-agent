# 决策记录（Agent Note）

借鉴 deepseek-harness `.agents/notes/`（2026-08-19 落地，见 docs/dsh-highlights.md 二-3）。

**规则：每个改变行为的变更必须在同一 PR 新增或更新至少一篇决策记录。**
改的是「为什么这么设计」，不是「改了什么」——后者在 git 历史里。

## 目录

- `proposed/`——提议中，尚未实施
- `implemented/`——已实施并验证
- `rejected/`——评估后否决（记录否决理由，防止后人重新踩坑）

## 格式

文件名：`yyyy-mm-dd-topic-slug.md`

```markdown
# 决策：<一句话标题>

状态：implemented | proposed | rejected（必须与所在文件夹一致）

## Problem（问题）
为什么需要这个决策？当时的实况/痛点/约束。

## Decision（决定）
做了什么决定。implemented 只记录已交付的事实（路径、名称、默认值），
严禁把旧 Note 改写成一个不同的决策——新决策写新 Note 并互相链接。

## Alternatives considered（考虑过的替代方案）
列过哪些方案、为什么没选。

## Consequences（后果）
正面与负面后果、需要跟进的债。
```

## 已记录决策

| 日期 | 决策 | 状态 |
|---|---|---|
| 2026-08-19 | [路由去预判：无预闸架构](implemented/2026-08-19-no-pregating-routing.md) | implemented |
| 2026-08-19 | [NL2SQL 结果包装层：数据≠答案](implemented/2026-08-19-nlsql-answer-wrapper.md) | implemented |
| 2026-08-19 | [去 OA 化：审批业务直连本库](implemented/2026-08-19-de-oa-approval-direct-db.md) | implemented |
| 2026-08-20 | [SOP 五段式全形式化重构（暂缓）](rejected/2026-08-20-sop-five-aspect-full-refactor.md) | rejected |
