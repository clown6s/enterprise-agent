# -*- coding: utf-8 -*-
"""
全局配置：从项目根目录 .env 加载（python-dotenv），统一以模块级常量使用。

用 os.getenv 直接读，缺省值写在第二个参数里（不做 ${VAR} 注入）。
"""
import os

from dotenv import load_dotenv

# 项目根目录的 .env（run.py 从根目录启动时 cwd 就在根目录；保险起见用绝对路径）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE, ".env"))


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------- LLM ----------------
# 主力模型 qwen3-max（百炼旗舰，OpenAI 兼容接口）：对话 + 流式
# （2026-08-17 用户指定：主力生成换更强千问，替代 deepseek-chat）
DASHSCOPE_KEY = _env("DASHSCOPE_API_KEY")
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MAIN_KEY = _env("MODEL_MAIN_KEY") or DASHSCOPE_KEY
MAIN_BASE = _env("MODEL_MAIN_BASE", DASHSCOPE_BASE)
MAIN_MODEL = _env("MODEL_MAIN", "qwen3-max")
# 阿里云百炼 qwen3-max：答案校验 + 相关性评审（2026-08-17 从 qwen-plus 升级——
# 弱模型当裁判是半数守卫补丁的根源：洗格式/剥数字/误判相关，擦屁股没完）
VERIFY_MODEL = _env("MODEL_VERIFY", "qwen3-max")

# 模型注册表（借鉴 ragflow llm_factories：按用途声明式管理，.env 可覆盖——
# 加新用途（如 OCR）/换厂商只改 .env，不改代码。曾模型写死在调用点）
MODEL_REGISTRY = {
    "main": {"model": MAIN_MODEL, "base": MAIN_BASE, "key": MAIN_KEY},
    "verify": {"model": VERIFY_MODEL, "base": DASHSCOPE_BASE, "key": DASHSCOPE_KEY},
    # 图片→文字（PDF/docx 内嵌图 chunk 用，qwen-vl 多模态）
    "ocr": {"model": _env("MODEL_OCR", "qwen-vl-plus"),
            "base": _env("MODEL_OCR_BASE", DASHSCOPE_BASE),
            "key": _env("MODEL_OCR_KEY") or DASHSCOPE_KEY},
}

# ---------------- 向量检索 ----------------
SILICONFLOW_KEY = _env("SILICONFLOW_API_KEY")
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"   # 4096 维（2026-08-17 从 BGE-M3 1024 升级，用户要求提维度）
EMBED_DIM = 4096
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"   # 2026-08-17 从 bge-reranker-v2-m3 升级
RERANK_MIN_SCORE = 0.3                 # rerank 分数下限（低于此分视为不相关）
TOP_K = 8                              # 向量召回数
RERANK_TOP_N = 4                       # 重排后保留数

# ---------------- Milvus ----------------
MILVUS_HOST = _env("MILVUS_HOST", "10.211.55.5")
MILVUS_PORT = int(_env("MILVUS_PORT", "19530"))
MILVUS_DB = _env("MILVUS_DB", "rag_service")
MILVUS_COLLECTION = "documents_v2"     # 4096 维 + page 字段的新集合（旧 documents 仅作迁移源）
LEGACY_COLLECTION = "documents"

# ---------------- MySQL（后台统一身份：sys_user 表） ----------------
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
