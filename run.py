# -*- coding: utf-8 -*-
"""启动入口：python run.py（开发可加 --reload 热重载）"""
import sys

import uvicorn

if __name__ == "__main__":
    reload = "--reload" in sys.argv
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=reload)
