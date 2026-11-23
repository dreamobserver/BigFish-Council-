"""命令行入口（无 GUI 也能跑完整闭环）.

用法示例：
  python -m bigfish.cli init
  python -m bigfish.cli task-create --title "演示" --prompt "..."
  python -m bigfish.cli review-start task-xxx
  python -m bigfish.cli review task-xxx --model gpt --verdict approve --kind vote
  python -m bigfish.cli approve task-xxx
  python -m bigfish.cli cancel task-xxx
  python -m bigfish.cli executor-add dsh-cloud --role executor
  python -m bigfish.cli serve --port 3096
  python -m bigfish.cli export backup.zip
  python -m bigfish.cli audit-export audit.zip
  python -m bigfish.cli restore backup.zip
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backup import (RestoreError, export_audit_bundle, export_bundle,
                     inspect_bundle)
from .config import Settings
from .core import Council, CouncilError
from .db import Database, DatabaseVersionError
from .runtime import open_council


def build(data_dir: str | None = None) -> tuple[Settings, Database, Council]:
    s = Settings.load(Path(data_dir).expanduser() if data_dir else None)
    db, council = open_council(s)
    return s, db, council


def dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _port_busy(host: str, port: int, timeout: float = 0.4) -> bool:
    """端口上是否已经有服务在监听（只探本机回环，不发数据）。"""
    import socket
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((target, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bigfish", description="BigFish Council CLI")
    p.add_argument("--data-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    re_en = sub.add_parser("reenable")
    re_en.add_argument("exec_id")

    sub.add_parser("init")
    sub.add_parser("overview")
    sub.add_parser("allowed-ports")

    tc = sub.add_parser("task-create")
    tc.add_argument("--title", required=True)
    tc.add_argument("--goal", default="")
    tc.add_argument("--scope", default="")
    tc.add_argument("--risk", default="low")
    tc.add_argument("--prompt", default="")

    tl = sub.add_parser("task-list")
    tl.add_argument("--status", default=None)

    ts = sub.add_parser("task-show")
    ts.add_argument("task_id")

    ex = sub.add_parser("executor-add")
    ex.add_argument("name")
    ex.add_argument("--role", default="executor",
                    choices=["executor", "reviewer", "admin", "mobile"])
    ex.add_argument("--model", default=None, choices=["gpt", "gemini"],
                    help="审查身份绑定的模型（role=reviewer 时使用）")

    sr = sub.add_parser("review-start")
    sr.add_argument("task_id")

    rv = sub.add_parser("review")
    rv.add_argument("task_id")
    rv.add_argument("--model", required=True, choices=["gpt", "gemini"])
    rv.add_argument("--content", default="")
    rv.add_argument("--verdict", default=None, choices=["approve", "reject", "comment"])
    rv.add_argument("--kind", default="vote", choices=["comment", "vote"])
    rv.add_argument("--version", type=int, default=None)

    ap = sub.add_parser("approve")
    ap.add_argument("task_id")
    ap.add_argument("--decision", default="approve", choices=["approve", "reject", "return"])
    ap.add_argument("--note", default="")
    ap.add_argument("--expected-version", type=int, default=None)

    cn = sub.add_parser("cancel")
    cn.add_argument("task_id")

    vf = sub.add_parser("verify")
    vf.add_argument("task_id")
    vf.add_argument("--accept", action="store_true")
    vf.add_argument("--note", default="")

    sub.add_parser("sweep")
    sub.add_parser("purge-idem")

    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--host", default=None)

    wd = sub.add_parser("watchdog")
    wd.add_argument("--interval", type=float, default=None)

    e = sub.add_parser("export")
    e.add_argument("path")
    ae = sub.add_parser("audit-export")
    ae.add_argument("path")
    ins = sub.add_parser("inspect")
    ins.add_argument("path")
    rs = sub.add_parser("restore")
    rs.add_argument("path")
    rs.add_argument("--force", action="store_true",
                    help="本机回环端口上还有服务在跑时，仍然强行恢复（危险）")

    a = p.parse_args(argv)
    if a.cmd == "restore":
        # 恢复前先做端口预检，再打开数据库；open_council 可能执行 schema / key 自愈。
        s = Settings.load(Path(a.data_dir).expanduser() if a.data_dir else None)
        if not a.force and _port_busy(s.api_host, s.api_port):
            print(f"拒绝恢复：{s.api_host}:{s.api_port} 上还有服务在监听，"
                  "请先停掉它（tools/stop-headless.ps1 或关闭 GUI），"
                  "确认没有其他实例后再重试；确实要强行恢复请加 --force。",
                  file=sys.stderr)
            return 2
        db, council = open_council(s)
    else:
        s, db, council = build(a.data_dir)

    try:
        return _dispatch(a, s, db, council)
    finally:
        _close_quietly(db)


def _close_quietly(db) -> None:
    """退出前收掉数据库（BFC-05）。

    以前只有 restore 分支关库，其他分支靠进程退出来释放句柄。这在 Windows 上
    会让紧接着调用本 CLI 的脚本（移动/删除/再次恢复）撞上文件占用；库没关还
    可能留下没 checkpoint 的 WAL。统一在 main() 的 finally 里关。
    """
    try:
        db.close()
    except Exception:                # pragma: no cover - 关闭失败不掩盖原异常
        pass


def _dispatch(a, s, db, council) -> int:
    """真正的命令分发。所有分支共用 main() 的关库逻辑。"""
    if a.cmd == "init":
        print(f"数据目录: {s.data_dir}\n数据库: {s.db_path}\n密钥: {s.key_path}")
        return 0
    if a.cmd == "allowed-ports":
        from .config import PORT_RESERVED, PORT_ROLES
        dump({
            "implemented": {k: {"port": v[0], "role": v[1]}
                            for k, v in PORT_ROLES.items()},
            # BFC-06：预留端口也要打出来，否则看 allowed-ports 的人会以为
            # 3099-3101 是空着的，随手拿去用。
            "reserved": {str(p): {"name": v[0], "note": v[1]}
                         for p, v in sorted(PORT_RESERVED.items())},
        })
        return 0
    if a.cmd == "serve":
        from .server import BindError, serve
        try:
            # 校验在 serve() 内部统一执行，这里只是把参数传进去
            serve(council, a.host or s.api_host, a.port or s.api_port,
                  watchdog_interval=s.watchdog_interval_sec)
        except BindError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "overview":
        dump(council.overview())
        return 0
    if a.cmd == "task-create":
        dump(council.create_task(a.title, a.goal, a.scope, a.risk, a.prompt))
        return 0
    if a.cmd == "task-list":
        dump([{k: t[k] for k in
               ("id", "title", "status", "status_label", "risk", "current_version")}
              for t in council.list_tasks(a.status)])
        return 0
    if a.cmd == "task-show":
        dump(council.get_task(a.task_id))
        return 0
    if a.cmd == "executor-add":
        out = council.register_executor(a.name, a.role, a.model)
        print("执行器已创建，Token 只显示这一次：")
        dump(out)
        return 0
    if a.cmd == "review-start":
        dump(council.submit_for_review(a.task_id))
        return 0
    if a.cmd == "review":
        dump(council.add_review(a.task_id, a.model, a.content, a.verdict,
                                version=a.version, kind=a.kind))
        return 0
    if a.cmd == "approve":
        dump(council.approve(a.task_id, a.decision, a.note,
                             expected_version=a.expected_version))
        return 0
    if a.cmd == "cancel":
        dump(council.request_cancel(a.task_id))
        return 0
    if a.cmd == "verify":
        dump(council.verify(a.task_id, a.accept, a.note))
        return 0
    if a.cmd == "sweep":
        dump(council.sweep_timeouts())
        return 0
    if a.cmd == "purge-idem":
        print(f"已清理过期幂等记录 {council.purge_idempotency()} 条")
        return 0
    if a.cmd == "watchdog":
        import time
        interval = a.interval or s.watchdog_interval_sec
        council.start_watchdog(interval)
        print(f"看门狗已启动，每 {interval}s 扫描一次；Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            council.stop_watchdog()
        return 0
    if a.cmd == "export":
        print(export_bundle(db, a.path))
        return 0
    if a.cmd == "audit-export":
        print(export_audit_bundle(db, a.path))
        return 0
    if a.cmd == "inspect":
        dump(inspect_bundle(a.path))
        return 0
    if a.cmd == "restore":
        dump(council.restore_from_bundle(a.path))
        return 0
    if a.cmd == "reenable":
        dump(council.reenable_executor(a.exec_id))
        return 0
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CouncilError, RestoreError, DatabaseVersionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(2)
