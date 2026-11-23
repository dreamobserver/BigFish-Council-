"""Windows 侧自检：不需要 GUI，验证服务能起、能鉴权、能走完 MCP 闭环."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bigfish.config import Settings          # noqa: E402
from bigfish.runtime import open_council     # noqa: E402
from bigfish.server import create_app        # noqa: E402

# 3099 已被预留为手机端 REST 网关（见 docs/ARCHITECTURE-FUTURE.md 第 2 节），
# 自检不能占那个号。3095 在本容器里已被 quota-hub 长期监听（2026-11-19 实测），
# 属于「端口冲突会让自检认错服务」的真实故障，故改用 3094。
PORT = int(os.environ.get("BFC_SELFCHECK_PORT", "3094"))
PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))


def post(path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode(errors="replace") or "{}")


def get(path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode(errors="replace") or "{}")


def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="bfc-selfcheck-"))
    s = Settings.load(tmp)
    s.ensure_dirs()
    db, council = open_council(s)     # 走真实启动路径：建库 + 生成/加载执行票密钥

    task = council.create_task("自检任务", "验证闭环", "本机", "low", "echo selfcheck")
    council.submit_for_review(task["id"])
    council.add_review(task["id"], "gpt", "ok", "approve", kind="vote")
    council.add_review(task["id"], "gemini", "ok", "approve", kind="vote")
    council.approve(task["id"], "approve", "自检批准")
    ex = council.register_executor("selfcheck", "executor")

    import uvicorn
    app = create_app(council, watchdog_interval=2.0)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error",
                         access_log=False, lifespan="on")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    time.sleep(2.0)

    print("健康检查")
    # 端口被别人占着时 uvicorn 会 bind 失败，而 /health 仍会返回别人的服务。
    # 必须先确认对面真的是 bigfish-council，否则后面每条都会拿着别人的响应瞎判。
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=5) as r:
            health = json.loads(r.read().decode())
    except Exception as exc:                       # noqa: BLE001
        print("  [FAIL] 连不上自检服务：%r" % (exc,))
        print("\n端口 %d 可能被占用，用 BFC_SELFCHECK_PORT 换一个空闲端口。" % PORT)
        return 1
    if health.get("service") != "bigfish-council":
        print("  [FAIL] 端口 %d 上跑的不是本服务（service=%r）"
              % (PORT, health.get("service")))
        print("\n端口被别的程序占用，用 BFC_SELFCHECK_PORT 换一个空闲端口。")
        return 1
    check("服务在线", health.get("status") == "ok", health.get("service", ""))
    check("health 不含任务信息", "tasks" not in json.dumps(health))
    check("看门狗已随服务启动", health.get("watchdog") is True)

    print("MCP 协议")
    status, init = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26",
                                            "capabilities": {},
                                            "clientInfo": {"name": "selfcheck", "version": "1"}}}, ex["token"])
    check("initialize 成功", status == 200 and init["result"]["serverInfo"]["name"] == "bigfish-council")

    status, tl = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, ex["token"])
    names = [t["name"] for t in tl["result"]["tools"]]
    check("五个执行器工具", names == ["council_list_tasks", "council_claim_task",
                                      "council_heartbeat", "council_submit_result", "council_task_status"],
          ",".join(names))
    check("没有审批类工具", not any("approve" in n or "admin" in n for n in names))

    def call(name: str, args: dict) -> dict:
        st, out = post("/mcp", {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                "params": {"name": name, "arguments": args}}, ex["token"])
        return json.loads(out["result"]["content"][0]["text"])

    print("闭环")
    listed = call("council_list_tasks", {})
    check("能列出已批准任务", listed["ok"] and len(listed["tasks"]) == 1)
    check("列表不含 Prompt", "prompt" not in json.dumps(listed))
    claimed = call("council_claim_task", {"task_id": task["id"]})
    check("能领取并拿到 Prompt", claimed["ok"] and bool(claimed.get("prompt")))
    check("重复领取被拒", call("council_claim_task", {"task_id": task["id"]})["ok"] is False)
    hb = call("council_heartbeat", {"ticket": claimed["ticket"], "status": "running", "detail": "自检"})
    check("心跳上报", hb["ok"] and hb["task_status"] == "RUNNING")
    sub = call("council_submit_result", {"ticket": claimed["ticket"], "status": "completed",
                                         "log": "selfcheck log", "tests": "ok", "note": "自检回传"})
    check("回传只到待验收", sub["ok"] and sub["task_status"] == "SUBMITTED")
    st = call("council_task_status", {"task_id": task["id"]})
    check("状态可查", st["status"] == "SUBMITTED")

    print("权限")
    st, body = post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                    "bfc_ex_bogus")
    check("坏 Token 被拒", st == 200 and body.get("error", {}).get("code") == -32001)
    st, body = get("/api/v1/tasks/available", "bfc_ex_bogus")
    check("HTTP 坏 Token 被拒", st == 401)
    check("审批只能在进程内", council.get_task(task["id"])["status"] == "SUBMITTED")

    srv.should_exit = True
    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("自检全部通过，可以启动 GUI。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
