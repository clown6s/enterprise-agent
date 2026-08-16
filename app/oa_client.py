# -*- coding: utf-8 -*-
"""
OA 业务系统 HTTP 客户端（本地 mock 系统，端口 7000）。
Java 版对应：OaClient。软依赖——OA 挂了只影响数据问答，问答链路不崩。
"""
import httpx

from . import config


class OaClient:
    def __init__(self):
        self._token: str | None = None

    def _login(self) -> str | None:
        try:
            resp = httpx.post(
                f"{config.OA_BASE_URL}/api/auth/login",
                json={"username": config.OA_USERNAME, "password": config.OA_PASSWORD},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 200:
                self._token = data["data"]
        except (httpx.HTTPError, ValueError):
            self._token = None
        return self._token

    def call(self, path: str) -> dict | None:
        """GET 请求（自动登录重试一次）；返回 JSON dict，失败返回 None"""
        for _ in range(2):
            try:
                resp = httpx.get(
                    f"{config.OA_BASE_URL}{path}",
                    # OA 侧 JwtAuthenticationFilter 要求 "Bearer <token>" 前缀
                    # （Java 版 OaClient 同款；不带前缀直接 403）
                    headers={"Authorization": f"Bearer {self._token}" if self._token else ""},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json()
                # token 过期 → 重新登录再试一次
                self._token = None
                self._login()
            except httpx.HTTPError:
                return None
        return None

    def metrics(self, days: int) -> dict | None:
        """经营数据：dates/revenue/cost/profit/summary"""
        return self.call(f"/api/biz/metrics?days={max(1, days)}")

    def expenses(self, days: int, employee: str | None = None) -> dict | None:
        """报销记录：items(d/expenseType/amount/remark/empName/deptName)+total+count"""
        import urllib.parse
        qs = f"days={max(1, days)}"
        if employee:
            qs += "&employee=" + urllib.parse.quote(employee)
        return self.call(f"/api/biz/expenses?{qs}")


oa = OaClient()
