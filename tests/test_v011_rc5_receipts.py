"""v0.1.1-rc5 新增回归：执行端「执行收据」（R1 决定二的恢复对账基础）.

这一批对应一个具体缺陷：服务端数据库从旧备份恢复后，它不知道某次派工到底有没有
真正开工。执行端本地收据是唯一能证明「开工过 / 收尾过」的证据，因此：

  A. ReceiptLog 基本契约：写三条读三条、事件名白名单、坏行容错、目录自建
  B. state_of 判定表：未开工 / 结果未知 / 已完成 / 未知（四格全覆盖）
  C. 落盘语义：写完立刻 fsync，另起一次读取（不走对象内存）就能看到该行
  D. run_once 端到端：假 HTTP 服务返回含 dispatch_id 的 claim，
     验证按顺序写出 claimed→started→finished，且 dispatch_id 与响应一致
  E. 客户端装配：receipts 为 None / False 表示「不要收据」，不偷偷建文件

全程不连真实服务、不用真实 token：D 段起的是本地 ThreadingHTTPServer。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish.executor_client import (ExecutorClient, ReceiptLog,
                                     _UncertainNetworkError)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "receipts" / "exec.jsonl"

    def tearDown(self):
        self.tmp.cleanup()


# ======================================================================
# A. ReceiptLog 基本契约
# ======================================================================
class TestReceiptLog(Base):
    def test_record_and_read_all(self):
        log = ReceiptLog(self.path)
        log.record("dsp-aaaa", "claimed", task_id="t1", ticket_id="tk1")
        log.record("dsp-aaaa", "started", task_id="t1", ticket_id="tk1")
        log.record("dsp-aaaa", "finished", task_id="t1", ticket_id="tk1", note="done")
        rows = log.read_all()
        self.assertEqual(len(rows), 3, "写三条读回来的不是三条")
        self.assertEqual([r["event"] for r in rows],
                         ["claimed", "started", "finished"], "收据顺序被打乱")
        for key in ("ts", "dispatch_id", "event", "task_id", "ticket_id", "note"):
            self.assertIn(key, rows[0], f"收据缺字段 {key}")
        self.assertEqual(rows[0]["dispatch_id"], "dsp-aaaa")

    def test_creates_missing_directory(self):
        nested = self.root / "a" / "b" / "c" / "r.jsonl"
        self.assertFalse(nested.parent.exists())
        ReceiptLog(nested).record("dsp-dir", "claimed")
        self.assertTrue(nested.exists(), "目录不存在时没有自动创建")

    def test_invalid_event_raises_value_error(self):
        log = ReceiptLog(self.path)
        with self.assertRaises(ValueError):
            log.record("dsp-aaaa", "almost-done")
        self.assertEqual(log.read_all(), [], "非法事件名不该留下任何收据")

    def test_bad_line_is_skipped_not_fatal(self):
        log = ReceiptLog(self.path)
        log.record("dsp-aaaa", "claimed")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{oops\n")            # 模拟断电写了一半的行
        log.record("dsp-aaaa", "started")
        rows = log.read_all()             # 不能抛
        self.assertEqual([r["event"] for r in rows], ["claimed", "started"],
                         "坏行容错把好行也一起丢了")

    def test_none_path_is_memory_only(self):
        log = ReceiptLog(None)
        log.record("dsp-mem", "claimed")
        self.assertEqual(len(log.read_all()), 1, "内存模式读不回自己写的收据")
        self.assertIsNone(log.path)

    def test_env_var_as_default_path(self):
        import os
        old = os.environ.get("BFC_RECEIPTS")
        os.environ["BFC_RECEIPTS"] = str(self.path)
        try:
            ReceiptLog(None)
            log = ReceiptLog()
            log.record("dsp-env", "claimed")
            self.assertEqual(ReceiptLog(self.path).read_all()[0]["dispatch_id"],
                             "dsp-env")
        finally:
            if old is None:
                os.environ.pop("BFC_RECEIPTS", None)
            else:
                os.environ["BFC_RECEIPTS"] = old


# ======================================================================
# B. state_of 判定表
# ======================================================================
class TestStateOf(Base):
    def test_claimed_only_means_not_started(self):
        log = ReceiptLog(self.path)
        log.record("dsp-a", "claimed", task_id="t1")
        st = log.state_of("dsp-a")
        self.assertEqual(st["verdict"], "未开工")
        self.assertEqual(st["last_event"], "claimed")
        self.assertIsNone(st["started_at"])
        self.assertIsNone(st["finished_at"])

    def test_started_without_finish_means_unknown(self):
        log = ReceiptLog(self.path)
        log.record("dsp-b", "claimed")
        log.record("dsp-b", "started")
        st = log.state_of("dsp-b")
        self.assertEqual(st["verdict"], "结果未知", "开工没收尾必须交人判断")
        self.assertIsNotNone(st["started_at"])
        self.assertIsNone(st["finished_at"])

    def test_finished_means_done(self):
        log = ReceiptLog(self.path)
        log.record("dsp-c", "claimed")
        log.record("dsp-c", "started")
        log.record("dsp-c", "finished")
        st = log.state_of("dsp-c")
        self.assertEqual(st["verdict"], "已完成")
        self.assertIsNotNone(st["started_at"])
        self.assertIsNotNone(st["finished_at"])

    def test_failed_and_blocked_and_cancelled_are_closed(self):
        log = ReceiptLog(self.path)
        for i, ev in enumerate(("failed", "blocked", "cancelled")):
            did = f"dsp-x{i}"
            log.record(did, "claimed")
            log.record(did, "started")
            log.record(did, ev)
            self.assertEqual(log.state_of(did)["verdict"], "已完成",
                             f"{ev} 应当算「已完成」（执行端已明确交代）")

    def test_unknown_dispatch_id(self):
        log = ReceiptLog(self.path)
        log.record("dsp-a", "claimed")
        st = log.state_of("dsp-nope")
        self.assertEqual(st["verdict"], "未知")
        self.assertIsNone(st["last_event"])

    def test_summary_counts(self):
        log = ReceiptLog(self.path)
        log.record("dsp-done", "claimed")
        log.record("dsp-done", "started")
        log.record("dsp-done", "finished")
        log.record("dsp-un", "claimed")
        log.record("dsp-un", "started")
        log.record("dsp-idle", "claimed")
        s = log.summary()
        self.assertEqual(s, {"total": 3, "完成": 1, "结果未知": 1,
                             "未开工": 1, "未知": 0})


# ======================================================================
# C. 落盘语义
# ======================================================================
class TestDurability(Base):
    def test_record_is_flushed_to_disk_immediately(self):
        log = ReceiptLog(self.path)
        log.record("dsp-fsync", "started", task_id="t9")
        # 不碰对象缓存，直接读文件：断电前收据必须已经在盘上。
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("dsp-fsync", text, "写完没有立刻落盘")
        row = json.loads(text.strip().splitlines()[-1])
        self.assertEqual(row["event"], "started")

    def test_reader_in_new_process_sees_rows(self):
        import subprocess
        log = ReceiptLog(self.path)
        log.record("dsp-fsync2", "finished")
        code = (
            "import json,sys;"
            f"print(json.loads(open(r'{self.path}',encoding='utf-8')"
            ".read().strip().splitlines()[-1])['dispatch_id'])"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "dsp-fsync2",
                         "另起进程读不到刚写的收据")


# ======================================================================
# D. run_once 端到端（本地假服务）
# ======================================================================
class _FakeServer:
    """最小可用的假服务端：claim / heartbeat / result 三个接口。

    claim 返回体带 dispatch_id，用来验证收据用的是服务端下发的稳定派工号。
    """

    DISPATCH_ID = "dsp-abcdef123456"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):      # 静音，别把测试输出刷满
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                outer.calls.append((self.path, body))
                if self.path == "/api/v1/tasks/claim":
                    payload = {"task_id": body.get("task_id"),
                               "version": 3,
                               "ticket": "bfc_tk_fake",
                               "ticket_id": "ticket-fake-1",
                               "dispatch_id": outer.DISPATCH_ID}
                elif self.path == "/api/v1/runs/heartbeat":
                    payload = {"ok": True, "cancel": False}
                else:
                    payload = {"ok": True, "status": "SUBMITTED"}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(10)


class TestRunOnceReceipts(Base):
    def setUp(self):
        super().setUp()
        self.srv = _FakeServer()

    def tearDown(self):
        self.srv.stop()
        super().tearDown()

    def client(self, receipts=None) -> ExecutorClient:
        return ExecutorClient(base_url=self.srv.base_url, token="bfc_ex_test",
                              timeout=5, receipts=receipts)

    def test_run_once_writes_claimed_started_finished_in_order(self):
        log = ReceiptLog(self.path)
        out = self.client(receipts=log).run_once("task-e2e")
        self.assertEqual(out["claimed"]["ticket_id"], "ticket-fake-1")
        rows = ReceiptLog(self.path).read_all()
        self.assertEqual([r["event"] for r in rows],
                         ["claimed", "started", "finished"],
                         f"收据顺序不对：{[r['event'] for r in rows]}")
        self.assertEqual({r["dispatch_id"] for r in rows}, {_FakeServer.DISPATCH_ID},
                         "收据没用服务端下发的 dispatch_id")
        for r in rows:
            self.assertEqual(r["task_id"], "task-e2e")
            self.assertEqual(r["ticket_id"], "ticket-fake-1")
        self.assertEqual(log.state_of(_FakeServer.DISPATCH_ID)["verdict"], "已完成")

    def test_run_once_records_uncertain_and_reraises(self):
        log = ReceiptLog(self.path)
        cli = self.client(receipts=log)

        def boom(*a, **k):
            raise _UncertainNetworkError("模拟结果丢失")

        cli.submit = boom
        with self.assertRaises(_UncertainNetworkError):
            cli.run_once("task-lost")
        events = [r["event"] for r in ReceiptLog(self.path).read_all()]
        self.assertEqual(events, ["claimed", "started", "uncertain"],
                         "submit 结果未知时没有先落 uncertain")
        self.assertEqual(log.state_of(_FakeServer.DISPATCH_ID)["verdict"], "结果未知")

    def test_reconcile_lists_unknown_inflight(self):
        log = ReceiptLog(self.path)
        log.record("dsp-inflight", "claimed")
        log.record("dsp-inflight", "started")
        log.record("dsp-ok", "claimed")
        log.record("dsp-ok", "started")
        log.record("dsp-ok", "finished")
        cli = self.client(receipts=log)
        out = cli.reconcile()
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["结果未知"], 1)
        self.assertEqual(out["unknown_inflight"], ["dsp-inflight"],
                         "对账没把「开工没收尾」的派工号交出来")

    def test_reconcile_without_any_receipt(self):
        out = self.client(receipts=ReceiptLog(None)).reconcile()
        self.assertEqual(out, {"total": 0, "unknown_inflight": []})

    def test_dispatch_id_falls_back_to_ticket_id(self):
        """旧服务端没有 dispatch_id 时必须降级，而不是把空串写进收据。"""
        log = ReceiptLog(self.path)
        cli = self.client(receipts=log)
        cli.claim = lambda task_id, idem=None: {
            "task_id": task_id, "version": 1,
            "ticket": "bfc_tk_old", "ticket_id": "ticket-old-1"}
        cli.run_once("task-old")
        self.assertEqual(ReceiptLog(self.path).read_all()[0]["dispatch_id"],
                         "ticket-old-1", "没有 dispatch_id 时没有降级到 ticket_id")


# ======================================================================
# E. 客户端装配
# ======================================================================
class TestClientWiring(Base):
    def test_receipts_none_means_disabled(self):
        cli = ExecutorClient(base_url="http://127.0.0.1:1", token="x",
                             timeout=1, receipts=None)
        self.assertIsInstance(cli.receipts, ReceiptLog)
        self.assertIsNone(cli.receipts.path, "显式 None 不该偷偷落盘")

    def test_receipts_false_means_disabled(self):
        cli = ExecutorClient(base_url="http://127.0.0.1:1", token="x",
                             timeout=1, receipts=False)
        self.assertIsNone(cli.receipts.path)

    def test_receipts_accepts_path_string(self):
        cli = ExecutorClient(base_url="http://127.0.0.1:1", token="x",
                             timeout=1, receipts=str(self.path))
        cli.receipts.record("dsp-p", "claimed")
        self.assertEqual(cli.receipts.read_all()[0]["dispatch_id"], "dsp-p")

    def test_receipts_accepts_receiptlog_instance(self):
        log = ReceiptLog(self.path)
        cli = ExecutorClient(base_url="http://127.0.0.1:1", token="x",
                             timeout=1, receipts=log)
        self.assertIs(cli.receipts, log)

    def test_default_reads_env_and_stays_memory_when_unset(self):
        import os
        old = os.environ.pop("BFC_RECEIPTS", None)
        try:
            cli = ExecutorClient(base_url="http://127.0.0.1:1", token="x", timeout=1)
            self.assertIsNone(cli.receipts.path, "没配环境变量却落了盘")
        finally:
            if old is not None:
                os.environ["BFC_RECEIPTS"] = old


if __name__ == "__main__":
    unittest.main()
