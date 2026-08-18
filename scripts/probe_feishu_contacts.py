# -*- coding: utf-8 -*-
"""飞书通讯录探针（只读）：app_access_token → 拉全部成员 → 打印基础字段。
用于确认应用是否具备通讯录 scope 以及「拉到账号」的可行性。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import httpx  # noqa: E402

APP_ID = config._env("FEISHU_APP_ID")
APP_SECRET = config._env("FEISHU_APP_SECRET")

if not APP_ID or not APP_SECRET:
    print("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
    sys.exit(1)

# 1. app_access_token
tok_resp = httpx.post(
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
    json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
tok_data = tok_resp.json()
print("token:", tok_data.get("code"), tok_data.get("msg", ""))
if tok_data.get("code") != 0:
    sys.exit(1)
token = tok_data["app_access_token"]
headers = {"Authorization": f"Bearer {token}"}

# 2. 拉成员（根部门遍历；分页）
all_users = []
page_token = ""
while True:
    url = ("https://open.feishu.cn/open-apis/contact/v3/users"
           f"?user_id_type=open_id&department_id=0&page_size=50"
           + (f"&page_token={page_token}" if page_token else ""))
    resp = httpx.get(url, headers=headers, timeout=10)
    data = resp.json()
    print("users:", data.get("code"), data.get("msg", ""))
    if data.get("code") != 0:
        break
    d = data.get("data") or {}
    for u in d.get("items") or []:
        all_users.append(u)
    if not d.get("has_more"):
        break
    page_token = d.get("page_token") or ""
    if not page_token:
        break

print(f"\n共 {len(all_users)} 人:")
for u in all_users:
    print(" -", u.get("open_id"), "|", u.get("name"), "|", u.get("mobile"),
          "|", u.get("employee_no"), "|", u.get("job_title"),
          "| dept_ids:", u.get("department_ids"))
