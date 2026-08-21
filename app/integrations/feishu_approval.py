# -*- coding: utf-8 -*-
"""飞书原生审批适配层（本地审批状态为最终事实源）。

默认关闭：未配置 approval_code 或 FEISHU_APPROVAL_ENABLED 时，适配器不发起
任何飞书审批请求，现有本地审批链继续工作。HTTP seam 保持模块级，便于离线契约测试。
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeApprovalResult:
    ok: bool
    instance_code: str = ""
    instance_link: str = ""
    error: str = ""


_STATUS_MAP = {
    "PENDING": 1,
    "APPROVING": 2,
    "APPROVED": 3,
    "REJECTED": 4,
    "CANCELED": 5,
    "CANCELLED": 5,
    "DELETED": 5,
}


class FeishuApprovalProvider:
    """只封装 token/API/状态映射，不复制本地审批状态机。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._token = ""
        self._token_expires_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(config.FEISHU_APPROVAL_ENABLED and config.FEISHU_APP_ID
                    and config.FEISHU_APP_SECRET)

    def approval_code(self, business_type: str) -> str:
        return (config.FEISHU_APPROVAL_CODES.get(business_type) or "").strip()

    def ready_for(self, business_type: str) -> bool:
        return self.enabled and bool(self.approval_code(business_type))

    def tenant_access_token(self) -> str | None:
        """获取并缓存 tenant_access_token；不复用 app_access_token。"""
        now = time.time()
        with self._lock:
            if self._token and now < self._token_expires_at:
                return self._token
            try:
                resp = httpx.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": config.FEISHU_APP_ID,
                          "app_secret": config.FEISHU_APP_SECRET}, timeout=15)
                body = resp.json()
                token = body.get("tenant_access_token") or ""
                expires = int(body.get("expire", 0) or 0)
                if resp.status_code != 200 or not token:
                    log.warning("飞书 tenant token 获取失败 code=%s", body.get("code"))
                    return None
                self._token = token
                self._token_expires_at = now + max(expires - 60, 1)
                return token
            except (httpx.HTTPError, ValueError, TypeError) as e:
                log.warning("飞书 tenant token 请求失败 type=%s", type(e).__name__)
                return None

    def create_instance(self, business_type: str, apply_no: str,
                        applicant_open_id: str, form: dict,
                        approver_open_ids: list[str] | None = None) -> NativeApprovalResult:
        """创建原生实例；飞书模板/字段由 approval_code 配置决定。"""
        if not self.ready_for(business_type):
            return NativeApprovalResult(False, error="native approval disabled or approval_code missing")
        token = self.tenant_access_token()
        if not token:
            return NativeApprovalResult(False, error="tenant token unavailable")
        payload: dict[str, Any] = {
            "approval_code": self.approval_code(business_type),
            "user_id": applicant_open_id,
            "form": json.dumps(form, ensure_ascii=False),
            "uuid": apply_no,
        }
        if approver_open_ids:
            payload["node_approver_open_id_list"] = [approver_open_ids]
        try:
            resp = httpx.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload, timeout=20)
            body = resp.json()
            data = body.get("data") or {}
            instance = data.get("instance") or data
            code = (instance.get("instance_code") or "").strip()
            if resp.status_code != 200 or body.get("code", 0) != 0 or not code:
                return NativeApprovalResult(False, error=f"create failed code={body.get('code')}")
            return NativeApprovalResult(True, instance_code=code,
                                       instance_link=instance.get("instance_link") or "")
        except (httpx.HTTPError, ValueError, TypeError) as e:
            return NativeApprovalResult(False, error=f"create request failed: {type(e).__name__}")

    @staticmethod
    def map_status(status: str) -> int | None:
        return _STATUS_MAP.get((status or "").upper())

    def form_for(self, business_type: str, row: dict) -> dict:
        """默认以本地摘要生成模板表单；字段键由飞书模板配置决定，需租户验收。"""
        from ..plan.approval import summary_text
        return {"申请单号": row.get("apply_no", ""),
                "申请人": row.get("user_name", ""),
                "业务类型": business_type,
                "申请摘要": summary_text(business_type, row)}

    @staticmethod
    def event_data(event: Any) -> dict:
        """兼容 SDK 对象和字典，提取最小事件数据，不执行状态变更。"""
        header = getattr(event, "header", None)
        data = getattr(event, "event", event) or {}
        if isinstance(event, dict):
            header = event.get("header")
            data = event.get("event") or event
        def get(obj, key, default=""):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default) or default
        start_user = get(get(data, "start_user", {}), "open_id")
        return {"event_id": get(header, "event_id"),
                "approval_code": get(data, "approval_code"),
                "instance_code": get(data, "instance_code"),
                "external_id": get(data, "external_id"),
                "status": get(data, "status"),
                "operate_time": get(data, "operate_time"),
                "start_user_open_id": start_user}


def handle_status_event(event: Any) -> None:
    """WS 状态事件入口：当前仅解析并记录，业务映射接入前不改本地状态。

    这样开关/事件订阅可先安全上线；没有 external instance 映射时绝不把
    飞书状态直接当成本地审批结果。
    """
    data = provider.event_data(event)
    if not data.get("instance_code"):
        log.warning("飞书审批事件缺少 instance_code")
        return
    log.info("飞书审批状态事件 instance=%s status=%s event_id=%s",
             data["instance_code"], data.get("status"), data.get("event_id", ""))


provider = FeishuApprovalProvider()
