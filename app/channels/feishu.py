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
            threading.Thread(target=self._handle, daemon=True,
                             args=(chat_id, message_id, mtype, content)).start()
        except Exception as e:
            print(f"[飞书] 事件处理异常: {e}")

    def _handle(self, chat_id: str, message_id: str, mtype: str, content: str) -> None:
        if mtype == "text":
            try:
                text = (json.loads(content).get("text") or "").strip()
            except (json.JSONDecodeError, AttributeError):
                text = ""
            if not text:
                return
            self._handle_text(chat_id, message_id, text)
        elif mtype == "image":
            self._handle_image(chat_id, message_id, content)
        else:
            pass   # 其他消息类型忽略（文件/卡片等后续迭代）

    # ---------------- 文本 ----------------

    def _handle_text(self, chat_id: str, message_id: str, text: str) -> None:
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
        result = agent.answer(text, chat_id, file_block=file_block)
        self._last_answers[chat_id] = {
            "query": text, "answer": result["answer"],
            "source": (result["sources"] or [None])[0] or "",
        }
        if result.get("chart"):
            # 数据类回答 → 图表卡（VChart + 原生表格 + 汇总）
            if not self._reply_card(message_id, self.build_data_card(
                    result["answer"], result["chart"])):
                self._reply(message_id, result["answer"])
        elif not self._reply_card(message_id, self.build_answer_card(
                result["answer"], result["sources"])):
            self._reply(message_id, result["answer"])   # 卡片失败回退纯文本

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
        """智谱 glm-4v-flash 图片识别"""
        import base64
        if not self.zhipu_key:
            return ""
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
        try:
            resp = httpx.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {self.zhipu_key}"},
                json={
                    "model": "glm-4v-flash",
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": "识别图片中的所有文字，直接输出文字内容，不要任何解释。"},
                    ]}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
        except (httpx.HTTPError, KeyError, IndexError):
            return ""

    # ---------------- 入库 ----------------

    def _ingest_ocr_text(self, text: str, message_id: str) -> None:
        import hashlib
        from .. import embeddings, milvus_store
        qh = hashlib.sha256(text.encode()).hexdigest()[:16]
        vec = embeddings.embed(text)
        ok = milvus_store.insert_document(
            text=text, vector=vec, document_id=f"ocr-{qh}",
            title=f"图片识别_{qh[:8]}", source="飞书图片OCR")
        self._reply(message_id,
                    "已入库《图片识别_%s》，现在知识库检索可以找到它了。" % qh[:8]
                    if ok else "入库失败，请稍后再试。")

    # ---------------- 卡片 ----------------

    @staticmethod
    def build_data_card(answer: str, chart: dict) -> dict:
        """数据图表卡（schema 2.0）：VChart 折线/柱状 + 原生表格 + 汇总 + 来源说明"""
        def _cell_type(v: str) -> str:
            try:
                float(v)
                return "number"
            except (TypeError, ValueError):
                return "text"
        head = chart.get("table", [["-"]])[0]
        rows = chart.get("table", [])[1:]
        columns = [{"name": h, "display_name": h,
                    "data_type": _cell_type(rows[0][i]) if rows else "text"}
                   for i, h in enumerate(head)]
        vchart = {
            "type": chart.get("chartType", "line"),
            "data": {"values": chart.get("values", [])},
            "xField": chart.get("xField", "date"),
            "yField": chart.get("yField", "value"),
            "seriesField": chart.get("seriesField", "series"),
            "height": 280,
        }
        return {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": chart.get("title", "数据看板")},
                       "template": "blue"},
            "body": {"elements": [
                {"tag": "markdown", "content": answer[:2000]},
                {"tag": "chart", "chart_spec": vchart},
                {"tag": "table", "columns": columns,
                 "rows": [dict(zip(head, row)) for row in rows]},
                {"tag": "markdown", "content": "**" + (chart.get("summary") or "") + "**"},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": "数据来源：OA 业务系统 · 图表由 VChart 渲染"}]},
            ]},
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
    def build_answer_card(answer: str, sources: list[str]) -> dict:
        """回答卡片（schema 2.0）：markdown 正文 + 来源 note + 👍👎 反馈按钮"""
        source_note = "、".join(sources) if sources else "无来源"
        return {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "🤖 数字员工回答"},
                       "template": "blue"},
            "body": {"elements": [
                {"tag": "markdown", "content": answer[:6000]},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "📄 来源：" + source_note}]},
                {"tag": "action", "actions": [
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "👍 有帮助"},
                     "type": "primary", "value": {"action": "helpful"}},
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "👎 无帮助"},
                     "type": "default", "value": {"action": "unhelpful"}},
                ]},
            ]},
        }

    def _reply_card(self, message_id: str, card: dict) -> bool:
        token = self._app_access_token()
        if not token:
            return False
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
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

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
            last = self._last_answers.get(chat_id)
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
                if result:
                    from ..service import docwizard as _dw
                    url = docwizard._active_docs.get(chat_id, {}).get("doc_id", "")
                    if url:
                        msg_id = getattr(trigger.event.context, "open_message_id", None) or ""
                        self._reply(msg_id, "已生成《" + title + "》云文档（在线可编辑）：" + chr(10)
                                    + "https://feishu.cn/docx/" + doc_id)
                toast.content = "已生成，请查看消息回复"
            elif action == "helpful":
                feedback.adopt(last["query"], last["answer"], last["source"])
                toast.content = "已采纳，该问答已回流知识库"
            elif action == "unhelpful":
                result = feedback.record_unhelpful(last["query"], last["answer"])
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
