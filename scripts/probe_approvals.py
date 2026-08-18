# -*- coding: utf-8 -*-
"""审批类 SOP 探针：报销/出差/采购/合同各测一条（提交+校验拦截）。"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import agent  # noqa: E402

CASES = [
    ("报销成功", "帮我报销，王强交通费 96 元，打车去客户那", None),
    ("报销金额非法", "帮我报销，王强买咖啡 -5 元", None),
    ("出差成功", "帮我出差，赵敏明天去上海展会，后天回来，参加行业展", None),
    ("出差日期倒挂", "帮我出差，赵敏8月25日去北京，8月20日回来，开会", None),
    ("采购成功", "帮我采购，陈杰申请采购办公椅 10 把，总价 5000 元，装修新工位", None),
    ("合同用印成功", "合同用印，孙悦申请天启科技采购合同盖章，金额 30 万元，中标项目", None),
]

for name, q, followup in CASES:
    t0 = time.time()
    try:
        r = agent.answer(q, conversation_id=f"probe_{name}", user="admin")
    except Exception as e:
        r = {"answer": f"EXC: {e!r}", "route": "EXC"}
    print(f"\n===== {name} | {time.time()-t0:.1f}s | route={r.get('route')} =====")
    print(q)
    print("→", r.get("answer", "")[:300])
