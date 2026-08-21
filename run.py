# -*- coding: utf-8 -*-
"""启动入口：python run.py（开发可加 --reload 热重载）"""
import os
import sys
from dotenv import load_dotenv
import uvicorn

from app.logging_config import setup as setup_logging

# 加载 .env 文件（项目根目录）
_BASE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_BASE, ".env")
load_dotenv(_ENV_PATH)

if __name__ == "__main__":
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level)
    reload = "--reload" in sys.argv
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=reload,
                log_config=None, access_log=False)
