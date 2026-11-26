"""跨机 MCP 检查：从 VPS/DSH 侧直连 Windows 上的 BigFish Council /mcp 端点.

只读为主，闭环需显式授权。全部通过 MCP JSON-RPC（不是 REST），
用来证明「DSH 通过 Tailscale 真的能用 MCP 工具集」，而不是只有 /health 通。

用法：
  # 1) 默认只读：只检查传输/鉴权/协议/工具清单/可领取列表，不动任何任务
  BFC_BASE_URL=http://100.64.0.11:3096 BFC_TOKEN=bfc_ex_xxx \
      python tools/remote_check.py

  # 也可以从命令行传（比环境变量优先），两种写法都支持
  python tools/remote_check.py --base-url http://100.64.0.11:3096 --token bfc_ex_xxx
  python tools/remote_check.py --base-url=http://100.64.0.11:3096 --token=bfc_ex_xxx

  # 2) 显式指定任务才做 claim + heartbeat（仍然不提交结果）
  python tools/remote_check.py --task-id task-xxxxxx

  # 3) 只有再加 --allow-submit 才会回传结果，且 status 固定走 cancelled
  #    （表示「联调中止」，绝不是业务完成，绝不写 completed）
  python tools/remote_check.py --task-id task-xxxxxx --allow-submit

判定原则：拿不到证据就报 FAIL，绝不因为 /health 通就宣布联调成功。
安全原则（rc3 / rc4）：
  - **绝不自动挑选任务**。不传 --task-id 时只做只读检查，
    不会 claim / heartbeat / submit —— 旧版本自动拿 tasks[0] 会污染真实看板。
  - **绝不把测试写成业务完成**。v0.1.1-rc4 之前这里回传 status=completed，
    会把任务推到 SUBMITTED（待验收），等于用一次联调污染业务状态；
    现在默认不提交，--allow-submit 时也只提交 cancelled。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROTO = "2025-03-26"
PASS, FAIL, SKIP = [], [], []

# 运行期由 main() 按「命令行 > 环境变量」的优先级填好
BASE = ""
TOKEN = ""

# cancelled 需要任务已经处于 CANCELLING（人类已点取消），见 bigfish/core.py 的
# submit_result：非 CANCELLING 状态提交 cancelled 会被服务端拒绝。
CANCEL_PRECONDITION_HINT = (
    "cancelled 只能在任务已进入「取消中」之后提交：请先在 GUI 对该任务点取消，"
    "再重跑本脚本 --task-id <id> --allow-submit"
)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))


def skip(name, extra=""):
    SKIP.append(name)
    print("  [skip] " + name + (("  " + extra) if extra else ""))


def rpc(method, params=None, token=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + "/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw.strip() else {}


def tool(name, args=None, rid=1):
    st, r = rpc("tools/call", {"name": name, "arguments": args or {}}, TOKEN, rid)
    res = r.get("result") or {}
    text = ""
    for c in res.get("content") or []:
        if c.get("type") == "text":
            text += c.get("text") or ""
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"_raw": text}
    err = res.get("isError", False)
    if not err and isinstance(r.get("error"), dict):
        # JSON-RPC 层面的错误（例如 -32001 鉴权失败）也算失败
        return st, True, {"ok": False, "error": r["error"].get("message")}
    return st, err, payload


def parse_args(argv):
    """argparse 天然同时支持 --task-id xxx 与 --task-id=xxx 两种写法。"""
    ap = argparse.ArgumentParser(add_help=True, description="跨机 MCP 检查（默认只读）")
    ap.add_argument("--base-url", default=None, help="覆盖 BFC_BASE_URL")
    ap.add_argument("--token", default=None, help="覆盖 BFC_TOKEN")
    ap.add_argument("--task-id", default=None, help="显式指定联调任务；不给则纯只读")
    ap.add_argument("--allow-submit", action="store_true",
                    help="允许回传结果（status 固定 cancelled，不会写 completed）")
    a, unknown = ap.parse_known_args(argv)
    if unknown:
        print("  [warn] 忽略未知参数：%s" % " ".join(unknown), file=sys.stderr)
    return a


def main():
    global BASE, TOKEN
    a = parse_args(sys.argv[1:])

    BASE = (a.base_url or os.environ.get("BFC_BASE_URL") or "").rstrip("/")
    TOKEN = a.token or os.environ.get("BFC_TOKEN") or ""
    if not BASE or not TOKEN:
        print("需要 base-url 与 token：命令行 --base-url/--token，或环境变量 "
              "BFC_BASE_URL/BFC_TOKEN")
        return 2
    print("== 目标 %s ==" % BASE)

    # 1. 传输层
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=15) as resp:
            health = json.loads(resp.read().decode())
        check("/health 可达", health.get("status") == "ok", json.dumps(health, ensure_ascii=False))
    except Exception as exc:                                    # noqa: BLE001
        check("/health 可达", False, type(exc).__name__ + ": " + str(exc))
        return 1

    # 2. 鉴权必须真的生效（无 token 应被拒）
    #    rc9 起 MCP 走官方 SDK，凭据无效按规范回 HTTP 401（body 里仍带
    #    JSON-RPC error -32001）；rc8 及以前是 200 + error。两种都要认，
    #    但绝不能只看「有没有返回」——成功也会返回。
    try:
        st, r = rpc("tools/list", {}, None)
        err = r.get("error") or {}
        check("无 token 被拒绝", st == 200 and err.get("code") == -32001,
              "HTTP %d code=%s" % (st, err.get("code")))
    except urllib.error.HTTPError as exc:
        check("无 token 被拒绝", exc.code in (401, 403), "HTTP %d" % exc.code)
    except Exception as exc:                                    # noqa: BLE001
        check("无 token 被拒绝", False, str(exc))

    # 3. 协议握手
    init = {}
    info = {}
    for _ in range(2):
        try:
            st, init = rpc("initialize", {"protocolVersion": PROTO, "capabilities": {},
                                          "clientInfo": {"name": "bfc-remote-check", "version": "1"}},
                           TOKEN, 900)
            info = (init.get("result") or {}).get("serverInfo") or {}
            if info.get("name"):
                break
        except Exception as exc:                                # noqa: BLE001
            info = {"_err": str(exc)}
    check("MCP initialize", bool(info.get("name")),
          "serverInfo=%s" % json.dumps(info, ensure_ascii=False))

    # 4. 工具清单：只能有 5 个执行器工具，绝不能有审批类
    try:
        st, r = rpc("tools/list", {}, TOKEN)
        names = sorted(t["name"] for t in (r.get("result") or {}).get("tools", []))
        check("tools/list 返回 5 个工具", len(names) == 5, ",".join(names))
        banned = [n for n in names if "approve" in n or "admin" in n]
        check("没有审批/提权工具", not banned, ",".join(banned) or "无")
    except Exception as exc:                                    # noqa: BLE001
        check("tools/list 返回 5 个工具", False, str(exc))
        names = []

    # 5. 只读工具
    try:
        st, err, payload = tool("council_list_tasks", {}, 3)
        check("council_list_tasks 可用", st == 200 and not err, json.dumps(payload, ensure_ascii=False)[:160])
        tasks = payload.get("tasks") or []
    except Exception as exc:                                    # noqa: BLE001
        check("council_list_tasks 可用", False, str(exc))
        tasks = []

    # 6. 闭环只在显式指定任务时执行（不接受自动挑选，P2-8.1）
    task_id = a.task_id
    if task_id is None:
        skip("闭环（claim/heartbeat）未执行：本脚本默认只读，且绝不自动挑任务。")
        print("         需要闭环请显式指定专为联调创建的任务：--task-id task-xxxxxx")
        print("         已有可领取任务 %d 个（不自动选中任何一个）" % len(tasks))
        if a.allow_submit:
            skip("--allow-submit 已忽略：没有 --task-id 时不会提交任何结果。")
    else:
        try:
            st, err, cl = tool("council_claim_task", {"task_id": task_id}, 4)
            ok = st == 200 and not err and cl.get("ticket")
            check("council_claim_task 可用", bool(ok), "ticket_id=" + str(cl.get("ticket_id")))
            if ok:
                ticket = cl["ticket"]
                _, e1, hb = tool("council_heartbeat",
                                 {"ticket": ticket, "status": "running", "detail": "remote check"}, 5)
                check("council_heartbeat 可用", not e1,
                      "lease=%s" % hb.get("lease_expires_at"))
                # 同一 idempotency_key 重放，必须命中缓存（replayed=True）且不新增事件
                _, e4, hb2 = tool("council_heartbeat",
                                  {"ticket": ticket, "status": "running",
                                   "detail": "remote check", "idempotency_key": "rc-hb-1"}, 6)
                _, e5, hb3 = tool("council_heartbeat",
                                  {"ticket": ticket, "status": "running",
                                   "detail": "remote check", "idempotency_key": "rc-hb-1"}, 60)
                check("心跳幂等重放生效",
                      not e4 and not e5 and hb3.get("replayed") is True
                      and hb2.get("lease_expires_at") == hb3.get("lease_expires_at"),
                      "replayed=%s lease_equal=%s" % (
                          hb3.get("replayed"),
                          hb2.get("lease_expires_at") == hb3.get("lease_expires_at")))

                if not a.allow_submit:
                    skip("council_submit_result 未执行：默认不提交（--allow-submit 才提交，"
                         "且只提交 cancelled）。")
                    print("         任务 %s 仍处于运行中，票在 watchdog 超时后随任务转 BLOCKED 释放。"
                          % task_id)
                    print("         要收尾：" + CANCEL_PRECONDITION_HINT)
                else:
                    # 只提交 cancelled：cancelled 表示「联调中止」，不是业务完成。
                    # 服务端要求任务已处于 CANCELLING（人类已点取消），否则拒绝。
                    _, e2, sub = tool("council_submit_result",
                                      {"ticket": ticket, "status": "cancelled",
                                       "log": "remote check via tailscale",
                                       "tests": "mcp-path ok",
                                       "note": "跨机 MCP 联调中止（非业务完成）"}, 7)
                    if not e2:
                        check("council_submit_result 可用", True,
                              "run_id=%s status=%s" % (sub.get("run_id"), sub.get("task_status")))
                        check("回传状态是 cancelled（不是 completed）",
                              sub.get("task_status") == "CANCELLED",
                              "task_status=%s" % sub.get("task_status"))
                        _, e3, stt = tool("council_task_status", {"task_id": task_id}, 8)
                        check("council_task_status 可用",
                              not e3 and stt.get("status") == "CANCELLED",
                              json.dumps(stt, ensure_ascii=False)[:140])
                    else:
                        # 不是 FAIL：这是服务端的合法前置条件（任务未进入取消中），
                        # 脚本不能用一次联调去伪造任务被取消。
                        skip("council_submit_result 未执行成功：%s" % sub.get("error"))
                        print("         原因：" + CANCEL_PRECONDITION_HINT)
        except Exception as exc:                                # noqa: BLE001
            check("闭环（claim/heartbeat）", False, str(exc))

    print("\n通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    if task_id is None:
        scope = "只读检查（未 claim/heartbeat/submit）"
    elif not a.allow_submit:
        scope = "只读检查 + 显式任务的 claim/heartbeat（未提交结果，任务 %s）" % task_id
    else:
        scope = "只读检查 + 显式任务的 claim/heartbeat/submit(cancelled)（任务 %s）" % task_id
    print("本次范围：%s" % scope)
    print("本脚本走的是真实跨机链路（%s），不是本地回环模拟。" % BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
