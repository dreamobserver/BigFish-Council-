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
- 业务失败 isError=true，鉴权失败 HTTP 401 + -32001（P1-4；rc9 起按 MCP
  规范从 200 改成 401，否则官方客户端会把 error 当成协议错挂住）。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import __version__, roles as R
from .config import DEFAULT_API_HOST, DEFAULT_API_PORT, is_loopback_host
from .core import Council, CouncilError
from .db import MaintenanceError
from .mcp_adapter import (  # noqa: F401 —— 协议层迁出后仍从这里导出，历史 import 不用改
    MAX_BODY_BYTES,
    MAX_TEXT_FIELD,
    MCP_PROTOCOL,
    SERVICE,
    TOOL_ORDER,
    TOOL_PERMISSIONS,
    McpAsgi,
    _status_payload,
    dispatch_tool,
    have_sdk,
    tool_specs,
)

# rc9：协议层搬到 mcp_adapter 之后，这些名字仍然从 server 导出（历史 import
# 路径不动，测试与 tools/selfcheck.py 直接 from bigfish.server import ...）。
# 写进 __all__ 既表明是「有意再导出」，也让 pyflakes 静态检查不把它们误报成未使用。
__all__ = [
    "BindError",
    "MAX_BODY_BYTES",
    "MAX_TEXT_FIELD",
    "MCP_PROTOCOL",
    "McpAsgi",
    "SERVICE",
    "TOOL_ORDER",
    "TOOL_PERMISSIONS",
    "check_bind_host",
    "create_app",
    "create_production_app",
    "dispatch_tool",
    "have_sdk",
    "serve",
    "tool_specs",
]

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


class LoopbackGuardMiddleware:
    """连接级回环闸门（BFC-03）。

    审计指出：create_app() 只返回 app，第三方仍可用
    uvicorn.run(create_app(...), host="0.0.0.0") 绕开 check_bind_host()。
    与其在文档里声明「别这么用」，不如让 app 自己对每一根连接再判一次——
    这样端口就算被误绑到全网卡，外部请求也拿不到任何数据。

    判据用两个都由传输层填充、客户端伪造不了的字段：
    - scope["client"][0]：对端 IP，局域网/公网来客必然不是回环。
    - scope["server"][0]：本端 IP（绑 0.0.0.0 时是**该连接实际落到的本地地址**），
      外部来客连上来时这里是网卡地址，同样不是回环。

    两者任一非回环就 403。TestClient 走进程内调用，对端写作 testclient，
    按受信处理（它不经过任何 socket，攻击者无法从外部获得这个值）。
    """

    # TestClient 的 scope 写作 client=("testclient", 50000) / server=("testserver", 80)，
    # 两个都是进程内常量，不经过 socket。
    TRUSTED_PEERS = frozenset({"testclient", "testserver"})

    def __init__(self, app):
        self.app = app

    @classmethod
    def _ok(cls, addr) -> bool:
        if not addr:
            return True                       # 无对端信息（unix socket）交由上游
        if addr in cls.TRUSTED_PEERS:
            return True
        return is_loopback_host(addr)

    @staticmethod
    async def _reject(send, addr) -> None:
        body = json.dumps({
            "ok": False,
            "error": f"本服务只接受回环访问，当前连接来自 {addr or 'unknown'}",
        }, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            peer = (scope.get("client") or (None,))[0]
            local = (scope.get("server") or (None,))[0]
            for addr in (peer, local):
                if not self._ok(addr):
                    await self._reject(send, addr)
                    return
        await self.app(scope, receive, send)


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
    """执行器权限边界（P2-2）：HTTP/MCP 只放行执行器能力，越权一律 403。

    rc8 / R7-02：身份里没有 role_canonical 时**不再默认当执行器**，而是零权限
    的 DENIED_ROLE —— 缺字段只会被拒，不会顺带拿到 task.claim。
    """
    role = ex.get("role_canonical") or R.DENIED_ROLE
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
    """组装 ASGI app。**本函数不校验监听地址**（供测试/进程内挂载）。

    它只返回 app 对象，不 bind socket。要对外提供服务请用
    create_production_app()（带监听地址闸门）或 serve()。

    BFC-03 的两道防线是分层的：
    1. create_production_app()/serve()/gui.run()/cli serve 在**启动前**拒掉非回环；
    2. LoopbackGuardMiddleware 在**每根连接**上再判一次，所以即使有人硬拿
       本函数配 uvicorn.run(host="0.0.0.0")，外部请求也只能拿到 403。
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
    # BFC-03：连接级回环闸门，堵住「绕过 serve() 直接 uvicorn.run」这条路。
    app.add_middleware(LoopbackGuardMiddleware)

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

    # ---------------- MCP（streamable HTTP，官方 SDK 承载协议） ----------------
    # rc9 / AUDIT-RC8-01：协议层不再手写。McpAsgi 内部先过鉴权与维护期闸门，
    # 再交给 SDK 的 StreamableHTTPSessionManager；业务仍然只有 dispatch_tool() 一个入口。
    #
    # 用 Route 而不是 Mount：Mount("/mcp") 对精确路径 /mcp 只会给 PARTIAL 匹配，
    # Starlette 的 redirect_slashes 会先回一个 307 到 /mcp/。老客户端（以及
    # tools/selfcheck.py、remote_check.py）都是直接打 /mcp，不该被跳一次；
    # Route 的 endpoint 传「类实例」时 Starlette 按 ASGI 调用（函数才会被当成
    # request-response 包一层），所以这里两个路径都能直接命中。
    _mcp_app = McpAsgi(council)
    app.router.routes.insert(0, Route("/mcp", endpoint=_mcp_app,
                                      methods=["POST", "GET", "DELETE"]))
    app.router.routes.insert(1, Route("/mcp/", endpoint=_mcp_app,
                                      methods=["POST", "GET", "DELETE"]))

    return app


def _owned_status(council: Council, ex: dict, task_id: str) -> dict:
    """任务状态只给自己的执行器看；未分配任务对执行器一律 403（P1-3.3）。"""
    t = council.get_task(task_id)
    owner = t.get("executor_id")
    if not owner or owner != ex.get("id"):
        raise HTTPException(status_code=403, detail="任务不属于该执行器")
    return _status_payload(t)


def create_production_app(council: Council, host: str | None = None,
                            watchdog_interval: float | None = None) -> tuple[FastAPI, str]:
    """带监听闸门的 app 工厂（BFC-03 的官方入口）。

    与 create_app() 的唯一区别：host 先过 check_bind_host()，非回环直接抛
    BindError。这样「谁调这个工厂，谁就不可能拿到一个准备绑 0.0.0.0 的 app」。
    返回的 tuple 第二项是校验后的 host，避免调用方各写一遍归一化。
    """
    return create_app(council, watchdog_interval=watchdog_interval), check_bind_host(host)


def serve(council: Council, host: str = DEFAULT_API_HOST, port: int = DEFAULT_API_PORT,
          watchdog_interval: float | None = 10.0):
    """阻塞启动（供 CLI / 后台线程使用）。

    监听地址在这里统一校验：GUI 内嵌线程、CLI、模块入口都走同一条路，
    不存在「换个入口就能绑 0.0.0.0」的绕过（P1-3.1）。
    """
    import uvicorn
    app, host = create_production_app(
        council, host, watchdog_interval=watchdog_interval)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
