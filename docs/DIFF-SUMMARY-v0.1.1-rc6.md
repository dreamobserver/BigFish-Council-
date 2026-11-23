# 代码 Diff 摘要：v0.1.1-rc5 → v0.1.1-rc6

分支 `fix/v0.1.1-rc4-hardening`，审计基线 `c6d663d`（rc5 tag 点，在 `remotes/ghrc5` 上），rc5 发布提交 `b8f6de1`，HEAD `ae2fe82`（`fix(rc6): 处理 Astra 审计 F-01~F-09…`）。仓库无 tag，rc6 仍是发布候选。

相对 `c6d663d` 的代码侧统计，`git diff --stat c6d663d HEAD -- bigfish tests tools` 的真实输出：

     bigfish/__init__.py                      |   2 +-
     bigfish/backup.py                        | 107 ++++++------
     bigfish/cli.py                           |  22 +--
     bigfish/db.py                            |  99 +++++++----
     bigfish/gui.py                           |  42 ++++-
     bigfish/locks.py                         | 157 +++++++++++++++++
     bigfish/roles.py                         |   2 +-
     bigfish/runtime.py                       |  11 +-
     bigfish/security.py                      |  39 ++++-
     bigfish/server.py                        |  96 +++++++++--
     tests/test_v011_p3.py                    |   4 +-
     tests/test_v011_rc5_cli_guard.py         |  16 +-
     tests/test_v011_rc5_key_heal.py          |  15 ++
     tests/test_v011_rc5_restore_authority.py |  64 +++++++
     tests/test_v011_rc6.py                   | 280 +++++++++++++++++++++++++++++++
     tools/bfc-proxy.mjs                      |   4 +-
     tools/dev_setup.sh                       |   0
     tools/net_check.py                       |   2 +-
     tools/remote_check.py                    |   6 +-
     19 files changed, 826 insertions(+), 142 deletions(-)

**与任务书给的「17 文件 +389/-142」不一致，这里如实说明**：那个数字在本仓库复现不出来。三个可复现的口径是——

1. `c6d663d..HEAD` 代码侧：**19 files changed, 826 insertions(+), 142 deletions(-)**（上面这段输出）。它混进了 5 个 rc5 收尾时就有的文件：`tools/bfc-proxy.mjs`、`tools/dev_setup.sh`、`tools/net_check.py`、`tools/remote_check.py`、`tests/test_v011_p3.py`。
2. 剔除这 5 个文件：**14 files changed, 818 insertions(+), 134 deletions(-)**。
3. 相对 `b8f6de1`（纯 rc6 口径）：**13 files changed, 817 insertions(+), 133 deletions(-)**；其中 `bigfish/` 单独是 **10 files changed, 444 insertions(+), 133 deletions(-)**。

为什么会出现两套：`c6d663d` **不是 HEAD 的祖先**（`git merge-base c6d663d HEAD` 为空，仓库是两条独立历史线），所以「相对审计基线」和「相对 rc5 发布提交」列出来的文件集不同。本文正文一律按 HEAD `ae2fe82` 核对行号。

另有两个新文件，行数与体积单列（已含在上面的 insertions 里）：

| 文件 | 行数 | 字节 | 内容 |
| --- | --- | --- | --- |
| `bigfish/locks.py` | 157 | 5850 | 跨进程写者租约 + 排他访问锁 |
| `tests/test_v011_rc6.py` | 280 | 10646 | 4 类 16 项 |

区间内三次提交：

    ae2fe82  fix(rc6): 处理 Astra 审计 F-01~F-09（维护模式原子性/世代读取/启动失败关闭/CLI预检/GUI停服确认/跨进程恢复租约/审计包凭据声明/请求体上限）
    f1d1ce0  fix: surface key consistency startup failure cleanly
    cbf4d89  fix: harden rc5 restore and startup safety

`cbf4d89` 与 `f1d1ce0` 由 Astra 写（F-01~F-04 + `AUDIT_REVIEW.md` 122 行），`ae2fe82` 是本轮主体（F-05~F-09 + 顺手修）。

平台状态（截至 HEAD）：

| 平台 | 环境 | 结果 |
| --- | --- | --- |
| A | Debian 12.15 / Python 3.11.2 / SQLite 3.40.1 | **265 项全绿**（本轮两次实测 46.926s / 67.168s） |
| B | Windows 11 / Python 3.13.11 / SQLite 3.50.4 | **已跑通 265 项**：`Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0。首跑 `FAILED (errors=2)` 暴露缺陷 17/18，修完提交 `73104a8` 才全绿 |

265 的构成：rc5 的 244 项 + `test_v011_rc6.py` 新增 16 项 + 三个 rc5 测试文件共补 5 项（`restore_authority` +3、`cli_guard` +1、`key_heal` +1）= 265。

> 口径提示：本文所有行号按 HEAD `ae2fe82` 逐条 grep -n 核对，不是估值。`bigfish/db.py:24` 的 `SCHEMA_VERSION` 与 rc5 文档写的 `:22` 差两行（本轮文件头多了一行 `from .locks import` 加一行空行），其他文件有少量漂移，本文一律用当前树的行号。

## 0. 怎么读这篇 diff

rc6 不加功能，是把 Astra 审计点名的 9 条（F-01~F-09）逐条封口。读法建议：先看每节标题后面的审计项编号，再看「为什么」——每处改动都能对回到一条具体缺陷，没有顺手重构。

| 审计项 | 一句话 | 主要落点 | 本文节 |
| --- | --- | --- | --- |
| F-01 | 维护检查与抢锁不是原子的 | `db.query/one/execute` / `db._Tx.__enter__` | 2 |
| F-02 | 恢复世代读失败被吞成 0 | `backup.restore_bundle` | 3 |
| F-03 | 密钥自愈失败仍继续启动 | `runtime.open_council` | 8 |
| F-04 | CLI restore 端口预检晚于开库 | `cli.main` | 7 |
| F-05 | GUI 停服失败被当成成功 | `gui.ApiServer.stop` / `gui.MainWindow.pause_services` | 6 |
| F-06 | 跨进程互斥只是尽力而为 | 新增 `bigfish/locks.py` + `db` + `backup` | 1、2、3 |
| F-07 | 审计包「无凭据」声明过强 | `security` + `backup.export_audit_bundle` | 4、3 |
| F-08 | `create_app` 不限制监听地址 | `server.create_app` | 5 |
| F-09 | 自由文本请求没有 body 上限 | `server` 中间件 + 三个请求模型 | 5 |

另有一节讲**不属于 rc6 的改动**（第 12 节），那 5 个 rc5 收尾文件混在这份 diff 里，必须单独标出来，否则会被误读成本轮改动。

---

## 1. `bigfish/locks.py`（新增，157 insertions；157 行 / 5850 字节）

F-06 的主体，也是本轮唯一的新模块。要解决的问题：原来的 `_restore_lock()` 只互斥「restore 对 restore」，两个普通 `Database` 实例照样能在恢复时继续写；POSIX 上替换文件后旧连接写旧 inode（数据静默分叉），Windows 上直接替换失败。

### 1.1 设计：写者租约 vs 排他访问锁

模块 docstring 把两种平台的分法写死了：

    """跨进程「谁在用这个库」租约 + 恢复期排他锁（rc6 F-06）.

    问题（审计 F-06）：restore 只和另一个 restore 互斥，管不住普通 Database 实例。
    POSIX 上替换文件后旧连接继续写旧 inode（数据静默分叉），Windows 上直接替换失败。

    办法：所有 Database 实例在打开时持一份写者租约（writer lease），恢复流程
    必须拿到排他访问锁才能替换文件。

    - POSIX：两者同一把 flock，写者 LOCK_SH（可多个），恢复 LOCK_EX（与任何写者互斥）。
    - Windows：msvcrt 只有排他锁，没有共享锁。写者各自持有一个
      <db>.access.<pid>.lock（同一 pid 自然唯一），恢复逐个试探这些文件：
      能锁上说明那个 pid 已经退出（租约文件是残留），删掉继续；锁不上说明还活着，
      直接拒绝恢复。这不是共享锁，但得到的是同一个结论：有人在用，就不许换。
    """

**为什么不用一把「排他锁走天下」**：写者是多份并存的（一个 GUI + 一个 headless + 测试进程），排他锁无法表达「多个读者」。POSIX 的 `flock` 恰好有 `LOCK_SH` / `LOCK_EX` 这一对，天然贴合；Windows 的 `msvcrt.locking` 只有排他语义，于是退一步用「一个进程一个租约文件 + 逐个试探」换出同一个结论。

### 1.2 锁文件与租约本体（:23-86）

    class LeaseError(RuntimeError):
        """拿不到访问锁：说明还有别的进程/实例在使用这个数据库。"""

    _guard = threading.Lock()

    def _lock_file(db_path: Path) -> Path:
        """POSIX 用同一个文件；Windows 用按 pid 分开的文件。"""
        p = Path(db_path)
        if os.name == "nt":                      # pragma: no cover - 仅 Windows
            return p.with_name(p.name + f".access.{os.getpid()}.lock")
        return p.with_name(p.name + ".access.lock")

    class WriterLease:
        """一个 Database 实例持有到 close() 为止的共享访问权。"""

        def __init__(self, db_path: Path):
            self.path = Path(db_path)
            self._fh = None
            self.acquire()

        def acquire(self) -> None:
            if self._fh is not None:
                return
            target = _lock_file(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = open(target, "a+b")
            try:
                if os.name == "nt":              # pragma: no cover - 仅 Windows
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            except OSError as exc:
                fh.close()
                raise LeaseError(
                    f"无法登记数据库写者租约（{target}）：{exc}") from None
            self._fh = fh

`acquire()` 里那句 `except OSError as exc: fh.close()` 是必须的：拿不到锁时把刚打开的句柄关掉再抛，否则这个句柄同样会泄漏到 GC（与 2.2 节讲的是同一类问题）。`__init__` 里直接 `self.acquire()`，因此「构造即登记」——没有「先构造、后忘记登记」的窗口。

`release()`（:66）先 `LOCK_UN` 再 `close`，Windows 额外 `os.unlink`：

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name != "nt":
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        # Windows 的租约文件是「一个进程一个」，退出时要清掉，否则恢复端
        # 只能在试探锁时才知道它是残留。
        if os.name == "nt":                  # pragma: no cover - 仅 Windows
            try:
                os.unlink(fh.name)
            except OSError:
                pass

**为什么 Windows 要 `unlink`**：POSIX 上锁随 fd 关闭自动消失，文件留着也无害（所有进程共用一个名字）。Windows 上是「一进程一文件」，正常退出不删就只能在恢复时靠「试锁成功」判断为残留，多一次无谓探测；主动删干净更省事。删失败（比如被别的进程开着）就吞掉，不影响正确性。

### 1.3 排他访问锁（:112-157）

    @contextlib.contextmanager
    def exclusive_access(db_path: Path, timeout: float = 0.0):
        """恢复期间独占访问：有活着的写者就立即失败，绝不「先替换后打架」。"""
        target = Path(db_path).with_name(Path(db_path).name + ".access.lock")
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = open(target, "a+b")
        got = False
        try:
            if os.name == "nt":                  # pragma: no cover - 仅 Windows
                others = _probe_other_writers(db_path)
                if others:
                    raise LeaseError(
                        "还有进程在用这个数据库（" + ", ".join(others) +
                        "），拒绝替换。请先关闭 GUI / 停掉 headless 服务。")
                try:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    got = True
                except OSError:
                    raise LeaseError("另一个进程正在恢复这个数据库，请稍后重试") from None
            else:
                import fcntl
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                except OSError:
                    raise LeaseError(
                        "还有进程在用这个数据库（写者租约仍在生效），拒绝替换。"
                        "请先关闭 GUI / 停掉 headless 服务。") from None
            yield
        finally:
            if got:
                ...

POSIX 分支的 `try/except OSError` 是踩过的坑：第一版把 `fcntl.flock(... LOCK_EX | LOCK_NB)` 裸着写，锁被占时抛的是 `BlockingIOError: [Errno 11]` 而不是 `LeaseError`，测试里断言 `LeaseError` 全部失败。现在所有 OSError 都翻成带中文处置建议的 `LeaseError`。

`timeout` 参数目前是 `0.0` 且没被用到（立即失败语义）；保留它是因为「恢复等一等另一个 restore 收尾」在将来可能需要，现在不实现等待，避免又一个「等到一半状态不明」的分支。

### 1.4 Windows 的试探逻辑（:89-109）

    def _probe_other_writers(db_path: Path) -> list[str]:
        """Windows：列出还活着的写者租约文件。"""
        p = Path(db_path)
        alive: list[str] = []
        for f in p.parent.glob(p.name + ".access.*.lock"):        # pragma: no cover
            try:
                fh = open(f, "a+b")
                try:
                    fh.seek(0)
                    import msvcrt
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        alive.append(f.name)
                    else:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    fh.close()
            except OSError:
                alive.append(f.name)
        return alive

「锁得上 = 那个进程已经退出，文件是残留」，「锁不上 = 还活着」，「连打开都失败 = 也当活人」。往返都失败时偏向保守，宁可拒绝恢复让人去关进程，也不冒风险替换。

**影响面**：这个分支只在 Windows 上走，本批次**没有真机跑过**，全部标了 `# pragma: no cover`。POSIX 分支是实际被测的（第 11.1 节六项）。

**回归**：`tests/test_v011_rc6.py::TestCrossProcessLease` 六项，见 11.1。

---

## 2. `bigfish/db.py`（67 insertions，32 deletions）

本文件承担 F-01（维护检查与加锁的原子性）和 F-06 的「库这一层」（打开即登记租约、恢复期间临时放开、关闭时归还）。

### 2.1 三个访问器：先拿锁，再查维护标志（:461-475）

    -    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    -        self._guard()
    +    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    +        # 锁与维护标志必须在同一个临界区内检查，避免「先过 guard、后等锁」的竞态。
             with self._lock:
    +            self._guard()
                 return list(self._conn.execute(sql, tuple(params)).fetchall())

`one`（:467）与 `execute`（:472）同形。这一改把「检查」和「占位」压进同一个临界区：A 线程过了 `_guard()` 之后必定已经拿着 `_lock`，B 线程的 `enter_maintenance()`（它靠 `self._lock.acquire(timeout)` 做排空屏障）就只能等 A 把这次读/写做完。

**为什么是 P1**：审计给的说法是「维护屏障可能放过一个已经排队的读写操作」。恢复流程一旦认为「已经排空」去替换文件，而 A 还捏着一个旧连接在写，就是数据分叉。

### 2.2 构造函数：租约先登记，失败必归还（:238、:254-274）

    self._lease = WriterLease(self.path)

这一行插在 `_peek_schema_version()` 与三条 `PRAGMA` **之前**。随后整段包进 `try/except BaseException`：

            try:
                ver = self._peek_schema_version()
                if ver > SCHEMA_VERSION:
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        pass
                    self._closed = True
                    raise DatabaseVersionError(
                        f"数据库 schema 版本 {ver} 高于本程序支持的 {SCHEMA_VERSION}，"
                        "拒绝打开（升级程序，不要降级数据库）")

                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
            except BaseException:
                try:
                    self._lease.release()
                except Exception:
                    pass
                raise

**为什么这处值得单独写**：构造函数抛错的对象没人会去 `close()`，租约就落到 GC 手里。实测表现是 `ResourceWarning: unclosed file <_io.BufferedRandom name='.../council.db.access.lock'>`，但真正的危害不是警告——**下一个进程会以为还有人在用，从而无法恢复这个已经打不开的库**。用例 `test_failed_constructor_does_not_leak_lease` 就是先造一个 schema 版本过高的库，断言 `Database(path)` 抛 `DatabaseVersionError` 之后仍然能拿到 `exclusive_access`。

用 `except BaseException` 而不是 `Exception`：`KeyboardInterrupt` / `SystemExit` 打在构造中途同样要还锁。

### 2.3 事务入口：guard 与 BEGIN 失败统一归还锁（:485-505）

    def __enter__(self):
        # 先拿锁、再检查维护标志：enter_maintenance() 与本次检查因此有确定顺序。
        self.db._lock.acquire()
        try:
            self.db._guard()
            if self.db._depth == 0:
                self.db._conn.execute("BEGIN IMMEDIATE")
                self.outer = True
            else:
                self.sp = f"sp_{self.db._depth}"
                self.db._conn.execute(f"SAVEPOINT {self.sp}")
            self.db._depth += 1
            return self.db._conn
        except BaseException:
            # guard / BEGIN / SAVEPOINT 任一步失败都必须归还锁。
            self.db._lock.release()
            raise

原来 `_guard()` 在 `acquire()` 之前，且只有 `BEGIN` / `SAVEPOINT` 有 `except Exception` 归还锁的补丁；现在 `_guard()` 挪进 `try`，三类失败（维护模式、BEGIN 失败、SAVEPOINT 失败）共用一条 `except BaseException` 归还路径，不会出现「guard 抛了但锁没还」这种把整库写操作永久卡死的情况。

### 2.4 close / pause_lease / resume_lease（:525-563）

    def close(self, release_lease: bool = True) -> None:
        """关闭连接。幂等：重复调用不抛错（退出流程可能多路触发）。

        release_lease：恢复流程要自己掌管租约（先放开、抢排他、再抢回来），
        所以它调 close(release_lease=False)，否则中途会把自己锁在门外。
        """
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            if release_lease:
                try:
                    self._lease.release()
                except Exception:            # pragma: no cover
                    pass
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def pause_lease(self) -> None:
        """临时放开写者租约（恢复流程抢排他锁前必须先放开自己那份 SH）。"""
        with self._lock:
            try:
                self._lease.release()
            except Exception:                # pragma: no cover
                pass

    def resume_lease(self) -> None:
        """抢回写者租约。恢复成功/失败都必须调用，否则之后这实例不再登记，
        另一个进程就能在我们眼皮底下把库换掉。"""
        with self._lock:
            self._lease.acquire()

三个方法都取 `self._lock`，和 `close()` 用同一把锁，保证「关」与「暂停/恢复」不会交错。

**影响面**：`close()` 多了个默认 `True` 的关键字参数，既有调用方行为不变；`pause_lease()` / `resume_lease()` 是新增 API，目前只有 `backup.restore_bundle()` 用。

**回归**：F-01 两项在 `tests/test_v011_rc5_restore_authority.py`（:340、:343）；F-06 的关闭归还见 `test_lease_is_released_on_close`。

---

## 3. `bigfish/backup.py`（52 insertions，55 deletions）

净减 3 行，但改动面是这个文件里最险的：删掉整个 `_restore_lock()`，把恢复流程改成「放开自己的租约 → 抢排他 → 读真实世代 → 换库 → 夺回租约」。

### 3.1 删掉 `_restore_lock()`（原 :364-413，约 45 行）

原来那段是自己实现的 `contextlib.contextmanager`：POSIX `fcntl.flock(LOCK_EX)`、Windows `msvcrt.locking`，锁文件是 `<db>.restore.lock`。整段删除，原因写在现在的调用点：

      # F-06：恢复锁只管「另一个 restore」；跨进程的普通写者靠 Database 打开时
      # 登记的写者租约来拦。先放开自己那份，再抢排他，否则自己会把自己挡在外面。

**为什么删而不是留着**：两把锁（`.restore.lock` 与 `.access.lock`）语义重叠，留着就会有人以为「拿了 restore 锁就安全了」，而真正的保证其实在 `exclusive_access` 上。

### 3.2 恢复入口重排（:479-490）

    db.enter_maintenance("正在从备份恢复数据库")
    try:
      # F-06：恢复锁只管「另一个 restore」；跨进程的普通写者靠 Database 打开时
      # 登记的写者租约来拦。先放开自己那份，再抢排他，否则自己会把自己挡在外面。
      db.pause_lease()
      with _translate_lease(exclusive_access(target)):
       with db._allow_during_maintenance():
        # 必须在维护旁路内读取真实活库世代；不能把 MaintenanceError 吞成 0。
        captured_epoch = db.restore_epoch()

改动前这里是：

    -    try:
    -        captured_epoch = db.restore_epoch()
    -    except Exception:
    -        captured_epoch = 0
    -    new_epoch = captured_epoch + 1
    -
    -    try:
    -      with _restore_lock(original_dir / (target.name + ".restore.lock")):
    +      db.pause_lease()
    +      with _translate_lease(exclusive_access(target)):
            with db._allow_during_maintenance():
    +        captured_epoch = db.restore_epoch()

**这就是 F-02**：原来 `enter_maintenance()` 之后直接读 `restore_epoch()`，而这个读走普通访问会被自己的维护模式打成 `MaintenanceError`，外层 `except Exception` 把它吞成 `0`。于是 `_prepare_staging()` 里算的 `max(captured_epoch, staged_epoch) + 1` 拿的不是活库真实世代，「恢复后世代一定更大」这条保证在「恢复一个更旧的快照」时失效——旧快照里 epoch 可能是 0，活库其实已经 7，恢复完还是 1 甚至更小，**恢复前的票在恢复后又活了**。

修法是把这一行整体挪到 `with db._allow_during_maintenance():` **里面**（旁路生效后才读），异常不再被吞掉。缩进看起来别扭（`try:` 下缩进只有 2 空格，是历史遗留写法，本轮没顺手改缩进，避免把 diff 搅浑）。

### 3.3 租约翻译成 RestoreError（:651-664）

    @contextlib.contextmanager
    def _translate_lease(cm):
        """把 LeaseError 翻译成 RestoreError。

        恢复的调用方（GUI / CLI / core）只认识 RestoreError；锁模块不知道
        上层有「恢复」这个业务概念，所以翻译放在这一层做。
        """
        try:
            with cm:
                yield
        except LeaseError as exc:
            raise RestoreError(str(exc)) from None

**为什么需要翻译**：`locks.py` 不该知道「恢复」这个业务词（它也可能被别的场景复用），而 GUI / CLI / `core` 的 `except` 链里只认 `RestoreError`。翻译放在 `backup` 这一层，`from None` 是为了不把底层 `OSError: [Errno 11]` 的栈抛给用户看。

### 3.4 finally 里无条件夺回租约（:637-646）

    finally:
        # 无论成功失败都要把写者租约抢回来：否则本实例退出「在用」名单，
        # 另一个进程就能在我们眼皮底下把库换掉（F-06）。
        try:
            db.resume_lease()
        except Exception:            # pragma: no cover - 进程正在退出时可能失败
            pass
        if not keep_maintenance:
            db.exit_maintenance()

**这一句是 F-06 的兜底**：恢复失败（读包坏、迁移失败、回滚也失败）时，如果只 `exit_maintenance()` 不夺回租约，本进程虽然还在跑，但已不在「在用」名单里，另一个进程的恢复可以合法地替换掉正在被它使用的库。用例 `test_restore_keeps_lease_after_success` 钉的是成功路径，失败路径靠这条无条件调用兜。

### 3.5 审计包如实声明（:183-225）

新增「脱敏后逐字段回扫」：

            # 逐字段回扫：脱敏之后还剩什么形态的凭据（预期为空，但要说出来）
            hits: dict[str, int] = {}
            for table, rows in payload.items():
                for row in rows:
                    for v in row.values():
                        if isinstance(v, str):
                            for kind in scan_secrets(v):
                                hits[kind] = hits.get(kind, 0) + 1

manifest 里换掉原来那句强声明：

    -            "handling": "可外发：无 token、无 token 哈希、无执行票、无幂等响应。",
    +            "credential_scan": {
    +                "mode": "pattern-based",
    +                "patterns": list(SCRUB_KINDS),
    +                "fields_scanned": "全部导出字符串列（含 task_versions.prompt 正文）",
    +                "coverage": "只覆盖上述已知形态；换行拆分、编码变形、"
    +                            "私有格式的密钥无法保证命中",
    +                "hits": hits,
    +                "manual_review_required": True,
    +            },
    +            "handling": ("可外发（需人工复核）：无 token、无 token 哈希、无执行票、"
    +                         "无幂等响应；自由文本已按已知模式脱敏，"
    +                         "但脱敏不等于证明，外发前请人工过一遍 manifest.credential_scan。"),

**为什么 `hits` 要放在脱敏之后算**：放在之前只能说明「输入里有几处像凭据」，放在之后才是「输出里还剩什么」——后者才是拿包的人关心的。预期恒为空，但空也要写出来（空字典本身就是一条证据）。

**影响面**：审计包 manifest 结构变了（新增 `credential_scan` 键，`handling` 文案改成带「需人工复核」），拿旧结构解析运维脚本需要同步。`contains_plaintext_credentials` 与 `contains_credential_hashes` 两个布尔字段没动——它们描述的是白名单列本身（不含 token 列），这个结论仍然成立；被推翻的是「自由文本里一定没有凭据」这层延伸含义，改由 `credential_scan` 承担。

**回归**：`TestAuditManifestHonesty` 五项，见 11.1。

---

## 4. `bigfish/security.py`（30 insertions，9 deletions）

### 4.1 正则表加第三元：种类名（:36-54）

    _PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
        (re.compile(r"bfc_(?:tk|ex|rv|ad)_[A-Za-z0-9_-]{8,}"), "bfc_…[REDACTED]", "bfc-token"),

原来是 `tuple[tuple[re.Pattern, str], ...]` 的二元组，现在每条末尾多一个种类名。原有六条补名：`bfc-token` / `bearer` / `openai-key` / `github-token` / `private-key` / `aws-key`。

### 4.2 新增五类（:44-51）

        # rc6 F-07：审计点名补上的几类。不追求穷尽，只求「漏了也不会被说成保证」。
        (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED GOOGLE KEY]", "google-key"),
        (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED SLACK TOKEN]", "slack-token"),
        (re.compile(r"[sr]k_live_[A-Za-z0-9]{10,}"), "[REDACTED STRIPE KEY]", "stripe-key"),
        (re.compile(r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)s*[=:]s*"
                    r"['"]?[^s'"]{6,}", re.I), "credential=[REDACTED]", "key-value"),
        (re.compile(r"[a-z][a-z0-9+.-]*://[^s:/@]+:[^s:/@]{3,}@", re.I),
         "://[REDACTED]@", "uri-userinfo"),

对应审计 F-07 点名的五类：Google API key、Slack token、Stripe `sk_live_`、通用 `password=...`、数据库连接串里的 `user:pass`。`uri-userinfo` 那条特意只把 userinfo 换掉，scheme 与 host 留下——脱敏的目的是去凭据，不是把整条 DSN 变成不可读。

### 4.3 SCRUB_KINDS 与 scan_secrets（:54、:88-93）

    SCRUB_KINDS = tuple(k for _, _, k in _PATTERNS)

    def scan_secrets(text: str) -> list[str]:
        """返回这段文本命中的凭据种类（去重、排序），用于如实报告扫过什么。"""
        if not isinstance(text, str) or not text:
            return []
        hits = {kind for pat, _repl, kind in _PATTERNS if pat.search(text)}
        hits.update(k for k in SECRET_PREFIXES if k in text)
        return sorted(hits)

`SECRET_PREFIXES` 那几行（`bfc_tk_` 等）是前缀匹配，捕获不到正则那种形态，单独补一次，和 `contains_secret()` 的判定口径保持一致。

`scrub()` 与 `contains_secret()` 只是跟着改元组解包：

    -    for pat, repl in _PATTERNS:
    +    for pat, repl, _kind in _PATTERNS:
    ...
    -    return any(pat.search(text) for pat, _ in _PATTERNS)
    +    return any(pat.search(text) for pat, _repl, _kind in _PATTERNS)

**影响面**：脱敏结果比 rc5 多覆盖五类；`contains_secret()` 也会因为新正则而更早返回 `True`（对外表现为「拒写更多文本」）。这是收紧，不是放宽。

---

## 5. `bigfish/server.py`（85 insertions，11 deletions）

两件事：F-08 把 `create_app()` 的边界写清，F-09 加请求体上限。

### 5.1 两个上限常量（:38-40）

    # F-09：请求体上限。默认 8 MiB，够放一份大 diff + 长日志，又能挡住
    # 「被控制的执行器一次塞 1GB 进来把我们 2.4G 的容器打爆」。
    MAX_BODY_BYTES = int(os.environ.get("BFC_MAX_BODY_BYTES") or 8 * 1024 * 1024)
    # 单个自由文本字段的上限（MCP/REST 共用），比整体上限小一半，避免一个字段吃满。
    MAX_TEXT_FIELD = int(os.environ.get("BFC_MAX_TEXT_FIELD") or 4 * 1024 * 1024)

用 `or` 而不是 `os.environ.get(key, default)`，是为了让「显式设成空串」也退回默认值（空串在这个场景下必然是配置失误）。

### 5.2 BodyLimitMiddleware（:64-114）

    class BodyLimitMiddleware:
        """ASGI 层请求体上限（F-09）。

        为什么不用 Content-Length 判断就够了：分块传输（chunked）根本没有这个头。
        所以这里包住 receive，边收边数，超了立刻回 413 并断开，不让它进内存。
        """

        def __init__(self, app, limit: int = MAX_BODY_BYTES):
            self.app = app
            self.limit = limit

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            declared = None
            for k, v in scope.get("headers") or []:
                if k == b"content-length":
                    try:
                        declared = int(v)
                    except ValueError:
                        declared = None
                    break
            if declared is not None and declared > self.limit:
                await self._too_large(send)
                return

            seen = 0

            async def limited_receive():
                nonlocal seen
                message = await receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body") or b"")
                    if seen > self.limit:
                        raise _BodyTooLarge()
                return message

            try:
                await self.app(scope, limited_receive, send)
            except _BodyTooLarge:
                await self._too_large(send)

两条路径缺一不可：有 `content-length` 就在进内存前直接拒（便宜且快），没有（chunked）就边收边数（拦住「慢慢喂」）。`_too_large` 回的是固定 JSON：

    class _BodyTooLarge(Exception):
        pass

    @staticmethod
    async def _too_large(send):
        body = b'{"ok":false,"error":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

手写 ASGI 而不是套一个现成 Starlette 中间件，是因为这里要知道「已收字节数」，而 Starlette 的现成方案都基于 `content-length`。

### 5.3 字段级上限（:120-140）

    class ClaimBody(BaseModel):
        task_id: str = Field(max_length=128)
        idempotency_key: str | None = Field(default=None, max_length=200)

    class HeartbeatBody(BaseModel):
        ticket: str = Field(max_length=256)
        status: str = Field(default="running", pattern="^(running|paused)$")
        detail: str = Field(default="", max_length=MAX_TEXT_FIELD)
        idempotency_key: str | None = Field(default=None, max_length=200)

    class ResultBody(BaseModel):
        ticket: str = Field(max_length=256)
        status: str = Field(pattern=SUBMIT_CHOICES)
        # F-09：单个自由文本字段也要有上限，否则一个字段就能吃满请求体配额
        log: str = Field(default="", max_length=MAX_TEXT_FIELD)
        diff: str = Field(default="", max_length=MAX_TEXT_FIELD)
        tests: str = Field(default="", max_length=MAX_TEXT_FIELD)
        note: str = Field(default="", max_length=MAX_TEXT_FIELD)
        idempotency_key: str | None = Field(default=None, max_length=200)

改动前这几行都是裸的 `task_id: str` / `detail: str = ""` / `log: str = ""`，没有任何长度约束。字段上限比整体上限小一半（4 MiB vs 8 MiB），这样「一个字段吃满」的请求即使侥幸没超整体上限，也会被 pydantic 以 422 挡下。`ticket` 给 256 够用（票是 HMAC 派生 + 前缀，不会更长），`task_id` 128 / `idempotency_key` 200 同理。

### 5.4 create_app 的边界 docstring（:161-171）

    def create_app(council: Council, watchdog_interval: float | None = None) -> FastAPI:
        """组装 ASGI app。本函数不负责监听地址（rc6 F-08）。

        它只返回 app，不 bind socket。想真正对外提供服务，必须走 serve()——
        serve() 里有 check_bind_host() 这道回环闸门。直接
        uvicorn.run(create_app(...), host="0.0.0.0") 会绕过闸门，这不是官方入口，
        也不被支持：部署只用 serve() / GUI / python -m bigfish。

        为什么要写这么长一段：审计指出「四个入口都校验了」不等于「app 本身安全」，
        与其假装封死，不如把边界说清楚，让后来的人别再自己 uvicorn.run。
        """

（原文里这几个标识符是 Markdown 行内代码格式，这里为可读性去掉了反引号。）

**为什么选择写文档而不是封死**：封死（比如在 `create_app` 里断言「必须由 `serve()` 调用」）会连带废掉 `fastapi.testclient.TestClient` 与 GUI 内嵌的构造路径——两者都不 bind socket。审计原话也是「应在 API 文档中明确」或「提供封装后的唯一启动接口」，前者本轮做了，后者留作后续改造（见第 15 节）。

### 5.5 挂中间件（:187）

    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)

**回归**：`TestBodyLimit` 两项，见 11.1。

---

## 6. `bigfish/gui.py`（33 insertions，9 deletions）

### 6.1 stop() 超时必须报错（:165-181）

    def stop(self, timeout: float = 15.0) -> None:
        """停 API 线程。超时未退必须报错，不能安静地把「还在跑」当成已停。

        F-05：原来 join 完就返回，调用方（恢复备份）会以为服务已经停了，
        于是继续替换数据库文件——正是这条路径会让恢复和写请求撞在一起。
        """
        if self._server is not None:
            self._server.should_exit = True
        th = self.thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=timeout)
            if th.is_alive():
                raise TimeoutError(
                    f"API 线程 {timeout:.1f} 秒内没有退出，仍可能有请求在写库，"
                    "已中止本次操作")
        self.stop_watcher()

改动前三处差异：原签名是 `def stop(self, timeout: float = 15.0):`（没有返回注解），`join` 之后直接跟 `self.stop_watcher()`，中间没有 `is_alive()` 检查。抛 `TimeoutError` 而不是自定义异常，是为了让「超时」与其他失败在调用方那里能被分别处理（`except TimeoutError` 是内置语义，不需要 import 本模块）。

### 6.2 is_running()（:182-184）

    def is_running(self) -> bool:
        th = self.thread
        return bool(th is not None and th.is_alive())

把「停没停」变成可断言的事实，测试里直接断言 `self.assertFalse(win.server.is_running())`，不用去摸 `thread` 私有字段。

### 6.3 pause_services 语义收紧（:1235-1264）

    def pause_services(self):
        if self._services_paused:
            return
        try:
            if self.server.receivers(self.server.started):
                self.server.started.disconnect(self.on_server)
        except Exception:
            pass
        try:
            self.server.stop()
            self.council.stop_watchdog()
        except Exception as exc:
            # 半停状态：把已经停掉的 API 接回来，然后如实报错中止恢复。
            try:
                self.server.start()
                self.council.start_watchdog(self.settings.watchdog_interval_sec)
            except Exception:                # pragma: no cover
                pass
            raise RuntimeError(
                f"停服失败，已中止本次操作（服务可能仍在运行）：{exc}") from exc
        self._services_paused = True

三处语义变化：① `self._services_paused = True` 从「操作开始前」挪到「两步都成功之后」（diff 里删除的是紧跟 `if self._services_paused: return` 之后那一行）；② 原来 `stop()` 与 `stop_watchdog()` 各自一个 `except Exception: pass`，现在合并成一个 `try` + 一个会 `raise` 的 `except`，不再吞异常；③ 失败时把已经停掉的服务接回来（尽力而为，接不回来也照抛）。

**为什么接回来**：`pause_services` 的调用方（恢复对话框）在拿到异常后会中止恢复并提示用户，此时如果 API 已经停了而 watchdog 没停，用户面对的是一个「半边服务」的界面——要么全停要么全起，才符合「中止」的含义。

### 6.4 两处 disconnect 前置判断（:1248、:1349）

    -            self.server.started.disconnect(self.on_server)
    +            if self.server.receivers(self.server.started):
    +                self.server.started.disconnect(self.on_server)

`:1349` 在 `MainWindow.closeEvent` 里，同理。这不是 F-05 的正面要求，是顺手消的噪音：重复断开时 PySide6 会打 `RuntimeWarning: libpyside: Failed to disconnect`，加个「还有没有接收者」的判断就没有了。两处外面还各有 ``try/except Exception: pass` 兜底，所以判断写法不必担心自己抛。

**回归**：`TestGuiPauseConfirms` 三项，见 11.1。三例都用真线程，没有用 TestClient 顶替（审计 F-05 明确要求「增加真实线程生命周期测试」）。

---

## 7. `bigfish/cli.py`（12 insertions，10 deletions）

### 7.1 restore 前置分支（:130-141）

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

改动前这一行是 `s, db, council = build(a.data_dir)`，无条件执行。

**F-04 的关键点**：`build()`（:33 里有 `db, council = open_council(s)`）会开库、建/迁 schema、跑密钥一致性自检，原来这些都发生在端口预检之前。改成按子命令分流后，`restore` 路径上端口被占时**一行数据库代码都不会执行**。用例 `test_busy_port_is_checked_before_database_open` 用 `patch("bigfish.cli.open_council")` 断言它没被调用，就是钉这一条。

这里用 `Settings.load()` 而不是 `build()`，是因为 `Settings.load()` 只读配置文件与默认值，不碰库。

### 7.2 restore 分支只剩 dump（:222-224）

    if a.cmd == "restore":
        dump(council.restore_from_bundle(a.path))
        return 0

原来这个分支里还塞着一段 R8 注释 + 端口探测 + `return 2`（共 9 行），本轮整段删除（逻辑搬到 7.1），避免两处判定漂移。注意 R8 那段注释里「先探端口」的说法现在只解决「另一个实例占着端口」这一种情况，真正的跨进程互斥已经交给 `locks.py`——所以注释一起删掉是对的，留着会误导。

**影响面**：自动化脚本在服务运行时调 `restore` 会拿到退出码 2（rc5 起就是 2，只是现在「拿不到 2 之前不碰库」）。

---

## 8. `bigfish/runtime.py`（6 insertions，5 deletions）

### 8.1 密钥自愈失败 = 拒绝启动（:18-21）

    from .core import Council, CouncilError
    ...
        try:
            ensure_key_consistency(db, settings.key_path)
        except Exception as exc:
            db.close()
            raise CouncilError("执行票密钥一致性校验失败，拒绝启动") from exc
        if key is None:
            key = load_or_create_key(settings.key_path)

改动前是 `except Exception:` + `pass`（注释「自愈失败不阻止启动，但会留在 audit / 日志里」），导入行也只有 `from .core import Council`。

三点：① `db.close()` 先关库——构造函数已经把租约登记上了（第 2.2 节），不关就把句柄留给 GC，同时拒绝启动的进程还挂着「我在用这个库」；② 抛 `CouncilError` 而不是裸 `RuntimeError`，让 `bigfish/cli.py:234` 的 `except (CouncilError, RestoreError, DatabaseVersionError)` 能接住并统一成「退出码 2 + 错误: …」；③ `from exc` 保留原始异常链，运维能看到到底是 OSError 还是别的。

**被推翻的原注释**：那句「自愈失败不阻止启动，但会留在 audit / 日志里」在两个方向上都不成立——这个 `except` 分支本身不写任何日志，而且 `ensure_key_consistency` 若在「换钥前」失败，磁盘上留着的是未核验的旧密钥，继续启动等于让旧执行票在恢复异常路径上继续有效。这正是 `cbf4d89` 修的东西，`f1d1ce0` 只是把异常类型收口成 `CouncilError`。

**回归**：`tests/test_v011_rc5_key_heal.py::TestStartupFailsClosed::test_open_council_refuses_to_start_when_key_repair_fails`（:140）。

---

## 9. `bigfish/roles.py`（1 insertion，1 deletion）

    -    EXECUTOR_DSH: "执行器（VPS DSH）",
    +    EXECUTOR_DSH: "执行器（VPS DSH）",

**⚠️ 这不是 rc6 的改动。** 它在 `b8f6de1`（rc5 发布提交）里已经是「VPS」，`git diff b8f6de1 HEAD -- bigfish/roles.py` 为空；只是相对 `c6d663d` 算 diff 时被带进来了。本文列出来只是为了解释 `--stat` 里那 2 行是什么。

---

## 10. `bigfish/__init__.py`（1 insertion，1 deletion）

    -__version__ = "0.1.1-rc5"
    +__version__ = "0.1.1-rc6"

**为什么**：版本号进位。这个值同时进 `/health` 的 `version`、备份包 manifest 的 `app_version` 与审计包 manifest。`SCHEMA_VERSION` / `BACKUP_FORMAT_VERSION` 都没动（仍是 4），所以 rc6 的库和包 rc5 的程序理论上能读——没有任何 schema 迁移，见第 14 节。

---

## 11. `tests/`

### 11.1 `tests/test_v011_rc6.py`（新增，280 insertions；280 行 / 10646 字节 / 16 个 test）

文件 docstring 直接对回审计编号：「v0.1.1-rc6 回归：跨进程排他、停服确认、审计包如实声明、请求体上限. 对应审计（review/rc5-hardening-20261120）里的 F-05 / F-06 / F-07 / F-09。」

四类：

| 类 | 行 | 项数 | 钉什么 |
| --- | --- | --- | --- |
| `TestCrossProcessLease` | :60 | 6 | F-06 |
| `TestAuditManifestHonesty` | :130 | 5 | F-07 |
| `TestBodyLimit` | :166 | 2 | F-09 |
| `TestGuiPauseConfirms` | :202 | 3 | F-05 |

**F-06 的六项**（`TestCrossProcessLease`）：构造失败不泄漏租约（:61，先造 schema+1 的库，断言打不开之后仍能 `exclusive_access`）、同文件第二个实例挡住恢复（:78）、没有别的写者时恢复成功（:89）、close 之后归还租约（:96）、有人持租约时抢不到排他（:106）、恢复成功后仍持租约（:117）。

    def test_second_database_on_same_file_blocks_restore(self):
        """另开一个 Database 实例（模拟另一个进程）后，恢复必须被拒。"""
        other = Database(self.db.path)
        try:
            bundle = self.root / "b.zip"
            export_bundle(self.db, bundle)
            with self.assertRaisesRegex(RestoreError, "还有进程在用这个数据库|正在恢复"):
                self.c.restore_from_bundle(bundle)
        finally:
            other.close()

这条最能说明「租约真的生效」：`other` 和 `self.db` 在同一个进程里，只是两个 `Database` 实例——POSIX 的 `flock` 是**按 fd** 判定的，同进程两次 `open()` 得到两个 fd，所以 `LOCK_EX` 照样被挡。也就是说这条用例证明的是「同一进程内两个实例也互斥」，跨进程结论由同一个机制保证（不同进程必然不同 fd）。

**F-07 的五项**：manifest 必须声明 `manual_review_required`、`mode == "pattern-based"`、`coverage` 里出现「无法保证」、`patterns` 里出现 `google-key`；`handling` 不许出现「无凭据」且必须出现「复核」；三类新凭据必须被脱敏；DSN 里的口令必须消失；`scan_secrets` 要报出种类名。

**F-09 的两项**：

    def test_declared_oversize_body_is_rejected(self):
        r = self.client.post("/api/v1/tasks/claim",
                             content=b"x" * 10,
                             headers={"content-length": str(64 * 1024 * 1024)})
        self.assertEqual(r.status_code, 413)

只发 10 字节却在头里声明 64 MiB——这条测的是「信不信 `content-length`」。httpx 会按头里的长度发请求，所以中间件在收到第一个 chunk 之前就拒了。

**F-05 的三项**：`test_stop_raises_when_thread_will_not_exit`（:233）自造一个睡 3 秒的 `Stuck` 线程塞进 `srv.thread`，然后 `srv.stop(timeout=0.05)` 断言 `TimeoutError`：

    class Stuck(threading.Thread):
        def run(self):
            time.sleep(3)

    srv = ApiServer(self.council, "127.0.0.1", 0)
    srv.thread = Stuck(daemon=True)
    srv.thread.start()
    try:
        with self.assertRaises(TimeoutError):
            srv.stop(timeout=0.05)
    finally:
        srv.thread.join(5)

`test_pause_services_stops_api_and_watchdog`（:250）断言 `_services_paused` 为真、`server.is_running()` 为假、watchdog 线程已死；`test_pause_is_not_set_when_stop_fails`（:263）把 `server.stop` 换成抛 `RuntimeError("模拟停不下来")` 的 lambda，断言报错含「已中止」且 `_services_paused` 仍为假。

### 11.2 `tests/test_v011_rc5_restore_authority.py`（64 insertions，0 deletions，24 项）

补 3 项，由 Astra 写（随 `cbf4d89` 并入）：

- `test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot`（:199）：先导出包，再把活库 `restore_epoch` 抬到 7，恢复旧包后断言 `out["restore_epoch"] == 8` 且 `db.restore_epoch() == 8`。
- `_assert_guard_lock_order`（:296）+ `test_query_guard_is_atomic_with_lock`（:340）/ `test_transaction_guard_is_atomic_with_lock`（:343）。

    def _assert_guard_lock_order(self, operation):
        checked = threading.Event()
        release = threading.Event()
        worker_errors = []
        original_guard = self.db._guard
        entered_maintenance_before_operation = False

        def blocked_guard():
            original_guard()
            checked.set()
            if not release.wait(5):
                raise RuntimeError("测试同步等待超时")

这个写法的价值在于它**能失败**：如果 `_guard()` 仍在 `acquire()` 之前，主线程就能在 worker 卡住时成功进入维护模式，`entered_maintenance_before_operation` 变真，断言直接挂——它复现的正是审计描述的那个交错序列。

### 11.3 `tests/test_v011_rc5_cli_guard.py`（15 insertions，1 deletion，4 项）

补 F-04 一项（:84），外加顺手修的 settimeout 噪音（:51-57）：

    def accept_loop():
        # settimeout 在 srv.close() 之后调用会 EINVAL/EBADF；先设好再进循环，
        # 并且把 OSError 一律当成「已经被关掉」正常退出（测试收尾不该刷栈）。
        try:
            srv.settimeout(0.2)
        except OSError:
            return
        while not stop.is_set():
            try:
                conn, _ = srv.accept()

原来 `srv.settimeout(0.2)` 是 `accept_loop` 的第一行、且无保护，`srv.close()` 之后再进一轮就抛 `OSError: [Errno 9] Bad file descriptor` 并刷栈。挪到受保护的 `try` 里就干净了。

### 11.4 `tests/test_v011_rc5_key_heal.py`（15 insertions，0 deletions，8 项）

补 F-03 一项（:140，`TestStartupFailsClosed`）。断言方式是双层：`assertRaisesRegex(RuntimeError, "密钥一致性校验失败")` 管「拒绝了」，`load_key.assert_not_called()` 管「拒绝之后没有继续用旧密钥」。

### 11.5 用例总数变化

grep -c 实测：`test_api` 6、`test_core` 15、`test_v011_p0` 16、`test_v011_p1` 24、`test_v011_p2` 24、`test_v011_p3` 20、`test_v011_rc3` 26、`test_v011_rc4` 33、`test_v011_rc5_cli_guard` 4、`test_v011_rc5_dispatch_id` 3、`test_v011_rc5_key_heal` 8、`test_v011_rc5_proxy` 9、`test_v011_rc5_receipts` 24、`test_v011_rc5_restore_authority` 24、`test_v011_rc5_review_audit` 13、`test_v011_rc6` 16，合计 **265**。

244 → 265 = +21：新文件 16 + `restore_authority` 3 + `cli_guard` 1 + `key_heal` 1。

---

## 12. `tools/` 与 `tests/test_v011_p3.py`：**这部分是 rc5 收尾时就有的，不是 rc6 新改**

相对 `c6d663d` 的 diff 里混进了 5 个文件的改动，全部逐个确认过「`b8f6de1` 到 HEAD 无变化」（`git diff --quiet b8f6de1 HEAD -- <file>` 对这五个文件全部返回 SAME）。它们是 rc5 收尾时把文档里的占位符还原成真实 Tailnet IP 的那一批（提交 `4281114` 等），rc6 一行没动。

| 文件 | 改动 | 真实内容 |
| --- | --- | --- |
| `tools/bfc-proxy.mjs` | 2/2 | 注释与 `TARGET_HOST` 默认值：`CLIENT-A-TAILNET-IP` → `100.64.0.12` |
| `tools/dev_setup.sh` | 0/0 | 仅权限位 `100644 → 100755`，内容零改动 |
| `tools/net_check.py` | 1/1 | docstring 里的示例环境变量值换成真实 IP |
| `tools/remote_check.py` | 3/3 | docstring 里三处 `CLIENT-B-TAILNET-IP` → `100.64.0.11` |
| `tests/test_v011_p3.py` | 2/2 | :61 与 :105 两处 `CLIENT-A-TAILNET-IP` → `100.64.0.12` |

`test_v011_p3.py` 那两处原文：

    -        s.api_host = "CLIENT-A-TAILNET-IP"
    +        s.api_host = "100.64.0.12"
             self.assertFalse(s.is_loopback, "Tailnet 100.x 也不是回环，必须拒绝")

    -            [sys.executable, "-m", "bigfish", "--headless", "--host", "CLIENT-A-TAILNET-IP",
    +            [sys.executable, "-m", "bigfish", "--headless", "--host", "100.64.0.12",

注意这两个用例的语义没变（都是「100.x 的 Tailnet 地址不是回环，必须拒绝」），只是把占位符换成一个真实的 100.x 地址，让断言更具体。这条改动**不属于任何 F-xx**。

**为什么要在文档里点明**：如果不说明，读者会把 `--stat` 里的 `tools/` 那几行读成「rc6 又改了代理」，从而误判改动面。rc6 对 `tools/` **零改动**。

---

## 13. 回归实测

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 全量单元测试 | `LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t .` | `Ran 265 tests` / `OK` / exit 0（两次：46.926s、67.168s） |
| 静态检查 | `pyflakes bigfish tests tools` | 无输出 / exit 0 |
| 编译检查 | `compileall -q bigfish tests tools` | exit 0 |
| 自检 | `tools/selfcheck.py` | 16/16（「通过 16 项，失败 0 项」） |
| GUI 冒烟 | `tools/gui_smoke.py` | 9/9（离屏，「GUI 冒烟通过」） |

全量日志里剩两条噪音，如实记录：

1. `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` —— 来自 `fastapi.testclient` 对 `httpx` 的依赖，第三方库自身，不是本仓库代码。
2. `ResourceWarning: unclosed file <_io.BufferedRandom name='/tmp/tmpXXXX/council.db.access.lock'>` —— 已定位到 `tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard::test_force_bypasses_the_guard`（该文件单跑复现 1 次，其余 15 个测试文件单跑均为 0）。该用例让 `cli.main --force` 走进 `open_council` → `Database` 打开租约 → 随后读包失败抛 `RestoreError`，用例没关 `db`，flock 句柄留到 GC。**是测试收尾瑕疵，不是产品缺陷，本轮未修**——修它要动那个用例的 try/finally，超出「只碰 rc6 相关文件」的范围。

---

## 14. schema 与兼容性影响

### 14.1 schema 没动

`SCHEMA_VERSION = 4`（`bigfish/db.py:24`）、`BACKUP_FORMAT_VERSION = 4`（`:25`）都没变，`MIGRATIONS` 也没加条目。所以：

- rc6 写的库，rc5 的程序能打开（不存在「版本过高」拒绝）。
- rc6 导出的备份包与审计包，格式版本仍是 4。
- 反过来，rc5 的库在 rc6 里也不需要迁移。

**但有一样东西变了**：库旁边多了个 `council.db.access.lock` 文件（POSIX）或 `council.db.access.<pid>.lock`（Windows）。它是 0 字节的锁文件，正常运行时会一直存在（进程持有 fd），进程退出后文件仍在但锁已释放。**不要手删**：删掉之后正在跑的进程仍然持有那把 flock（fd 还在），但名字对不上了，会让排障时看不出「谁在用」。

### 14.2 API 新增两种失败形态

| 场景 | rc5 | rc6 |
| --- | --- | --- |
| 请求体超过 `BFC_MAX_BODY_BYTES`（默认 8 MiB） | 收下并解析进内存 | `413` + `{"ok":false,"error":"request body too large"}` |
| 单个文本字段超过 `BFC_MAX_TEXT_FIELD`（默认 4 MiB） | 收下 | `422`（pydantic 校验） |

客户端侧要留意的是：原来「大请求慢慢收、最后 200/409」的路径，现在可能在收的过程中直接 413——这是刻意的，因为拒的理由是「不该收」，不是「处理失败」。

### 14.3 恢复的失败语义更严格

- 有别的进程/实例在用库 → 恢复抛 `RestoreError`（文案「还有进程在用这个数据库…」或「另一个进程正在恢复这个数据库」），POSIX 上由 `flock` 判定，不再靠端口探测猜。
- 恢复成功后本实例仍持租约；失败后也持（`finally` 里无条件 `resume_lease()`）。因此**恢复失败不会让本进程掉出「在用」名单**。
- 启动时密钥一致性校验失败 → `CouncilError("执行票密钥一致性校验失败，拒绝启动")`，CLI 退出码 2。原来会带着未核验的密钥继续跑。

### 14.4 审计包 manifest 结构变化

`redaction` 保持 rc5 的白名单结构不变；新增 `credential_scan` 键（六个子字段，见 3.5），`handling` 文案从「可外发：…」改成「可外发（需人工复核）：…」。拿旧结构解析审计包的脚本（比如断言 `handling` 以「可外发：」开头）需要同步。

### 14.5 CLI 行为

`bigfish restore` 在端口被占且没加 `--force` 时仍返回 2，但现在**返回 2 之前不会碰库**——原来会先把库打开、schema 迁完、密钥自检跑完，然后才拒绝。对自动化脚本的可见差异是：拒绝变快了，数据目录不会被无谓地动。

### 14.6 环境变量

| 变量 | 变化 |
| --- | --- |
| `BFC_MAX_BODY_BYTES` | 新增，默认 `8388608`（8 MiB）；显式空串退回默认 |
| `BFC_MAX_TEXT_FIELD` | 新增，默认 `4194304`（4 MiB）；同上 |

---

## 15. 本节没做的事

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）跑 rc6 的 265 项 | **未跑** | 本批次只在平台 A 执行；Windows 最后一次实测是 rc3 的 129 项 |
| `locks.py` 的 `msvcrt` 分支真机验证 | **未验证** | 全部标 `# pragma: no cover`；按 pid 分文件、`_probe_other_writers` 逐项试探、`release` 里 `os.unlink` 都只有代码路径 |
| GUI 真窗口验收 `pause_services` / `resume_services` | **未验收** | F-05 的三项都是单测（自造 `Stuck` 线程、替换 `server.stop` 为 lambda）；没有人在真窗口点过「恢复备份」看服务是否真停、真起、失败时是否真接回来 |
| 跨设备 MCP 闭环 | **未做** | 本轮所有证据都是单机；Tailscale 两侧真机 claim → heartbeat → submit 一次没跑 |
| F-08 之后的「唯一启动接口」 | **未做** | 只把边界写进 docstring，没做「必须经由 `serve()` 才能构造 app」的封装。真封死会连带影响 `TestClient` 与 GUI 内嵌构造，需要单独改造 |
| 审计包默认不导出 `task_versions.prompt` | **未做** | 审计建议「考虑默认不导出 prompt 正文或提供显式 opt-in」，本轮只做了如实声明（`fields_scanned` 点名含 prompt 正文 + `manual_review_required: True`），没改导出面 |
| `test_force_bypasses_the_guard` 的租约 ResourceWarning | **未修** | 测试没关 `db`，属收尾瑕疵（第 13 节） |
| `--force` 之后的数据一致性 | **无保障** | 逃生门的定义就是「明知道可能有另一个实例在写」；没有做替换后的分叉检测 |
| `exclusive_access(timeout)` 的等待语义 | **未实现** | 参数存在但没用，当前一律立即失败；留着是为了将来「等另一个 restore 收尾」有地方加 |

一句话总结 rc6：**把「我在用这个库」从一句口头约定变成了 OS 级的租约**——244 项里加的 21 项，基本都围着这一条，以及「失败时必须承认失败」这一条。
