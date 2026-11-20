"""v0.1.1 回归：P0 数据与安全（SQLite 备份 / 审批并发 / Token 明文）."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bigfish import state as S
from bigfish.backup import export_bundle, inspect_bundle, restore_bundle
from bigfish.core import Council, CouncilError
from bigfish.db import Database, REQUIRED_TABLES
from bigfish.security import hash_token, load_or_create_key


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.c = Council(self.db, key=load_or_create_key(self.root / "secrets" / "ticket.key"),
                         heartbeat_ttl=2, ticket_ttl=60)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def reviewed(self, prompt="prompt v1"):
        t = self.c.create_task("演示任务", "验证", "独立目录", "low", prompt)
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        return tid


class TestSqliteBackup(Base):
    """P0-1：WAL 里未 checkpoint 的数据必须进备份。"""

    def test_wal_data_survives_backup_and_restore(self):
        tid = self.reviewed()
        # 刚写入的数据大概率还在 -wal 里
        wal = Path(str(self.db.path) + "-wal")
        self.assertTrue(wal.exists(), "WAL 模式未生效")

        bundle = export_bundle(self.db, self.root / "b.zip")
        info = inspect_bundle(bundle)
        self.assertEqual(info["table_counts"]["tasks"], 1)
        self.assertEqual(info["table_counts"]["task_versions"], 1)
        self.assertFalse(info["manifest"]["includes_key_file"])

        # 恢复到全新库
        other = Database(self.root / "restored.db")
        try:
            out = restore_bundle(other, bundle)
            self.assertTrue(out["restored"])
            row = other.one("SELECT COUNT(*) AS n FROM tasks")
            self.assertEqual(row["n"], 1, "恢复后任务丢失（WAL 数据没进备份）")
            self.assertEqual(other.one("SELECT status FROM tasks WHERE id=?", (tid,))["status"],
                             S.PENDING_APPROVAL)
        finally:
            other.close()

    def test_snapshot_is_consistent_and_complete(self):
        for i in range(5):
            self.c.create_task(f"t{i}", prompt="p")
        snap = self.db.snapshot(self.root / "snap.db")
        conn = sqlite3.connect(str(snap))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 5)
        self.assertEqual(snap.read_bytes()[:16], b"SQLite format 3\x00")

    def test_bundle_has_required_tables(self):
        self.reviewed()
        bundle = export_bundle(self.db, self.root / "b.zip")
        info = inspect_bundle(bundle)
        for t in REQUIRED_TABLES:
            self.assertIn(t, info["table_counts"])
            self.assertGreaterEqual(info["table_counts"][t], 0, f"表 {t} 缺失")

    def test_corrupted_bundle_does_not_touch_db(self):
        tid = self.reviewed()
        bundle = export_bundle(self.db, self.root / "b.zip")
        data = bytearray(bundle.read_bytes())
        data[-200:-100] = b"\x00" * 100          # 破坏 zip 尾部
        broken = self.root / "broken.zip"
        broken.write_bytes(bytes(data))
        with self.assertRaises(Exception):
            restore_bundle(self.db, broken)
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL, "失败恢复动了原库")

    def test_restore_keeps_prerestore_copy(self):
        self.reviewed()
        bundle = export_bundle(self.db, self.root / "b.zip")
        out = restore_bundle(self.db, bundle)
        self.assertTrue(Path(out["prerestore_copy"]).exists())


class TestApprovalConcurrency(Base):
    """P0-2：审批不得跨版本漂移。"""

    def test_version_drift_rejected(self):
        tid = self.reviewed()
        self.c.update_prompt(tid, "prompt v2", "改需求")
        with self.assertRaises(CouncilError):
            self.c.approve(tid, "approve", expected_version=1)
        self.assertEqual(self.c.get_task(tid)["status"], S.IN_REVIEW,
                         "v1 的审批把未审的 v2 推进了 APPROVED")

    def test_stale_expected_version_rejected(self):
        tid = self.reviewed()
        with self.assertRaises(CouncilError) as ctx:
            self.c.approve(tid, "approve", expected_version=99)
        self.assertIn("版本", str(ctx.exception))
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)

    def test_drifted_version_cannot_be_approved_without_votes(self):
        tid = self.reviewed()
        self.c.update_prompt(tid, "prompt v2", "改需求")
        with self.assertRaises(CouncilError):
            self.c.approve(tid, "approve")     # 不传 expected_version 也必须被票挡住

    def test_approval_records_current_version_only(self):
        tid = self.reviewed()
        self.c.approve(tid, "approve")
        t = self.c.get_task(tid)
        self.assertEqual(t["current_version"], t["latest_approval"]["version"])
        self.assertEqual(t["status"], S.APPROVED)

    def test_double_approve_rejected(self):
        tid = self.reviewed()
        self.c.approve(tid, "approve")
        with self.assertRaises(CouncilError):
            self.c.approve(tid, "approve")

    def test_old_version_vote_revoked_after_update(self):
        tid = self.reviewed()
        tid2 = self.reviewed()
        self.c.update_prompt(tid2, "v2", "改")
        self.assertEqual(self.c.get_task(tid2)["status"], S.IN_REVIEW)
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)


class TestNoPlaintextToken(Base):
    """P0-4：数据库、幂等表、导出包都不得出现明文执行票。"""

    def test_claim_idempotency_has_no_ticket_or_prompt(self):
        tid = self.reviewed(prompt="SECRET-PROMPT-BODY")
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex, idem_key="k1")
        ticket = p["ticket"]
        self.assertTrue(ticket.startswith("bfc_tk_"))

        rows = self.db.query("SELECT key, scope, response FROM idempotency")
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn(ticket, r["response"])
            self.assertNotIn("bfc_tk_", r["response"])
            self.assertNotIn("SECRET-PROMPT-BODY", r["response"])

    def test_db_file_has_no_plaintext_ticket(self):
        """审计点名：原写法把 prompt 从 blob 里减掉再断言它不在，恒真。

        改成两条独立的硬断言：
        1) 明文执行票不得出现在主库与 WAL 文件的任何一个字节里；
        2) tickets 表只存哈希——读出来的 token_hash 必须等于 sha256(明文票)，
           且 tickets 表里没有任何一列的值以 bfc_tk_ 开头。
        """
        tid = self.reviewed(prompt="SECRET-PROMPT-BODY")
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex, idem_key="k1")
        ticket = p["ticket"]
        blob = self.db.path.read_bytes()
        wal = Path(str(self.db.path) + "-wal")
        if wal.exists():
            blob += wal.read_bytes()
        self.assertNotIn(ticket.encode(), blob, "数据库里出现明文执行票")
        self.assertNotIn(b"bfc_tk_", blob, "数据库里出现执行票前缀")

        row = self.db.one("SELECT * FROM tickets WHERE id=?", (p["ticket_id"],))
        self.assertTrue(row)
        self.assertEqual(row["token_hash"], hash_token(ticket))
        for k, v in dict(row).items():
            if isinstance(v, str):
                self.assertFalse(v.startswith("bfc_tk_"),
                                 f"tickets.{k} 里存了明文执行票")

    def test_prompt_body_written_to_task_versions(self):
        """上面那条不再靠 prompt 做减法，这里单独确认 prompt 确实落库了，
        否则「库里没有 SECRET-PROMPT-BODY」可能只是因为根本没写进去。"""
        tid = self.reviewed(prompt="SECRET-PROMPT-BODY")
        got = self.db.one("SELECT prompt FROM task_versions WHERE task_id=? AND version=1",
                          (tid,))["prompt"]
        self.assertEqual(got, "SECRET-PROMPT-BODY")

    def test_bundle_has_no_ticket(self):
        tid = self.reviewed()
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        bundle = export_bundle(self.db, self.root / "b.zip")
        raw = bundle.read_bytes()
        import zipfile
        with zipfile.ZipFile(bundle) as z:
            for name in z.namelist():
                raw += z.read(name)
        self.assertNotIn(p["ticket"].encode(), raw)
        self.assertNotIn(b"bfc_tk_", raw)

    def test_guard_rejects_secret_fields(self):
        from bigfish.core import assert_no_secrets
        with self.assertRaises(CouncilError):
            assert_no_secrets({"prompt": "x"})
        with self.assertRaises(CouncilError):
            assert_no_secrets({"nested": {"t": "bfc_tk_abc"}})
        assert_no_secrets({"task_id": "t", "version": 1})   # 正常数据不报错


if __name__ == "__main__":
    unittest.main(verbosity=2)
