# -*- coding: utf-8 -*-
"""飞书原生审批适配层离线契约测试（不请求网络/数据库）。"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.integrations import feishu_approval as fa  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


class Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_status_and_event():
    check("native disabled by default", not fa.FeishuApprovalProvider().enabled)
    check("status approved", fa.FeishuApprovalProvider.map_status("APPROVED") == 3)
    check("status canceled", fa.FeishuApprovalProvider.map_status("CANCELLED") == 5)
    check("unknown status", fa.FeishuApprovalProvider.map_status("WHAT") is None)
    event = {"header": {"event_id": "evt-1"},
             "event": {"approval_code": "ac", "instance_code": "ic",
                       "status": "APPROVED", "start_user": {"open_id": "ou_x"}}}
    data = fa.FeishuApprovalProvider.event_data(event)
    check("event id extracted", data["event_id"] == "evt-1")
    check("event instance extracted", data["instance_code"] == "ic")
    check("event applicant extracted", data["start_user_open_id"] == "ou_x")


def test_token_and_create():
    p = fa.FeishuApprovalProvider()
    with patch.object(fa.config, "FEISHU_APPROVAL_ENABLED", True), \
         patch.object(fa.config, "FEISHU_APP_ID", "app"), \
         patch.object(fa.config, "FEISHU_APP_SECRET", "secret"), \
         patch.dict(fa.config.FEISHU_APPROVAL_CODES, {"leave": "approval-code"}), \
         patch("app.integrations.feishu_approval.httpx.post",
               side_effect=[Resp({"tenant_access_token": "tenant", "expire": 3600}),
                            Resp({"code": 0, "data": {"instance": {
                                "instance_code": "instance-1",
                                "instance_link": "https://feishu.cn/approval/1"}}})]) as post:
        result = p.create_instance("leave", "LEAVE20260821ABCDEF12", "ou_user", {"申请人": "李娜"})
        check("create success", result.ok and result.instance_code == "instance-1")
        check("tenant token then create", post.call_count == 2)
        create_call = post.call_args_list[1]
        check("approval endpoint", "/approval/v4/instances" in create_call.args[0])
        check("tenant bearer", create_call.kwargs["headers"]["Authorization"] == "Bearer tenant")
        check("uuid apply no", create_call.kwargs["json"]["uuid"] == "LEAVE20260821ABCDEF12")


def test_fail_closed():
    p = fa.FeishuApprovalProvider()
    with patch.object(fa.config, "FEISHU_APPROVAL_ENABLED", True), \
         patch.object(fa.config, "FEISHU_APP_ID", "app"), \
         patch.object(fa.config, "FEISHU_APP_SECRET", "secret"), \
         patch.dict(fa.config.FEISHU_APPROVAL_CODES, {}, clear=True), \
         patch("app.integrations.feishu_approval.httpx.post") as post:
        result = p.create_instance("leave", "A", "ou", {})
        check("missing approval code fails closed", not result.ok and post.call_count == 0)


if __name__ == "__main__":
    test_status_and_event()
    test_token_and_create()
    test_fail_closed()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
