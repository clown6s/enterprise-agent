# -*- coding: utf-8 -*-
"""
文档生成（一句话直达云文档，Java 版 DocWizardService 的核心路径移植）：
触发词 → 标题推断 → LLM 生成（防编造规则 + 类型标准结构）→ 飞书云文档直出
（org 可读 + 发起者编辑权）→ 回复文档链接。

未移植（路线图）：多轮修改闭环、表单卡片兜底、docx 文件兜底。
"""
import json
import re
from datetime import datetime

import httpx

from .. import config, llm
from ..channels.feishu import channel as feishu_channel

# ---------------- 触发与标题 ----------------

_TRIGGER = re.compile(
    r"生成.{0,10}(文档|通知|报告|方案|周报|纪要|安排|手册|指南)"
    r"|写.{0,10}(文档|通知|报告|方案|周报|纪要|安排|手册|指南)"
    r"|起草|帮我写|拟一份"
    r"|关于.{2,40}(通知|报告|方案|周报|纪要|安排|文档|手册|指南)"
    r"|做.{0,12}(攻略|手册|指南|方案|计划)|(旅游|旅行)?攻略$"
)

_GENERATE_PROMPT = """你是企业文档撰写助手。请按【{doc_type}】的标准结构撰写《{title}》。
写作要求：
1. 只输出正文本身（不要"好的""以下是"等前缀或任何解释）
2. 每个章节用「## 」开头的标题行，章节下条目用「1. 2. 3.」编号
3. 严禁编造事实：不得杜撰统计数字、百分比、日期年份、法规名称与条文、
   机构名称、人名、价格、时间表——用户未提供的一律不写；
   时效信息（门票/开放时间/班次等）无法确定时写「以官方发布为准」
4. 通用文档按说明文体撰写，不写成"须/不得/应"式规章制度条文
5. 使用规范中文术语，不要中英混写
6. 今天是 {today}，涉及年份与星期必须与实际日历一致

用户要点：
{points}
"""

_DOC_STRUCTURES = {
    "攻略": "一、景点概况\n二、必看景点（各景点看点说明）\n三、建议游览路线\n"
            "四、游览贴士（时间安排/穿着/注意事项）\n五、周边延伸\n"
            "（门票价格、开放时间、交通班次等时效信息写「以景区官方发布为准」）",
}


def guess_title(query: str) -> str | None:
    m = re.search(r"(关于[^，。；\n]{2,40})", query)
    if m:
        return m.group(1)
    m = re.search(r"[《「]([^》」]{2,40})[》」]", query)
    if m:
        return m.group(1)
    stripped = query.strip()
    if len(stripped) <= 30 and not re.search(r"[，。；、\n]", stripped):
        title = re.sub(
            r"^(给我|帮我|请|麻烦)?(生成|起草|拟一份|写一个|写一份|写份|写个|写|做一个|做个|做一份|做份|做)",
            "", stripped).strip()
        title = re.sub(r"^(去|来)", "", title).strip()
        if len(title) >= 4:
            if not title.startswith("关于") and re.search(r"(通知|报告|方案|周报|纪要)$", title):
                return "关于" + title
            if title.endswith("攻略"):
                title = re.sub(r"的旅游攻略$", "旅游攻略", title).replace("的攻略", "攻略")
            return title
    return None


def matches(query: str) -> bool:
    return _TRIGGER.search(query) is not None


# 按会话的进行中文档（chat_id → {title, doc_id, content}），群聊修改指令回写同一份
_active_docs: dict = {}
_REVISE_WORDS = re.compile(r"修改|改一下|精简|调整|更新|补充|删除|删掉|压缩|润色|第二段|第三段|第一段")


def revise(chat_id: str, query: str) -> str | None:
    """多轮修改闭环：会话有进行中文档且问法是修改指令 → LLM 改写 → 回写云文档。
    返回更新后的云文档链接；未命中/失败返回 None。"""
    doc = _active_docs.get(chat_id)
    if not doc or not _REVISE_WORDS.search(query):
        return None
    try:
        new_content = llm.chat(
            "", "你是文档编辑。按用户指令修改原文，直接输出修改后的完整正文，不要解释。\n\n"
                "【原文】\n" + doc["content"] + "\n\n【修改指令】\n" + query,
        ).strip()
    except Exception:
        return None
    if not new_content or new_content == doc["content"]:
        return None
    doc["content"] = new_content
    if doc["doc_id"] and rewrite_cloud_doc(doc["doc_id"], new_content):
        return f"https://feishu.cn/docx/{doc['doc_id']}"
    return None


def try_generate(query: str, editor_open_id: str = "", chat_id: str = "") -> dict | None:
    """
    一句话直达：命中返回 {answer(含文档链接), route:"DOC_WIZARD"}；
    触发词命中但标题不可推断 → {needs_form: True}（渠道弹表单卡片兜底）；
    未命中/失败返回 None。
    """
    title = guess_title(query)
    if not matches(query):
        return None
    if not title:
        return {"needs_form": True}
    return _generate(title, query, editor_open_id, chat_id)


def generate_with_form(chat_id: str, title: str, points: str, editor_open_id: str = "") -> dict | None:
    """表单卡片兜底：用户填了标题 → 与一句话直达同流程生成"""
    if not title or not title.strip():
        return None
    return _generate(title.strip(), points or title, editor_open_id, chat_id)


def _generate(title: str, query: str, editor_open_id: str, chat_id: str) -> dict | None:
    doc_type = ("通知" if title.endswith("通知")
                else "攻略" if title.endswith("攻略")
                else "报告" if title.endswith("报告")
                else "方案" if title.endswith("方案")
                else "周报" if title.endswith("周报")
                else "纪要" if title.endswith("纪要") else "通用文档")
    structure = _DOC_STRUCTURES.get(doc_type, "通用文档")
    points = re.sub(r"^(帮我|请|麻烦)?(生成|起草|写一个|写|做一个|做)", "", query).strip()
    try:
        content = llm.chat(
            "", _GENERATE_PROMPT.format(
                doc_type=structure, title=title, points=points,
                today=datetime.now().strftime("%Y-%m-%d")),
        ).strip()
    except Exception:
        return None
    if not content:
        return None
    url = publish_cloud_doc(title, content, editor_open_id)
    doc_id = url.rsplit("/", 1)[-1] if url else ""
    if chat_id:
        _active_docs[chat_id] = {"title": title, "doc_id": doc_id, "content": content}
    if url:
        return {"answer": f"已生成《{title}》云文档（在线可编辑）：\n{url}\n"
                          "后续可直接在群聊回复修改要求（如「第二段精简」），我会同步更新。",
                "route": "DOC_WIZARD"}
    # 云文档不可用 → 正文直接回复（降级）
    return {"answer": f"《{title}》\n\n{content[:4000]}", "route": "DOC_WIZARD"}


def rewrite_cloud_doc(doc_id: str, content: str) -> bool:
    """云文档回写：清空全部 children → 重写（多轮修改闭环用）"""
    token = feishu_channel._app_access_token()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # 1. 收集现有 block id（分页）
        page_token = None
        ids = []
        while True:
            url = (f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks"
                   f"?page_size=500&document_revision_id=-1"
                   + (f"&page_token={page_token}" if page_token else ""))
            resp = httpx.get(url, headers=headers, timeout=15).json()
            for b in (resp.get("data", {}).get("items") or []):
                if b.get("parent_id") == doc_id:
                    ids.append(b["block_id"])
            if not resp.get("data", {}).get("has_more"):
                break
            page_token = resp.get("data", {}).get("page_token")
        # 2. 删除全部子块（逐个 DELETE；飞书无批量删接口）
        for bid in ids:
            httpx.delete(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{bid}"
                f"?document_revision_id=-1",
                headers=headers, timeout=15)
        # 3. 重写（反向 index-0 插入保持顺序）
        blocks = _md_to_blocks(content)
        for i in range((len(blocks) - 1) // 50, -1, -1):
            httpx.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
                f"/blocks/{doc_id}/children?document_revision_id=-1",
                headers=headers, json={"children": blocks[i * 50: (i + 1) * 50],
                                       "index": 0}, timeout=30)
        return True
    except (httpx.HTTPError, KeyError):
        return False


# ---------------- 云文档发布 ----------------

def publish_cloud_doc(title: str, content: str, editor_open_id: str = "") -> str | None:
    """创建 docx 云文档 → 写入正文（分块）→ org 可读 + 编辑者；返回 url，失败 None"""
    token = feishu_channel._app_access_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # 1. 创建空文档
        resp = httpx.post("https://open.feishu.cn/open-apis/docx/v1/documents",
                          headers=headers, json={"title": title}, timeout=15)
        doc_id = (resp.json().get("data", {}).get("document", {}).get("document_id")
                  or "").strip()
        if not doc_id:
            return None
        # 2. 正文写入：markdown → blocks；>50 个分块，反向在 index 0 插入保持顺序
        blocks = _md_to_blocks(content)
        for i in range((len(blocks) - 1) // 50, -1, -1):
            batch = blocks[i * 50: (i + 1) * 50]
            httpx.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
                f"/blocks/{doc_id}/children?document_revision_id=-1",
                headers=headers, json={"children": batch, "index": 0}, timeout=30)
        # 3. 权限：org 可读 + 发起者编辑
        httpx.patch(
            f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public"
            f"?type=docx",
            headers=headers, json={"link_share_entity": "tenant_readable"}, timeout=15)
        if editor_open_id:
            httpx.post(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/members"
                f"?type=docx",
                headers=headers,
                json={"member_type": "openid", "member_id": editor_open_id,
                      "perm": "full_access"}, timeout=15)
        return f"https://feishu.cn/docx/{doc_id}"
    except (httpx.HTTPError, KeyError):
        return None


def _md_to_blocks(md: str) -> list[dict]:
    """markdown → docx blocks（## → heading1，- → bullet，N. → ordered，其余 text）"""
    blocks = []
    for line in md.split("\n"):
        s = line.strip()
        if not s:
            continue
        # 内联加粗剥除（v1 纯文本；样式版路线图）
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        if s.startswith("### "):
            blocks.append(_block(4, s[4:]))
        elif s.startswith("## "):
            blocks.append(_block(3, s[3:]))
        elif re.match(r"^\d+[.、] ", s):
            blocks.append(_block(13, re.sub(r"^\d+[.、] ", "", s)))
        elif s.startswith(("- ", "* ")):
            blocks.append(_block(12, s[2:]))
        else:
            blocks.append(_block(2, s))
    return blocks


def _block(block_type: int, text: str) -> dict:
    key = {2: "text", 3: "heading1", 4: "heading2", 12: "bullet", 13: "ordered"}[block_type]
    return {
        "block_type": block_type,
        key: {"elements": [{"text_run": {"content": text,
                                         "text_element_style": {}}}]},
    }
