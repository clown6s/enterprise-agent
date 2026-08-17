# -*- coding: utf-8 -*-
"""
飞书渠道（P5）：官方 lark-oapi SDK 长连接（WS，自动重连）→ 消息处理 → 回复。

- 文本消息 → agent 作答 → im/v1 回复（纯文本）
- 图片消息 → 下载 → 智谱 glm-4v-flash OCR → 按群建立文件上下文，
  后续提问自动结合；「入库」把识别文本写入知识库；「清除文件」移除
- 按 message_id 去重（飞书重投语义）
"""
import json
import threading
import time

import httpx

from .. import config

# 官方 SDK 依赖较重且日志啰嗦，只在这里引入（软依赖：无 lark-oapi 则渠道禁用）
try:
    import lark_oapi
    from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast)
    from lark_oapi.ws import Client as WsClient
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


class FeishuChannel:
    def __init__(self):
        self.app_id = config._env("FEISHU_APP_ID")
        self.app_secret = config._env("FEISHU_APP_SECRET")
        self.zhipu_key = config._env("ZHIPU_API_KEY")
        self._app_token: str | None = None
        self._token_expire = 0.0
        self._ws = None
        # 按群的图片 OCR 文件上下文（chat_id → 文本）
        self._file_contexts: dict[str, str] = {}
        # 按群的最近一次问答（卡片 👍👎 回调用，值只带 action 不带全文）
        self._last_answers: dict[str, dict] = {}
        self._seen: set[str] = set()

    # ---------------- 生命周期 ----------------

    def enabled(self) -> bool:
        return SDK_AVAILABLE and bool(self.app_id) and bool(self.app_secret)

    def start(self) -> None:
        if not self.enabled():
            return
        dispatcher = (EventDispatcherHandler.builder(self.app_id, self.app_secret)
                      .register_p2_im_message_receive_v1(self._on_message)
                      .register_p2_card_action_trigger(self._on_card_action)
                      .build())
        self._ws = WsClient(self.app_id, self.app_secret,
                            log_level=lark_oapi.LogLevel.ERROR,
                            event_handler=dispatcher)
        t = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        t.start()

    def _run(self) -> None:
        try:
            self._ws.start()   # 阻塞；SDK 内部自动重连
        except Exception as e:
            print(f"[飞书] 长连接异常退出: {e}")

    # ---------------- token ----------------

    def _app_access_token(self) -> str | None:
        if self._app_token and time.time() < self._token_expire - 60:
            return self._app_token
        try:
            resp = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                self._app_token = data["app_access_token"]
                self._token_expire = time.time() + data.get("expire", 7200)
                return self._app_token
        except httpx.HTTPError:
            pass
        return None

    # ---------------- 消息入口（SDK 线程） ----------------

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            msg = data.event.message
            message_id = getattr(msg, "message_id", None) or ""
            chat_id = getattr(msg, "chat_id", None) or ""
            if not message_id or message_id in self._seen:
                return
            self._seen.add(message_id)
            if len(self._seen) > 2000:
                self._seen.clear()
            mtype = getattr(msg, "message_type", None) or ""
            content = getattr(msg, "content", None) or "{}"
            # 提问者标识（open_id）——检索 ACL 过滤与审计/反馈落库用
            open_id = ""
            try:
                open_id = getattr(data.event.sender.sender_id, "open_id", "") or ""
            except Exception:
                pass
            threading.Thread(target=self._handle, daemon=True,
                             args=(chat_id, message_id, mtype, content, open_id)).start()
        except Exception as e:
            print(f"[飞书] 事件处理异常: {e}")

    def _handle(self, chat_id: str, message_id: str, mtype: str, content: str,
                open_id: str = "") -> None:
        if mtype == "text":
            try:
                text = (json.loads(content).get("text") or "").strip()
            except (json.JSONDecodeError, AttributeError):
                text = ""
            if not text:
                return
            self._handle_text(chat_id, message_id, text, open_id)
        elif mtype == "image":
            self._handle_image(chat_id, message_id, content)
        else:
            pass   # 其他消息类型忽略（文件/卡片等后续迭代）

    # ---------------- 文本 ----------------

    def _handle_text(self, chat_id: str, message_id: str, text: str, open_id: str = "") -> None:
        try:
            self._handle_text_inner(chat_id, message_id, text, open_id)
        except Exception as e:
            # 生成链路意外异常（LLM 挂/嵌入挂等）→ 兜底回复，不让飞书用户石沉大海
            print(f"[飞书] 文本处理异常: {e!r}")
            self._reply(message_id, "抱歉，回答生成失败，请稍后重试。")

    def _handle_text_inner(self, chat_id: str, message_id: str, text: str, open_id: str = "") -> None:
        # 文件上下文指令
        if text.strip() in ("清除文件", "移除文件"):
            removed = self._file_contexts.pop(chat_id, None)
            self._reply(message_id, "已移除文件上下文。" if removed else "当前没有文件上下文。")
            return
        if text.strip() in ("入库", "存入知识库"):
            ocr_text = self._file_contexts.pop(chat_id, None)
            if not ocr_text:
                self._reply(message_id, "当前没有可入库的图片识别内容。")
                return
            self._ingest_ocr_text(ocr_text, message_id)
            return
        # 文档生成：触发词命中但标题不可推断 → 弹表单卡片兜底
        from ..service import docwizard
        if docwizard.matches(text) and not docwizard.guess_title(text):
            if not self._reply_card(message_id, self.build_doc_form_card()):
                self._reply(message_id, "请告诉我文档标题，例如：「帮我生成关于五一的放假通知」。")
            return
        # 常规问答（带文件上下文）→ 卡片回复（带 👍👎 反馈按钮）
        file_block = self._file_contexts.get(chat_id)
        from ..service import agent
        result = agent.answer(text, chat_id, file_block=file_block, user=open_id,
                              endpoint="feishu/chat")
        last = {"query": text, "answer": result["answer"],
                "source": (result["sources"] or [None])[0] or "",
                "pairs": result.get("_learned_pairs") or []}
        card_msg_id = ""
        if result.get("chart"):
            # 数据类回答 → 图表卡（VChart + 原生表格 + 汇总）
            card_msg_id = self._reply_card(message_id, self.build_data_card(
                result["answer"], result["chart"])) or ""
            if not card_msg_id:
                self._reply(message_id, result["answer"])
        else:
            suggestions = []
            if (result.get("route") == "KNOWLEDGE_QA" and result.get("sources")
                    and len(result["answer"]) > 50):
                from ..service import agent as _agent
                suggestions = _agent.suggest_followups(text, result["answer"])
            card_msg_id = self._reply_card(message_id, self.build_answer_card(
                result["answer"], result["sources"], suggestions)) or ""
            if not card_msg_id:
                self._reply(message_id, result["answer"])
        # 按卡片消息 id 存问答（👍👎 回调用 open_message_id 精确对应——曾按
        # chat_id 单槽存，连问两个问题后点第一个卡会采纳第二个的答案，污染知识库）
        if card_msg_id:
            self._last_answers[card_msg_id] = last
        else:
            self._last_answers[chat_id] = last   # 卡片失败回退纯文本

    def _handle_suggestion(self, chat_id: str, msg_id: str, q: str, open_id: str = "") -> None:
        """延伸问题异步作答：回复新卡片（带新一轮延伸推荐）"""
        try:
            from ..service import agent
            result = agent.answer(q, chat_id, user=open_id, endpoint="feishu/chat")
            last = {"query": q, "answer": result["answer"],
                    "source": (result["sources"] or [None])[0] or "",
                    "pairs": result.get("_learned_pairs") or []}
            suggestions = []
            if (result.get("route") == "KNOWLEDGE_QA" and result.get("sources")
                    and len(result["answer"]) > 50):
                suggestions = agent.suggest_followups(q, result["answer"])
            if msg_id:
                card_msg_id = self._reply_card(msg_id, self.build_answer_card(
                    result["answer"], result["sources"], suggestions))
                if card_msg_id:
                    self._last_answers[card_msg_id] = last   # 按卡片 id 存，反馈不错位
                    return
            self._reply(msg_id, result["answer"])
            self._last_answers[chat_id] = last
        except Exception as e:
            print(f"[飞书] 延伸问答异常: {e!r}")
            self._reply(msg_id or "", "抱歉，回答生成失败，请稍后重试。")

    # ---------------- 图片 OCR ----------------

    def _handle_image(self, chat_id: str, message_id: str, content: str) -> None:
        try:
            image_key = json.loads(content).get("image_key") or ""
        except (json.JSONDecodeError, AttributeError):
            image_key = ""
        if not image_key:
            self._reply(message_id, "未识别到图片。")
            return
        token = self._app_access_token()
        if not token:
            self._reply(message_id, "飞书鉴权失败，请稍后再试。")
            return
        try:
            resp = httpx.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
                f"?type=image",
                headers={"Authorization": f"Bearer {token}"}, timeout=30,
            )
            resp.raise_for_status()
            image_bytes = resp.content
        except httpx.HTTPError:
            self._reply(message_id, "图片下载失败，请重试。")
            return
        text = self._ocr(image_bytes)
        if not text:
            self._reply(message_id, "图片里没有识别到文字。")
            return
        self._file_contexts[chat_id] = text
        preview = text[:200] + ("…（全文已存）" if len(text) > 200 else "")
        self._reply(message_id,
                    f"已识别图片文字（{len(text)} 字）：\n{preview}\n\n"
                    "接下来的提问我会结合图片内容回答；回复「入库」把识别内容存入知识库；"
                    "回复「清除文件」移除。")

    def _ocr(self, image_bytes: bytes) -> str:
        """图片识别：走模型注册表 ocr 条目（曾写死智谱 glm-4v-flash——
        与文档解析的 OCR 两套供应商；注册表统一后换模型只改 .env）"""
        from .. import llm
        try:
            return llm.chat_image(
                "你是一个图片文字识别助手。",
                "识别图片中的所有文字，直接输出文字内容，不要任何解释。",
                image_bytes).strip()
        except Exception:
            return ""

    # ---------------- 入库 ----------------

    def _ingest_ocr_text(self, text: str, message_id: str) -> None:
        import hashlib
        from .. import embeddings, milvus_store
        qh = hashlib.sha256(text.encode()).hexdigest()[:16]
        doc_id = f"ocr-{qh}"
        # 去重：同一识别内容重复入库会产生重复 chunk（与上传管线同款防护）
        if milvus_store.has_document(doc_id):
            self._reply(message_id, f"该内容已在知识库中（《图片识别_{qh[:8]}》），无需重复入库。")
            return
        vec = embeddings.embed(text)
        ok = milvus_store.insert_document(
            text=text, vector=vec, document_id=doc_id,
            title=f"图片识别_{qh[:8]}", source="飞书图片OCR")
        self._reply(message_id,
                    "已入库《图片识别_%s》，现在知识库检索可以找到它了。" % qh[:8]
                    if ok else "入库失败，请稍后再试。")

    # ---------------- 卡片 ----------------

    @staticmethod
    def build_data_card(answer: str, chart: dict) -> dict:
        """数据图表卡（schema 2.0）：VChart + 原生表格 + 汇总
        （表格列名用 c0/c1 索引名、数值列传数字类型、chart_spec 带 title）"""
        head = chart.get("table", [["-"]])[0]
        rows = chart.get("table", [])[1:]
        # 数值列检测：除表头外全部可解析为数字 → number（右对齐+千分位）
        numeric = []
        for c in range(len(head)):
            all_num = True
            for row in rows:
                try:
                    float(str(row[c]))
                except (ValueError, IndexError, TypeError):
                    all_num = False
                    break
            numeric.append(all_num)
        columns = [{"name": "c%d" % c, "display_name": head[c],
                    "data_type": "number" if numeric[c] else "text"}
                   for c in range(len(head))]
        row_maps = []
        for row in rows:
            m = {}
            for c in range(len(head)):
                cell = str(row[c]) if c < len(row) else ""
                if numeric[c]:
                    try:
                        m["c%d" % c] = float(cell)
                    except ValueError:
                        m["c%d" % c] = cell
                else:
                    m["c%d" % c] = cell
            row_maps.append(m)
        vchart = {
            "type": chart.get("chartType", "line"),
            "data": {"values": chart.get("values", [])},
            "xField": chart.get("xField", "date"),
            "yField": chart.get("yField", "value"),
            "seriesField": chart.get("seriesField", "series"),
            "title": {"text": chart.get("title", "数据")},
        }
        elements = [{"tag": "markdown", "content": answer[:2000]}]
        elements.append({"tag": "chart", "chart_spec": vchart})   # snake_case（camelCase 会被拒 400）
        if rows:
            elements.append({"tag": "table", "page_size": 10,
                             "columns": columns, "rows": row_maps})
            if chart.get("summary"):
                elements.append({"tag": "markdown", "content": "**" + chart["summary"] + "**"})
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown",
                         "content": "数据来源：OA 业务系统 · 图表由 VChart 渲染"})
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "数字员工数据看板"},
                       "template": "blue"},
            "body": {"elements": elements},
        }

    @staticmethod
    def build_doc_form_card() -> dict:
        """文档生成表单卡片（schema 2.0 表单容器：标题 + 内容要点）"""
        return {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "📝 文档生成"},
                       "template": "blue"},
            "body": {"elements": [
                {"tag": "input", "name": "title", "input_type": "text",
                 "label": {"tag": "plain_text", "content": "文档标题（必填）"},
                 "placeholder": {"tag": "plain_text", "content": "如：关于五一的放假通知"}},
                {"tag": "input", "name": "points", "input_type": "multiline_text",
                 "label": {"tag": "plain_text", "content": "内容要点（可选）"},
                 "placeholder": {"tag": "plain_text", "content": "如：放假时间、调休安排、注意事项"}},
            ], "form_action_type": "submit"},
        }

    @staticmethod
    def build_answer_card(answer: str, sources: list[str], suggestions: list[str] = ()) -> dict:
        """回答卡片（schema 1.0）：markdown 正文 + 来源 note + 延伸追问按钮 + 👍👎。
        suggestions：'你可能还想问'按钮（点击即追问）。"""
        source_note = "、".join(sources) if sources else "无来源"
        # schema 1.0：无 schema 键 = 1.0，elements 在顶层；
        # action 按钮/note 组件在 1.0 支持（2.0 会拒 unsupported tag action）
        actions = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"💡 {s[:28]}"},
            "type": "default", "value": {"action": "suggest", "q": s},
        } for s in suggestions]
        actions += [
            {"tag": "button",
             "text": {"tag": "plain_text", "content": "👍 有帮助"},
             "type": "primary", "value": {"action": "helpful"}},
            {"tag": "button",
             "text": {"tag": "plain_text", "content": "👎 无帮助"},
             "type": "default", "value": {"action": "unhelpful"}},
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "🤖 数字员工回答"},
                       "template": "blue"},
            "elements": [
                {"tag": "markdown", "content": answer[:6000]},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "📄 来源：" + source_note}]},
                {"tag": "action", "actions": actions},
            ],
        }

    def _reply_card(self, message_id: str, card: dict) -> str:
        """发卡片；成功返回卡片 message_id（反馈回调定位用），失败返回空串"""
        token = self._app_access_token()
        if not token:
            return ""
        try:
            resp = httpx.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
                f"?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"msg_type": "interactive",
                      "content": json.dumps(card, ensure_ascii=False)},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[飞书] 卡片发送失败 http={resp.status_code} body={resp.text[:300]}")
                return ""
            body = resp.json()
            if body.get("code") != 0:
                print(f"[飞书] 卡片业务拒绝 code={body.get('code')} "
                      f"msg={body.get('msg')} body={resp.text[:300]}")
                return ""
            return (body.get("data") or {}).get("message_id", "") or ""
        except (httpx.HTTPError, ValueError):
            return ""

    def _on_card_action(self, trigger: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """卡片按钮回调 → 反馈闭环（👍 采纳回流 / 👎 自动锁用例）"""
        from ..learn import feedback
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        try:
            action = (trigger.event.action.value or {}).get("action", "")
            ctx = trigger.event.context
            chat_id = (getattr(ctx, "open_chat_id", None)
                       or getattr(ctx, "chat_id", None) or "")
            open_id = getattr(trigger.event.operator, "open_id", "") or ""
            # 优先按卡片 message_id 精确定位（曾按 chat_id 单槽——连续问答时
            # 点旧卡的👍会采纳最新一条答案，反馈错位污染知识库）
            card_msg_id = getattr(ctx, "open_message_id", None) or ""
            last = self._last_answers.get(card_msg_id) or self._last_answers.get(chat_id)
            if not last:
                toast.content = "未找到对应问答记录（可能已过期）"
                resp.toast = toast
                return resp
            if action == "doc_form_submit":
                from ..service import docwizard
                form = (trigger.event.action.form_value or {})
                title = (form.get("title") or "").strip()
                result = docwizard.generate_with_form(chat_id, title, form.get("points") or "",
                                                      getattr(trigger.event.operator, "open_id", "") or "")
                # 曾引用未定义的 doc_id → NameError 被外层吞掉，表单生成恒"反馈处理异常"
                doc_id = docwizard._active_docs.get(chat_id, {}).get("doc_id", "") if result else ""
                if doc_id:
                    msg_id = getattr(trigger.event.context, "open_message_id", None) or ""
                    self._reply(msg_id, "已生成《" + title + "》云文档（在线可编辑）：" + chr(10)
                                + "https://feishu.cn/docx/" + doc_id)
                    toast.content = "已生成，请查看消息回复"
                else:
                    toast.content = "生成失败，请重试"
            elif action == "suggest":
                q = ((trigger.event.action.value or {}).get("q") or "").strip()
                if q:
                    # 卡片回调有 3s 超时，问答链路必须异步（toast 先回"正在查询"）
                    toast.content = "正在查询…"
                    msg_id = getattr(trigger.event.context, "open_message_id", None) or ""
                    threading.Thread(target=self._handle_suggestion, daemon=True,
                                     args=(chat_id, msg_id, q, open_id)).start()
                else:
                    toast.content = "无效的追问"
            elif action == "helpful":
                result = feedback.adopt(last["query"], last["answer"], last["source"], user=open_id)
                toast.content = result.get("error") or "已采纳，该问答已回流知识库"
            elif action == "unhelpful":
                # 恢复答案被点👎：其使用的自学习词对失败计数（满 2 次移除）
                # ——曾词对只增不减，坏词对永久固化
                from ..learn import synonyms
                for t, e in last.get("pairs") or []:
                    synonyms.penalize(t, e)
                result = feedback.record_unhelpful(last["query"], last["answer"], user=open_id)
                toast.content = ("已记录并生成回归用例，我会改进"
                                 if result.get("status") == "case_created"
                                 else "已记录，我会改进")
            else:
                toast.content = "未知操作"
        except Exception as e:
            toast.content = "反馈处理异常"
            print(f"[飞书] 卡片回调异常: {e}")
        resp.toast = toast
        return resp

    # ---------------- 回复 ----------------

    def _reply(self, message_id: str, text: str) -> None:
        token = self._app_access_token()
        if not token:
            return
        try:
            httpx.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
                f"?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"msg_type": "text",
                      "content": json.dumps({"text": text[:6000]}, ensure_ascii=False)},
                timeout=15,
            )
        except httpx.HTTPError as e:
            print(f"[飞书] 回复失败 messageId={message_id}: {e}")


channel = FeishuChannel()
