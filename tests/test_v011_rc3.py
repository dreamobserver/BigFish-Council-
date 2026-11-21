"""v0.1.1-rc3 新增回归：状态机 / 并发 / 数据库恢复 / 安全边界.

本文件对应任务书 §9「必须新增」的 26 项，四组：
  A. 状态机 7 项（取消语义、撤票重算、先票后审）
  B. 并发 7 项（真实线程 + Barrier，不用 sleep 代替同步）
  C. 恢复 7 项（旧票失效、幂等清理、在途阻断、失败保原库、跨版本）
  D. 安全 5 项（监听闸门、越权读取、凭据不入库、审计包、文件权限）

并发测试一律用 threading.Barrier 对齐起跑点；断言的是**数据库最终状态与副作用**，
不是「函数被调用过」。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from bigfish import state as S
from bigfish.backup import (AUDIT_DENY_COLUMNS, AUDIT_TABLES, RestoreError,
                            export_audit_bundle, export_bundle, inspect_bundle)
from bigfish.core import Council, CouncilError
from bigfish.db import Database, REQUIRED_TABLES, SCHEMA_VERSION, file_sha256
from bigfish.security import load_or_create_key, verify_file_restricted
from bigfish.server import check_bind_host, create_app, BindError


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_is_bindable(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Base(unittest.TestCase):
    heartbeat_ttl = 60
    ticket_ttl = 120

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.c = Council(self.db, key=load_or_create_key(self.key_path),
                         heartbeat_ttl=self.heartbeat_ttl, ticket_ttl=self.ticket_ttl,
                         ticket_max_ttl=3600, heartbeat_lease=180,
                         key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    # ---- 夹具 ----
    def approved(self, title="演示任务") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", "prompt v1")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid

    def pending(self, title="待审批") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", "prompt v1")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        return tid

    def vote_id(self, tid, model) -> int:
        rows = [r for r in self.c.reviews(tid, self.c.get_task(tid)["current_version"])
                if r["model"] == model and r["verdict"] == "approve" and not r["revoked"]]
        self.assertTrue(rows, f"{model} 没有可撤销的票")
        return rows[0]["id"]

    def status(self, tid) -> str:
        return self.c.get_task(tid)["status"]


# ======================================================================
# A. 状态机 7 项
# ======================================================================
class TestStateMachine(Base):
    """P1-2：取消与审批的状态迁移必须有唯一合法结果。"""

    def test_cancel_after_approved_cannot_claim(self):
        tid = self.approved()
        self.c.request_cancel(tid)
        self.assertEqual(self.status(tid), S.CANCELLED)
        self.assertEqual(self.c.available_tasks(), [])
        with self.assertRaises(CouncilError) as cm:
            self.c.claim(tid, self.ex)
        self.assertIn("取消", str(cm.exception))
        # 没有任何票被签出去
        n = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 0, "取消后的任务不该留下执行票")

    def test_cancel_before_approval_cannot_approve(self):
        tid = self.pending()
        self.assertEqual(self.status(tid), S.PENDING_APPROVAL)
        self.c.request_cancel(tid)
        self.assertEqual(self.status(tid), S.CANCELLED)
        with self.assertRaises(CouncilError):
            self.c.approve(tid, "approve")
        approvals = self.db.one(
            "SELECT COUNT(*) AS n FROM approvals WHERE task_id=?", (tid,))["n"]
        self.assertEqual(approvals, 0, "取消后不得留下审批记录")

    def test_cancel_and_claim_concurrent(self):
        """取消与领取并发：只能有一个合法结果，且都不许「取消后又签出票」。"""
        tid = self.approved()
        barrier = threading.Barrier(2)
        out = {}

        def do_cancel():
            barrier.wait()
            try:
                out["cancel"] = self.c.request_cancel(tid)
            except Exception as exc:            # pragma: no cover - 不应发生
                out["cancel_err"] = exc

        def do_claim():
            barrier.wait()
            try:
                out["claim"] = self.c.claim(tid, self.ex)
            except Exception as exc:
                out["claim_err"] = exc

        ts = [threading.Thread(target=do_cancel), threading.Thread(target=do_claim)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        self.assertIn("cancel", out, f"取消失败：{out.get('cancel_err')}")
        st = self.status(tid)
        self.assertIn(st, (S.CANCELLED, S.CANCELLING),
                      f"并发后状态不合法：{st}")
        if "claim" in out:
            # 领取先赢：票已发出，取消只能进 CANCELLING 等执行器确认
            self.assertEqual(st, S.CANCELLING)
            self.assertTrue(self.c.get_task(tid)["control"].get("cancel_requested"))
            # 再领一次必须失败，且不能重复签票
            with self.assertRaises(CouncilError):
                self.c.claim(tid, self.ex)
        else:
            self.assertEqual(st, S.CANCELLED)
            self.assertIn("取消", str(out.get("claim_err")))
            n = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE task_id=?", (tid,))["n"]
            self.assertEqual(n, 0)

    def test_revoke_vote_reopens_approval(self):
        tid = self.pending()
        self.c.approve(tid, "approve")
        self.assertEqual(self.status(tid), S.APPROVED)
        self.c.revoke_review(self.vote_id(tid, "gpt"))
        self.assertEqual(self.status(tid), S.IN_REVIEW,
                         "撤掉必要通过票后不能停留在已批准")
        # 历史审批证据保留（不删），但必须被标记为不再生效
        ap = self.c.latest_approval(tid)
        self.assertIsNotNone(ap, "审批历史被删掉了，证据链断裂")
        self.assertFalse(ap["effective"], "撤票后审批仍被当成有效授权")
        # 撤票记录本身也要留痕
        self.assertTrue(any(r["revoked"] for r in self.c.reviews(tid)))
        # 失效审批不能派工
        with self.assertRaises(CouncilError):
            self.c.claim(tid, self.ex)

    def test_votes_before_submit_reach_pending(self):
        t = self.c.create_task("先票后审", prompt="p")
        tid = t["id"]
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.assertEqual(self.status(tid), S.DRAFT)
        self.c.submit_for_review(tid)
        self.assertEqual(self.status(tid), S.PENDING_APPROVAL,
                         "送审后必须按既有票重算，不能卡在议事中")

    def test_cancel_while_dispatched_needs_ack(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.request_cancel(tid)
        self.assertEqual(self.status(tid), S.CANCELLING)
        # 执行器仍能心跳，并被告知需要确认取消
        hb = self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertTrue(hb["cancel"])
        self.assertTrue(hb["cancel_ack_required"])
        # 但不能谎报「完成」
        with self.assertRaises(CouncilError):
            self.c.submit_result(p["ticket"], self.ex, "completed")
        out = self.c.confirm_cancel(p["ticket"], self.ex, "已停止")
        self.assertEqual(out["task_status"], S.CANCELLED)
        # 确认之后票彻底失效
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "running")

    def test_cancel_while_dispatched_executor_lost_blocks(self):
        self.c.heartbeat_ttl = 1
        tid = self.approved()
        self.c.claim(tid, self.ex)
        self.c.request_cancel(tid)
        self.assertEqual(self.status(tid), S.CANCELLING)
        import time
        time.sleep(1.1)
        blocked = self.c.sweep_timeouts()
        self.assertEqual(self.status(tid), S.BLOCKED,
                         "取消请求无人确认时必须阻断，不能假装已停")
        self.assertEqual([b["task_id"] for b in blocked], [tid])
        self.assertIn("取消", self.c.get_task(tid)["blocked_reason"])


# ======================================================================
# B. 并发 7 项
# ======================================================================
class TestConcurrency(Base):
    """P1-5：真实线程 + Barrier；断言落库结果，不靠 sleep 编排。"""

    def test_two_threads_claim_same_task_only_one_wins(self):
        tid = self.approved()
        ex2 = self.c.register_executor("dsh-2")
        barrier = threading.Barrier(2)
        results = {}

        def grab(key, ex):
            barrier.wait()
            try:
                results[key] = self.c.claim(tid, ex)
            except Exception as exc:
                results[key + "_err"] = exc

        ts = [threading.Thread(target=grab, args=("a", self.ex)),
              threading.Thread(target=grab, args=("b", ex2))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        ok = [k for k in ("a", "b") if k in results]
        self.assertEqual(len(ok), 1, f"并发领取结果不唯一：{results}")
        self.assertEqual(self.status(tid), S.DISPATCHED)
        n = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 1, "并发领取签出了多张票")

    def test_same_idem_key_claim_returns_identical(self):
        tid = self.approved()
        r1 = self.c.claim(tid, self.ex, idem_key="k-claim")
        r2 = self.c.claim(tid, self.ex, idem_key="k-claim")
        self.assertEqual(r1["ticket"], r2["ticket"], "重放必须拿到同一张票")
        self.assertEqual(r1["ticket_id"], r2["ticket_id"])
        self.assertTrue(r2.get("replayed"))
        n = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 1)

    def test_same_idem_key_heartbeat_no_duplicate_events(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        h1 = self.c.heartbeat(p["ticket"], self.ex, "running", "step1", idem_key="k-hb")
        h2 = self.c.heartbeat(p["ticket"], self.ex, "running", "step1", idem_key="k-hb")
        self.assertEqual(h1["run_id"], h2["run_id"])
        self.assertEqual(h1["lease_expires_at"], h2["lease_expires_at"])
        self.assertTrue(h2.get("replayed"))
        n = self.db.one("SELECT COUNT(*) AS n FROM events WHERE task_id=? AND kind=?",
                        (tid, "run.heartbeat"))["n"]
        self.assertEqual(n, 1, "幂等重放产生了重复事件")
        n = self.db.one("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 1, "幂等重放产生了重复 run")

    def test_same_idem_key_submit_no_duplicate_runs(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        r1 = self.c.submit_result(p["ticket"], self.ex, "completed",
                                  log="build ok", idem_key="k-sub")
        r2 = self.c.submit_result(p["ticket"], self.ex, "completed",
                                  log="build ok", idem_key="k-sub")
        self.assertEqual(r1, r2, "重放必须返回完全一致的历史响应")
        n = self.db.one("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (tid,))["n"]
        self.assertEqual(n, 1)
        n = self.db.one("SELECT COUNT(*) AS n FROM artifacts WHERE run_id=?", (r1["run_id"],))["n"]
        self.assertEqual(n, 1, "幂等重放重复写了 artifact")
        self.assertEqual(self.status(tid), S.SUBMITTED)
        # 同一把键换一个请求体必须被拒，不能张冠李戴
        with self.assertRaises(CouncilError):
            self.c.submit_result(p["ticket"], self.ex, "failed", idem_key="k-sub")

    def test_watchdog_and_heartbeat_race(self):
        """看门狗扫描与心跳并发：持续心跳的任务绝不能被误杀。"""
        self.c.heartbeat_ttl = 2
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        stop = threading.Event()
        errors = []

        def beat():
            while not stop.is_set():
                try:
                    self.c.heartbeat(p["ticket"], self.ex, "running")
                except Exception as exc:
                    errors.append(exc)
                    return
                stop.wait(0.05)

        def sweep():
            while not stop.is_set():
                try:
                    self.c.sweep_timeouts()
                except Exception as exc:      # pragma: no cover
                    errors.append(exc)
                    return
                stop.wait(0.02)

        bt = threading.Thread(target=beat)
        wt = threading.Thread(target=sweep)
        bt.start(); wt.start()
        stop.wait(1.2)
        stop.set()
        bt.join(timeout=10); wt.join(timeout=10)
        self.assertEqual(errors, [], f"并发期间报错：{errors}")
        self.assertEqual(self.status(tid), S.RUNNING,
                         "心跳持续续租的任务被看门狗误杀")

    def test_revoke_and_claim_race(self):
        tid = self.approved()
        barrier = threading.Barrier(2)
        out = {}

        def do_revoke():
            barrier.wait()
            self.c.revoke_executor(self.ex["id"])

        def do_claim():
            barrier.wait()
            try:
                out["claim"] = self.c.claim(tid, self.ex)
            except Exception as exc:
                out["claim_err"] = exc

        ts = [threading.Thread(target=do_revoke), threading.Thread(target=do_claim)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        row = self.db.one("SELECT enabled FROM executors WHERE id=?", (self.ex["id"],))
        self.assertEqual(row["enabled"], 0)
        if "claim" in out:
            # 领取先赢：随后吊销必须让票立刻不可用
            with self.assertRaises(CouncilError):
                self.c.heartbeat(out["claim"]["ticket"], self.ex, "running")
        else:
            self.assertIn("吊销", str(out.get("claim_err")))
            self.assertEqual(self.status(tid), S.APPROVED)

    def test_revoke_and_submit_race(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        barrier = threading.Barrier(2)
        out = {}

        def do_submit():
            barrier.wait()
            try:
                out["submit"] = self.c.submit_result(p["ticket"], self.ex, "completed")
            except Exception as exc:
                out["submit_err"] = exc

        def do_revoke():
            barrier.wait()
            self.c.revoke_executor(self.ex["id"])

        ts = [threading.Thread(target=do_submit), threading.Thread(target=do_revoke)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        row = self.db.one("SELECT enabled FROM executors WHERE id=?", (self.ex["id"],))
        self.assertEqual(row["enabled"], 0)
        if "submit" in out:
            self.assertEqual(self.status(tid), S.SUBMITTED)
        else:
            # R1 之后「被管理员吊销」与「恢复后统一禁用」共用一条可执行文案
            # （都要求人工重新授权并更换凭据），所以这里断新文案而不是旧的「吊销」二字。
            self.assertIn("该身份已被禁用", str(out.get("submit_err")))
            self.assertIn("重新授权", str(out.get("submit_err")))
            self.assertIn(self.status(tid), (S.BLOCKED, S.DISPATCHED, S.RUNNING))
        # 不论谁先赢，吊销之后都不许再有新的写操作
        with self.assertRaises(CouncilError):
            self.c.heartbeat(p["ticket"], self.ex, "running")


# ======================================================================
# C. 数据库恢复 7 项
# ======================================================================
def _write_bundle(src: Path, dest: Path, kind: str = "operational") -> Path:
    """按 backup 模块的格式手写一个包，用于构造「异常/跨版本」用例。"""
    conn = sqlite3.connect(str(src))
    try:
        counts = {}
        for t in REQUIRED_TABLES:
            counts[t] = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        ver = int(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
    finally:
        conn.close()
    manifest = {"backup_format": 2, "app_version": "test", "schema_version": ver,
                "created_at": 0, "created_at_iso": "2026-01-01T00:00:00+00:00",
                "db_sha256": file_sha256(src), "db_size": src.stat().st_size,
                "table_counts": counts, "backup_kind": kind}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.write(src, "council.db")
    return dest


class TestRestore(Base):
    """P1-4：恢复必须原子、旧授权必须作废、失败必须保原库。"""

    def _bundle(self, name="bk.zip") -> Path:
        p = self.root / name
        export_bundle(self.db, p)
        return p

    def test_old_ticket_invalid_after_restore(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        old_ticket = p["ticket"]
        bk = self._bundle()
        out = self.c.restore_from_bundle(bk)
        self.assertTrue(out["key_rotated"], "恢复必须轮换执行票密钥")
        self.assertGreaterEqual(out["restore_epoch"], 1)
        with self.assertRaises(CouncilError):
            self.c.heartbeat(old_ticket, self.ex, "running")
        with self.assertRaises(CouncilError):
            self.c.submit_result(old_ticket, self.ex, "completed")

    def test_old_claim_idempotency_purged_after_restore(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex, idem_key="k-old")
        bk = self._bundle()
        out = self.c.restore_from_bundle(bk)
        self.assertGreaterEqual(out.get("idempotency_purged", 0), 1,
                                "旧的 claim 幂等响应没被清掉，重放会复活旧票")
        with self.assertRaises(CouncilError):
            self.c.claim(tid, self.ex, idem_key="k-old")
        self.assertEqual(p["ticket_id"],
                         self.db.one("SELECT id FROM tickets WHERE task_id=?",
                                     (tid,))["id"])

    def test_inflight_task_blocked_after_restore(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertEqual(self.status(tid), S.RUNNING)
        bk = self._bundle()
        out = self.c.restore_from_bundle(bk)
        self.assertEqual(out["blocked_inflight"], 1)
        self.assertEqual(self.status(tid), S.BLOCKED)
        self.assertIn("恢复", self.c.get_task(tid)["blocked_reason"])
        self.assertEqual(self.c.available_tasks(), [])

    def test_restore_failure_keeps_original_db(self):
        tid = self.approved()
        bk = self._bundle()

        def boom():
            raise RuntimeError("模拟替换前中断")

        with self.assertRaises(RuntimeError):
            self.c.restore_from_bundle(bk, before_replace=boom)
        # 原库必须完好且连接可用
        self.assertEqual(self.status(tid), S.APPROVED)
        self.assertTrue(self.c.overview()["counts"])
        n = self.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
        self.assertEqual(n, 1)
        # 还能正常继续写
        t2 = self.c.create_task("恢复失败后", prompt="p")
        self.assertEqual(self.status(t2["id"]), S.DRAFT)

    def test_restore_older_schema_migrates(self):
        tid = self.approved()
        raw = self.root / "old.db"
        self.db.snapshot(raw)
        conn = sqlite3.connect(str(raw))
        try:
            conn.execute("ALTER TABLE tickets DROP COLUMN epoch")
            conn.execute("ALTER TABLE executors DROP COLUMN model")
            conn.execute("ALTER TABLE tasks DROP COLUMN cancel_requested_at")
            conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        bk = _write_bundle(raw, self.root / "old.zip")
        info = inspect_bundle(bk)
        self.assertEqual(info["schema_version"], 2)
        out = self.c.restore_from_bundle(bk)
        self.assertTrue(out["restored"])
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)
        # R1：恢复后的库里不许留下「可领取」的授权。备份里那张 APPROVED 必须被冻结，
        # 否则这次恢复等于把一个早已批准、可能已被撤销的派工许可重新发出去。
        self.assertEqual(self.status(tid), S.BLOCKED)
        self.assertIn("冻结", self.db.one(
            "SELECT blocked_reason AS r FROM tasks WHERE id=?", (tid,))["r"])
        self.assertGreaterEqual(out["frozen_approved"], 1)

    def test_restore_future_schema_rejected(self):
        tid = self.approved()
        raw = self.root / "future.db"
        self.db.snapshot(raw)
        conn = sqlite3.connect(str(raw))
        try:
            conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        bk = _write_bundle(raw, self.root / "future.zip")
        with self.assertRaises(RestoreError) as cm:
            self.c.restore_from_bundle(bk)
        self.assertIn("高于", str(cm.exception))
        self.assertEqual(self.status(tid), S.APPROVED, "被拒绝的恢复不能动原库")
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)

    def test_restore_corrupted_bundle_rejected(self):
        tid = self.approved()
        bad = self.root / "bad.zip"
        bad.write_bytes(b"PK\x03\x04this is not a real zip")
        with self.assertRaises(RestoreError):
            self.c.restore_from_bundle(bad)
        self.assertEqual(self.status(tid), S.APPROVED)
        # 只有 manifest 没有库，也要拒绝
        half = self.root / "half.zip"
        with zipfile.ZipFile(half, "w") as z:
            z.writestr("manifest.json", json.dumps({"backup_format": 2}))
        with self.assertRaises(RestoreError):
            self.c.restore_from_bundle(half)
        self.assertEqual(self.status(tid), S.APPROVED)


# ======================================================================
# D. 安全 5 项
# ======================================================================
class TestSecurityBoundary(Base):
    """P1-3 / P2：监听闸门、越权、凭据、审计包、文件权限。"""

    def test_all_entry_points_reject_public_bind(self):
        for host in ("0.0.0.0", "::", "[::]", "192.168.1.10", "100.64.1.2",
                     "10.0.0.1", "0:0:0:0:0:0:0:0"):
            with self.assertRaises(BindError, msg=f"{host} 竟然被放行"):
                check_bind_host(host)
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", None):
            self.assertIn(check_bind_host(host),
                          ("127.0.0.1", "localhost", "::1"))
        # 真实进程入口：拒绝时服务根本没起来
        port = free_port()
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        r = subprocess.run([sys.executable, "-m", "bigfish", "--headless",
                            "--data-dir", str(self.root / "bindtest"),
                            "--host", "0.0.0.0", "--port", str(port)],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 2, f"退出码 {r.returncode}，stderr={r.stderr}")
        self.assertIn("拒绝启动", r.stderr)
        self.assertTrue(port_is_bindable(port), "被拒绝的入口居然占用了端口")

    def test_executor_cannot_read_other_task(self):
        tid = self.approved("A 的任务")
        self.c.claim(tid, self.ex)
        other = self.c.register_executor("other")
        app = create_app(self.c, watchdog_interval=None)
        cli = TestClient(app)
        h_dst = {"Authorization": "Bearer " + self.ex["token"]}
        h_oth = {"Authorization": "Bearer " + other["token"]}
        self.assertEqual(cli.get(f"/api/v1/tasks/{tid}/status", headers=h_dst).status_code, 200)
        r = cli.get(f"/api/v1/tasks/{tid}/status", headers=h_oth)
        self.assertEqual(r.status_code, 403, "执行器读到了别人的任务")
        # 未分配任务也不许任何人读
        t2 = self.c.create_task("未分配", prompt="p")
        r = cli.get(f"/api/v1/tasks/{t2['id']}/status", headers=h_dst)
        self.assertEqual(r.status_code, 403, "未分配任务被当成自己的放行了")

    def test_ticket_not_plaintext_in_storage(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        ticket = p["ticket"]
        # 日志/事件/工件里塞明文票，必须被脱敏
        self.c.heartbeat(ticket, self.ex, "running", f"token={ticket}")
        self.c.submit_result(ticket, self.ex, "completed", log=f"leak {ticket}")
        blob = json.dumps(self.c.events(tid), ensure_ascii=False)
        self.assertNotIn(ticket, blob, "事件表里留下了明文执行票")
        run = self.c.runs(tid)[0]
        self.assertNotIn(ticket, json.dumps(run, ensure_ascii=False),
                         "工件里留下了明文执行票")
        # 整个数据库文件里也不许有
        self.db.close()
        raw = (self.root / "council.db").read_bytes()
        self.assertNotIn(ticket.encode(), raw, "数据库文件里存在明文执行票")

    def test_audit_bundle_internal_tables(self):
        tid = self.approved()
        p = self.c.claim(tid, self.ex)
        self.c.heartbeat(p["ticket"], self.ex, "running", f"ticket={p['ticket']}")
        self.c.submit_result(p["ticket"], self.ex, "completed",
                             log="Bearer eyJhbGciOiJIUzI1NiJ9.x.y")
        ab = self.root / "audit.zip"
        export_audit_bundle(self.db, ab)
        with zipfile.ZipFile(ab) as z:
            names = set(z.namelist())
            self.assertIn("manifest.json", names)
            for t in AUDIT_TABLES:
                self.assertIn(f"{t}.json", names, f"审计包缺 {t}")
            for banned in ("executors", "tickets", "idempotency", "runs", "artifacts"):
                self.assertNotIn(f"{banned}.json", names, f"审计包泄露了 {banned} 表")
            for n in names:
                if not n.endswith(".json") or n == "manifest.json":
                    continue
                rows = json.loads(z.read(n).decode("utf-8"))
                for row in rows:
                    for col in AUDIT_DENY_COLUMNS:
                        self.assertNotIn(col, row, f"{n} 出现禁止字段 {col}")
            body = b"".join(z.read(n) for n in names if n.endswith(".json")
                            and n != "manifest.json")
        self.assertNotIn(p["ticket"].encode(), body, "审计包里有明文执行票")
        self.assertNotIn(self.ex["token"].encode(), body, "审计包里有明文令牌")
        self.assertNotIn(b"eyJhbGciOiJIUzI1NiJ9", body, "审计包里有明文 Bearer")
        # 审计包不能拿来恢复
        with self.assertRaises(RestoreError):
            self.c.restore_from_bundle(ab)

    def test_key_file_permissions(self):
        self.assertTrue(self.key_path.exists())
        ok, detail = verify_file_restricted(self.key_path)
        self.assertTrue(ok, f"密钥文件权限不安全：{detail}")
        self.assertNotIn(str(self.root / "council.db"), str(self.key_path))
        if os.name == "nt":                       # pragma: no cover - Windows
            self.assertIn("acl", detail.lower())
        else:
            mode = self.key_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"POSIX 权限应为 0600，实际 {oct(mode)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
