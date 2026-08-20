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
        self._real_embed = None
        self._real_embed_batch = None
        self._real_rerank = None
        self._real_search = None
        self._real_dense_search = None

    def activate(self):
        """patch llm.chat + llm.chat_stream + 检索层 为 mock（保存原始引用）。

        检索 mock：replay 模式下 embeddings.embed/embed_batch/rerank +
        milvus_store.search/dense_search 从 fixture 读，不调真 Milvus/嵌入 API。
        这样 replay 只依赖 MySQL（verify_sql 用），CI 可用 service container 或
        跳过 verify_sql 用例。
        """
        from . import llm
        self._real_chat = llm.chat
        self._real_chat_stream = llm.chat_stream
        llm.chat = self._mock_chat
        llm.chat_stream = self._mock_chat_stream

        if self.mode in ("record", "replay"):
            from . import embeddings, milvus_store
            self._real_embed = embeddings.embed
            self._real_embed_batch = embeddings.embed_batch
            self._real_rerank = embeddings.rerank
            self._real_search = milvus_store.search
            self._real_dense_search = milvus_store.dense_search
            embeddings.embed = self._mock_embed
            embeddings.embed_batch = self._mock_embed_batch
            embeddings.rerank = self._mock_rerank
            milvus_store.search = self._mock_search
            milvus_store.dense_search = self._mock_dense_search

        log.info("LLM replay 模式=%s，fixture_dir=%s（含检索 mock）",
                 self.mode, self.fixture_dir)

    def deactivate(self):
        """恢复原始 llm.chat + llm.chat_stream + 检索层。"""
        from . import llm
        if self._real_chat is not None:
            llm.chat = self._real_chat
            llm.chat_stream = self._real_chat_stream
            self._real_chat = None
            self._real_chat_stream = None
        if self._real_embed is not None:
            from . import embeddings, milvus_store
            embeddings.embed = self._real_embed
            embeddings.embed_batch = self._real_embed_batch
            embeddings.rerank = self._real_rerank
            milvus_store.search = self._real_search
            milvus_store.dense_search = self._real_dense_search
            self._real_embed = None
            self._real_embed_batch = None
            self._real_rerank = None
            self._real_search = None
            self._real_dense_search = None

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

    # ---------------- 检索 mock ----------------

    def _mock_embed(self, text: str, retries: int = 2) -> list[float]:
        """replay：从 fixture 读嵌入向量；record：调真 + 录；off：透传。"""
        if self.mode == "off":
            return self._real_embed(text, retries=retries)
        if self.mode == "record":
            vec = self._real_embed(text, retries=retries)
            self._calls.append({"fn": "embed", "system": text, "user": "",
                                "output": vec})
            return vec
        return self._next_fixture("embed", text, "")

    def _mock_embed_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        if self.mode == "off":
            return self._real_embed_batch(texts, batch_size=batch_size)
        if self.mode == "record":
            vecs = self._real_embed_batch(texts, batch_size=batch_size)
            self._calls.append({"fn": "embed_batch", "system": json.dumps(texts),
                                "user": "", "output": vecs})
            return vecs
        entry = self._next_entry("embed_batch")
        return entry["output"]

    def _mock_rerank(self, query: str, documents: list[str],
                     top_n: int | None = None) -> list[tuple[int, float]]:
        if self.mode == "off":
            return self._real_rerank(query, documents, top_n=top_n)
        if self.mode == "record":
            result = self._real_rerank(query, documents, top_n=top_n)
            self._calls.append({"fn": "rerank", "system": query,
                                "user": json.dumps(documents), "output": result})
            return result
        entry = self._next_entry("rerank")
        return [tuple(x) for x in entry["output"]]

    def _mock_search(self, vector: list[float], query_text: str,
                     top_k: int = 12, expr: str | None = None) -> list[dict]:
        if self.mode == "off":
            return self._real_search(vector, query_text, top_k=top_k, expr=expr)
        if self.mode == "record":
            hits = self._real_search(vector, query_text, top_k=top_k, expr=expr)
            self._calls.append({"fn": "search", "system": query_text,
                                "user": "", "output": hits})
            return hits
        entry = self._next_entry("search")
        return entry["output"]

    def _mock_dense_search(self, vector: list[float], top_k: int,
                           expr: str | None = None) -> list[dict]:
        if self.mode == "off":
            return self._real_dense_search(vector, top_k, expr=expr)
        if self.mode == "record":
            hits = self._real_dense_search(vector, top_k, expr=expr)
            self._calls.append({"fn": "dense_search", "system": "",
                                "user": "", "output": hits})
            return hits
        entry = self._next_entry("dense_search")
        return entry["output"]

    def _next_entry(self, fn_name: str) -> dict:
        """从 replay 队列取下一条 fixture，校验 fn 匹配。"""
        if self._consumed >= len(self._replay_queue):
            raise RuntimeError(
                f"fixture 耗尽：{self._case_id} 第 {self._consumed + 1} 次调用无 fixture")
        entry = self._replay_queue[self._consumed]
        self._consumed += 1
        if entry["fn"] != fn_name:
            raise RuntimeError(
                f"fixture 不匹配：期望 {entry['fn']}，实际 {fn_name}"
                f"（{self._case_id} #{self._consumed}）")
        return entry

    def _next_fixture(self, fn_name: str, system: str, user: str):
        """_next_entry 的别名，返回 output 字段。"""
        return self._next_entry(fn_name)["output"]
