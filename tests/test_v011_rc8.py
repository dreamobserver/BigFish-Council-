"""v0.1.1-rc8 新增回归：GPT 复核剩下的两条 P1（R7-01 / R7-02）.

R7-01  Windows 同进程并发创建 Database 时 _LOCAL 句柄表存在竞争窗口：
       两个线程都看到「还没有已登记的句柄」，各自开文件抢槽位，后者覆盖
       _LOCAL[key]；先完成者的 release() 因 held[0] is not fh 直接 return，
       句柄与槽位双双泄漏，泄漏累积到 SLOTS 之后谁都打不开库。
       验证方式：真线程 + 屏障，反复并发 open/close，断言零失败、槽位未泄漏。

R7-02  未知数据库角色回落成 EXECUTOR_DSH（写错一个字符串就拿到执行器权限）。
       现在必须失败关闭：映射成零权限角色并拒绝鉴权。
       验证方式：直接往库里插一行 role='ghost' 的身份，从四个入口分别试。

两条断言的都是**可观测结果**（能不能拿到租约 / 能不能claim），
不是「某个函数被调用过」。
"""
from __future__ import annotations

import os
import sys
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from bigfish import roles as R
from bigfish import state as S
from bigfish.core import Council, CouncilError
from bigfish.db import Database
from bigfish.security import hash_token, load_or_create_key, new_token
from bigfish.server import create_app, dispatch_tool


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
        self.ex = self.c.register_executor("dsh-cloud", "executor")

    def tearDown(self):
        try:
            self.c.stop_watchdog()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def approved(self, title="演示") -> str:
        t = self.c.create_task(title, "验证", "独立目录", "low", "prompt-body")
        tid = t["id"]
        self.c.submit_for_review(tid)
        self.c.add_review(tid, "gpt", "ok", "approve", kind="vote")
        self.c.add_review(tid, "gemini", "ok", "approve", kind="vote")
        self.c.approve(tid, "approve")
        return tid

    def ghost_identity(self, role="ghost") -> str:
        """直接写一行库内角色未登记的身份，拿到它的明文令牌。"""
        token = new_token("bfc_ex_")
        self.db.execute(
            "INSERT INTO executors(id,name,role,model,token_hash,enabled,created_at)"
            " VALUES(?,?,?,?,?,1,?)",
            ("ghost-0001", "ghost", role, None, hash_token(token), 0.0),
        )
        return token


# ======================================================================
# R7-01 Windows 同进程并发建库：_LOCAL 句柄表不能有竞争窗口
# ======================================================================
class TestConcurrentLocalHandles(Base):
    """同一进程里多个线程同时 open/close 同一个数据库。

    旧实现在 Windows 上会漏句柄和槽位：两个线程都判定「还没有人持有」，
    各自申请一个槽位，_LOCAL 被后者覆盖，先完成的那个 release() 直接 return。
    这里跑的线程数是 4、每个线程 25 轮，共 100 次 open/close，
    断言全程零异常，且结束后 _LOCAL 表里没有该路径的残留项。
    """

    THREADS = 4
    ROUNDS = 25

    def test_parallel_open_close_100_times(self):
        from bigfish import locks

        path = self.root / "parallel.db"
        seed = Database(path)
        seed.init_schema()               # 先把库、表和锁文件建出来
        seed.close()

        barrier = threading.Barrier(self.THREADS)
        errors: list[str] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            try:
                barrier.wait(10)        # 让四个线程尽量同时冲进 acquire
            except threading.BrokenBarrierError:
                pass
            for _ in range(self.ROUNDS):
                db = None
                try:
                    db = Database(path)
                    db.one("SELECT COUNT(*) AS n FROM tasks")
                except BaseException as exc:      # noqa: BLE001 - 要收集全部
                    with lock:
                        errors.append(f"线程{n}: {type(exc).__name__}: {exc}")
                    return
                finally:
                    if db is not None:
                        try:
                            db.close()
                        except BaseException as exc:   # noqa: BLE001
                            with lock:
                                errors.append(f"线程{n} close: {exc}")

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [], "并发建库时报错：" + "；".join(errors[:5]))
        self.assertTrue(all(not t.is_alive() for t in threads), "有线程没退出")

        # 句柄表必须干净：残留项意味着 release() 漏掉了一个句柄/槽位。
        key = str(locks.access_lock_path(path).resolve())
        self.assertNotIn(key, locks._LOCAL,
                         f"_LOCAL 残留 {key}：句柄或槽位泄漏")

        # 泄漏过的话现在会打不开；这里必须还能正常开。
        Database(path).close()

    @unittest.skipUnless(os.name == "nt", "槽位模型只在 Windows 上生效")
    def test_local_table_refcount_shared(self):     # pragma: no cover - 仅 Windows
        """同进程第二个实例共享同一个句柄，关掉一个不能把锁放掉。"""
        from bigfish import locks

        path = self.root / "shared.db"
        a = Database(path)
        b = Database(path)
        key = str(locks.access_lock_path(path).resolve())
        self.assertIn(key, locks._LOCAL)
        self.assertEqual(locks._LOCAL[key][1], 2, "引用计数应为 2")
        a.close()
        self.assertIn(key, locks._LOCAL, "还有实例在用，不能摘表")
        b.close()
        self.assertNotIn(key, locks._LOCAL, "全部关闭后必须摘表")


# ======================================================================
# R7-02 未知角色必须失败关闭
# ======================================================================
class TestUnknownRoleFailsClosed(Base):
    def test_mapping_returns_denied_role(self):
        self.assertEqual(R.role_for_db_row("ghost"), R.DENIED_ROLE)
        self.assertEqual(R.role_for_db_row(""), R.DENIED_ROLE)
        # 明确登记过的仍然正确映射
        self.assertEqual(R.role_for_db_row("executor"), R.EXECUTOR_DSH)
        self.assertEqual(R.role_for_db_row("admin"), R.HUMAN_OWNER)
        self.assertEqual(R.role_for_db_row("mobile"), R.MOBILE_VIEWER)
        self.assertEqual(R.role_for_db_row("reviewer", model="gpt"), R.ARCHITECT_GPT)

    def test_denied_role_has_no_permissions(self):
        self.assertEqual(R.PERMISSIONS[R.DENIED_ROLE], set())
        for perm, _, _ in [(p[0], p[1], "") for p in
                           [("task.claim", ""), ("run.heartbeat", ""),
                            ("run.submit", ""), ("task.read.own", ""),
                            ("approve", ""), ("backup.restore", "")]]:
            self.assertFalse(R.can(R.DENIED_ROLE, perm), f"{perm} 不该放行")
        # 也不能被 register_executor 白名单接受
        self.assertNotIn(R.DENIED_ROLE, R.DB_ROLES)

    def test_authenticate_rejects_unknown_role(self):
        """两层闸都关着：先撞「令牌与通道角色不符」，再撞「库内角色未登记」。"""
        token = self.ghost_identity("ghost")
        # 通道角色是 executor，库里是 ghost —— 第一道就拿不到身份
        with self.assertRaises(CouncilError) as cm:
            self.c.authenticate(token, role="executor")
        self.assertIn("无效", str(cm.exception))
        # 通道角色与库内一致时才会走到角色映射，这里必须由未登记角色拦下
        with self.assertRaises(CouncilError) as cm2:
            self.c.authenticate(token, role="ghost")
        self.assertIn("未登记", str(cm2.exception))
        # 审计里要留下痕迹，且不能写令牌本身
        rows = self.db.query("SELECT kind, detail FROM events"
                           " WHERE kind='auth.denied_unknown_role'")
        self.assertEqual(len(rows), 1, f"应有一条审计事件，实际 {rows}")
        self.assertEqual(rows[0]["detail"], "ghost-0001")
        self.assertNotIn(token, str(rows))

    def test_authenticate_any_rejects_unknown_role(self):
        token = self.ghost_identity("ghost")
        with self.assertRaises(CouncilError) as cm:
            self.c.authenticate_any(token)
        self.assertIn("未登记", str(cm.exception))

    def test_unknown_role_cannot_claim_via_service(self):
        """服务层：未知角色连业务层都进不去。"""
        token = self.ghost_identity("ghost")
        tid = self.approved()
        cli = TestClient(create_app(self.c, watchdog_interval=None))
        hdr = {"Authorization": f"Bearer {token}"}
        r = cli.post("/api/v1/tasks/claim", json={"task_id": tid}, headers=hdr)
        self.assertGreaterEqual(r.status_code, 400, "未登记角色竟然拿到 2xx")
        # 任务必须还是可领取状态，没被这次失败调用改掉
        self.assertEqual(self.c.get_task(tid)["status"], S.APPROVED)

    def test_dispatch_tool_denies_missing_canonical_role(self):
        """dispatch_tool 收到缺 role_canonical 的身份时零权限，不是执行器。"""
        tid = self.approved()
        bare = {"id": self.ex["id"], "role": "executor"}   # 没有 role_canonical
        for tool in ("council_list_tasks", "council_claim_task",
                     "council_heartbeat", "council_submit_result",
                     "council_task_status"):
            with self.assertRaises(CouncilError) as cm:
                dispatch_tool(self.c, bare, tool, {"task_id": tid, "ticket": "x"})
            self.assertIn("无权", str(cm.exception), f"{tool} 应被拒")
        self.assertEqual(self.c.get_task(tid)["status"], S.APPROVED)

    def test_known_executor_still_works(self):
        """反向确认修复没有误伤：正常执行器照旧能领。"""
        tid = self.approved()
        ex = self.c.authenticate(self.ex["token"], role="executor")
        self.assertEqual(ex["role_canonical"], R.EXECUTOR_DSH)
        self.assertEqual(self.c.claim(tid, ex)["ticket"][:7], "bfc_tk_")


# ======================================================================
# CI 回归：子进程断言不能依赖宿主代码页
# ======================================================================
class TestSubprocessEncodingIndependence(unittest.TestCase):
    """rc7 的 CI 在 windows-latest 上红了三条，根因都在「断言依赖宿主编码/速度」。

    这里固化的不是那三条用例本身（它们在原文件里改），而是**约定**：
    从子进程读中文，必须显式指定编码并让子进程说 UTF-8。
    """

    def test_chinese_survives_under_ascii_env(self):
        import subprocess
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        r = subprocess.run(
            [sys.executable, "-c", "import sys; print('拒绝启动：测试')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("拒绝启动", r.stdout)

    def test_proxy_gate_constant_is_a_window_not_a_latency(self):
        """proxy 闸门的等待时间必须够大，别把「node 冷启动慢」当成「闸门失效」。"""
        import importlib
        mod = importlib.import_module("tests.test_v011_rc5_proxy")
        self.assertGreaterEqual(mod.STARTUP_TIMEOUT, 10.0,
                                "STARTUP_TIMEOUT 太小，Windows 冷启动会误判")


# ======================================================================
# Windows ACL 判定：跨平台可测的纯函数
# ======================================================================
class TestWindowsAclMatching(unittest.TestCase):
    """rc8 真机回归：Windows 全量跑时 `test_key_file_permissions` 偶发失败。

    单跑 100% 通过、整文件跑也通过，只在全量时挂 —— 断言依赖
    `os.environ["USERNAME"]` 单一路径太脆（域账户 / SID 显示 / whoami 输出
    对不上）。这里把判定抽成纯函数并固化三种真实 icacls 形态。
    """

    def test_domain_user_line_is_recognised(self):
        from bigfish.security import acl_mentions_user
        out = ("c:\\data\\secrets\\ticket.key CLIENT-B\\USER-B:(F)\n"
               "                                NT AUTHORITY\\SYSTEM:(F)\n"
               "                                BUILTIN\\Administrators:(F)")
        self.assertTrue(acl_mentions_user(out.lower(), ["user-b"]))

    def test_bare_username_line_is_recognised(self):
        from bigfish.security import acl_mentions_user
        self.assertTrue(acl_mentions_user("c:\\k.key user-b:(f)", ["user-b"]))

    def test_sid_only_output_is_recognised(self):
        from bigfish.security import acl_mentions_user
        out = "c:\\k.key S-1-5-21-1111-2222-3333-1001:(f)"
        self.assertTrue(acl_mentions_user(out.lower(), [],
                                          ["s-1-5-21-1111-2222-3333-1001"]))
        # 换成别人的 SID 就不该通过
        self.assertFalse(acl_mentions_user(out.lower(), [],
                                           ["s-1-5-21-9999-9999-9999-1002"]))

    def test_other_user_only_is_rejected(self):
        from bigfish.security import acl_mentions_user
        self.assertFalse(acl_mentions_user("c:\\k.key others\\bob:(f)", ["user-b"]))

    def test_broad_subjects_are_flagged(self):
        from bigfish.security import acl_broad_subject
        for line in ("x everyone:(f)", "x builtin\\users:(rx)",
                     "x nt authority\\authenticated users:(m)",
                     "x nt authority\\interactive:(m)", "x users:(rx)"):
            self.assertIsNotNone(acl_broad_subject(line), f"没识别出宽泛主体：{line}")
        self.assertIsNone(acl_broad_subject("x client-b\\user-b:(f)"))

    def test_verify_still_requires_current_user(self):
        """verify_file_restricted 在 POSIX 上只认 0600，这里证明它没被改松。"""
        import tempfile
        from pathlib import Path
        from bigfish.security import secure_file, verify_file_restricted
        d = tempfile.mkdtemp()
        p = Path(d) / "k.key"
        p.write_bytes(b"x" * 32)
        secure_file(p)
        ok, detail = verify_file_restricted(p)
        if os.name == "nt":
            self.assertTrue(ok, f"Windows ACL 校验失败：{detail}")
        else:
            self.assertTrue(ok, detail)
            p.chmod(0o644)
            ok2, detail2 = verify_file_restricted(p)
            self.assertFalse(ok2, "0644 竟然通过了校验")
            self.assertIn("mode", detail2)


class TestConsoleDecoding(unittest.TestCase):
    """rc8 真机根因回归：icacls 输出 GBK，text=True 在 UTF-8 模式下解码炸掉。

    崩的是 subprocess 的 reader 线程，主线程只看到 rc=0 + stdout=""，
    于是「ACL 正常」被误判成「ACL 未包含当前用户」。这里固化解码兜底。
    """

    def test_gbk_bytes_decode_without_raising(self):
        from bigfish.security import _decode_console
        raw = "C:\\Users\\x\\k.key CLIENT-B\\USER-B:(F)".encode("gbk")
        text = _decode_console(raw)
        self.assertIn("USER-B", text)
        self.assertNotIn("\ufffd", text)

    def test_utf8_bytes_decode(self):
        from bigfish.security import _decode_console
        self.assertIn("用户", _decode_console("用户:(F)".encode("utf-8")))

    def test_undecodable_bytes_still_return_text(self):
        from bigfish.security import _decode_console
        text = _decode_console(b"\xff\xfe\x00\xd2broken")
        self.assertIsInstance(text, str)
        self.assertTrue(text)

    def test_empty_bytes(self):
        from bigfish.security import _decode_console
        self.assertEqual(_decode_console(b""), "")

    def test_run_capture_never_raises_on_bad_encoding(self):
        """真跑一个输出非 UTF-8 的程序：必须拿到 rc 与文本，而不是异常。"""
        from bigfish.security import run_capture
        code = "import sys; sys.stdout.buffer.write(b'\\xd2\\xf3'); sys.exit(0)"
        rc, out, _err = run_capture([sys.executable, "-c", code])
        self.assertEqual(rc, 0)
        self.assertIsInstance(out, str)
        self.assertTrue(out, "非 UTF-8 输出被吞成空串（正是 rc8 的病灶）")

    def test_run_capture_reports_missing_binary(self):
        from bigfish.security import run_capture
        rc, out, err = run_capture(["bfc-definitely-not-a-real-binary-xyz"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("调用失败", err)


# ======================================================================
# R8-02：控制台编码兜底
#
# Windows 英文版 / CI runner 的 sys.stdout 是 cp1252，print("健康检查") 直接抛
# UnicodeEncodeError，tools/selfcheck.py 会在第一行输出就整进程退出。
# 这里不测「函数返回了什么」，而是真起子进程，看中文能不能打出来、进程退没退。


class TestConsoleEncodingFallback(unittest.TestCase):
    """R8-02：cp1252 控制台上中文输出必须不崩，cp936 不许被乱改。"""

    SCRIPT = "import sys; sys.path.insert(0, {root!r}); import bigfish; " \
             "print('健康检查'); print('ENC=' + str(sys.stdout.encoding))"

    def _run(self, encoding, extra_env=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = encoding
        env.pop("PYTHONUTF8", None)          # 别让 UTF-8 模式掩盖 cp1252 路径
        env.pop("BFC_NO_STDIO_FIX", None)
        if extra_env:
            env.update(extra_env)
        # 拿原始字节：子进程到底按哪种编码写，本身就是被测行为，
        # 父进程替它解码等于把结论提前写进测试。
        return subprocess.run(
            [sys.executable, "-c", self.SCRIPT.format(root=str(ROOT))],
            capture_output=True, env=env, timeout=60,
        )

    def test_cp1252_console_survives(self):
        """cp1252 下 import bigfish 之后中文必须能打出来，且退出码为 0。"""
        p = self._run("cp1252")
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-800:])
        self.assertIn("健康检查", out)
        self.assertIn("ENC=utf-8", out)

    def test_gbk_console_is_left_alone(self):
        """中文 Windows（cp936）本来就能表示中文，不许改成 UTF-8 搞乱码。"""
        p = self._run("cp936")
        self.assertEqual(p.returncode, 0, p.stderr.decode("gbk", "replace")[-800:])
        self.assertIn("健康检查".encode("gbk"), p.stdout,
                      "cp936 控制台上的中文输出丢了")
        self.assertIn(b"ENC=gbk", p.stdout,
                      "cp936 控制台被无谓地切成了 UTF-8")

    def test_utf8_console_untouched(self):
        p = self._run("utf-8")
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-800:])
        self.assertIn(b"ENC=utf-8", p.stdout)

    def test_env_switch_actually_disables(self):
        """BFC_NO_STDIO_FIX=1 时必须回到「崩」，否则上面三条可能是巧合。"""
        p = self._run("cp1252", {"BFC_NO_STDIO_FIX": "1"})
        self.assertNotEqual(p.returncode, 0, "关掉兜底后居然还能打中文，说明测试没真的测到")
        self.assertIn("UnicodeEncodeError", p.stderr.decode("utf-8", "replace"))
