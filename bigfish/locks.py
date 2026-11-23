"""跨进程「谁在用这个库」租约 + 恢复期排他锁（rc6 F-06）.

问题（审计 F-06）：restore 只和另一个 restore 互斥，管不住**普通 Database 实例**。
POSIX 上替换文件后旧连接继续写旧 inode（数据静默分叉），Windows 上直接替换失败。

办法：所有 Database 实例在打开时持一份**写者租约**（writer lease），恢复流程
必须拿到**排他访问锁**才能替换文件。

- POSIX：两者同一把 flock，写者 LOCK_SH（可多个），恢复 LOCK_EX（与任何写者互斥）。
- Windows：msvcrt 只有排他锁，没有共享锁。写者各自持有一个
  `<db>.access.<pid>.lock`（同一 pid 自然唯一），恢复逐个试探这些文件：
  能锁上说明那个 pid 已经退出（租约文件是残留），删掉继续；锁不上说明还活着，
  直接拒绝恢复。这不是共享锁，但得到的是同一个结论：有人在用，就不许换。

Windows 的一个坑（rc6 平台 B 实测）：msvcrt.locking 的字节区间锁是**按文件句柄**
的，同一进程用第二个句柄去锁同一区间会拿到 EACCES。而租约的语义本来就是
「每进程一份」，所以同一进程内的多个 Database 实例必须共享同一个句柄，用引用
计数决定什么时候真正释放——否则同进程开第二个实例会被误判成「有别人在用」，
把本该成功的用例打成 LeaseError。
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path


class LeaseError(RuntimeError):
    """拿不到访问锁：说明还有别的进程/实例在使用这个数据库。"""


_guard = threading.Lock()

# Windows 专用：锁文件路径 -> [文件句柄, 引用计数]。
# POSIX 不需要，因为 flock 可以对同一文件开多个句柄各自 LOCK_SH。
_win_leases: dict = {}


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
        self._key = None
        self.acquire()

    def acquire(self) -> None:
        if self._fh is not None:
            return
        target = _lock_file(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":                  # pragma: no cover - 仅 Windows
            key = str(target)
            with _guard:
                held = _win_leases.get(key)
                if held is not None:
                    # 同一进程已经在用这个库：共用句柄，只加计数。
                    held[1] += 1
                    self._fh = held[0]
                    self._key = key
                    return
                fh = open(target, "a+b")
                try:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    fh.close()
                    raise LeaseError(
                        f"无法登记数据库写者租约（{target}）：{exc}") from None
                _win_leases[key] = [fh, 1]
                self._fh = fh
                self._key = key
            return
        fh = open(target, "a+b")
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        except OSError as exc:
            fh.close()
            raise LeaseError(
                f"无法登记数据库写者租约（{target}）：{exc}") from None
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        if os.name == "nt":                  # pragma: no cover - 仅 Windows
            key, self._key = self._key, None
            if key is None:
                return
            with _guard:
                held = _win_leases.get(key)
                if held is None or held[0] is not fh:
                    return
                held[1] -= 1
                if held[1] > 0:
                    # 同进程还有别的实例在用，锁不能放。
                    return
                _win_leases.pop(key, None)
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass
            # 租约文件是「一个进程一个」，退出时要清掉，否则恢复端只能在
            # 试探锁时才知道它是残留。
            try:
                os.unlink(fh.name)
            except OSError:
                pass
            return
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def _probe_other_writers(db_path: Path) -> list:
    """Windows：列出还活着的写者租约文件。"""
    p = Path(db_path)
    alive: list = []
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
            try:
                if os.name != "nt":
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                else:                        # pragma: no cover - 仅 Windows
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass
