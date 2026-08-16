# -*- coding: utf-8 -*-
"""
全局配置：从项目根目录 .env 加载（python-dotenv），统一以模块级常量使用。

Java 版对应：application.yml + .env。Python 里没有 Spring 的 ${VAR} 注入，
用 os.getenv 直接读，缺省值写在第二个参数里。
"""
import os

from dotenv import load_dotenv

# 项目根目录的 .env（run.py 从根目录启动时 cwd 就在根目录；保险起见用绝对路径）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE, ".env"))


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------- LLM ----------------
# DeepSeek 主力模型（OpenAI 兼容接口）：对话 + 流式
DEEPSEEK_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
# 阿里云百炼 qwen-plus：答案校验小模型（省成本）
DASHSCOPE_KEY = _env("DASHSCOPE_API_KEY")
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VERIFY_MODEL = "qwen-plus"

# ---------------- 向量检索 ----------------
SILICONFLOW_KEY = _env("SILICONFLOW_API_KEY")
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "BAAI/bge-m3"            # 1024 维稠密向量
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_MIN_SCORE = 0.3                 # rerank 分数下限（低于此分视为不相关）
TOP_K = 8                              # 向量召回数
RERANK_TOP_N = 4                       # 重排后保留数

# ---------------- Milvus ----------------
MILVUS_HOST = _env("MILVUS_HOST", "10.211.55.5")
MILVUS_PORT = int(_env("MILVUS_PORT", "19530"))
MILVUS_DB = _env("MILVUS_DB", "rag_service")
MILVUS_COLLECTION = "documents"

# ---------------- MySQL（后台统一身份：复用 Java 版同一张表） ----------------
MYSQL_HOST = _env("MYSQL_HOST", "10.211.55.5")
MYSQL_PORT = int(_env("MYSQL_PORT", "3306"))
MYSQL_PASSWORD = _env("MYSQL_ROOT_PASSWORD")
MYSQL_DB = "rag_service"

# ---------------- OA 业务系统（本地 mock，HTTP 直连） ----------------
OA_BASE_URL = _env("OA_BASE_URL", "http://localhost:7000")
OA_USERNAME = _env("OA_USERNAME", "admin")
OA_PASSWORD = _env("OA_PASSWORD", "123456")

# ---------------- 服务 ----------------
PORT = 8080
LOG_DIR = os.path.join(_BASE, "logs")
AUDIT_FILE = os.path.join(LOG_DIR, "audit", "audit-{date}.jsonl")

# ---------------- 校验 ----------------
VERIFY_ENABLED = True                  # 答案生成后事实校验开关（对应 rag.agent.verify.enabled）
