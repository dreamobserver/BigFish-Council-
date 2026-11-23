"""跨进程「谁在用这个库」租约 + 恢复期排他锁.

问题（rc6 F-06 / rc7 BFC-01）：restore 只和另一个 restore 互斥，管不住
**普通 Database 实例**。POSIX 上替换文件后旧连接继续写旧 inode（数据静默分叉），
Windows 上直接替换失败。

rc6 的 Windows 实现是「先扫一遍别的 pid 的租约文件，再去锁另一把文件」——
扫描和上锁之间，新写者可以插进来把库打开。这就是审计 BFC-01 说的 TOCTOU：
两把锁不是同一个内核对象，所以「我检查过了没人」这句话在上锁那一刻已经过期。

rc7 改成**一个内核对象 + 原子语义**，再没有「检查」与「生效」之间的空档：

- POSIX：访问锁文件上，写者持 flock **LOCK_SH**（可多个），恢复持 **LOCK_EX**
  （与任何写者互斥）。flock 本身就是原子的，不需要扫谁。
- Windows：msvcrt 只有排他区间锁、表达不了共享锁，所以改用**槽位模型**：
  访问锁文件预留 SLOTS 个字节，每个写者独占其中 1 个字节（从自己的 pid
  开始往后找空槽），恢复端一次性锁住 **整段 SLOTS 字节**。
  任何一个写者占着槽，恢复就锁不上；反过来恢复持着整段时，写者连一个槽
  都拿不到。同一文件、同一段字节区间，由 Windows 内核保证互斥——同样没有
  「检查完再上锁」的时间窗。

同一进程内多个 Database 实例的问题（rc6 平台 B 实测过）：Windows 的区间锁是
**按文件句柄**记的，同一进程第二个句柄去锁同一区间会 EACCES。而租约语义是
「每进程一份」，所以 Windows 侧用 _LOCAL 表把同进程实例共享成一个句柄并做
引用计数。POSIX 的 LOCK_SH 天然允许多个句柄共享，不需要这张表。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

# Windows 槽位总数：同时最多这么多进程持有写者租约。超出属于异常用法，
# 宁可明确报错也不要静默放行。
SLOTS = 64

DEFAULT_RESTORE_TIMEOUT = 5.0        # 恢复：给上层把连接关掉留一点余量

# Windows 专用：访问锁文件绝对路径 -> [文件句柄, 引用计数, 槽位号]
# POSIX 不需要，因为 flock 允许多个句柄各自 LOCK_SH。
_LOCAL: dict[str, list] = {}
_LOCAL_LOCK = threading.Lock()


class LeaseError(RuntimeError):
    """拿不到访问锁：说明还有别的进程/实例在使用这个数据库。"""


def access_lock_path(db_path: Path) -> Path:
    p = Path(db_path)
    return p.with_name(p.name + ".access.lock")


def restore_gate_path(db_path: Path) -> Path:
    p = Path(db_path)
    return p.with_name(p.name + ".restore.lock")


def _open_rw(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+b")


def _posix_flock(fh, exclusive: bool) -> bool:
    """非阻塞加锁；成功返回 True。"""
    try:
        import fcntl
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _posix_unlock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _win_try_lock(fh, offset: int, nbytes: int) -> bool:
    """Windows：在 [offset, offset+nbytes) 上做非阻塞独占区间锁。"""
    try:
        import msvcrt
        fh.seek(offset)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, nbytes)
        return True
    except OSError:
        return False


def _win_unlock(fh, offset: int, nbytes: int) -> None:
    try:
        import msvcrt
        fh.seek(offset)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, nbytes)
    except OSError:
        pass


class WriterLease:
    """一个 Database 实例持有到 close() 为止的写者身份。

    必须在**打开 sqlite 连接之前**取得（Database.__init__ 就是这么做的），
    否则「库已经被我打开」这件事对恢复端不可见。
    """

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self._lock_path = access_lock_path(self.path)
        self._fh = None
        self._key = None
        self._slot = None
        self.acquire()

    # ------------------------------------------------------------------
    def acquire(self) -> None:
        if self._fh is not None:
            return
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            self._acquire_windows()
            return
        fh = _open_rw(self._lock_path)
        if not _posix_flock(fh, exclusive=False):
            fh.close()
            raise LeaseError(
                "数据库正在恢复或维护中，暂时不能打开（"
                + str(self._lock_path) + "）。请稍后重试。")
        self._fh = fh
        self._key = str(self._lock_path.resolve())

    def _acquire_windows(self) -> None:           # pragma: no cover - 仅 Windows
        key = str(self._lock_path.resolve())
        with _LOCAL_LOCK:
            held = _LOCAL.get(key)
            if held is not None:
                held[1] += 1                  # 同进程已在用，共享句柄
                self._fh = held[0]
                self._key = key
                self._slot = held[2]
                return
        fh = _open_rw(self._lock_path)
        # 从自己的 pid 开始往后找空槽；全满说明要么恢复正在持整段，要么
        # 真有 SLOTS 个写者——两种都不该放行。
        start = os.getpid() % SLOTS
        for i in range(SLOTS):
            slot = (start + i) % SLOTS
            if _win_try_lock(fh, slot, 1):
                with _LOCAL_LOCK:
                    _LOCAL[key] = [fh, 1, slot]
                self._fh = fh
                self._key = key
                self._slot = slot
                return
        fh.close()
        raise LeaseError(
            "数据库正在被恢复，或同时使用的实例过多，暂时不能打开（"
            + str(self._lock_path) + "）。请稍后重试。")

    # ------------------------------------------------------------------
    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        key, self._key = self._key, None
        slot, self._slot = self._slot, None
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            with _LOCAL_LOCK:
                held = _LOCAL.get(key)
                if held is None or held[0] is not fh:
                    return
                held[1] -= 1
                if held[1] > 0:
                    return                    # 同进程还有实例在用，锁不能放
                _LOCAL.pop(key, None)
            _win_unlock(fh, slot, 1)
        else:
            _posix_unlock(fh)
        try:
            fh.close()
        except OSError:
            pass
        # 绝不删除锁文件：unlink 之后新来的实例会在**新的 inode** 上加锁，
        # 而老实例还锁着旧 inode，两边各用各的锁，互斥就此失效。
        # （rc7 一度这么写过，tests.test_v011_rc6 里「另开实例阻止恢复」
        # 当场失败，就是这个原因。）

    def __del__(self):                            # pragma: no cover - 兜底
        try:
            self.release()
        except Exception:
            pass


@contextlib.contextmanager
def exclusive_access(db_path: Path, timeout: float = DEFAULT_RESTORE_TIMEOUT):
    """恢复期间独占访问：还有写者就失败，绝不「先替换后打架」。

    两道锁各管一件事，顺序固定：

    1. restore_gate 排他 —— 只用来区分「另一个恢复在跑」和「有写者在用」，
       给的是能看懂的报错，不是正确性来源。
    2. 访问锁排他 —— 正确性在这里。它与写者的共享锁（POSIX LOCK_SH /
       Windows 槽位）是同一个内核对象，所以不存在「检查完到上锁之间」的窗口。
    """
    p = Path(db_path)
    gate_path = restore_gate_path(p)
    lock_path = access_lock_path(p)

    gate = _open_rw(gate_path)
    if os.name == "nt":                           # pragma: no cover - 仅 Windows
        if not _win_try_lock(gate, 0, 1):
            gate.close()
            raise LeaseError("另一个进程正在恢复这个数据库，请稍后重试")
    else:
        if not _posix_flock(gate, exclusive=True):
            gate.close()
            raise LeaseError("另一个进程正在恢复这个数据库，请稍后重试")

    fh = _open_rw(lock_path)
    deadline = time.monotonic() + max(0.0, timeout)
    ok = False
    while True:
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            ok = _win_try_lock(fh, 0, SLOTS)
        else:
            ok = _posix_flock(fh, exclusive=True)
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    if not ok:
        fh.close()
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            _win_unlock(gate, 0, 1)
        else:
            _posix_unlock(gate)
        gate.close()
        raise LeaseError(
            "还有进程在用这个数据库，拒绝替换。请先关闭 GUI / 停掉 headless 服务。")

    try:
        yield
    finally:
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            _win_unlock(fh, 0, SLOTS)
        else:
            _posix_unlock(fh)
        try:
            fh.close()
        except OSError:
            pass
        if os.name == "nt":                       # pragma: no cover - 仅 Windows
            _win_unlock(gate, 0, 1)
        else:
            _posix_unlock(gate)
        try:
            gate.close()
        except OSError:
            pass
        # 同样的理由不删 gate 文件：删了就等于把互斥换成「看谁先创建」。
