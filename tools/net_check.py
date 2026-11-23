"""网络拓扑检查：只做只读探测，任何一步失败都如实报告，不做假设.

检查项：
1. 配置层：API/PROXY/测试端口是否互不冲突、API 是否只绑回环
2. 本机层：本地 API 是否真的在 127.0.0.1:<port> 上监听（TCP + /health）
3. 跳板层：VPS 上 bfc-proxy.mjs 是否在 127.0.0.1:<proxy> 上转发（/health 透传版本号）
4. 跨机层：VPS -> Windows Tailnet IP:3096 是否可达（这一步经常是失败的，
   失败时明确写「未打通」，不允许用跳板本机回环的通关代替跨机联调）

判定口径（rc4）：第 2/3 节查的是**本机**有没有起服务，不是被测链路的质量。
本机没起服务时记 SKIP 并给出启动提示，不是 FAIL —— 否则在 VPS 上单独跑本脚本
必然报红，人会把「我没起服务」误读成「网络不通」，进而掩盖真正该看的第 4 节。
真正会判失败的是：配置层冲突、端口被占却不是本服务、本机服务在跑但 /health 坏了。

用法：
  python tools/net_check.py                     # 全量
  python tools/net_check.py --skip-cross         # 只查本机与跳板
  BFC_TARGET_HOST=100.64.0.12 BFC_TARGET_PORT=3096 python tools/net_check.py

MCP 全链路（claim/heartbeat）另跑 tools/remote_check.py：默认只读，
闭环需显式 --task-id，回传结果需再加 --allow-submit。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bigfish.config import (DEFAULT_API_HOST, DEFAULT_API_PORT,  # noqa: E402
                            DEFAULT_PROXY_PORT, LOOPBACK_HOSTS, PORT_ROLES)

OK, FAIL, SKIP = [], [], []


def record(name: str, cond: bool, extra: str = "") -> bool:
    bucket = OK if cond else FAIL
    bucket.append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    return cond


def record_skip(name: str, extra: str = "") -> None:
    """本机没起服务这类「本次没测到」的情况：记 SKIP，不记 FAIL。"""
    SKIP.append(name)
    print("  [skip] " + name + (("  " + extra) if extra else ""))


def tcp_open(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True, "open"
    except Exception as exc:
        return False, type(exc).__name__
    finally:
        s.close()


def http_json(url: str, timeout: float = 5.0) -> tuple[bool, dict, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return True, json.loads(raw), raw
    except urllib.error.HTTPError as exc:
        return False, {}, "HTTP %s" % exc.code
    except Exception as exc:
        return False, {}, type(exc).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("BIGFISH_API_PORT",
                                                                 DEFAULT_API_PORT)))
    ap.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    ap.add_argument("--skip-cross", action="store_true")
    ap.add_argument("--target-host", default=os.environ.get("BFC_TARGET_HOST", ""))
    ap.add_argument("--target-port", type=int,
                    default=int(os.environ.get("BFC_TARGET_PORT", DEFAULT_API_PORT)))
    a = ap.parse_args()

    print("== 1. 配置层 ==")
    record("API 默认只绑回环", DEFAULT_API_HOST in LOOPBACK_HOSTS, DEFAULT_API_HOST)
    ports = [v[0] for v in PORT_ROLES.values()]
    record("三个端口互不冲突", len(set(ports)) == len(ports), str(ports))
    record("API 端口 != 跳板端口", a.port != a.proxy_port, "%d vs %d" % (a.port, a.proxy_port))

    print("== 2. 本机 API ==")
    open_, why = tcp_open("127.0.0.1", a.port)
    if not open_:
        # 没起服务 ≠ 网络故障。记 SKIP，并说清怎么起、怎么区分。
        record_skip("本机 API 未监听 127.0.0.1:%d" % a.port,
                    "（%s；不是 FAIL：本机没起服务时链路无从谈起）" % why)
        print("         启动： .venv/bin/python -m bigfish --headless --port %d" % a.port)
        print("         若你确认服务应当已在跑，再看这一项：端口被别的进程占了就是问题，")
        print("         可用 ss -ltnp | grep :%d 核对宿主进程。" % a.port)
    else:
        record("127.0.0.1:%d 可连接" % a.port, True, why)
        got, body, raw = http_json("http://127.0.0.1:%d/health" % a.port)
        # 端口通了但 /health 不像本服务 -> 这才是真 FAIL（端口被占/服务崩溃）
        record("/health 可解析", got, raw[:120] if got else body)
        if got:
            record("健康检查里没有任务信息",
                   not ({"tasks", "prompt", "ticket"} & set(body.keys())),
                   ",".join(sorted(body.keys())))

    print("== 3. VPS 跳板 ==")
    popen, pwhy = tcp_open("127.0.0.1", a.proxy_port)
    if not popen:
        record_skip("跳板未监听 127.0.0.1:%d" % a.proxy_port,
                    "（%s；不是 FAIL：跳板只是本机转发进程，没起就是没起）" % pwhy)
        print("         启动： node tools/bfc-proxy.mjs（需要 BFC_TARGET_HOST/PORT）")
    else:
        record("127.0.0.1:%d 可连接" % a.proxy_port, True, pwhy)
        got, body, raw = http_json("http://127.0.0.1:%d/health" % a.proxy_port)
        record("跳板 /health 透传成功", got, raw[:160] if got else body)
        if got:
            the_ok = body.get("service") == "bigfish-council"
            record("跳板转发到的是 bigfish-council", the_ok, str(body.get("service")))
            record("跳板报告的版本与源码一致",
                   body.get("version") == __import__("bigfish").__version__,
                   "%s" % body.get("version"))

    print("== 4. 跨机（VPS -> Windows Tailnet） ==")
    if a.skip_cross:
        record_skip("跨机探测", "（--skip-cross 指定跳过；这一步不能算通过）")
    elif not a.target_host:
        record_skip("跨机探测", "（未提供 --target-host；这一步不能算通过）")
    else:
        cok, cwhy = tcp_open(a.target_host, a.target_port, timeout=5.0)
        record("%s:%d 可连接" % (a.target_host, a.target_port), cok, cwhy)
        if cok:
            got, body, raw = http_json(
                "http://%s:%d/health" % (a.target_host, a.target_port))
            record("跨机 /health 可解析", got, raw[:160] if got else body)
            if got:
                record_skip("MCP 全链路", "（/health 通不代表 MCP 通，本脚本不覆盖）")
                print("  [info] 跨机 /health 通了，但 MCP 工具调用需另跑 "
                      "tools/remote_check.py（默认只读；闭环需显式 --task-id）")
        else:
            print("  [info] 跨机不通：Windows 侧 API 未启动，或 Tailscale 断开，或防火墙拦截")

    print()
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(OK), len(FAIL), len(SKIP)))
    if SKIP:
        print("跳过项（未测到，不等于通过）：" + "，".join(SKIP))
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        print("注意：本脚本不会因为跳板本机回环通就宣布跨机联调成功。")
        return 1
    print("网络检查完成。跨机结论以第 4 节为准，未打通就是未打通。")
    if SKIP:
        print("本次有 %d 项未测到（见上面的跳过项），别把它们当成通过。" % len(SKIP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
