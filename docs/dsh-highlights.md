# 全量亮点清单（2026-08-19 三代理并行扫描）

## 一、架构与运行时（源码级，14 条）

1. **单调守卫**（packages/core/tools/src/index.ts L711-754）：守卫只返回「拒绝理由」或 undefined，没有 allow 结果——注册顺序永远无法把否决翻回放行。→ 校验链改「只读否决」形态，安全策略永远优先于业务校验，杜绝顺序依赖 bug。
2. **五段式工具执行管线**（同文件 L1329-1362）：pre-execute（allow/deny/ask 瀑布）→ 单调守卫 → around 分派（只允许替换 signal，调用身份不可变）→ post-execute（accept/replace/block，可注入附加上下文）→ observe（逐监听器容错）。deny/block 物化成 isError 结果而非抛异常。→ SOP 执行器复刻五切面：前置校验、硬守卫、执行（超时/降级）、后置改写（审计脱敏）、观测（异步旁路失败不影响主链路）。
3. **运行时不变量服务**（packages/runtime-diagnostics/invariants/src/index.ts）：每个包带 invariant.ts 伴生插件，检查挂在真实事件流上（如 internal/dispatch 钩住 tools 各阶段顺序），fail() 抛带包名归属的 InvariantError；prepend:true 防短路监听器屏蔽；全局开关+包名 allow/blocklist。→ 把「审计事件必须成对」「降级后结果仍合法」这类跨请求契约注册成运行时检查，线上即发现而非等测试。
4. **Session：append-only 事件日志 + surface 投影**（packages/core/session/src/index.ts）：日志纯追加、深冻结、seq 连续；给模型的历史是 surfaceOp 标记驱动的派生投影，replace 遮蔽旧节点但不删日志——日志是唯一事实源，投影可随时重建。→ 审计轨迹做成 append-only，给模型/报表的视图是投影，审计完整性与上下文大小解耦，回放/fork/恢复免费获得。
5. **surface replace 强校验 + 先计划后提交**（surface.ts L321-347）：replace 必须声明被遮蔽的全部节点（provenance）；改写只能动 content（逐字段深比较）；候选先 validateNext 生成 plan 通过才进日志。→ 审计记录变更必须：声明来源、限制可改字段、先验证后提交。
6. **Compaction 事务括号协议**（compaction-basic/src/region.ts）：compaction/start/end 即持久锁；失败也强制写 end；摘要必须严格更小；提交前 whole-surface 稳定性校验（世界变了就放弃）。→ SOP 长事务用日志开/闭标记做锁与可恢复性，失败留痕而非回滚删除。
7. **工具并行调度分段抽象**（agent-loop/src/tool-calls.ts）：pre/guard 串行过门禁，around/body 重叠执行，提交只提交连续前缀（提交序=模型序）；abort 时未启动调用写合成错误结果保持回放合法。→ SOP 步骤并行化时门禁串行、执行重叠、提交按原序。
8. **Spill 溢出存储**（packages/spill/）：SpillStore 只定义 saveText（无策略），策略是独立插件挂 post-execute；存储失败/无后端绝不把成功变失败；字节预算先预留 notice 成本。→ 降级链每级「能力缝隙+策略插件」分离，降级失败回退原值。
9. **Provider 确认的溢出恢复链**（compaction-basic/src/index.ts L179-224）：只对 CONTEXT_WINDOW_EXCEEDED 这类确认失败码触发恢复；降级产生可验证进展才 retry，超预算保留原错误。→ 降级不凭猜测，只对明确错误码降级，每次降级审计。
10. **system-prompt 多插件合并**（core/system-prompt/src/index.ts）：具名有序 section+作用域遮蔽+{{variable}} 严格插值（未知变量抛错）+装配期 fail-loud。→ 拼提示词失败比拼出错的提示词好。
11. **Inbox 持久化**（core/agent/src/inbox.ts）：用户消息/注入每次 splice 写会话事件，内存 Inbox 从日志重放重建。→ 任务队列落审计日志，重启重建，杜绝内存队列丢失。
12. **状态即日志折叠**（plan/plan-mode/src/index.ts）：模式状态从 plan/mode 事件折叠（last-wins），用户选择挂 pending 到下个被接受的步骤边界才落日志。→ 状态切换即审计，恢复即回放。
13. **agent-loop 三态状态机**（core/agent-loop/src/agent.ts）：idle/maintenance/running，maintenance 期间 wake 被 latch；所有异常结构化为错误码+消息。→ 主循环显式建模状态，取消与唤醒用 latch 而非竞态标志。
14. **工具配对平衡**（compaction/src/tool-pairing.ts）：切点合法性从表面内容折叠推导（不依赖 step 标记），按 replaceGeneration 增量缓存。→ 范围操作的安全边界从数据内容本身推导。

## 二、文档工程（13 条）

1. **文档分层 +「一个事实只有一个家」**（docs/AGENTS.md）：根 AGENTS.md=常驻短规则、architecture=有序地图、subsystems=每子系统一页、Agent Note=决策记录、postmortem=事故故事、cookbook=步骤手册、生成目录=穷举参考；归属规则：bug→postmortem、rationale→Agent Note、procedure→cookbook。→ CLAUDE.md 只放短规则+链接，决策进 docs/decisions/，事故进 docs/postmortem/。
2. **生成目录+新鲜度门禁**（scripts/gen-*.ts）：tool/config/persistence catalog 从源码生成，verify-*-catalog 门禁 CI 失败即红；完整性守卫 glob 匹配新增包漏生成直接失败。→ API/配置目录从 OpenAPI schema 生成，CI「生成→比对→不一致即红」。
3. **Agent Note 生命周期**（.agents/notes/README.md）：路径 {lifecycle}/{class}/yyyy-mm-dd-topic.md（lifecycle=proposed/implemented/rejected/archived）；「每个非平凡变更必须同 PR 附至少一篇」；状态必须与文件夹一致（门禁交叉检查）；正文 ## Problem 开头；implemented 只记已交付事实，严禁改写旧 Note。→ 建 docs/decisions/ + 20 行校验脚本。
4. **postmortem 体系**（docs/postmortem/README.zh.md）：只有「隐蔽+系统性+重新发现代价高」的 bug 才写；摘要先行、时间线以持久化事件序列为依据不反推意图；「复盘必须产生至少一条防回归的测试或规则」。→ 咱们每个实况修完强制产出一条回归用例，制度化。
5. **词数预算门禁**（verify-doc-budgets）：每个常驻文档设上限（根 AGENTS.md ≤1600 词），超限先搬迁再删减最后才提额。→ 给 CLAUDE.md 定上限，防指令文档膨胀稀释上下文。
6. **新行为归属表**（architecture.zh.md 末尾）：「我要加 X → 改哪里」20 余行表，并声明改动循环本身时同步更新。→ 维护「加 API 端点→app/routers/；加权限→依赖项」表，既是新人入口也是评审 checklist。
7. **type-equiv 防漂移**：文档粘贴类型声明用 ```ts type-equiv 围栏+manifest 登记，verify-type-equiv 用解析器从源码提取声明比对（含 JSDoc）。→ 文档引用关键签名时写 AST 检查器断言一致。
8. **规范术语表**（docs/glossary.zh.md）：每个领域概念唯一规范术语，禁隐喻、写完整契约。→ docs/glossary 定义 session/turn/task 等核心词。
9. **AGENTS.md symlink 单一真源**：多子目录的 CLAUDE.md 都是 AGENTS.md 的 symlink，规则行自包含 1-3 行+链接。→ 多子目录指令用 symlink 不复制粘贴。
10. **图集维护模式声明**（graph-atlas）：每张图标注 generated/hybrid/curated，图源文件头部声明生成命令，verify-mermaid 校验语法。→ 图与代码脱节是文档腐烂最快形态。
11. **子系统目录即索引**（subsystems/README.zh.md）：每页一行表，刻意不做集中 INDEX（集中索引必漂移）。→ docs/ README 表格每页一行。
12. **预发布立场声明**（AGENTS.md）：预发布期优先正确地基而非兼容垫片，可自由重命名/重构；「首个 tagged release 时必须删除本节」。→ CLAUDE.md 显式声明兼容立场，AI 协作者不再因怕破坏兼容不敢重构。
13. **双语一致性边车**（.i18n.yaml）：记录两侧 git blob hash 作为「最后确认一致」，--write 重录，git 合并驱动自动推导冲突。→ 仅双语仓库需要。

## 三、测试与工具链（14 条）

1. **run-gates 门禁矩阵**（scripts/run-gates.ts）：一张表声明所有 gate（id/label/command/needs/allowFailure），运行时依赖 DAG+有界并发+分片汇总；每种 CI 模式只是表的 mode。→ run_eval/test_invariants/test_guards 合成一张门禁表。
2. **快照三态 replay/record/refresh**（vitest.snapshot.config.ts）：replay 无 key 默认并行；record 才调真实 API；refresh 只重放已提交脚本；record/refresh 强制串行防写坏金样。→ LLM 评测照搬三态：CI 永远 replay，本地 --record 才花钱。
3. **llm-replay + assertConsumed**（packages/test-support/llm-replay/）：fixture 即真实会话日志，按 (turn,step) 重建 chunk 流；assertConsumed 断言每个脚本都被消费、游标排干——场景少调模型会明确报错。→ mock LLM 以真实日志为黄金+消费断言。
4. **gen-X/verify-X 结对**：生成器自带 --check 与自测；产物提交仓库由 --check 保新鲜；rescope-vendor 带 --reverse 可逆。→ 生成类脚本统一「生成+新鲜度门+自测」三件套。
5. **不变量伴生文件强制约定**（scripts/test-invariants.ts）：setup 拦截插件装载，测试根自动装本包伴生不变量，找不到伴生直接抛错；tsconfig paths 通配符让「每包必须有 invariant」成为编译期约定。→ test_invariants 变成「框架强制每模块贡献伴生检查文件」。
6. **覆盖率 100% per-file + 成员资格契约豁免**（vitest.config.ts、scripts/coverage-exempt.ts）：失败打印精确 path:line:col；豁免用探针条件激活（resolvePwshPath 探测存在才生效），不是静态白名单。→ 豁免只在环境真跑不了时生效。
7. **平台条件化测试精确到文件**（windowsUnsupportedPackages 显式包名列表+注释为何保留邻居套件）。→ 跨平台跳过精确到套件并注释。
8. **lefthook 快钩子**：pre-commit 只做几百毫秒内的事（staged 增量 lint+--fix 回写暂存区+i18n 校验）；可再生的文件自动重生成而非拒绝；pre-push 只 typecheck。→ 钩子只挂增量快检查。
9. **「验证外部世界，而非自我报告」**（docs/testing.md）：e2e 断言重新执行命令或外部读文件，关键词探测会让作弊 agent 通过。→ 回归断言从 answer_contain 升级为查库验证实际状态。
10. **「带密钥策略：推理很便宜」**：无 key 测试只证明管线通，with-key 才证明产品真能用；keyless CI 靠 self-skip 保持绿而非削减真实测试。→ 每次大改后跑真实链路冒烟。
11. **「回归测试必须证明有效」**：引入回归→观察变红→回退，证明守卫真能拦住。→ 新守卫用例先做变红验证。
12. **真实入口路径测试**：包 bin 跑 built lib 而非 tsx（暴露结算竞态/模块解析/被吞的加载失败）；构建产物冒烟测试保持绿。→ 测部署产物而非源码路径。
13. **examples 即测试资产**：每个示例强制「无 key 冒烟+有 key 冒烟+快照」三件套；可复用逻辑必须下沉 packages/ 受覆盖率约束。→ demo 数据/脚本按此规范。
14. **vitest 六车道**：unit/e2e/snapshot/web/web-stress/web-perf 独立配置独立 script，默认配置绝不 include 重型车道，并发上限环境变量旋钮。→ 不同成本测试拆独立命令。

## 落地优先级建议

P0（直接治打地鼠病根）：三-9（验证外部世界）、三-10（带 key 冒烟常态化）、三-11（回归变红验证）、二-4（postmortem 制度）
P1（架构演进）：一-1（单调守卫）、一-3（运行时不变量）、一-4（append-only 审计日志）
P2（工程化）：三-1（门禁矩阵）、二-1（文档分层）、二-3（Agent Note）
