"""v0.1.1-rc5 回归：R2（双通过事件丢失）/ R3（撤评论打挂任务）/ R4（时间戳取在锁外）/
R6（审计包白名单）.

这四条都来自第三方审计，共同点是「函数返回值看起来对，但库里/包里的东西不对」，
所以断言一律落在数据库最终状态与导出包的真实内容上，不看返回码。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import state as S
from bigfish.backup import AUDIT_COLUMNS, AUDIT_TABLES, export_audit_bundle
from bigfish.core import Council
from bigfish.db import Database
from bigfish.security import load_or_create_key


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "council.db")
        self.db.init_schema()
        self.key_path = self.root / "secrets" / "ticket.key"
        self.c = Council(self.db, key=load_or_create_key(self.key_path),
                         heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600,
                         heartbeat_lease=180, key_path=self.key_path)
        self.ex = self.c.register_executor("dsh-cloud")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def two_votes(self, tid):
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")

    def draft(self, title="演示任务"):
        return self.c.create_task(title, "验证", "独立目录", "low", "prompt v1")["id"]

    def comment_id(self, tid):
        """add_review 返回的是任务快照，不是审查行；评论 id 要从 reviews 里取。"""
        rows = [r for r in self.c.reviews(tid) if r["kind"] == "comment"
                and not r["revoked"]]
        return rows[0]["id"]

    def event_kinds(self, tid):
        return [r["kind"] for r in self.db.query(
            "SELECT kind FROM events WHERE task_id=? ORDER BY id", (tid,))]


# ======================================================================
# R2：双模型通过的两条事件必须真的写进 events
# ======================================================================
class TestReviewPassedEvents(Base):
    def test_events_written_on_all_approved(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        kinds = self.event_kinds(tid)
        self.assertIn("task.review_passed", kinds,
                      "双模型通过事件丢了：审计只看事件表会以为任务没通过审查")
        self.assertIn("task.pending_approval", kinds)

    def test_status_still_reaches_pending_approval(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)

    def test_events_written_when_votes_cast_before_review(self):
        """先投票后送审也要能推进（P1-2.3），并且事件同样不能丢。"""
        tid = self.draft()
        self.two_votes(tid)
        self.c.submit_for_review(tid)
        self.assertEqual(self.c.get_task(tid)["status"], S.PENDING_APPROVAL)
        self.assertIn("task.review_passed", self.event_kinds(tid))


# ======================================================================
# R3：撤普通评论不得影响审批/执行
# ======================================================================
class TestRevokeComment(Base):
    def test_revoke_comment_does_not_touch_approved_task(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.add_review(tid, "gpt", "只是评论", kind="comment")
        cid = self.comment_id(tid)
        self.c.approve(tid, "approve")
        self.assertEqual(self.c.get_task(tid)["status"], S.APPROVED)

        self.c.revoke_review(cid)
        self.assertEqual(self.c.get_task(tid)["status"], S.APPROVED,
                         "撤一条评论把已批准任务打回议事，等于用评论行使了否决权")

    def test_revoke_comment_does_not_block_running_task(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.add_review(tid, "gpt", "只是评论", kind="comment")
        cid = self.comment_id(tid)
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        self.assertEqual(self.c.get_task(tid)["status"], S.DISPATCHED)

        self.c.revoke_review(cid)
        self.assertEqual(self.c.get_task(tid)["status"], S.DISPATCHED)
        # 票据必须仍然有效
        r = self.c.heartbeat(p["ticket"], self.ex, "running")
        self.assertEqual(r["task_status"], S.RUNNING)

    def test_revoke_vote_still_invalidates(self):
        """反向保护：撤真票必须照旧生效，别把 R3 修过头。"""
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.approve(tid, "approve")
        vid = [r for r in self.c.reviews(tid, 1)
               if r["model"] == "gpt" and r["kind"] == "vote" and not r["revoked"]][0]["id"]
        self.c.revoke_review(vid)
        self.assertEqual(self.c.get_task(tid)["status"], S.IN_REVIEW)


# ======================================================================
# R4：心跳/提交的时间戳必须在事务内取
# ======================================================================
class TestTimestampInsideTransaction(Base):
    def test_heartbeat_lease_is_in_the_future(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        before = self.db.one("SELECT expires_at, renewed_at FROM tickets WHERE id=?",
                             (p["ticket_id"],))
        r = self.c.heartbeat(p["ticket"], self.ex, "running")
        after = self.db.one("SELECT expires_at, renewed_at FROM tickets WHERE id=?",
                            (p["ticket_id"],))
        self.assertGreater(after["renewed_at"], before["renewed_at"],
                           "续租时间没有前进")
        self.assertGreater(after["expires_at"], after["renewed_at"],
                           "续租后票据必须比续租时刻更晚过期")
        self.assertAlmostEqual(r["lease_expires_at"], after["expires_at"], places=6)

    def test_heartbeat_keeps_lease_alive_repeatedly(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        last = 0.0
        for _ in range(5):
            self.c.heartbeat(p["ticket"], self.ex, "running")
            cur = self.db.one("SELECT expires_at FROM tickets WHERE id=?",
                              (p["ticket_id"],))["expires_at"]
            self.assertGreaterEqual(cur, last)
            last = cur


# ======================================================================
# R6：审计包按列白名单导出，且真的检查包里有什么
# ======================================================================
SENSITIVE_HINTS = ("token", "hash", "ticket", "secret", "key", "password")


class TestAuditBundleAllowlist(Base):
    def _bundle(self):
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.approve(tid, "approve")
        self.c.claim(tid, self.ex)
        p = self.root / "audit.zip"
        export_audit_bundle(self.db, p)
        return p

    def test_every_exported_column_is_on_the_allowlist(self):
        z = self._bundle()
        with zipfile.ZipFile(z) as zf:
            for table in AUDIT_TABLES:
                rows = json.loads(zf.read(table + ".json"))
                for row in rows:
                    for k in row:
                        self.assertIn(k, AUDIT_COLUMNS[table],
                                      f"{table} 导出了白名单之外的列 {k}")

    def test_no_sensitive_column_names_leak(self):
        """不看注释、不看 manifest，直接把包里所有 JSON 的键名拉出来查。"""
        z = self._bundle()
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if not name.endswith(".json") or name == "manifest.json":
                    continue
                rows = json.loads(zf.read(name))
                stack = list(rows)
                while stack:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        for k, v in cur.items():
                            low = k.lower()
                            for bad in SENSITIVE_HINTS:
                                self.assertNotIn(
                                    bad, low,
                                    f"{name} 里出现了可疑列名 {k}")
                            stack.append(v)
                    elif isinstance(cur, list):
                        stack.extend(cur)

    def test_manifest_declares_allowlist_policy(self):
        z = self._bundle()
        with zipfile.ZipFile(z) as zf:
            m = json.loads(zf.read("manifest.json"))
        self.assertEqual(m["redaction"]["policy"], "allowlist")
        self.assertEqual(set(m["redaction"]["allowed_columns"]), set(AUDIT_TABLES))
        # rc7 BFC-04：manifest 不再用「contains_plaintext_credentials: false」
        # 这种布尔承诺（它读起来像「我保证没有」，但白名单只能证明「没导出那一列」）。
        # 改成枚举后，审计包必须自报「按模式脱敏过、未验证干净」。
        self.assertEqual(m["credential_status"], "pattern-scrubbed-unverified")
        self.assertNotIn("contains_plaintext_credentials", m)
        self.assertTrue(m["credential_scan"]["manual_review_required"])

    def test_dropped_columns_reported_when_present(self):
        """库里确实有被跳过的列时，manifest 要说清楚跳了哪些，不能静默。"""
        z = self._bundle()
        with zipfile.ZipFile(z) as zf:
            m = json.loads(zf.read("manifest.json"))
        # tasks 表在库里没有敏感列，所以可能为空；但只要 dropped 非空，
        # 就必须是白名单之外的列名。
        for table, cols in m["redaction"]["dropped_columns"].items():
            for c in cols:
                self.assertNotIn(c, AUDIT_COLUMNS[table])

    def test_prompt_body_scrubbed_of_ticket(self):
        """往事件里塞一张真票，导出包必须看不到明文的它。"""
        tid = self.draft()
        self.c.submit_for_review(tid)
        self.two_votes(tid)
        self.c.approve(tid, "approve")
        p = self.c.claim(tid, self.ex)
        self.c.log_event(tid, "leak.test", f"token={p['ticket']}")
        z = self._bundle()
        with zipfile.ZipFile(z) as zf:
            blob = b"".join(zf.read(n) for n in zf.namelist())
        self.assertNotIn(p["ticket"].encode(), blob)
        self.assertIn(b"REDACTED", blob)


if __name__ == "__main__":
    unittest.main()
