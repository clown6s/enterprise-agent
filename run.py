# -*- coding: utf-8 -*-
"""启动入口：python run.py（开发可加 --reload 热重载）"""
import sys

import uvicorn

from app.logging_config import setup as setup_logging

if __name__ == "__main__":
    setup_logging()
    reload = "--reload" in sys.argv
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=reload,
                log_config=None, access_log=False)
