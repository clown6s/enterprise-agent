# -*- coding: utf-8__
import json
from upload_test_common import parsed, classify_document

EVIDENCE_EXPLANATIONS = {
    "definition_density": "连续出现较多‘术语：解释’结构，属于词条/定义类文档。",
    "question_answer_pairs": "检测到多组‘编号问题/疑问句 + 答案段落’配对，属于问答类文档。",
    "article_structure": "检测到‘第X条’及编、篇、章、节层级，属于法规条文类文档。",
    "outline_lines": "检测到章节标题、层级编号和说明正文，属于层级大纲/业务手册类文档。",
    "medical_signals": "检测到诊断、治疗、症状、患者、临床或医学单位等结构信号，属于医学混合类文档。",
    "technical_signals": "检测到目录、章节、API、代码、函数、模块或示例等技术书信号。",
    "dotted_leaders": "检测到‘标题 + 点线/空格 + 页码’的目录行，属于目录结构。",
    "fallback": "没有 profile 达到置信度阈值，安全回退到通用切分。",
    "ambiguous": "多个 profile 置信度接近，无法安全判断，回退到通用切分。",
}

if __name__ == '__main__':
    path, data, text, diagnostics = parsed()
    c = classify_document(text, path)
    evidence = list(c.evidence)
    print(json.dumps({
        'stage': 'structure', 'file': path, 'profile': c.kind,
        'confidence': c.confidence, 'evidence': evidence,
        'evidence_details': [EVIDENCE_EXPLANATIONS.get(x, f'未登记的证据类型：{x}')
                             for x in evidence],
    }, ensure_ascii=False, indent=2))