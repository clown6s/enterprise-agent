# Type Equiv 登记表（dsh 二-7）

文档中引用的稳定公共签名。`scripts/verify_doc_types.py` 用 inspect 从源码提取
真实签名与此处比对，漂移即 CI 红。变更签名必须同步更新此文件。

## 锁定的签名

```python type-equiv
# app/prompts.py
def render_prompt(template: str, **vars) -> str: ...
```

```python type-equiv
# app/config.py
def validate_config() -> list: ...
```

```python type-equiv
# app/request_context.py
def get_request_id(default: str = "") -> str: ...
def set_request_id(value: str) -> object: ...
def reset_request_id(token: object) -> None: ...
```

```python type-equiv
# app/plan/sop.py
class SopDefinition:  # dataclass 字段集
    code: str
    name: str
    keywords: list
    steps: list
    handlers: dict
    finish: object  # Callable | None
    confirm_detail: object  # Callable | None
```

```python type-equiv
# app/plan/approvals.py
class ApprovalDef:  # dataclass 字段集
    code: str
    name: str
    keywords: list
    prefix: str
    table: str
    business_type: str
    fields: dict
    extract_schema: str
    extra_rules: str
    validate: object  # Callable
    receipt_detail: object  # Callable
    col_labels: object  # dict | None
    summary_cols: object  # tuple | None
```

```python type-equiv
# app/llm.py
def chat_chain(system: str, user: str, *, purposes: tuple = ..., **kw) -> str: ...
def chat_stream_chain(system: str, user: str, *, purposes: tuple = ...) -> object: ...
def _classify_err(e: Exception) -> str: ...
```
