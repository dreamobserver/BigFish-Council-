"""回环端到端联调：Windows 侧建任务 -> 双模型审查 -> 人类批准 ->
通过 127.0.0.1:<proxy>/mcp 领取 -> 心跳 -> 回传，全流程走 MCP 协议。

⚠️ 重要：这跑在**同一台机器**上（API 与跳板都在回环），
只能证明「协议链路 + 权限边界 + 状态机」正确，**不能**当作跨设备联调成功。
跨设备结果看 tools/net_check.py 的第 4 节，或人工在 Windows 上执行。

用法：
  python tools/e2e_loopback.py --url http://127.0.0.1:3098 --data-dir /path/to/data
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bigfish.config import Settings          # noqa: E402
from bigfish.runtime import open_council     # noqa: E402

OK, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> bool:
    (OK if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    return cond


def rpc(url: str, token: str, method: str, params: dict | None = None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode(errors="replace") or "{}")


def call_tool(url: str, token: str, name: str, args: dict) -> dict:
    st, out = rpc(url, token, "tools/call", {"name": name, "arguments": args})
    payload = out["result"]["content"][0]["text"]
    data = json.loads(payload)
    data["_isError"] = out["result"]["isError"]
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:3098/mcp",
                    help="走跳板的 MCP 地址（默认验证跳板链路）")
    ap.add_argument("--data-dir", required=True, help="正在运行的那个实例的数据目录")
    a = ap.parse_args()

    s = Settings.load(Path(a.data_dir))
    db, council = open_council(s)       # 同一份库，跨进程读写靠 WAL

    print("== 0. 前置 ==")
    with urllib.request.urlopen(a.url.replace("/mcp", "/health"), timeout=10) as r:
        health = json.loads(r.read().decode())
    check("跳板 /health 在线", health.get("status") == "ok", health.get("version", ""))
    check("跳板后面确实是本版本", health.get("version") == __import__("bigfish").__version__,
          health.get("version", ""))

    print("== 1. 人类侧建任务并送审 ==")
    t = council.create_task("E2E 回环联调", "验证 MCP 全链路", "本机回环", "low",
                            "echo e2e-loopback")
    council.submit_for_review(t["id"])
    check("任务已送审", council.get_task(t["id"])["status"] == "IN_REVIEW")

    print("== 2. 双模型审查 ==")
    council.add_review(t["id"], "gpt", "结构没问题", "approve", kind="vote")
    mid = council.get_task(t["id"])
    check("单模型 approve 不推进", mid["status"] == "IN_REVIEW", mid["status"])
    council.add_review(t["id"], "gemini", "无安全疑点", "approve", kind="vote")
    mid = council.get_task(t["id"])
    check("双 approve 进入待审批", mid["status"] == "PENDING_APPROVAL", mid["status"])

    print("== 3. 人类审批 ==")
    council.approve(t["id"], "approve", "E2E 人工批准", expected_version=mid["current_version"])
    check("已批准", council.get_task(t["id"])["status"] == "APPROVED")

    print("== 4. 经跳板走 MCP 领取 ==")
    ex = council.register_executor("e2e-loopback", "executor")
    st, init = rpc(a.url, ex["token"], "initialize")
    check("initialize 成功", st == 200 and "result" in init)
    _, tl = rpc(a.url, ex["token"], "tools/list")
    names = [x["name"] for x in tl["result"]["tools"]]
    check("暴露面只有 5 个执行器工具", len(names) == 5, ",".join(names))
    check("没有审批/管理工具",
          not any(k in n for n in names for k in ("approve", "admin", "revoke")))

    listed = call_tool(a.url, ex["token"], "council_list_tasks", {})
    check("能列出可领取任务", listed["ok"] and any(x["id"] == t["id"] for x in listed["tasks"]))
    check("列表不含 Prompt", "prompt" not in json.dumps(listed["tasks"]))

    claimed = call_tool(a.url, ex["token"], "council_claim_task", {"task_id": t["id"]})
    check("领取成功并拿到 Prompt", claimed["ok"] and claimed.get("prompt") == "echo e2e-loopback")
    check("领取后状态是已派工", claimed["_isError"] is False)
    again = call_tool(a.url, ex["token"], "council_claim_task", {"task_id": t["id"]})
    check("重复领取被拒且 isError=true", again["_isError"] is True)

    print("== 5. 心跳 / 回传 ==")
    hb = call_tool(a.url, ex["token"], "council_heartbeat",
                   {"ticket": claimed["ticket"], "status": "running", "detail": "E2E 心跳"})
    check("心跳成功且转 RUNNING", hb["_isError"] is False and hb["task_status"] == "RUNNING")
    check("心跳返回续租时间", bool(hb.get("lease_expires_at")))

    sub = call_tool(a.url, ex["token"], "council_submit_result",
                    {"ticket": claimed["ticket"], "status": "completed",
                     "log": "e2e log", "diff": "", "tests": "ok", "note": "E2E 回传"})
    check("回传只到待验收", sub["task_status"] == "SUBMITTED", sub["task_status"])
    check("回传不会自动 DONE", council.get_task(t["id"])["status"] == "SUBMITTED")

    print("== 6. 越权与错误语义 ==")
    st2, raw = rpc(a.url, "bfc_ex_bogus", "tools/list")
    check("坏 Token 被拒（-32001）", raw.get("error", {}).get("code") == -32001)
    st3, raw3 = rpc(a.url, ex["token"], "tools/call",
                    {"name": "council_approve_task", "arguments": {}})
    check("审批工具不存在（-32601）", raw3.get("error", {}).get("code") == -32601)
    noauth = urllib.request.Request(a.url, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                                    method="POST")
    noauth.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(noauth, timeout=10) as r:
            payload = json.loads(r.read().decode())
        check("缺 Token 被拒", payload.get("error", {}).get("code") == -32001)
    except urllib.error.HTTPError as e:
        check("缺 Token 被拒", e.code == 401, str(e.code))

    print("== 7. 人类验收 ==")
    done = council.verify(t["id"], True)
    check("人类验收后 DONE", done["status"] == "DONE", done["status"])

    db.close()
    print()
    print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print()
    print("注意：以上全部在**同一台机器**的回环上完成。")
    print("这证明协议链路、权限边界与状态机正确；跨设备联调需另按 NETWORK-TOPOLOGY.md 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
