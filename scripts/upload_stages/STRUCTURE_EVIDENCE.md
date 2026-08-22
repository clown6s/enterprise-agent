# 结构分类阶段字段说明补充

`evidence` 是项目内部定义的机器可读分类依据代码，不是外部标准或模型内置字段。`evidence_details` 是面向人的中文解释。

- `definition_density`：连续出现较多“术语：解释”，属于词条/定义类。
- `question_answer_pairs`：检测到多组“编号问题/疑问句 + 答案段落”配对，属于问答类。
- `article_structure`：检测到“第 X 条”及编/篇/章/节层级，属于法规类。
- `outline_lines`：检测到章节、层级编号和说明项，属于大纲/业务手册类。
- `medical_signals`：检测到诊断、治疗、患者、临床、医学单位等信号，属于医学混合类。
- `technical_signals`：检测到目录、章节、API、代码、函数、模块、示例等信号，属于技术书类。
- `dotted_leaders`：检测到标题、点线/空格和页码组成的目录行，属于目录结构。
- `fallback`：没有 profile 达到置信度阈值，回退 generic。
- `ambiguous`：多个 profile 置信度接近，无法安全判断，回退 generic。
