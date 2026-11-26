"""SQLite 存储层（WAL + 一致性快照 + 有序迁移 + 原子恢复）.

约定：
- 所有状态变更都在 tx()（BEGIN IMMEDIATE）中完成；嵌套 tx() 用 SAVEPOINT，
  且 BEGIN 失败必须释放内部锁（v0.1.1-rc3 P5-4）。
- 备份一律走 snapshot()（sqlite3 backup API），不复制正在使用的主库文件。
- 迁移只加列/建表，版本记录在 meta.schema_version；发现库版本高于程序版本
  一律拒绝打开（禁止静默降级，v0.1.1-rc3 P7-1）。
- 恢复走 restore_from()：同目录临时文件 → 校验 → os.replace 原子替换。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .locks import WriterLease

SCHEMA_VERSION = 4
BACKUP_FORMAT_VERSION = 4

REQUIRED_TABLES = (
    "meta", "tasks", "task_versions", "reviews", "approvals",
    "executors", "tickets", "runs", "artifacts", "events", "idempotency", "audit",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    goal          TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT '',
    risk          TEXT NOT NULL DEFAULT 'low',
    status        TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    prompt        TEXT NOT NULL DEFAULT '',
    control       TEXT NOT NULL DEFAULT '{}',
    executor_id   TEXT,
    blocked_reason TEXT,
    cancel_requested_at REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_versions (
    task_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    prompt     TEXT NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT '',
    risk       TEXT NOT NULL DEFAULT 'low',
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, version)
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    model      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    verdict    TEXT,
    content    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'manual',
    actor      TEXT NOT NULL DEFAULT 'human_owner',
    revoked_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    decision   TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT 'human_owner',
    evidence   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS executors (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'executor',
    model      TEXT,
    token_hash TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    disabled_reason TEXT,
    created_at REAL NOT NULL,
    last_seen  REAL
);

CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    executor_id    TEXT NOT NULL,
    token_hash     TEXT NOT NULL,
    epoch          INTEGER NOT NULL DEFAULT 0,
    issued_at      REAL NOT NULL,
    expires_at     REAL NOT NULL,
    max_expires_at REAL,
    renewed_at     REAL,
    revoked_at     REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    ticket_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    executor_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    REAL NOT NULL,
    last_beat_at  REAL,
    finished_at   REAL,
    exit_note     TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key        TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    request_hash TEXT NOT NULL DEFAULT '',
    response   TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id, version);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_task ON tickets(task_id);
CREATE INDEX IF NOT EXISTS idx_tickets_executor ON tickets(executor_id);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency(expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at);
"""

# 增量迁移：只加列 / 建表，不删不重建（生产库升级安全）
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE reviews ADD COLUMN actor TEXT NOT NULL DEFAULT 'human_owner'",
        "ALTER TABLE reviews ADD COLUMN revoked_at REAL",
        "ALTER TABLE approvals ADD COLUMN actor TEXT NOT NULL DEFAULT 'human_owner'",
        "ALTER TABLE approvals ADD COLUMN evidence TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tickets ADD COLUMN max_expires_at REAL",
        "ALTER TABLE tickets ADD COLUMN renewed_at REAL",
        "ALTER TABLE idempotency ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE idempotency ADD COLUMN expires_at REAL",
        """CREATE TABLE IF NOT EXISTS audit (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL,
            target     TEXT NOT NULL DEFAULT '',
            detail     TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )""",
        # 补建：最新 SCHEMA 里有，但旧库里不存在（B1）
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at)",
    ],
    3: [
        # 审查身份绑定到不可随意改的列，不再靠 name 字符串猜
        "ALTER TABLE executors ADD COLUMN model TEXT",
        # 执行票记录签发时的密钥世代：恢复轮换密钥后旧票一律不认
        "ALTER TABLE tickets ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0",
        # 取消请求时间戳：CANCELLING 状态下用于判断执行器是否已失联
        "ALTER TABLE tasks ADD COLUMN cancel_requested_at REAL",
        "CREATE INDEX IF NOT EXISTS idx_tickets_executor ON tickets(executor_id)",
        "CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency(expires_at)",
    ],
    4: [
        # R1：恢复后所有远端身份一律禁用，必须记下「为什么被禁」供人工核对
        "ALTER TABLE executors ADD COLUMN disabled_reason TEXT",
        "CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled)",
    ],
}


class DatabaseVersionError(RuntimeError):
    """数据库版本高于程序版本：拒绝打开，绝不静默降级。"""


class MaintenanceError(RuntimeError):
    """数据库处于维护模式（正在从备份恢复），普通读写一律拒绝。

    R8：恢复期间必须有人对「谁能写」负责。这个异常是唯一的拒绝信号，
    上游（HTTP / MCP / GUI）应当把它翻译成 503 + 稍后重试，而不是 500。
    """


class Database:
    """线程安全的 SQLite 封装（单写连接 + 显式事务）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        # F-06：打开即登记写者租约。恢复流程要拿排他锁，因此「有人在用」这件事
        # 必须在打开那一刻就可见，而不是靠事后探端口去猜。
        self._lease = WriterLease(self.path)
        # R8：维护模式。设置后所有普通读写立刻报 MaintenanceError（fail-fast），
        # 设置前先用 self._lock 做一次屏障等待，确保没有正在进行的写事务。
        self._maint_depth = 0
        self._maint_reason = ""
        self._maint_bypass = threading.local()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")

        # 先只读识别版本，再决定能不能碰这个文件（B1）。
        # 历史缺陷：这里一上来就 PRAGMA journal_mode=WAL——那会改写文件头并建 -wal，
        # 对于「未来版本的库」我们本该只读拒绝，结果先把人家文件动了。
        # 注意：构造函数抛错也要把写者租约放掉，否则这个句柄泄漏到 GC，
        # 下一个进程会以为「还有人用」（ResourceWarning 也是这么冒出来的）。
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

    def _peek_schema_version(self) -> int:
        """只读探测 schema 版本：不建表、不写 meta、不改 journal_mode。

        返回 0 表示「全新库 / 还没有 meta 表」。
        """
        try:
            row = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
        except sqlite3.DatabaseError:
            raise
        if row is None:
            return 0
        try:
            got = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError:
            return 0
        if got is None:
            return 0
        try:
            return int(got[0])
        except (TypeError, ValueError):
            return 0

    # ---------- 维护模式（R8） ----------
    def _guard(self) -> None:
        """维护模式下拒绝普通访问；内部恢复流程用 _maint_bypass 放行。"""
        if self._maint_depth <= 0:
            return
        if getattr(self._maint_bypass, "on", False):
            return
        raise MaintenanceError(
            f"数据库正在维护（{self._maint_reason or '正在从备份恢复'}），"
            "本次操作未执行，请稍后重试")

    def enter_maintenance(self, reason: str = "", timeout: float = 15.0) -> None:
        """进入维护模式：先排空在途写事务（屏障），再对后续访问 fail-fast。

        为什么先抢锁：Database.tx() 在整个事务期间持有 self._lock，所以拿到锁
        的这一刻就已经没有正在进行的写事务了。这就是「排空」，不是靠 sleep 猜。
        """
        got = self._lock.acquire(timeout=timeout)
        try:
            if not got:
                raise MaintenanceError(
                    "有写事务在超时时间内没有结束，无法排空进入维护模式")
            self._maint_depth += 1
            if reason:
                self._maint_reason = reason
        finally:
            if got:
                self._lock.release()

    def exit_maintenance(self) -> None:
        with self._lock:
            self._maint_depth = max(0, self._maint_depth - 1)
            if self._maint_depth == 0:
                self._maint_reason = ""

    @contextlib.contextmanager
    def maintenance(self, reason: str = "", timeout: float = 15.0):
        """with db.maintenance("恢复备份"): ... —— 无论成败都会退出维护模式。"""
        self.enter_maintenance(reason, timeout)
        try:
            yield self
        finally:
            self.exit_maintenance()

    @contextlib.contextmanager
    def _allow_during_maintenance(self):
        """仅供恢复流程自身使用：在维护模式下仍能读写本连接。"""
        prev = getattr(self._maint_bypass, "on", False)
        self._maint_bypass.on = True
        try:
            yield self
        finally:
            self._maint_bypass.on = prev

    def in_maintenance(self) -> bool:
        return self._maint_depth > 0

    @property
    def maintenance_reason(self) -> str:
        """维护原因（供 MCP 的 503 文案复用，不给外部看内部字段）。"""
        return self._maint_reason

    # ---------- 基础 ----------
    def init_schema(self) -> None:
        """初始化 / 升级 schema（B1）。

        顺序很重要：先看当前版本 → 旧库先迁移 → 最后才跑最新 SCHEMA。
        历史缺陷：原来先 executescript(SCHEMA) 再迁移，导致 v1 库在
        「CREATE INDEX ... idempotency(expires_at)」处直接炸 no such column，
        迁移根本没机会执行。
        """
        with self._lock:
            current = self._peek_schema_version_locked()
            if current == 0:
                # 全新库（或空文件）：直接建最新结构
                self._conn.executescript(SCHEMA)
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('restore_epoch', '0')")
            else:
                # 已有库：先按版本有序迁移，再补最新 SCHEMA 里新增的索引/表
                self._migrate_locked(current)
                self._conn.executescript(SCHEMA)
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('restore_epoch', '0')")
            if self._peek_schema_version_locked() != SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"schema 版本未达到预期：当前 "
                    f"{self._peek_schema_version_locked()} / 期望 {SCHEMA_VERSION}")

    def _peek_schema_version_locked(self) -> int:
        """连接已持有锁时的版本读取（不建表不写库）。"""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if row is None:
            return 0
        got = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if got is None:
            return 0
        try:
            return int(got[0])
        except (TypeError, ValueError):
            return 0

    def schema_version(self) -> int:
        try:
            row = self.one("SELECT value FROM meta WHERE key='schema_version'")
        except sqlite3.DatabaseError:
            return 0          # 全新／空库还没有 meta 表
        return int(row["value"]) if row else 0

    def restore_epoch(self) -> int:
        """当前恢复世代。空库（还没有 meta 表）视为 0，不抛异常。"""
        try:
            row = self.one("SELECT value FROM meta WHERE key='restore_epoch'")
        except sqlite3.DatabaseError:
            return 0
        return int(row["value"]) if row else 0

    def bump_restore_epoch(self) -> int:
        """恢复一次数据库就把世代 +1（不随旧快照回滚）。"""
        with self.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key='restore_epoch'").fetchone()
            nxt = (int(row["value"]) if row else 0) + 1
            c.execute("UPDATE meta SET value=? WHERE key='restore_epoch'", (str(nxt),))
        return nxt

    def _migrate_locked(self, current: int | None = None) -> None:
        if current is None:
            current = self._peek_schema_version_locked()
        if current > SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"数据库 schema 版本 {current} 高于本程序支持的 {SCHEMA_VERSION}，"
                "拒绝打开（升级程序，不要降级数据库）")
        if current == SCHEMA_VERSION:
            return
        # 每一步迁移单独提交，失败就停在原版本，不留半迁移状态
        for ver in range(current + 1, SCHEMA_VERSION + 1):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for sql in MIGRATIONS.get(ver, []):
                    try:
                        self._conn.execute(sql)
                    except sqlite3.OperationalError as exc:
                        # 列已存在（重复执行）视为成功
                        if "duplicate column name" in str(exc).lower():
                            continue
                        raise
                self._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                                   (str(ver),))
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        # 锁与维护标志必须在同一个临界区内检查，避免「先过 guard、后等锁」的竞态。
        with self._lock:
            self._guard()
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            self._guard()
            return self._conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            self._guard()
            return self._conn.execute(sql, tuple(params))

    class _Tx:
        """写事务：外层 BEGIN IMMEDIATE，内层 SAVEPOINT（可嵌套）。"""

        def __init__(self, db: "Database"):
            self.db = db
            self.outer = False
            self.sp = ""

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

        def __exit__(self, exc_type, exc, tb):
            self.db._depth -= 1
            try:
                if self.outer:
                    if exc_type is None:
                        self.db._conn.execute("COMMIT")
                    else:
                        self.db._conn.execute("ROLLBACK")
                else:
                    if exc_type is None:
                        self.db._conn.execute(f"RELEASE {self.sp}")
                    else:
                        self.db._conn.execute(f"ROLLBACK TO {self.sp}")
                        self.db._conn.execute(f"RELEASE {self.sp}")
            finally:
                self.db._lock.release()
            return False

    def tx(self) -> "Database._Tx":
        """写事务：BEGIN IMMEDIATE，失败自动回滚并释放锁。"""
        return Database._Tx(self)

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
            except sqlite3.DatabaseError:
                pass
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass

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

    def _reopen(self) -> None:
        """重建底层连接（恢复备份后使用）。

        恢复流程会 close() 旧连接、替换数据库文件，再调用本方法重建。
        重建后事务深度清零，PRAGMA 与 row_factory 全部恢复，避免出现
        「库已换但连接还指向旧 inode」的半完成状态（P1-4.1）。
        """
        with self._lock:
            self._depth = 0
            self._closed = False
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass
            conn = sqlite3.connect(str(self.path), isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn

    # ---------- 快照 / 恢复 ----------
    def snapshot(self, dest: Path) -> Path:
        """用 sqlite3 backup API 生成一致性快照（含 WAL 中未 checkpoint 的数据）。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
            finally:
                target.close()
        return dest

    def integrity(self, path: Path | None = None) -> tuple[bool, str]:
        p = Path(path) if path else self.path
        conn = sqlite3.connect(str(p))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            verdict = (row[0] if row else "") or ""
            return verdict.lower() == "ok", verdict
        finally:
            conn.close()

    def table_counts(self, path: Path | None = None) -> dict[str, int]:
        p = Path(path) if path else self.path
        conn = sqlite3.connect(str(p))
        try:
            out: dict[str, int] = {}
            for t in REQUIRED_TABLES:
                try:
                    out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.DatabaseError:
                    out[t] = -1
            return out
        finally:
            conn.close()

    def audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        from .security import scrub
        self.execute(
            "INSERT INTO audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, action, scrub(target)[:200], scrub(detail)[:2000], now()),
        )

    def audits(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> float:
    return time.time()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default
