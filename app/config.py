# -*- coding: utf-8 -*-
"""
全局配置：从项目根目录 .env 加载（python-dotenv），统一以模块级常量使用。

用 os.getenv 直接读，缺省值写在第二个参数里（不做 ${VAR} 注入）。
"""
import logging
import os

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# 项目根目录的 .env（run.py 从根目录启动时 cwd 就在根目录；保险起见用绝对路径）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_BASE, ".env")
load_dotenv(_ENV_PATH)

# 这些键为空时，相关主能力会在第一次请求才失败；启动检查统一提前报告。
# 不把可选渠道（飞书/OCR）列进来，避免关闭可选能力时误报。
_REQUIRED_CONFIG_KEYS = (
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY",
    "SILICONFLOW_API_KEY",
    "MYSQL_ROOT_PASSWORD",
)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------- 百度云 PaddleOCR-VL ----------------
PADDLEOCR_VL_ENABLED = _env("PADDLEOCR_VL_ENABLED", "true").lower() in ("1", "true", "yes")
BAIDU_OCR_ACCESS_TOKEN = _env("BAIDU_OCR_ACCESS_TOKEN")

FEISHU_APPROVAL_ENABLED = _env("FEISHU_APPROVAL_ENABLED", "false").lower() in ("1", "true", "yes")
FEISHU_APP_ID = _env("FEISHU_APP_ID")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET")
FEISHU_APPROVAL_CODES = {
    "leave": _env("FEISHU_APPROVAL_CODE_LEAVE"),
    "expense": _env("FEISHU_APPROVAL_CODE_EXPENSE"),
    "travel": _env("FEISHU_APPROVAL_CODE_TRAVEL"),
    "purchase": _env("FEISHU_APPROVAL_CODE_PURCHASE"),
    "contract": _env("FEISHU_APPROVAL_CODE_CONTRACT"),
}
FEISHU_APPROVAL_TABLE = "approval_external_instance"
FEISHU_APPROVAL_EVENT_TABLE = "approval_external_event"
FEISHU_VERIFICATION_TOKEN = _env("FEISHU_VERIFICATION_TOKEN")
FEISHU_ENCRYPT_KEY = _env("FEISHU_ENCRYPT_KEY")


def validate_config() -> list[str]:
    """返回当前配置缺失的必要键，不抛异常、不打印密钥。

    启动阶段调用，服务仍可启动（便于只运行健康检查/管理端点）；真正依赖
    外部服务的请求会由各自软依赖/降级链处理。每次调用重新读取环境，方便
    测试和 reload 场景，不缓存结果。
    """
    missing = [key for key in _REQUIRED_CONFIG_KEYS if not _env(key)]
    if not os.path.exists(_ENV_PATH):
        missing.append(".env 文件")
    return missing


# ---------------- LLM ----------------
# 主力模型 deepseek-chat（DeepSeek，OpenAI 兼容接口）：对话 + 流式
DEEPSEEK_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE = _env("DEEPSEEK_BASE", "https://api.deepseek.com/v1")
MAIN_KEY = _env("MODEL_MAIN_KEY") or DEEPSEEK_KEY
MAIN_BASE = _env("MODEL_MAIN_BASE", DEEPSEEK_BASE)
MAIN_MODEL = _env("MODEL_MAIN", "deepseek-chat")
# 智谱 GLM（校验独立裁判 + 生成降级第二棒——与主力不同厂商，厂商级故障隔离）
ZHIPU_KEY = _env("ZHIPU_API_KEY")
ZHIPU_BASE = _env("ZHIPU_BASE", "https://open.bigmodel.cn/api/paas/v4")

# 模型注册表（借鉴 ragflow llm_factories：按用途声明式管理，.env 可覆盖——
# 加新用途（如 OCR）/换厂商只改 .env，不改代码。曾模型写死在调用点）
# extra：厂商特有参数随条目声明（thinking 关闭等），调用方不再传厂商参数
# （曾 enable_thinking 硬编码在调用点，换厂商会把百炼参数发给智谱）
MODEL_REGISTRY = {
    # timeout：主力 60s 挂起即降级（曾硬超时 120s 才换棒，用户等 2 分钟+的
    # 实况：qwen3-max 挂起 120s → GLM 接管 20s，答案断言全过但超时限）
    "main": {"model": MAIN_MODEL, "base": MAIN_BASE, "key": MAIN_KEY, "timeout": 60},
    # 生成降级链第二棒：主力失败时接管生成（llm.chat_chain 按序尝试）
    "main_fallback": {"model": _env("MODEL_MAIN_FALLBACK", "glm-4.6"),
                      "base": _env("MODEL_MAIN_FALLBACK_BASE", ZHIPU_BASE),
                      "key": _env("MODEL_MAIN_FALLBACK_KEY") or ZHIPU_KEY,
                      "extra": {"thinking": {"type": "disabled"}},
                      "timeout": 90},
    # 校验=独立裁判（与生成不同厂商：系统性偏见要两个厂商同时犯才漏）。
    # 2026-08-18 探针后从 qwen3-max 切 glm-4.6：核查/评审全过，反例判定
    # 更果断（1 分 vs 基线擦边 3 分），延迟 ~2s，价格 $0.6/M 入 $2.2/M 出
    "verify": {"model": _env("MODEL_VERIFY", "glm-4.6"),
               "base": _env("MODEL_VERIFY_BASE", ZHIPU_BASE),
               "key": _env("MODEL_VERIFY_KEY") or ZHIPU_KEY,
               "extra": {"thinking": {"type": "disabled"}},
               "timeout": 30},
    # 校验链第二棒：智谱挂了回退主力同厂商（DeepSeek）
    "verify_fallback": {"model": _env("MODEL_VERIFY_FALLBACK", MAIN_MODEL),
                        "base": _env("MODEL_VERIFY_FALLBACK_BASE", MAIN_BASE),
                        "key": _env("MODEL_VERIFY_FALLBACK_KEY") or MAIN_KEY,
                        "timeout": 30},
}

# ---------------- 向量检索 ----------------
SILICONFLOW_KEY = _env("SILICONFLOW_API_KEY")
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"   # 4096 维（2026-08-17 从 BGE-M3 1024 升级，用户要求提维度）
EMBED_DIM = 4096
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"   # 2026-08-17 从 bge-reranker-v2-m3 升级
RERANK_MIN_SCORE = 0.3                 # rerank 分数下限（低于此分视为不相关）
TOP_K = 12                             # 向量召回数（曾 8——法律类长文档多条款，
                                        # RRF 把目标条款挤出前 8 的实况；rerank 层
                                        # 控精度，召回放宽无副作用）
RERANK_TOP_N = 4                       # 最终证据条数
RERANK_CANDIDATE_N = 12                # 进入重排的候选窗口
EVIDENCE_PER_DOCUMENT = 2              # 单文档证据上限（先过分数阈值）

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
MYSQL_POOL_MIN = int(_env("MYSQL_POOL_MIN", "2") or 2)
MYSQL_POOL_MAX = int(_env("MYSQL_POOL_MAX", "10") or 10)
MYSQL_POOL_TIMEOUT = float(_env("MYSQL_POOL_TIMEOUT", "3") or 3)
MYSQL_POOL_RECYCLE = int(_env("MYSQL_POOL_RECYCLE", "1800") or 1800)
NEO4J_PASSWORD = _env("NEO4J_PASSWORD")

# ---------------- OA 业务系统（2026-08-19 退役：OA 7000 停用，审批直连本库） ----------------
# 保留配置项仅为向后兼容可能引用 OA_* 的历史代码；新代码不应使用。
OA_BASE_URL = _env("OA_BASE_URL", "")
OA_USERNAME = _env("OA_USERNAME", "")
OA_PASSWORD = _env("OA_PASSWORD", "")

# ---------------- 服务 ----------------
PORT = 8080
LOG_DIR = os.path.join(_BASE, "logs")
AUDIT_FILE = os.path.join(LOG_DIR, "audit", "audit-{date}.jsonl")

# ---------------- 校验 ----------------
VERIFY_ENABLED = True                  # 答案生成后事实校验开关（对应 rag.agent.verify.enabled）
