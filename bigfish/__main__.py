"""统一入口： python -m bigfish [--headless] [--data-dir DIR] [--port N]

监听地址校验不在本文件里做——它下沉到 server.serve()，这样 GUI 内嵌线程、
CLI serve、本入口三个路径共用同一道闸，换入口也绕不过去（P1-3.1）。
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bigfish", description="大肥鱼议事厅")
    p.add_argument("--headless", action="store_true", help="只启动本地 API，不启动 GUI")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--no-watchdog", action="store_true", help="不启动后台看门狗（仅调试用）")
    a = p.parse_args(argv)

    from .config import Settings
    from .db import DatabaseVersionError
    from .runtime import open_council
    from .server import BindError, check_bind_host

    settings = Settings.load(a.data_dir)
    if a.port:
        settings.api_port = a.port
    if a.host:
        settings.api_host = a.host

    # 先校验再开库：拒绝的入口不应该有任何副作用
    try:
        host = check_bind_host(settings.api_host)
    except BindError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    try:
        db, council = open_council(settings)
    except DatabaseVersionError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 3

    # 整个进程只开一个数据库连接（A.1）。GUI 分支把同一对 db/council 传下去，
    # 绝不允许 gui.run 再 open_council 一次——Windows 上两个连接会让恢复撞文件锁。
    try:
        if a.headless:
            from .server import serve
            serve(council, host, settings.api_port,
                  watchdog_interval=None if a.no_watchdog else settings.watchdog_interval_sec)
            return 0

        from . import gui
        return gui.run(a.data_dir, settings.api_port, settings.api_host,
                       db=db, council=council)
    finally:
        try:
            council.stop_watchdog()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
