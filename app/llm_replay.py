# -*- coding: utf-8 -*-
"""
LLM 调用录制/回放器（dsh 三-2/三-3）。

三种模式：
- record：调真 LLM，录 fixture（升级模型/改提示词后手动跑一次）
- replay：从 fixture 读，不调真 LLM（CI push 门跑这个——零成本、确定性）
- off：不拦截，调真 LLM（当前回归默认模式）

拦截 seam：patch llm.chat + llm.chat_stream（模块级全局）。
chat_chain/verify_chat 内部调 llm.chat；chat_stream_chain 调 llm.chat_stream。
一个 patch 覆盖所有降级链 + transitive caller（verify/nlsql/multihop）。

fixture key：case_id + 调用序号（不 hash system/user——{date} 每天变、检索 context
非确定，hash 太脆）。eval 已有确定性 per-case setup（clear_conversation + seeded history），
调用顺序在 case 内稳定。
"""
import json
import logging
import os
from typing import Iterator

log = logging.getLogger(__name__)


class LLMReplay:
    """LLM 调用录制/回放器。

    用法：
        replay = LLMReplay("replay", "eval/fixtures")
        replay.begin_case("guard-005")
        # ... agent.answer(query) 期间 llm.chat/chat_stream 被 mock ...
        replay.assert_consumed()
    """

    def __init__(self, mode: str, fixture_dir: str):
        self.mode = mode              # "record" | "replay" | "off"
        self.fixture_dir = fixture_dir
        self._case_id: str | None = None
        self._calls: list[dict] = []          # record 时的调用序列
        self._replay_queue: list[dict] = []    # replay 时的 fixture 队列
        self._consumed = 0                      # replay 时消费了多少条
        self._real_chat = None                  # 原始 llm.chat（patch 时保存）
        self._real_chat_stream = None           # 原始 llm.chat_stream

    def activate(self):
        """patch llm.chat + llm.chat_stream 为 mock（保存原始引用）。"""
        from . import llm
        self._real_chat = llm.chat
        self._real_chat_stream = llm.chat_stream
        llm.chat = self._mock_chat
        llm.chat_stream = self._mock_chat_stream
        log.info("LLM replay 模式=%s，fixture_dir=%s", self.mode, self.fixture_dir)

    def deactivate(self):
        """恢复原始 llm.chat + llm.chat_stream。"""
        from . import llm
        if self._real_chat is not None:
            llm.chat = self._real_chat
            llm.chat_stream = self._real_chat_stream
            self._real_chat = None
            self._real_chat_stream = None

    def begin_case(self, case_id: str):
        """切换到新用例：record 清空录制队列；replay 加载该用例 fixture。"""
        self._case_id = case_id
        self._calls = []
        self._replay_queue = []
        self._consumed = 0
        if self.mode == "replay":
            self._load(case_id)

    def _mock_chat(self, system: str, user: str, **kw) -> str:
        """替换 llm.chat 的 fake。record=调真+录；replay=返回 fixture。"""
        if self.mode == "off":
            return self._real_chat(system, user, **kw)
        if self.mode == "record":
            out = self._real_chat(system, user, **kw)
            self._calls.append({"fn": "chat", "system": system,
                                "user": user, "output": out})
            return out
        # replay
        if self._consumed >= len(self._replay_queue):
            raise RuntimeError(
                f"fixture 耗尽：{self._case_id} 第 {self._consumed + 1} 次调用无 fixture")
        entry = self._replay_queue[self._consumed]
        self._consumed += 1
        if entry["fn"] != "chat":
            raise RuntimeError(
                f"fixture 不匹配：期望 {entry['fn']}，实际 chat（{self._case_id} #{self._consumed}）")
        return entry["output"]

    def _mock_chat_stream(self, system: str, user: str, **kw) -> Iterator[str]:
        """替换 llm.chat_stream 的 fake。流式：fixture 存 chunks 列表。"""
        if self.mode == "off":
            yield from self._real_chat_stream(system, user, **kw)
            return
        if self.mode == "record":
            chunks = []
            for chunk in self._real_chat_stream(system, user, **kw):
                chunks.append(chunk)
                yield chunk
            self._calls.append({"fn": "chat_stream", "system": system,
                                "user": user, "chunks": chunks})
            return
        # replay
        if self._consumed >= len(self._replay_queue):
            raise RuntimeError(
                f"fixture 耗尽：{self._case_id} 第 {self._consumed + 1} 次调用（stream）")
        entry = self._replay_queue[self._consumed]
        self._consumed += 1
        if entry["fn"] != "chat_stream":
            raise RuntimeError(
                f"fixture 不匹配：期望 {entry['fn']}，实际 chat_stream（{self._case_id} #{self._consumed}）")
        for chunk in entry["chunks"]:
            yield chunk

    def assert_consumed(self):
        """replay 结束断言所有 fixture 被消费完——防假绿。

        代码改了多调 LLM → fixture 耗尽报错（mock_chat 里 raise）。
        代码改了少调 LLM → 这里报"未消费完"。
        """
        if self.mode == "replay" and self._consumed != len(self._replay_queue):
            raise RuntimeError(
                f"fixture 未消费完：{self._case_id} 共 {len(self._replay_queue)} 条"
                f"只用了 {self._consumed} 条——代码可能少了 LLM 调用")

    def save_case(self):
        """record 结束保存当前用例 fixture。"""
        if self.mode != "record" or not self._calls:
            return
        path = os.path.join(self.fixture_dir, f"{self._case_id}.jsonl")
        os.makedirs(self.fixture_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for entry in self._calls:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("fixture 已保存：%s（%d 条调用）", path, len(self._calls))

    def _load(self, case_id: str):
        """加载用例 fixture 到 replay 队列。"""
        path = os.path.join(self.fixture_dir, f"{case_id}.jsonl")
        if not os.path.exists(path):
            log.warning("fixture 不存在：%s（replay 模式将报'fixture 耗尽'）", path)
            return
        with open(path, encoding="utf-8") as f:
            self._replay_queue = [json.loads(l) for l in f if l.strip()]
        log.info("fixture 已加载：%s（%d 条调用）", path, len(self._replay_queue))
