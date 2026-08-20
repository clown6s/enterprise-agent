# -*- coding: utf-8 -*-
"""统一日志配置：应用启动时调用 setup()。

替代散落的 print()——曾 52 处 print 无级别无格式无时间戳，和 uvicorn/SSE/测试
输出混在一起难捞。改用标准 logging 后：有级别可过滤、带时间戳/模块名、可路由
（stdout/文件）、与 uvicorn 日志统一。结构化审计日志（logs/*.jsonl）不在此列，
那套是好的可观测性，保持不动。
"""
import logging
import sys

from .request_context import get_request_id

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s]: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 第三方库噪声压到 WARNING（pymilvus/grpc 的连接恢复会打大量 DEBUG/INFO）
_NOISY = ("pymilvus", "grpc", "urllib3", "httpx", "httpcore", "asyncio",
          "neo4j", "pypdf", "pdfplumber", "lark_oapi", "pymysql")


class RequestIdFilter(logging.Filter):
    """为所有日志记录注入 request_id；无上下文时用 '-'，第三方日志也不崩。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id("-")
        return True


def _install_filter(root: logging.Logger) -> None:
    """把 request-id filter 安装到现有 handler；重复 setup 不重复添加。"""
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())


def setup(level: str = "INFO") -> None:
    """应用启动调用：配 root logger 格式 + 压第三方噪声。幂等。"""
    root = logging.getLogger()
    if root.handlers:           # 已配置（如 uvicorn 自带）则只压噪声，不重复加 handler
        _install_filter(root)
        for n in _NOISY:
            logging.getLogger(n).setLevel(logging.WARNING)
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT, datefmt=_DATEFMT, stream=sys.stdout,
    )
    _install_filter(root)
    for n in _NOISY:
        logging.getLogger(n).setLevel(logging.WARNING)
