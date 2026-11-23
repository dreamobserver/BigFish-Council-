# 代码 Diff 摘要：bigfish-council v0.1.1-rc8

| 项 | 值 |
| --- | --- |
| 基线 | tag `v0.1.1-rc7` → `de255e9` |
| 本轮 | `031ebcf`（未合并） |
| 规模 | `12 files changed, 659 insertions(+), 83 deletions(-)` |
| 生成命令 | `git diff v0.1.1-rc7 HEAD --stat` |

本文件只写**实际改了哪几行、为什么这么改**，不复述概念。所有片段都是从工作树复制的原文，不是重写的示意。

---

## 1. `bigfish/locks.py`（+92 / −31）

### 1.1 新增进程级互斥锁

```python
_LOCAL: dict[str, list] = {}
_LOCAL_LOCK = threading.Lock()

# BFC-RC8 / R7-01：Windows 侧「取租约」整个过程（查已有句柄 → 打开文件 →
# 抢槽位 → 登记 _LOCAL）必须在同一把进程内互斥锁里完成。
_ACQUIRE_LOCK = threading.Lock()
```

两个锁的分工：`_LOCAL_LOCK` 只保护字典本身（读写 `_LOCAL`），`_ACQUIRE_LOCK` 保护整个「判定 → 抢槽 → 登记」流程。嵌套顺序固定为 `_ACQUIRE_LOCK` → `_LOCAL_LOCK`，不存在反向获取，也就没有死锁。

### 1.2 `_acquire_windows` 重写

改前（rc7，节选）：

```python
with _LOCAL_LOCK:
    held = _LOCAL.get(key)
    if held is not None:
        held[1] += 1
        ...
        return
fh = _open_rw(self._lock_path)          # ← 锁已释放
for i in range(SLOTS):                  # ← 两个线程会同时走到这里
    if _win_try_lock(fh, slot, 1):
        with _LOCAL_LOCK:
            _LOCAL[key] = [fh, 1, slot] # ← 后者覆盖前者
```

改后（rc8）：

```python
with _ACQUIRE_LOCK:
    with _LOCAL_LOCK:
        held = _LOCAL.get(key)
        if held is not None:
            held[1] += 1              # 同进程已在用，共享句柄
            self._fh = held[0]
            self._key = key
            self._slot = held[2]
            return
    fh = _open_rw(self._lock_path)
    try:
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
        raise LeaseError(...)
    except BaseException:
        try:
            fh.close()
        except OSError:
            pass
        raise
```

两处关键：整段串行（消除覆盖窗口）；`except BaseException` 里关句柄（`KeyboardInterrupt` 也要关，否则 Ctrl-C 一次漏一个）。

### 1.3 `release()` 用同一把锁

```python
if os.name == "nt":
    # 与 _acquire_windows 用同一把锁：否则「我正要把最后的引用减到 0」
    # 与新实例「我看到还有人在用，直接共享这个句柄」会交错，
    # 新实例会拿到一个马上被关闭的句柄。
    with _ACQUIRE_LOCK:
        with _LOCAL_LOCK:
            held = _LOCAL.get(key)
            if held is None or held[0] is not fh:
                # R7-01 修复后这条分支不可达；保留是为了绝不误放别人的锁。
                return
            held[1] -= 1
            ...
```

如果只改 acquire 不改 release，仍有一个窄窗口：线程 A 减到 0 准备关句柄，线程 B 在 acquire 里看到 `_LOCAL` 还有条目、直接共享 —— 共享到的是一个马上被关掉的句柄，B 之后所有 `msvcrt.locking` 都会失败。所以两边必须用同一把锁。

---

## 2. `bigfish/roles.py`（+18 / −6）

### 2.1 新增零权限哨兵

```python
DENIED_ROLE = "denied_unknown"

ALL_ROLES = (ARCHITECT_GPT, REVIEWER_GEMINI, HUMAN_OWNER, EXECUTOR_DSH,
             MOBILE_VIEWER, DENIED_ROLE)
ROLES = ALL_ROLES        # 兼容旧名字
```

`DENIED_ROLE` 故意放进 `ALL_ROLES`：`register_executor()` 的白名单是 `DB_ROLES`（不含它），所以注册路径拒绝；`ALL_ROLES` 里有它是为了让 `ROLE_LABELS` / 测试能枚举到它。

### 2.2 权限表补空集

```python
PERMISSIONS = {
    ...
    MOBILE_VIEWER: {"task.read", "event.read", "review.create"},
    # 空集是刻意的：认不出的身份不能因为「没写权限表」而默认得到任何能力。
    DENIED_ROLE: set(),
}
```

### 2.3 `role_for_db_row` 不再兜底

```python
    # 未知角色一律按最低权限处理
-   return EXECUTOR_DSH
+   return DENIED_ROLE
```

rc7 的注释也一并改掉：「未知 db_role 会落到 EXECUTOR_DSH，即没登记 = 拿到执行器权限」这句警示不再成立，新注释写明「缺一处只会让新角色被拒（失败关闭），不会提权（失败开放）」。

---

## 3. `bigfish/core.py`（+24 / −2）

### 3.1 新增 `_canonical_role_or_reject`

```python
def _canonical_role_or_reject(self, row: dict,
                              channel_role: str | None) -> str:
    """把库里的 role 映射成业务角色；认不出的身份直接拒绝（R7-02）。
    ...
    这里必须是普通方法而不是 staticmethod：拒绝时要写一条审计事件
    （auth.denied_unknown_role），走的是 self.log_event。
    """
    canonical = R.role_for_db_row(row.get("role"), row.get("name"),
                                  row.get("model"))
    if canonical == R.DENIED_ROLE:
        detail = f"身份 {row.get('id')} 的库内角色 {row.get('role')!r} 未登记，拒绝鉴权"
        # 只记身份 id，不记令牌
        self.log_event(None, "auth.denied_unknown_role", str(row.get("id")))
        raise CouncilError(detail)
    return canonical
```

`channel_role` 参数留着但没用：两个入口（`authenticate` 传令牌解析出的角色、`authenticate_any` 传 None）签名要一致，以后若要区分来源可以从这里加。

### 3.2 两个鉴权入口改调用

```python
-   d["role_canonical"] = R.role_for_db_row(d["role"], d["name"], d.get("model"))
+   d["role_canonical"] = self._canonical_role_or_reject(d, role)
```

`authenticate()` 与 `authenticate_any()` 各一处（`bigfish/core.py:210` / `:226`）。

---

## 4. `bigfish/server.py`（+8 / −2）

```python
 def _need(ex: dict, permission: str, action: str) -> None:
-    role = ex.get("role_canonical") or R.EXECUTOR_DSH
+    role = ex.get("role_canonical") or R.DENIED_ROLE
```

`dispatch_tool()` 同一处（`:565`）。这两个 `or` 是「鉴权过了但字典里没这个键」的最后一道，旧值等于把缺失当成执行器。

---

## 5. `bigfish/security.py`（+170 / −38，本轮最大）

### 5.1 强类型解码

```python
_WIN_TEXT_FALLBACKS = ("utf-8", "gbk", "cp936", "mbcs", "utf-16-le")

def _decode_console(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in _WIN_TEXT_FALLBACKS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")
```

顺序是先 UTF-8：Linux/macOS 与已设 `PYTHONUTF8=1` 的 Windows 都走这条；GBM 系（gbk/cp936）覆盖中文 Windows；`utf-16-le` 覆盖 PowerShell 某些管道输出；最后 `replace` 保证不抛。

### 5.2 `run_capture` 统一出口

```python
def run_capture(argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """跑一个外部命令并按控制台代码页兜底解码，永远不因编码抛异常。"""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"调用失败: {exc}"
    return (r.returncode if r.returncode is not None else 1,
            _decode_console(r.stdout or b""), _decode_console(r.stderr or b""))
```

注意 `capture_output=True` 且**不传** `text=True`：一旦传了 `text=True`，Python 就会在内部按 locale 解码，解码失败仍会抛，兜底逻辑根本轮不到执行。这是本轮最容易写错的一处。

### 5.3 全部调用点改走它

| 原调用 | 现在 |
| --- | --- |
| `secure_file` 里的 `icacls /inheritance:r`、`icacls /grant:r` | `run_capture` |
| `verify_file_restricted` 里的 `icacls <path>` | `_icacls_dump` → `run_capture` |
| `_current_user_candidates` 里的 `whoami` | `run_capture` + `text[0]` 解析 |
| `_current_user_sids` 里的 `whoami /user /fo csv /nh` | `run_capture` |

### 5.4 `_current_user_candidates` 扩到三来源

```python
user = os.environ.get("USERNAME") or ""
domain = os.environ.get("USERDOMAIN") or ""
add(user)
if user and domain:
    add(f"{domain}\\{user}")
try:
    import getpass
    add(getpass.getuser())
except Exception:
    pass
rc, stdout, _err = run_capture(["whoami"])
if rc == 0 and text:
    add(text[0])
```

这条只用于**匹配**（判断 icacls 输出里有没有当前用户），不用于授权。多来源只会让匹配更宽松；真正的授权仍然是文件 ACL 本身。

---

## 6. `bigfish/cli.py`（+2 / −1）

```python
-    ex.add_argument("--role", default="executor", choices=["executor", "reviewer", "admin"])
+    ex.add_argument("--role", default="executor",
+                    choices=["executor", "reviewer", "admin", "mobile"])
```

---

## 7. 测试侧改动

### 7.1 `tests/test_v011_p3.py`（+12 / −4）

```python
+        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
         r = subprocess.run(
             [...],
-            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
+            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
+            encoding="utf-8", errors="replace", env=env)
```

### 7.2 `tests/test_v011_rc3.py`（+8 / −4）

同款改法，`env` 里补 `PYTHONIOENCODING` / `PYTHONUTF8`，`subprocess.run` 补 `encoding="utf-8", errors="replace"`。

### 7.3 `tests/test_v011_rc5_proxy.py`（+8 / −2）

```python
-# 启动闸门判定的超时：给 3 秒，正常拒绝是毫秒级的，慢到这个数说明没在退出。
-STARTUP_TIMEOUT = 3.0
+STARTUP_TIMEOUT = 25.0
```

### 7.4 `tests/test_v011_rc5_restore_authority.py`（+11 / −4）

为适配 `roles.py` 的新哨兵：原来断言「未知角色 = 执行器」，改成断言「未知角色 = 被拒 + 写入审计事件」。**这是语义变更，不是放宽**——旧断言在新语义下本来就是错的。

### 7.5 `tests/test_v011_rc8.py`（新增 372 行 / 23 例）

五个测试类，行号见 `docs/FIX-MATRIX-v0.1.1-rc8.md` §三。

---

## 8. 没改的东西（明确声明）

- `bigfish/db.py`：`SCHEMA_VERSION` / `BACKUP_FORMAT_VERSION` / `MIGRATIONS` 全部未动。
- `bigfish/backup.py`：恢复流程未动。
- `bigfish/gui.py`：未动。
- `bigfish/executor_client.py`：未动。
- `bigfish/state.py`：状态机未动。
- `tools/`：本轮的 `tools/remote_check.py` / `tools/net_check.py` 两处 2 行改动来自 rc7 之后的文档提交，不在 rc8 范围内。

---

## 9. 第二轮补丁：`bigfish/console.py` 与心跳时钟粒度（CI 实测出来之后才有的）

上面的 1–8 节是第一次 CI 之前写的。第一次 CI（run `45170828326`）在 Windows 上挂了两个 job，补丁是本轮提交 `9941965`（4 files 163+/0−）。

### 9.1 为什么不在每个 tools 脚本里各修一遍

失败点看起来只是 `tools/selfcheck.py:79` 的一句 `print("健康检查")`。但同样的中文输出在 `tools/gui_smoke.py`、`tools/e2e_loopback.py`、`tools/net_check.py`、`tools/remote_check.py`、`bigfish/cli.py`、`bigfish/__main__.py` 里都有二三十处，逐个改是改不完的，而且以后新加的脚本还会踩。

所以修在**包的导入点**：只要 `import bigfish` 发生，控制台就先被体检一次。所有入口（`python -m bigfish`、`python -m bigfish.cli`、GUI、以及每个 tools 脚本开头的 `from bigfish import ...`）都自动覆盖。

### 9.2 `bigfish/console.py`（新增 92 行）

核心就三件事：判断当前编码能不能表示中文、不能表示才换、换失败也不能让程序起不来。

```python
_CANARY = "健康检查"

def _needs_upgrade(stream) -> bool:
    """当前流编码表示不了中文时才需要动手。"""
    enc = getattr(stream, "encoding", None)
    if not enc:
        return False
    try:
        _CANARY.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return True
    return False

def install_utf8_stdio() -> dict:
    if os.environ.get("BFC_NO_STDIO_FIX") == "1":
        return {"stdout": "skipped-env", "stderr": "skipped-env"}
    status = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            status[name] = "no-stream"
            continue
        if not hasattr(stream, "reconfigure"):
            status[name] = "not-text"
            continue
        if not _needs_upgrade(stream):
            status[name] = "ok"          # cp936 / UTF-8 一律不动
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            status[name] = "ok"
        except Exception as exc:          # noqa: BLE001 - 兜底不能反过来炸掉程序
            status[name] = "failed:" + type(exc).__name__
    return status
```

几处刻意的选择：

- **只在需要时动。** 中文 Windows（cp936）和 Linux（UTF-8）的编码本来就表示得了「健康检查」，换 encoding 会改变已有输出行为，属于无谓的副作用。
- **`errors="replace"` 而不是严格模式。** 真遇到表示不了的字符宁可输出一个 `?`，也不能让日志/自检在这一行崩掉。
- **全部异常吞掉。** 这是诊断兜底，不是业务逻辑；它自己失败时状态写进返回值，由调用方看。
- **`BFC_NO_STDIO_FIX=1` 可关。** 否则没法写「开关真的起作用」这条测试，也没法在特殊环境里保持原样。

安装点在包的顶层：

```python
# bigfish/__init__.py
__version__ = "0.1.1-rc8"
APP_NAME = "BigFish Council"
APP_NAME_CN = "大肥鱼议事厅"

from .console import install_utf8_stdio as _install_utf8_stdio  # noqa: E402

STDIO_STATUS = _install_utf8_stdio()
```

### 9.3 R8-03：把时间粒度从断言里摘出去

```python
before = self.db.one("SELECT expires_at, renewed_at FROM tickets WHERE id=?", (p["ticket_id"],))
# 时钟粒度：Windows 的 time.time() 精度约 15.6ms，同一 tick 内的两次心跳会让
# renewed_at「不前进」。这是系统时钟的分辨率，不是续租失效，所以先明确等过若干个
# tick，让这条断言测的是「续租有没有发生」，而不是「时钟够不够细」。
time.sleep(0.05)
r = self.c.heartbeat(p["ticket"], self.ex, "running")
after = self.db.one("SELECT expires_at, renewed_at FROM tickets WHERE id=?", (p["ticket_id"],))
```

断言本身还是 `assertGreater(after["renewed_at"], before["renewed_at"])`，**没有降级成 `assertGreaterEqual`**。降级等于「续租没发生也算过」，那这条测试就白写了。

### 9.4 新增 4 项（`tests/test_v011_rc8.py` 的 `TestConsoleEncodingFallback`）

| 测试 | 断言什么 |
| --- | --- |
| `test_cp1252_console_survives` | cp1252 子进程 rc=0，stdout 按 utf-8 解码后含「健康检查」与 `ENC=utf-8` |
| `test_gbk_console_is_left_alone` | cp936 子进程的输出是**原始 GBK 字节**，含 `b"ENC=gbk"` 与 `"健康检查".encode("gbk")` |
| `test_utf8_console_untouched` | UTF-8 子进程不被 reconfigure |
| `test_env_switch_actually_disables` | `BFC_NO_STDIO_FIX=1` 时必须复现 `UnicodeEncodeError` |

第二项是第一版写错的地方，值得单记：最初 `_run()` 用 `text=True, encoding="utf-8"` 让父进程解码子进程输出，结果 cp936 子进程写出来的 GBK 字节被父进程当 UTF-8 解，变成 U+FFFD，测试假红。改成 `capture_output=True` 拿 bytes、按被测编码各自断言。**子进程按哪种编码写就是被测行为，父进程替它解码等于把结论提前写进测试。**

### 9.5 这一轮有没有改产品语义

没有。`console.py` 只在编码不足时改变标准流的编码，不参与任何业务判断；心跳那条只动了测试。全量仍是 322 项，BFC 的状态机、权限、恢复语义一个字没动。

---

## 10. 复现这份 Diff

```bash
cd bigfish-council
git diff v0.1.1-rc7 HEAD --stat
git diff v0.1.1-rc7 HEAD -- bigfish/locks.py
git show 031ebcf --stat
```
