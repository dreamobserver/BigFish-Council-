
# -*- coding: utf-8 -*-
"""v0.1.1-rc9 新增回归：第三方审计（docs/AUDIT-CODEX-v0.1.1-rc8.md）余项.

AUDIT-RC8-02  未知 reviewer model 失败开放。
              rc8 的 role_for_db_row 在 db_role='reviewer' 且 model 不是
              gpt/gemini 时，会按 name 猜；name 里没有 gpt 就默认 Gemini。
              实测 role_for_db_row('reviewer','weird','unknown') -> reviewer_gemini，
              等于「写错一列 model」就拿到审查投票权（能投 approve 推进审批）。
              现在必须失败关闭成 DENIED_ROLE，且 authenticate() 直接拒绝。

断言的都是可观测结果：映射结果 + 能不能真的登录 + 能不能真的投票。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bigfish import roles as R
from bigfish import state as S
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import hash_token, load_or_create_key, new_token


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

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def insert_executor(self, role: str, name: str, model=None) -> str:
        """绕过 register_executor 的白名单，直接写一行脏数据。

        这是在模拟「旧版本残留行 / 手工 SQL 改错一列」，也正是审计担心的场景：
        白名单只挡得住正常注册路径，挡不住库里已经存在的行。
        """
        token = new_token("bfc_ex")
        ex_id = "executor-dirty-" + token[-8:]
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO executors (id,name,role,model,token_hash,enabled,created_at)"
                " VALUES (?,?,?,?,?,1,?)",
                (ex_id, name, role, model, hash_token(token), 0.0))
        return token


class TestUnknownReviewerModelFailsClosed(Base):
    """AUDIT-RC8-02：模型列认不出时必须拒绝，不能默认成 Gemini。"""

    def test_unknown_model_denied(self):
        self.assertEqual(R.role_for_db_row("reviewer", "weird", "unknown"),
                         R.DENIED_ROLE)
        self.assertEqual(R.role_for_db_row("reviewer", "weird", "claude"),
                         R.DENIED_ROLE)

    def test_empty_name_and_model_denied(self):
        self.assertEqual(R.role_for_db_row("reviewer", "", None), R.DENIED_ROLE)
        self.assertEqual(R.role_for_db_row("reviewer", "", ""), R.DENIED_ROLE)
        self.assertEqual(R.role_for_db_row("reviewer", "   ", "  "), R.DENIED_ROLE)

    def test_ambiguous_name_denied(self):
        """名字里同时出现 gpt 和 gemini 时不能瞎猜其中一个。"""
        self.assertEqual(R.role_for_db_row("reviewer", "gpt-gemini-bridge", None),
                         R.DENIED_ROLE)

    def test_explicit_model_still_works(self):
        """修完之后正常路径不能一起挂掉。"""
        self.assertEqual(R.role_for_db_row("reviewer", "whatever", "gpt"),
                         R.ARCHITECT_GPT)
        self.assertEqual(R.role_for_db_row("reviewer", "whatever", "gemini"),
                         R.REVIEWER_GEMINI)
        self.assertEqual(R.role_for_db_row("reviewer", "GPT", "GPT"),
                         R.ARCHITECT_GPT)

    def test_unique_name_inference_still_works(self):
        """rc2 之前的旧数据没有 model 列，靠唯一可辨的名字兼容——这条必须保住。"""
        self.assertEqual(R.role_for_db_row("reviewer", "my-gpt-bot", None),
                         R.ARCHITECT_GPT)
        self.assertEqual(R.role_for_db_row("reviewer", "gemini-reviewer", None),
                         R.REVIEWER_GEMINI)

    def test_authenticate_rejects_dirty_reviewer(self):
        """真正要紧的是：脏数据行不能登录。"""
        tok = self.insert_executor("reviewer", "weird", "unknown")
        with self.assertRaises(CouncilError) as ctx:
            self.c.authenticate(tok, "reviewer")
        self.assertIn("未登记", str(ctx.exception))

    def test_authenticate_any_rejects_dirty_reviewer(self):
        tok = self.insert_executor("reviewer", "", None)
        with self.assertRaises(CouncilError) as ctx:
            self.c.authenticate_any(tok)
        self.assertIn("未登记", str(ctx.exception))

    def _denial_events(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM events WHERE kind=? ORDER BY id DESC",
            ("auth.denied_unknown_role",))
        return [dict(r) for r in rows]

    def test_dirty_reviewer_cannot_vote(self):
        """端到端：脏 reviewer 连登录都过不去，任务不可能被推进。"""
        tok = self.insert_executor("reviewer", "bot", None)
        task = self.c.create_task("演示", "验证", "目录", "low", "prompt")
        tid = task["id"]
        with self.assertRaises(CouncilError):
            self.c.authenticate(tok, "reviewer")
        # 任务仍在草稿态，没有任何审查票
        self.assertEqual(self.c.get_task(tid)["status"], S.DRAFT)
        self.assertEqual(self.c.reviews(tid), [])

    def test_denial_is_audited(self):
        tok = self.insert_executor("reviewer", "weird", "unknown")
        before = len(self._denial_events())
        with self.assertRaises(CouncilError):
            self.c.authenticate(tok, "reviewer")
        after = len(self._denial_events())
        self.assertEqual(after, before + 1, "拒绝鉴权必须留下审计事件")

    def test_denial_does_not_leak_token(self):
        """审计事件里不许出现明文令牌。"""
        tok = self.insert_executor("reviewer", "weird", "unknown")
        with self.assertRaises(CouncilError):
            self.c.authenticate(tok, "reviewer")
        rows = self._denial_events()
        self.assertTrue(rows, "没有审计事件")
        for e in rows:
            blob = repr(e)
            self.assertNotIn(tok, blob, "审计事件泄露了明文令牌")
            self.assertNotIn(tok[-12:], blob, "审计事件泄露了令牌尾部")

class TestRegisterReviewerFailsClosed(Base):
    """注册入口必须和 role_for_db_row 同源：不能一个拒绝、一个默认 Gemini。"""

    def test_ambiguous_name_requires_explicit_model(self):
        with self.assertRaises(CouncilError) as ctx:
            self.c.register_executor("随便什么名", "reviewer", None)
        self.assertIn("显式指定", str(ctx.exception))

    def test_both_keywords_in_name_requires_explicit_model(self):
        with self.assertRaises(CouncilError):
            self.c.register_executor("gpt-gemini-bridge", "reviewer", None)

    def test_unique_name_still_infers(self):
        a = self.c.register_executor("my-gpt-bot", "reviewer", None)
        self.assertEqual(a["model"], "gpt")
        b = self.c.register_executor("gemini-reviewer", "reviewer", None)
        self.assertEqual(b["model"], "gemini")

    def test_explicit_model_always_wins(self):
        a = self.c.register_executor("随便什么名", "reviewer", "gemini")
        self.assertEqual(a["model"], "gemini")

    def test_registered_reviewer_can_login(self):
        """失败关闭不能把正常路径也关掉。"""
        a = self.c.register_executor("随便什么名", "reviewer", "gpt")
        got = self.c.authenticate(a["token"], "reviewer")
        self.assertEqual(got["role_canonical"], R.ARCHITECT_GPT)


if __name__ == "__main__":
    unittest.main()
