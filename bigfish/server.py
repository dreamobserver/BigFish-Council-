"""本地受限 API + MCP 服务器.

监听 127.0.0.1:3096（默认只绑回环，绝不监听 0.0.0.0）。Tailscale Serve 若启用，
由它把 3096 转发给 Tailnet 指定节点；公网 Funnel 一律不使用。

暴露面刻意区分：
- GUI / CLI（本地进程内调用 Council）拥有人类审批能力；
- HTTP/MCP 只暴露执行器能力：列任务、领取、心跳、回传、查状态。
  这里不注册任何 approve / 管理密钥 / 提权工具。

v0.1.1-rc3 修复：
- 监听地址校验下沉到 serve()，GUI / CLI / 模块三个入口共用同一道闸（P1-3.1）；
- 执行器查任务状态必须是自己的任务，未分配任务不再对任何人放行（P1-3.3）；
- MCP 与 REST 都接受 status=cancelled（取消确认），语义与 core 一致；
- 业务失败 isError=true，鉴权失败 -32001（P1-4）。
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, roles as R
from .config import DEFAULT_API_HOST, DEFAULT_API_PORT, is_loopback_host
from .core import Council, CouncilError
from .db import MaintenanceError

SERVICE = "bigfish-council"
MCP_PROTOCOL = "2025-03-26"
# F-09：请求体上限。默认 8 MiB，够放一份大 diff + 长日志，又能挡住
# 「被控制的执行器一次塞 1GB 进来把我们 2.4G 的容器打爆」。
MAX_BODY_BYTES = int(os.environ.get("BFC_MAX_BODY_BYTES") or 8 * 1024 * 1024)
# 单个自由文本字段的上限（MCP/REST 共用），比整体上限小一半，避免一个字段吃满。
MAX_TEXT_FIELD = int(os.environ.get("BFC_MAX_TEXT_FIELD") or 4 * 1024 * 1024)
SUBMIT_CHOICES = "^(completed|failed|blocked|cancelled)$"


class BindError(RuntimeError):
    """监听地址不被允许：非回环地址一律拒绝启动。"""


def check_bind_host(host: str | None) -> str:
    """监听地址闸门：只允许 IPv4/IPv6 回环（P1-3.1）。

    任何入口（GUI 后台线程 / CLI serve / python -m bigfish / 测试）都必须经过这里，
    所以「把校验写在 __main__ 里」那种做法不再成立。
    """
    h = (host or DEFAULT_API_HOST).strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not is_loopback_host(h):
        raise BindError(
            "拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。"
            "如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。")
    return h


class BodyLimitMiddleware:
    """ASGI 层请求体上限（F-09）。

    为什么不用 Content-Length 判断就够了：分块传输（chunked）根本没有这个头。
    所以这里包住 receive，边收边数，超了立刻回 413 并断开，不让它进内存。
    """

    def __init__(self, app, limit: int = MAX_BODY_BYTES):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = None
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > self.limit:
            await self._too_large(send)
            return

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._too_large(send)

    @staticmethod
    async def _too_large(send):
        body = b'{"ok":false,"error":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class ClaimBody(BaseModel):
    task_id: str = Field(max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=200)


class HeartbeatBody(BaseModel):
    ticket: str = Field(max_length=256)
    status: str = Field(default="running", pattern="^(running|paused)$")
    detail: str = Field(default="", max_length=MAX_TEXT_FIELD)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ResultBody(BaseModel):
    ticket: str = Field(max_length=256)
    status: str = Field(pattern=SUBMIT_CHOICES)
    # F-09：单个自由文本字段也要有上限，否则一个字段就能吃满请求体配额
    log: str = Field(default="", max_length=MAX_TEXT_FIELD)
    diff: str = Field(default="", max_length=MAX_TEXT_FIELD)
    tests: str = Field(default="", max_length=MAX_TEXT_FIELD)
    note: str = Field(default="", max_length=MAX_TEXT_FIELD)
    idempotency_key: str | None = Field(default=None, max_length=200)


def _need(ex: dict, permission: str, action: str) -> None:
    """执行器权限边界（P2-2）：HTTP/MCP 只放行执行器能力，越权一律 403。"""
    role = ex.get("role_canonical") or R.EXECUTOR_DSH
    if not R.can(role, permission):
        raise HTTPException(
            status_code=403,
            detail=f"角色「{R.ROLE_LABELS.get(role, role)}」无权{action}")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    return token


def create_app(council: Council, watchdog_interval: float | None = None) -> FastAPI:
    """组装 ASGI app。**本函数不负责监听地址**（rc6 F-08）。

    它只返回 app，不 bind socket。想真正对外提供服务，必须走 serve()——
    serve() 里有 check_bind_host() 这道回环闸门。直接
    `uvicorn.run(create_app(...), host="0.0.0.0")` 会绕过闸门，这不是官方入口，
    也不被支持：部署只用 serve() / GUI / `python -m bigfish`。

    为什么要写这么长一段：审计指出「四个入口都校验了」不等于「app 本身安全」，
    与其假装封死，不如把边界说清楚，让后来的人别再自己 uvicorn.run。
    """
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        started = False
        if watchdog_interval is not None:
            council.start_watchdog(watchdog_interval)
            started = True
        try:
            yield
        finally:
            # 只停自己启的；watchdog_interval=None 时绝不能停外部（GUI/CLI）的看门狗
            if started:
                council.stop_watchdog()

    app = FastAPI(title="BigFish Council Local API", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)

    def executor(authorization: str | None = Header(default=None)) -> dict:
        token = _bearer(authorization)
        try:
            return council.authenticate(token, role="executor")
        except CouncilError as e:
            raise HTTPException(status_code=401, detail=str(e))

    @app.exception_handler(CouncilError)
    async def _council_err(_req: Request, exc: CouncilError):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})

    @app.exception_handler(MaintenanceError)
    async def _maint_err(_req: Request, exc: MaintenanceError):
        # R8：维护模式是「暂时不能写」，不是「你错了」。503 + Retry-After 让执行器
        # 走重试/对账分支，而不是把它当成 4xx 业务失败直接放弃。
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": str(exc),
                                     "maintenance": True},
                            headers={"Retry-After": "15"})

    # ---------------- 健康检查（无需鉴权，信息最小化） ----------------
    @app.get("/health")
    def health():
        ok = True
        try:
            council.db.one("SELECT 1 AS x")
        except Exception:
            ok = False
        wd = getattr(council, "_watchdog", None)
        maint = council.db.in_maintenance()
        return {
            "status": "maintenance" if maint else ("ok" if ok else "degraded"),
            "service": SERVICE,
            "version": __version__,
            "time": time.time(),
            "db": "ok" if ok else "error",
            "watchdog": bool(wd and wd.is_alive()),
            "maintenance": maint,
            # 不返回任何任务、令牌或配置细节
        }

    # ---------------- 执行器接口 ----------------
    @app.get("/api/v1/tasks/available")
    def available(ex: dict = Depends(executor)):
        _need(ex, "task.claim", "领取任务")
        return {"ok": True, "tasks": council.available_tasks()}

    @app.post("/api/v1/tasks/claim")
    def claim(body: ClaimBody, ex: dict = Depends(executor)):
        _need(ex, "task.claim", "领取任务")
        return {"ok": True, **council.claim(body.task_id, ex, body.idempotency_key)}

    @app.post("/api/v1/runs/heartbeat")
    def heartbeat(body: HeartbeatBody, ex: dict = Depends(executor)):
        _need(ex, "run.heartbeat", "上报心跳")
        return council.heartbeat(body.ticket, ex, body.status, body.detail,
                                 body.idempotency_key)

    @app.post("/api/v1/runs/result")
    def result(body: ResultBody, ex: dict = Depends(executor)):
        _need(ex, "run.submit", "提交结果")
        return council.submit_result(body.ticket, ex, body.status, body.log, body.diff,
                                     body.tests, body.note, body.idempotency_key)

    @app.get("/api/v1/tasks/{task_id}/status")
    def task_status(task_id: str, ex: dict = Depends(executor)):
        _need(ex, "task.read.own", "查询任务状态")
        return _owned_status(council, ex, task_id)

    # ---------------- MCP（streamable HTTP, JSON 响应） ----------------
    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            token = _bearer(request.headers.get("authorization"))
        except HTTPException as e:
            return _mcp_error(None, -32001, str(e.detail))
        try:
            ex = council.authenticate(token, role="executor")
        except CouncilError as e:
            return _mcp_error(None, -32001, str(e))

        try:
            msg = await request.json()
        except Exception:
            return _mcp_error(None, -32700, "请求体不是合法 JSON")
        if not isinstance(msg, dict):
            return _mcp_error(None, -32600, "不支持批量请求")

        if not isinstance(msg.get("jsonrpc"), str) or msg.get("jsonrpc") != "2.0":
            return _mcp_error(msg.get("id"), -32600, "jsonrpc 必须是 \"2.0\"")
        method = msg.get("method")
        has_id = "id" in msg
        rid = msg.get("id")
        if not isinstance(method, str) or not method:
            # 通知 = 没有 id 且没有 method 合法值？不对：通知也有 method。
            # 缺 method 的请求既不是请求也不是通知，必须按 Invalid Request 回错。
            return _mcp_error(rid, -32600, "缺少 method")
        if not has_id:  # 真通知：无 id，不回结果体
            return JSONResponse(status_code=202, content={"ok": True})

        def ok(result: Any):
            return JSONResponse(content={"jsonrpc": "2.0", "id": rid, "result": result})

        def err(code: int, message: str):
            return JSONResponse(content={"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": code, "message": message}})

        if method == "initialize":
            return ok({
                "protocolVersion": MCP_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bigfish-council", "version": __version__},
                "instructions": "大肥鱼议事厅任务板。只能领取已获人类批准的任务；"
                                "汇报进度用 council_heartbeat，提交结果用 council_submit_result，"
                                "收到取消指令并用 council_submit_result(status=cancelled) 确认停止。",
            })
        if method == "ping":
            # ping 带 id 就是请求，必须回 result；回 202 会让客户端一直等超时（E3）
            return ok({})
        if method == "notifications/initialized":
            return JSONResponse(status_code=202, content={"ok": True})
        if method == "tools/list":
            return ok({"tools": [tool_specs()[n] for n in TOOL_ORDER]})
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return err(-32602, "arguments 必须是对象")
            if name not in tool_specs():
                return err(-32601, f"未知工具：{name}")
            is_error = False
            try:
                bad = _validate_args(tool_specs()[name], args)
                if bad:
                    raise CouncilError(bad)
                # SQLite 是同步阻塞调用，直接跑会卡住事件循环（并发心跳会被排队）
                content = await run_in_threadpool(dispatch_tool, council, ex, name, args)
                if not isinstance(content, dict) or content.get("ok") is False:
                    is_error = True
            except MaintenanceError as e:
                # 维护期：明确告诉调用方「这次没执行」，别让它以为业务失败
                return _mcp_error(rid, -32000, str(e))
            except CouncilError as e:
                content, is_error = {"ok": False, "error": str(e)}, True
            except HTTPException as e:
                content, is_error = {"ok": False, "error": str(e.detail)}, True
            except Exception:
                content, is_error = {"ok": False, "error": "服务端内部错误"}, True
            return ok({"content": [{"type": "text", "text": _json(content)}],
                       "isError": is_error})

        return err(-32601, f"不支持的方法：{method}")

    @app.get("/mcp")
    def mcp_get():
        return JSONResponse(status_code=405, content={"error": "只接受 POST"})

    return app


def _owned_status(council: Council, ex: dict, task_id: str) -> dict:
    """任务状态只给自己的执行器看；未分配任务对执行器一律 403（P1-3.3）。"""
    t = council.get_task(task_id)
    owner = t.get("executor_id")
    if not owner or owner != ex.get("id"):
        raise HTTPException(status_code=403, detail="任务不属于该执行器")
    return _status_payload(t)


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
        want = (props.get(k) or {}).get("type")
        if want == "string" and not isinstance(v, str):
            return f"参数 {k} 必须是字符串"
        enum = (props.get(k) or {}).get("enum")
        if enum is not None and v not in enum:
            return f"参数 {k} 只能是 {' / '.join(map(str, enum))}"
    return None


def _mcp_error(rid: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(content={"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": code, "message": message}})


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
    import json
    return json.dumps(obj, ensure_ascii=False)


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
                    "detail": {"type": "string"},
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
                    "log": {"type": "string"},
                    "diff": {"type": "string"},
                    "tests": {"type": "string"},
                    "note": {"type": "string"},
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


def dispatch_tool(council: Council, ex: dict, name: str, args: dict) -> Any:
    need = TOOL_PERMISSIONS.get(name)
    if need:
        role = ex.get("role_canonical") or R.EXECUTOR_DSH
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


def serve(council: Council, host: str = DEFAULT_API_HOST, port: int = DEFAULT_API_PORT,
          watchdog_interval: float | None = 10.0):
    """阻塞启动（供 CLI / 后台线程使用）。

    监听地址在这里统一校验：GUI 内嵌线程、CLI、模块入口都走同一条路，
    不存在「换个入口就能绑 0.0.0.0」的绕过（P1-3.1）。
    """
    import uvicorn
    host = check_bind_host(host)
    app = create_app(council, watchdog_interval=watchdog_interval)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
