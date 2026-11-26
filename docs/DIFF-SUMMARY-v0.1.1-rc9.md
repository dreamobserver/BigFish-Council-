# 逐文件差异摘要：bigfish-council v0.1.1-rc9

相对基线：远端 main `aeab598b3e`（= rc8 发布点 + Codex 审计文档入库）。
本轮全部改动：**11 files, 1118 insertions(+), 293 deletions(-)**。

```
 bigfish/__init__.py           |   2 +-
 bigfish/core.py               |  14 +-
 bigfish/db.py                 |   5 +
 bigfish/mcp_adapter.py        | 533 ++++++++++++++++++++++++++++++++
 bigfish/roles.py              |  17 +-
 bigfish/server.py             | 333 ++++---------------
 tests/test_v011_rc9.py        | 181 +++++++++
 tests/test_v011_rc9_socket.py | 306 +++++++++++++++
 tools/e2e_loopback.py         |  11 +-
 tools/remote_check.py         |   5 +-
 tools/selfcheck.py            |   4 +-
```

---

## 1. `bigfish/mcp_adapter.py`（新增 533 行）

这是本轮的主体。协议交给 SDK，业务只留一个入口。

### 1.1 模块坐标

| 行 | 内容 |
| --- | --- |
| `:42` | `SERVICE = "bigfish-council"` |
| `:44` | `MCP_PROTOCOL = "2025-03-26"`（仅保留兼容，实际由 SDK 协商） |
| `:47` | `MAX_BODY_BYTES`（env `BFC_MAX_BODY_BYTES`，默认 8 MiB） |
| `:49` | `MAX_TEXT_FIELD`（默认 4 MiB） |
| `:51` | `TOOL_ORDER`（五个执行器工具） |
| `:60` | `tool_specs()` —— 工具清单的唯一真源 |
| `:138` | `_status_payload()` |
| `:151` | `_json()` |
| `:155` | `_validate_args()` —— 字段级长度校验，中文错误文案 |
| `:187` | `dispatch_tool()` —— **唯一的业务实现**，REST 与 MCP 共用 |
| `:241` | `_sdk_supported_methods()` —— 从 SDK 模型反射方法名集合 |
| `:273` | `build_server(council)` —— lowlevel `Server`，只注册五个工具 |
| `:337` | `_current_executor(council)` |
| `:361` | `_response()` |
| `:377` | `class McpAsgi` |
| `:526` | `have_sdk()` |

### 1.2 反射方法名，而不是写死

```python
def _sdk_supported_methods() -> tuple[set, set]:
    req, note = set(), set()
    try:
        import mcp.types as types

        def _collect(root_model):
            out = set()
            field = root_model.model_fields.get("root")
            if field is None:
                return out
            for sub in typing.get_args(field.annotation):
                mf = getattr(sub, "model_fields", {}).get("method")
                if mf is None:
                    continue
                for lit in typing.get_args(mf.annotation):
                    if isinstance(lit, str):
                        out.add(lit)
            return out

        req = _collect(types.ClientRequest)
        note = _collect(types.ClientNotification)
    except Exception:  # pragma: no cover
        pass
    if not req:
        req = {"initialize", "ping", "tools/list", "tools/call"}
    return req, note | {"notifications/initialized"}
```

写死清单等于「SDK 加方法后我们回 -32601」。反射失败（SDK 结构大改）时退回保守四方法，仍然能跑。

### 1.3 每请求新建 manager（R9-01）

```python
def _new_manager(self):
    """每个请求新建一个 manager。

    SDK 的 StreamableHTTPSessionManager.run() 每个实例只能进一次
    （streamable_http_manager.py:155 会抛 "can only be called once per
    instance"）。stateless=True 下它本来也不保存会话状态。
    """
    return self._manager_factory(
        app=self.server,
        json_response=True,
        stateless=True,
        max_request_body_size=MAX_BODY_BYTES,
    )
```

### 1.4 闸门在 SDK 之前（R9-02 / R9-04）

```python
def _auth(self, headers):
    raw = headers.get("authorization")
    if not raw or not raw.lower().startswith("bearer "):
        return None, self._jsonrpc_error(None, -32001, "缺少 Bearer 令牌", 401)
    ...
    try:
        return self.council.authenticate(token, role="executor"), None
    except CouncilError as e:
        return None, self._jsonrpc_error(None, -32001, str(e), 401)
```

`status: int = 200` 这个参数就是本轮加的：默认仍是 200（其他业务错用它），鉴权失败显式传 401。

维护期：

```python
if self.council.db.in_maintenance():
    await self._send_json(
        send, 503,
        {"ok": False,
         "error": f"数据库正在维护（{self.council.db.maintenance_reason or '正在从备份恢复'}），"
                  "本次操作未执行，请稍后重试",
         "maintenance": True},
        extra_headers=[(b"retry-after", b"15")])
    return
```

### 1.5 Accept 兼容

SDK 的 streamable HTTP 要求客户端 `Accept: application/json, text/event-stream`。手写版从来不看这个头。脚本直接 curl 时不带，SDK 会回 406。所以在进 SDK 前补一次：

```python
if "text/event-stream" not in headers.get("accept", ""):
    new_headers = [(k, v) for k, v in scope["headers"]
                   if k.decode("latin-1").lower() != "accept"]
    new_headers.append((b"accept", b"application/json, text/event-stream"))
    scope = dict(scope, headers=new_headers)
```

### 1.6 上下文传递

鉴权拿到的执行器身份要能传到 `dispatch_tool()` 里（SDK 的 handler 签名里没有我们自定义的东西）：

```python
_CURRENT_EXECUTOR: contextvars.ContextVar = contextvars.ContextVar(
    "bigfish_mcp_executor", default=None)
...
token = _CURRENT_EXECUTOR.set(ex)
try:
    async with manager.run():
        await manager.handle_request(scope, replay_receive, send)
finally:
    _CURRENT_EXECUTOR.reset(token)
```

`contextvars` 在 stateless manager 的请求内子任务里会被继承——TestClient 与真 socket 都验过。

---

## 2. `bigfish/server.py`（51+/282−，630 → 393 行）

### 2.1 删掉的东西

整段手写 JSON-RPC：`_mcp_error` / `_bearer` / `_mcp_initialize` / `_mcp_tools_list` / `_mcp_tools_call` 与 `/mcp` 的 POST 处理，约 280 行。同时删掉不再用的 import：`os`、`typing.Any`、`fastapi.concurrency.run_in_threadpool`。

### 2.2 加的东西

```python
from starlette.routing import Route
from .mcp_adapter import (  # noqa: F401 —— 协议层迁出后仍从这里导出，历史 import 不用改
    MAX_BODY_BYTES, MAX_TEXT_FIELD, MCP_PROTOCOL, SERVICE, TOOL_ORDER,
    TOOL_PERMISSIONS, McpAsgi, _status_payload, dispatch_tool, have_sdk, tool_specs,
)

__all__ = [
    "BindError", "MAX_BODY_BYTES", "MAX_TEXT_FIELD", "MCP_PROTOCOL", "McpAsgi",
    "SERVICE", "TOOL_ORDER", "TOOL_PERMISSIONS", "check_bind_host", "create_app",
    "create_production_app", "dispatch_tool", "have_sdk", "serve", "tool_specs",
]
```

再导出是为了让 `tests/test_api.py`、`tools/selfcheck.py` 这些 `from bigfish.server import ...` 的调用方不用改一行。`__all__` 同时让 pyflakes 不把这批名字报成「未使用」。

### 2.3 用 Route 而不是 Mount（R9-03）

```python
_mcp_app = McpAsgi(council)
app.router.routes.insert(0, Route("/mcp", endpoint=_mcp_app,
                                  methods=["POST", "GET", "DELETE"]))
app.router.routes.insert(1, Route("/mcp/", endpoint=_mcp_app,
                                  methods=["POST", "GET", "DELETE"]))
```

为什么不是 `app.mount("/mcp", ...)`：

1. `Mount` 对**精确路径**只给 `PARTIAL` 匹配，Starlette 的 `redirect_slashes` 会先回 307，客户端要多跳一次。
2. `Mount` 会把前缀从 `scope["path"]` 里切掉，ASGI 子应用看到的路径变形。
3. `Route` 的 endpoint 传**类实例**时按 ASGI 直接调用；传函数会被包成 request-response，签名对不上（这也是为什么 `McpAsgi` 是类而不是函数）。

---

## 3. `bigfish/roles.py`（17 行）

```python
def role_for_db_row(db_role, name="", model=None):
    ...
    if db_role == "reviewer":
        m = (model or "").strip().lower()
        if m in ("gpt", "gemini"):
            return ARCHITECT_GPT if m == "gpt" else REVIEWER_GEMINI
        # rc9 / AUDIT-RC8-02：模型列认不出时曾经按名称猜、猜不到就默认 Gemini，
        # 等于「写错一列 model 就能拿到审查票权」。现在只接受名称里能唯一确定的那两种情况，
        # 其余一律零权限（失败关闭）。空名称 / model=unknown / 写成 claude 都落到这里。
        low = (name or "").lower()
        has_gpt = "gpt" in low
        has_gem = "gemini" in low
        if has_gpt and not has_gem:
            return ARCHITECT_GPT
        if has_gem and not has_gpt:
            return REVIEWER_GEMINI
        return DENIED_ROLE
    return DENIED_ROLE
```

修前实测：

```
role_for_db_row("reviewer", "weird", "unknown")  ->  reviewer_gemini
role_for_db_row("reviewer", "",      None)      ->  reviewer_gemini
```

修后：

```
role_for_db_row("reviewer", "weird",     "unknown")  ->  denied_unknown_role
role_for_db_row("reviewer", "",          "")         ->  denied_unknown_role
role_for_db_row("reviewer", "my-gpt-bot", None)      ->  architect_gpt
role_for_db_row("reviewer", "my-gemini",  None)      ->  reviewer_gemini
```

---

## 4. `bigfish/core.py`（14 行）

注册入口与上面对齐：

```python
if role == "reviewer":
    if model is None:
        # rc9 / AUDIT-RC8-02：注册入口与 role_for_db_row 用同一套判定，
        # 不能一个失败关闭、一个默认 Gemini —— 否则「名字里没有 gpt」
        # 就会静默建出一个 Gemini 审查身份，谁都不知道它代表谁。
        low = (name or "").lower()
        has_gpt = "gpt" in low
        has_gem = "gemini" in low
        if has_gpt and not has_gem:
            model = "gpt"
        elif has_gem and not has_gpt:
            model = "gemini"
        else:
            raise CouncilError(
                "审查身份必须显式指定 model=gpt 或 model=gemini"
                "（名称无法唯一判断，拒绝猜测）")
    if model not in MODELS:
        raise CouncilError("审查身份的 model 只能是 gpt 或 gemini")
```

原来那行是 `model = "gpt" if "gpt" in low and "gemini" not in low else "gemini"`。

---

## 5. `bigfish/db.py`（5 行）

```python
@property
def maintenance_reason(self) -> str:
    """维护原因（供 MCP 的 503 文案复用，不给外部看内部字段）。"""
    return self._maint_reason
```

原本 `_maint_reason` 是私有的，MCP 层要拿它做 503 文案就只能读私有字段。加一个只读属性，不改生命周期。

---

## 6. `bigfish/__init__.py`（1 行）

`__version__ = "0.1.1-rc9"`。

---

## 7. `tools/selfcheck.py`（4 行）

```diff
- check("坏 Token 被拒", st == 200 and body.get("error", {}).get("code") == -32001)
+ # rc9 起 MCP 走官方 SDK，凭据无效按 MCP 规范回 HTTP 401（旧手写版是 200 +
+ # -32001；官方客户端收到 200 的 error 会当成协议错并挂住，见 AUDIT-RC8-01）。
+ check("坏 Token 被拒", st == 401 and body.get("error", {}).get("code") == -32001)
```

这是「断言跟随语义修正」，不是放宽：断言对象从「状态码 200」变成「状态码 401」，`-32001` 那个更强的业务断言原样保留。

---

## 8. `tools/e2e_loopback.py`（11 行）

```python
st, init = rpc(a.url, ex["token"], "initialize", {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "e2e-loopback", "version": "1.0"},
})
check("initialize 成功", st == 200 and "result" in init, str(st))
```

两处改动：

1. initialize 必须带 `protocolVersion` / `capabilities` / `clientInfo`，否则 SDK 回 `-32602`（旧手写版不校验 params，传空也能过）。
2. `check()` 的第三参数是**字符串**（拼进失败文案），原来漏传导致失败时看不到状态码。

---

## 9. `tools/remote_check.py`（5 行）

只改注释：

```python
#    rc9 起 MCP 走官方 SDK，凭据无效按规范回 HTTP 401（body 里仍带
#    JSON-RPC error -32001）；rc8 及以前是 200 + error。两种都要认，
#    但绝不能只看「有没有返回」——成功也会返回。
```

代码逻辑不变（它本来就把两种都当「被拒」处理）。

---

## 10. `tests/test_v011_rc9.py`（新增 181 行 / 15 例）

结构：

```
:31  class Base(unittest.TestCase)                     # 临时库 + Council + register_executor
:66  class TestUnknownReviewerModelFailsClosed(Base)   # 10 例
:151 class TestRegisterReviewerFailsClosed(Base)       # 5 例
```

关键辅助（直接写脏行，绕过白名单）：

```python
def insert_executor(self, role, name, model):
    """模拟旧版本残留行 / 手工 SQL 改错一列。"""
    with self.db.tx() as c:
        c.execute(
            "INSERT INTO executors (id,name,role,model,token_hash,enabled,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("executor-dirty", name, role, model,
             hash_token("bfc_ex_" + "x" * 40), 1, now()))
```

---

## 11. `tests/test_v011_rc9_socket.py`（新增 306 行 / 8 例）

### 11.1 真 uvicorn

```python
class _Live:
    """进程内 uvicorn：真 socket、真 HTTP、真端口。"""

    def __init__(self, council):
        from bigfish.server import create_app
        self.port = _free_port()
        self.app = create_app(council, watchdog_interval=None)
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                             log_level="error", lifespan="on")
        self.srv = uvicorn.Server(cfg)
        self.th = threading.Thread(target=self.srv.run, daemon=True)

    def __enter__(self):
        self.th.start()
        for _ in range(200):
            if getattr(self.srv, "started", False):
                return self
            time.sleep(0.02)
        raise RuntimeError("uvicorn 没能在 4 秒内起来")
```

### 11.2 官方 SDK 客户端

```python
async def go():
    async with streamablehttp_client(live.url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            init = await sess.initialize()
            res = await sess.call_tool(tool, args)
            return init, res

return asyncio.run(go())
```

`_flatten(exc)` 把 `ExceptionGroup` 摊平，否则断言里只能看到 unhandled errors in a TaskGroup，看不到真正原因。

### 11.3 环境缺依赖时

```python
@unittest.skipUnless(HAVE_SDK, SDK_WHY)
```

`SDK_WHY` 里写明缺什么（例如 `官方 mcp SDK 或 uvicorn 不可用: ModuleNotFoundError: No module named 'mcp'`），不是静默跳过。

---

## 12. 复现这份 Diff

```bash
cd /home/dsh/bigfish-council
git diff aeab598b3e --stat      # 本仓库没有该对象时见下
git show HEAD:bigfish/server.py | sed -n '319,404p'   # 旧手写 MCP 段
```

> 本仓库的本地历史与远端 GitHub 历史**互不相连**（历史发布方式是在公开副本里 `git init` 单提交 `push -f`）。所以不能直接 `git diff <远端 sha>`，比对基线用本文件记录的 stat 与逐节代码。
