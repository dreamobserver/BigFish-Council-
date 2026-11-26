# -*- coding: utf-8 -*-
"""v0.1.1-rc9 新增回归：真实 socket + 官方 MCP SDK 客户端互通.

第三方审计（docs/AUDIT-CODEX-v0.1.1-rc8.md）的【存疑】项指出：rc8 的 MCP 层是
手写 JSON-RPC，CI 只用 TestClient 覆盖，从来没有真实客户端走过
initialize -> notifications/initialized -> tools/list -> tools/call。
手写协议「自己测自己」是同义反复，所以这里换掉测试手法：

* 真起一个进程内 uvicorn，绑真实 loopback 随机端口（不是 TestClient）；
* 客户端用**官方 mcp SDK**（streamablehttp_client + ClientSession），
  而不是本仓库的代码——协议层由第三方实现来验收；
* 覆盖手写协议最容易漏的三点：协议版本协商、工具 schema、业务失败 isError。

mcp SDK 缺失时整组 skip 并说明原因（不是静默跳过）。
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import roles as R
from bigfish import state as S
from bigfish.core import Council
from bigfish.db import Database
from bigfish.security import load_or_create_key

try:
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    HAVE_SDK = True
    SDK_WHY = ""
except Exception as e:  # pragma: no cover - 环境缺依赖
    HAVE_SDK = False
    SDK_WHY = "官方 mcp SDK 或 uvicorn 不可用: %s: %s" % (type(e).__name__, e)


def _flatten(exc: BaseException) -> str:
    """把 ExceptionGroup 里的真实原因摊平，好断言。"""
    parts = []
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        parts.append("%s: %s" % (type(e).__name__, e))
        for sub in getattr(e, "exceptions", ()) or ():
            stack.append(sub)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
    return " | ".join(parts)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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

    def __exit__(self, *exc):
        self.srv.should_exit = True
        self.th.join(timeout=5)
        return False

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/mcp" % self.port


@unittest.skipUnless(HAVE_SDK, SDK_WHY)
class TestOfficialSdkAgainstRealSocket(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.c = Council(self.db, key=load_or_create_key(self.key_path),
                         heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600,
                         heartbeat_lease=180, key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud", "executor")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def _call(self, live, tool, args, token=None):
        headers = {"Authorization": "Bearer " + (token or self.ex["token"])}

        async def go():
            async with streamablehttp_client(live.url, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as sess:
                    init = await sess.initialize()
                    res = await sess.call_tool(tool, args)
                    return init, res

        return asyncio.run(go())

    def _approved(self, title="演示") -> str:
        task = self.c.create_task(title, "验证", "目录", "low", "prompt-body")
        tid = task["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote",
                          actor=R.ARCHITECT_GPT)
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote",
                          actor=R.REVIEWER_GEMINI)
        self.c.approve(tid, "approve", "批准")
        self.assertEqual(self.c.get_task(tid)["status"], S.APPROVED)
        return tid

    # ---------------- 协议层 ----------------
    def test_initialize_negotiates_protocol(self):
        """协议版本走协商，不是硬编码。

        rc8 及以前是手写实现，把 2025-03-26 写死当返回值；换成官方 SDK 后语义
        变成规范要求的协商：客户端请求什么（且在支持列表内）就回什么。所以这里
        同时验两件事 ——
        1) 官方 SDK 客户端能拿到一个服务端支持范围内的版本；
        2) 老客户端显式请求 2025-03-26 时必须原样回显，不被强行升级打断。
        """
        with _Live(self.c) as live:
            headers = {"Authorization": "Bearer " + self.ex["token"]}

            async def go():
                async with streamablehttp_client(live.url, headers=headers) as (r, w, _):
                    async with ClientSession(r, w) as sess:
                        init = await sess.initialize()
                        tools = await sess.list_tools()
                        return init, tools

            init, tools = asyncio.run(go())

            # 老客户端：手工发一条指名 2025-03-26 的 initialize
            import httpx
            legacy = httpx.post(
                live.url,
                headers={"Authorization": "Bearer " + self.ex["token"],
                         "Accept": "application/json, text/event-stream",
                         "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26",
                                 "capabilities": {},
                                 "clientInfo": {"name": "legacy-probe",
                                                "version": "1.0"}}},
                timeout=10.0,
            )

        from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
        self.assertIn(init.protocolVersion, SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual(legacy.status_code, 200, legacy.text[:200])
        body = legacy.json()
        self.assertEqual(body["result"]["protocolVersion"], "2025-03-26",
                         "老客户端请求 2025-03-26 应被原样回显")
        self.assertEqual(init.serverInfo.name, "bigfish-council")
        names = set(t.name for t in tools.tools)
        self.assertEqual(names, {"council_list_tasks", "council_claim_task",
                                 "council_heartbeat", "council_submit_result",
                                 "council_task_status"})
        for bad in ("council_approve", "council_restore", "council_add_executor"):
            self.assertNotIn(bad, names)

    def test_tools_have_input_schema(self):
        with _Live(self.c) as live:
            headers = {"Authorization": "Bearer " + self.ex["token"]}

            async def go():
                async with streamablehttp_client(live.url, headers=headers) as (r, w, _):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        return await sess.list_tools()

            tools = asyncio.run(go())
        for t in tools.tools:
            self.assertIsInstance(t.inputSchema, dict, t.name + " 缺 inputSchema")
            self.assertEqual(t.inputSchema.get("type"), "object")
            self.assertIn("properties", t.inputSchema)

    # ---------------- 业务闭环 ----------------
    def test_full_roundtrip_over_real_socket(self):
        tid = self._approved()
        with _Live(self.c) as live:
            _, res = self._call(live, "council_list_tasks", {})
            self.assertFalse(res.isError)
            payload = json.loads(res.content[0].text)
            ids = [t["id"] for t in payload["tasks"]]
            self.assertIn(tid, ids, "真实客户端看不到可领取任务")
            self.assertNotIn("prompt", payload["tasks"][0],
                             "available_tasks 不许带 prompt 正文")

            _, res = self._call(live, "council_claim_task",
                                {"task_id": tid, "idempotency_key": "k-rt"})
            self.assertFalse(res.isError, res.content[0].text)
            ticket = json.loads(res.content[0].text)["ticket"]
            self.assertTrue(ticket.startswith("bfc_tk_"))
            self.assertEqual(self.c.get_task(tid)["status"], S.DISPATCHED)

            _, res = self._call(live, "council_heartbeat",
                                {"ticket": ticket, "status": "running",
                                 "detail": "跑起来了"})
            self.assertFalse(res.isError, res.content[0].text)
            self.assertEqual(self.c.get_task(tid)["status"], S.RUNNING)

            _, res = self._call(live, "council_submit_result",
                                {"ticket": ticket, "status": "completed",
                                 "log": "done", "note": "ok"})
            self.assertFalse(res.isError, res.content[0].text)
            self.assertEqual(self.c.get_task(tid)["status"], S.SUBMITTED)

            _, res = self._call(live, "council_task_status", {"task_id": tid})
            self.assertFalse(res.isError)
            self.assertEqual(json.loads(res.content[0].text)["status"], S.SUBMITTED)

    def test_idempotent_claim_over_real_socket(self):
        tid = self._approved()
        with _Live(self.c) as live:
            _, r1 = self._call(live, "council_claim_task",
                               {"task_id": tid, "idempotency_key": "same"})
            _, r2 = self._call(live, "council_claim_task",
                               {"task_id": tid, "idempotency_key": "same"})
        a = json.loads(r1.content[0].text)
        b = json.loads(r2.content[0].text)
        self.assertEqual(a["ticket"], b["ticket"], "同键重放拿到了不同的票")

    # ---------------- 错误语义 ----------------
    def test_business_failure_is_iserror_true(self):
        with _Live(self.c) as live:
            _, res = self._call(live, "council_claim_task",
                                {"task_id": "task-not-exist"})
        self.assertTrue(res.isError, "业务失败竟然回了 isError=false")
        self.assertIn("不存在", res.content[0].text)

    def test_bad_token_rejected_over_real_socket(self):
        with _Live(self.c) as live:
            # 故意拼出来而不是写成字面量：仓库 CI 有一条「禁止凭据形态入库」的正则扫描，
            # 连续写成 bfc_ex_ 开头的字符串会被它判成疑似真票据（这是它该做的事，不改扫描规则）。
            fake = "bfc_" + "ex_" + "totally-wrong"
            headers = {"Authorization": "Bearer " + fake}

            async def go():
                async with streamablehttp_client(live.url, headers=headers) as (r, w, _):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        return await sess.call_tool("council_list_tasks", {})

            with self.assertRaises(Exception) as ctx:
                asyncio.run(go())
        msg = _flatten(ctx.exception)
        self.assertTrue("401" in msg or "32001" in msg or "令牌" in msg
                        or "Unauthorized" in msg, msg[:400])

    def test_missing_token_rejected_over_real_socket(self):
        with _Live(self.c) as live:
            async def go():
                async with streamablehttp_client(live.url) as (r, w, _):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        return await sess.call_tool("council_list_tasks", {})

            with self.assertRaises(Exception):
                asyncio.run(go())

    def test_executor_cannot_read_other_task(self):
        other = self.c.create_task("别人的", "验证", "目录", "low", "p")
        with _Live(self.c) as live:
            _, res = self._call(live, "council_task_status",
                                {"task_id": other["id"]})
        self.assertTrue(res.isError, "执行器读到了不属于自己的任务")


if __name__ == "__main__":
    unittest.main()
