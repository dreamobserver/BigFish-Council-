"""MCP 传输层适配：官方 SDK（mcp>=1.9）承载协议，业务入口仍是 dispatch_tool()。

审计 AUDIT-CODEX-v0.1.1-rc8 第一条：requirements.txt 早就声明了官方 mcp SDK，
但 bigfish/server.py 一直手写 initialize / ping / tools/list / tools/call，
全仓没有一处 import mcp。手写协议的代价不是「代码难看」，而是：

- 协议演进跟不上。SDK 支持 2024-11-05 / 2025-03-26 / 2025-06-18 协商，
  手写版只会硬回一个版本号；客户端升级后只能靠猜。
- 错误语义靠自己维护。JSON-RPC 的「方法不存在」是 -32601，但 SDK 在参数
  校验失败时回 -32602 —— 手写版和 SDK 在同一个输入上给出不同错误码，
  客户端没法写稳定的分支。
- 每次改一处都要重读一遍规范。

所以这一层改成：**协议交给 SDK，业务只留一个入口**。

    build_server(council)   -> lowlevel Server（只注册 5 个执行器工具）
    McpAsgi(council)        -> 一段 ASGI，鉴权/维护期/结构校验在我们的闸门里
    dispatch_tool(...)      -> 唯一的业务实现，REST 与 MCP 共用

刻意保留的三件事（不因为「SDK 自带」就删）：
1. 工具清单仍由 tool_specs() 生成 —— 审批类工具不许出现在这个清单里；
2. 参数校验仍走 _validate_args() —— 中文错误文案与字段级 maxLength 是
   BFC-02 的交付物，换实现不能把长度上限丢掉；
3. 鉴权在进入 SDK 之前完成 —— MCP 规范要求凭据无效回 HTTP 401，
   而 SDK 的 handler 内抛错只能变成 JSON-RPC 错误体（HTTP 200）。
"""
from __future__ import annotations

import contextvars
import json
import os
import typing
from typing import Any

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from . import __version__, roles as R
from .core import Council, CouncilError
from .db import MaintenanceError

SERVICE = "bigfish-council"
# 保留旧的协议常量：客户端仍会用 2025-03-26 发起，SDK 会原样回显该版本。
MCP_PROTOCOL = "2025-03-26"
# F-09：请求体上限。默认 8 MiB，够放一份大 diff + 长日志，又能挡住
# 「被控制的执行器一次塞 1GB 进来把我们 2.4G 的容器打爆」。
MAX_BODY_BYTES = int(os.environ.get("BFC_MAX_BODY_BYTES") or 8 * 1024 * 1024)
# 单个自由文本字段的上限（MCP/REST 共用），比整体上限小一半，避免一个字段吃满。
MAX_TEXT_FIELD = int(os.environ.get("BFC_MAX_TEXT_FIELD") or 4 * 1024 * 1024)

TOOL_ORDER = [
    "council_list_tasks",
    "council_claim_task",
    "council_heartbeat",
    "council_submit_result",
    "council_task_status",
]


def tool_specs() -> dict[str, dict]:
    return {
        "council_list_tasks": {
            "name": "council_list_tasks",
            "description": "列出当前可领取的、已获人类批准的任务（不含 Prompt 正文）。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        "council_claim_task": {
            "name": "council_claim_task",
            "description": "领取指定任务，返回不可变 Prompt 与执行票。同一任务重复领取会失败。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID"},
                    "idempotency_key": {"type": "string", "description": "可选幂等键"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        "council_heartbeat": {
            "name": "council_heartbeat",
            "description": "上报执行心跳并续租，返回是否收到暂停/取消指令。超时未心跳任务会被阻断。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticket": {"type": "string"},
                    "status": {"type": "string", "enum": ["running", "paused"]},
                    "detail": {"type": "string", "maxLength": MAX_TEXT_FIELD},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["ticket"],
                "additionalProperties": False,
            },
        },
        "council_submit_result": {
            "name": "council_submit_result",
            "description": ("提交执行日志、Diff 与测试结果。completed 进入待人类验收，不会自动 DONE；"
                            "收到取消指令后必须用 status=cancelled 明确确认已停止。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticket": {"type": "string"},
                    "status": {"type": "string",
                               "enum": ["completed", "failed", "blocked", "cancelled"]},
                    "log": {"type": "string", "maxLength": MAX_TEXT_FIELD},
                    "diff": {"type": "string", "maxLength": MAX_TEXT_FIELD},
                    "tests": {"type": "string", "maxLength": MAX_TEXT_FIELD},
                    "note": {"type": "string", "maxLength": MAX_TEXT_FIELD},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["ticket", "status"],
                "additionalProperties": False,
            },
        },
        "council_task_status": {
            "name": "council_task_status",
            "description": "查询自己名下任务的状态与暂停/取消标记。",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    }


# 工具 -> 所需权限（P2-2：MCP 面同样受角色约束）
TOOL_PERMISSIONS = {
    "council_list_tasks": ("task.claim", "列出可领取任务"),
    "council_claim_task": ("task.claim", "领取任务"),
    "council_heartbeat": ("run.heartbeat", "上报心跳"),
    "council_submit_result": ("run.submit", "提交结果"),
    "council_task_status": ("task.read.own", "查询任务状态"),
}


def _status_payload(t: dict) -> dict:
    return {
        "ok": True,
        "task_id": t["id"],
        "status": t["status"],
        "status_label": t["status_label"],
        "version": t["current_version"],
        "control": {"pause": bool(t["control"].get("pause_requested")),
                    "cancel": bool(t["control"].get("cancel_requested"))},
        "blocked_reason": t.get("blocked_reason"),
    }


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _validate_args(spec: dict, args: dict) -> str | None:
    """按 inputSchema 做最小校验，返回错误文案（None 表示通过）。

    只校验三件事：必填、未知字段、基本类型。类型不符以前会一路穿到 core 里
    抛 TypeError、被兜底成「服务端内部错误」，客户端看不出是自己传错了。"""
    schema = spec.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [k for k in required if k not in args or args[k] in (None, "")]
    if missing:
        return "缺少必填参数：" + ", ".join(missing)
    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            return "不支持的参数：" + ", ".join(extra)
    for k, v in args.items():
        spec_k = props.get(k) or {}
        want = spec_k.get("type")
        if want == "string" and not isinstance(v, str):
            return f"参数 {k} 必须是字符串"
        # BFC-02：自由文本必须有字段级上限。MCP 请求整体虽然受 MAX_BODY_BYTES
        # 限制，但一个字段就能独占几乎整个请求体，在 JSON 解析和业务处理之前
        # 就已经进内存了。REST 侧 Pydantic 早就有 max_length，这里补上同一道。
        limit = spec_k.get("maxLength")
        if limit is not None and isinstance(v, str) and len(v) > limit:
            return f"参数 {k} 超过长度上限 {limit} 字符（实际 {len(v)}）"
        enum = spec_k.get("enum")
        if enum is not None and v not in enum:
            return f"参数 {k} 只能是 {' / '.join(map(str, enum))}"
    return None


def dispatch_tool(council: Council, ex: dict, name: str, args: dict) -> Any:
    """唯一的业务入口：MCP tools/call 与（未来）REST 薄封装都走这里。

    rc9：这里不碰协议、不碰 HTTP —— 协议在 mcp_adapter 的 ASGI 层，
    HTTP 状态码在 FastAPI 层。换传输方式不需要重写业务分发。"""
    need = TOOL_PERMISSIONS.get(name)
    if need:
        role = ex.get("role_canonical") or R.DENIED_ROLE
        if not R.can(role, need[0]):
            raise CouncilError(f"角色「{R.ROLE_LABELS.get(role, role)}」无权{need[1]}")
    if name == "council_list_tasks":
        return {"ok": True, "tasks": council.available_tasks()}
    if name == "council_claim_task":
        tid = args.get("task_id")
        if not tid:
            raise CouncilError("缺少 task_id")
        return {"ok": True, **council.claim(tid, ex, args.get("idempotency_key"))}
    if name == "council_heartbeat":
        ticket = args.get("ticket")
        if not ticket:
            raise CouncilError("缺少执行票")
        return council.heartbeat(ticket, ex, args.get("status", "running"),
                                 args.get("detail", ""), args.get("idempotency_key"))
    if name == "council_submit_result":
        ticket = args.get("ticket")
        if not ticket:
            raise CouncilError("缺少执行票")
        status = args.get("status", "")
        if status not in ("completed", "failed", "blocked", "cancelled"):
            raise CouncilError("status 只能是 completed / failed / blocked / cancelled")
        return council.submit_result(
            ticket, ex, status, args.get("log", ""), args.get("diff", ""),
            args.get("tests", ""), args.get("note", ""), args.get("idempotency_key"))
    if name == "council_task_status":
        tid = args.get("task_id")
        if not tid:
            raise CouncilError("缺少 task_id")
        t = council.get_task(tid)
        owner = t.get("executor_id")
        if not owner or owner != ex.get("id"):
            raise CouncilError("任务不属于该执行器")
        return _status_payload(t)
    raise CouncilError(f"未知工具：{name}")


# ---------------------------------------------------------------- SDK 桥接
# 当前请求的执行器身份。ContextVar 在这里是唯一正确的载体：
# - 不能放全局变量，两个执行器并发心跳会互相串号；
# - 不能塞进 Server 实例，SDK 的 Server 是全场共享的。
# 已实测：stateless manager 内子任务继承本请求的上下文（TestClient 与真 socket 都验过）。
_CURRENT_EXECUTOR: contextvars.ContextVar = contextvars.ContextVar(
    "bigfish_mcp_executor", default=None)


def _sdk_supported_methods() -> tuple[set, set]:
    """从 SDK 的请求/通知模型里反射出方法名集合。

    不写死的原因：SDK 会加方法（tasks/* 就是后来加的），写死等于给未来的自己
    埋一个「客户端升级后我们回 -32601」的坑。反射失败时退回一份保守清单。"""
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
    except Exception:  # pragma: no cover - 只在 SDK 结构大改时触发
        pass
    if not req:
        req = {"initialize", "ping", "tools/list", "tools/call"}
    return req, note | {"notifications/initialized"}


def build_server(council: Council):
    """构造 lowlevel Server：工具集由 tool_specs() 决定，处理器只做转发。"""
    from mcp.server.lowlevel import Server
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    srv = Server(
        name=SERVICE,
        version=__version__,
        instructions=("大肥鱼议事厅任务板。只能领取已获人类批准的任务；"
                      "汇报进度用 council_heartbeat，提交结果用 council_submit_result，"
                      "收到取消指令并用 council_submit_result(status=cancelled) 确认停止。"),
    )

    @srv.list_tools()
    async def _list_tools():
        specs = tool_specs()
        return [types.Tool(**specs[n]) for n in TOOL_ORDER]

    def _err_text(message: str):
        return types.ServerResult(types.CallToolResult(
            content=[types.TextContent(type="text",
                                       text=_json({"ok": False, "error": message}))],
            isError=True))

    async def _call_tool(req):
        name = req.params.name
        args = req.params.arguments or {}
        if name not in tool_specs():
            # 未知工具是协议层的「方法不存在」，不是业务失败：必须回 JSON-RPC
            # error(-32601)，否则客户端会把它当成一次「工具执行失败」而重试。
            raise McpError(types.ErrorData(code=-32601, message=f"未知工具：{name}"))
        if not isinstance(args, dict):
            return _err_text("arguments 必须是对象")
        bad = _validate_args(tool_specs()[name], args)
        if bad:
            return _err_text(bad)
        ex = _current_executor(council)
        try:
            # SQLite 是同步阻塞调用，直接跑会卡住事件循环（并发心跳会被排队）
            content = await run_in_threadpool(dispatch_tool, council, ex, name, args)
        except CouncilError as e:
            return _err_text(str(e))
        except HTTPException as e:
            return _err_text(str(e.detail))
        except MaintenanceError as e:
            # 理论上进不来（维护期在 ASGI 层就被拦成 503），留着是为了不把
            # 「这次没执行」降级成「你参数错了」。
            return _err_text(str(e))
        except Exception:
            return _err_text("服务端内部错误")
        if not isinstance(content, dict) or content.get("ok") is False:
            return _err_text(content.get("error") if isinstance(content, dict)
                             else str(content))
        return types.ServerResult(types.CallToolResult(
            content=[types.TextContent(type="text", text=_json(content))],
            isError=False))

    # 直接挂 request_handlers 而不用 @srv.call_tool() 装饰器：装饰器会把所有
    # 异常（含 McpError）吞成 isError 结果，那样「未知工具」就再也回不了 -32601。
    srv.request_handlers[types.CallToolRequest] = _call_tool
    return srv


def _current_executor(council: Council) -> dict:
    """取本次请求的执行器身份：优先 ContextVar，退回看原始请求头。"""
    ex = _CURRENT_EXECUTOR.get()
    if ex is not None:
        return ex
    # 兜底：SDK 内部实现若换了任务创建方式，上下文可能丢失。此时从
    # request_ctx 里取本请求的 Authorization 头重新认证，保证「宁可多查一次
    # 也不能让工具跑到别人的身份上」。
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
        req = getattr(ctx, "request", None)
        raw = req.headers.get("authorization") if req is not None else None
    except Exception:
        raw = None
    if raw and raw.lower().startswith("bearer "):
        try:
            return council.authenticate(raw.split(" ", 1)[1].strip(), role="executor")
        except CouncilError:
            pass
    raise CouncilError("缺少执行器身份上下文")


def _response(status: int, payload: Any, extra: dict | None = None) -> bytes:
    return _json(payload).encode()


async def _read_body(receive) -> bytes:
    chunks = []
    more = True
    while more:
        msg = await receive()
        if msg.get("type") == "http.disconnect":
            break
        chunks.append(msg.get("body", b"") or b"")
        more = bool(msg.get("more_body"))
    return b"".join(chunks)


class McpAsgi:
    """MCP 端点的 ASGI 实现。

    顺序刻意是「我们的闸门 -> SDK」：鉴权、维护期、结构校验都在进 SDK 之前
    完成，这样错误可以是正确的 HTTP 状态码（401 / 503），而不是 200 + 错误体。
    """

    def __init__(self, council: Council):
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        self.council = council
        self.server = build_server(council)
        self._manager_factory = StreamableHTTPSessionManager
        self.request_methods, self.notification_methods = _sdk_supported_methods()

    def _new_manager(self):
        """每个请求新建一个 manager。

        SDK 的 StreamableHTTPSessionManager.run() 每个实例只能进一次
        （streamable_http_manager.py:155 会抛 "can only be called once per
        instance"）。stateless=True 下它本来也不保存会话状态，所以按请求
        新建既不损失能力，也免掉了「必须把 app lifespan 拉起来」的要求 ——
        TestClient 不进 with、别人直接 app(scope, ...) 都能用。
        """
        return self._manager_factory(
            app=self.server,
            json_response=True,
            stateless=True,
            max_request_body_size=MAX_BODY_BYTES,
        )

    # ---- 走 SDK 之前的小闸门 ----
    @staticmethod
    def _jsonrpc_error(rid, code: int, message: str, status: int = 200):
        return status, {"jsonrpc": "2.0", "id": rid,
                        "error": {"code": code, "message": message}}

    def _auth(self, headers: dict) -> tuple[dict | None, tuple | None]:
        raw = headers.get("authorization")
        if not raw or not raw.lower().startswith("bearer "):
            return None, self._jsonrpc_error(None, -32001, "缺少 Bearer 令牌", 401)
        token = raw.split(" ", 1)[1].strip()
        if not token:
            return None, self._jsonrpc_error(None, -32001, "缺少 Bearer 令牌", 401)
        try:
            return self.council.authenticate(token, role="executor"), None
        except CouncilError as e:
            return None, self._jsonrpc_error(None, -32001, str(e), 401)

    def _precheck(self, body: bytes):
        """返回 (是否继续交给 SDK, 终止时的 (status, payload))。"""
        try:
            msg = json.loads(body.decode("utf-8") or "null")
        except Exception:
            return False, self._jsonrpc_error(None, -32700, "请求体不是合法 JSON")
        if not isinstance(msg, dict):
            return False, self._jsonrpc_error(None, -32600, "不支持批量请求")
        if msg.get("jsonrpc") != "2.0":
            return False, self._jsonrpc_error(msg.get("id"), -32600,
                                              'jsonrpc 必须是 "2.0"')
        method = msg.get("method")
        has_id = "id" in msg
        if not isinstance(method, str) or not method:
            # 缺 method 既不是请求也不是通知，按 Invalid Request 回错。
            return False, self._jsonrpc_error(msg.get("id"), -32600, "缺少 method")
        known = method in self.request_methods or method in self.notification_methods
        if not known and has_id:
            return False, self._jsonrpc_error(msg.get("id"), -32601,
                                              f"不支持的方法：{method}")
        if not has_id:
            # 通知不回应答（JSON-RPC 2.0 §4.1）。统一在这里吃掉，不再交给 SDK：
            # SDK 的模型只认 notifications/* 那五个方法，收到 {"method": "ping"}
            # 会打一堆 pydantic 校验告警；而客户端其实只关心「收到了 202」。
            return False, (202, {"ok": True})
        return True, None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":  # pragma: no cover
            return
        headers = {}
        for k, v in scope.get("headers") or []:
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        method = (scope.get("method") or "").upper()

        # GET 在协议里是「可选 SSE 通道」，本项目不提供：显式 405，别让客户端
        # 在 406/404 之间猜。文档 docs/ARCHITECTURE-FUTURE.md 也按这个写。
        if method != "POST":
            await self._send_json(send, 405, {"error": "只接受 POST"})
            return

        ex, stop = self._auth(headers)
        if stop is not None:
            await self._send_json(send, stop[0], stop[1])
            return

        body = await _read_body(receive)

        # 维护期（恢复备份中）：必须表达成「这次没执行」，绝不能回一个成功的
        # result —— 执行器会以为票据到手了，然后在恢复后的新库里找不到它。
        if self.council.db.in_maintenance():
            await self._send_json(
                send, 503,
                {"ok": False,
                 "error": f"数据库正在维护（{self.council.db.maintenance_reason or '正在从备份恢复'}），"
                          "本次操作未执行，请稍后重试",
                 "maintenance": True},
                extra_headers=[(b"retry-after", b"15")])
            return

        go_on, stop = self._precheck(body)
        if not go_on:
            await self._send_json(send, stop[0], stop[1])
            return

        # Accept 兼容：SDK 的 streamable HTTP 要求客户端声明两种响应类型，
        # 手写版从来不看这个头。ds h 之外还有脚本直接 curl，缺头就 406 太苛刻。
        if "text/event-stream" not in headers.get("accept", ""):
            new_headers = [(k, v) for k, v in scope["headers"]
                           if k.decode("latin-1").lower() != "accept"]
            new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope = dict(scope, headers=new_headers)

        token = _CURRENT_EXECUTOR.set(ex)
        replayed = {"done": False}

        async def replay_receive():
            if not replayed["done"]:
                replayed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        manager = self._new_manager()
        try:
            async with manager.run():
                await manager.handle_request(scope, replay_receive, send)
        finally:
            _CURRENT_EXECUTOR.reset(token)

    @staticmethod
    async def _send_json(send, status: int, payload: Any,
                         extra_headers: list | None = None) -> None:
        body = _response(status, payload, None)
        headers = [(b"content-type", b"application/json; charset=utf-8"),
                   (b"content-length", str(len(body)).encode())]
        headers.extend(extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def have_sdk() -> bool:
    """官方 MCP SDK 是否可用（依赖缺失时降级成明确报错，而不是 ImportError 栈）。"""
    import importlib.util

    for mod in ("mcp.server.lowlevel", "mcp.server.streamable_http_manager"):
        if importlib.util.find_spec(mod) is None:
            return False
    return True