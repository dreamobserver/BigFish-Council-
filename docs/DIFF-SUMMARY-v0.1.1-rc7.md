# 差异说明：bigfish-council v0.1.1-rc7

分支 `fix/v0.1.1-rc7-hardening`，基线 HEAD `c0c54bc`。rc6 的发布点是 tag `v0.1.1-rc6` → commit `a2e9275751`（`parents=0` 的根提交，BFC-08 指的就是它）。本轮改动已提交到 `fix/v0.1.1-rc7-hardening`（提交号用 `git log -1 --format=%h` 取）。下面所有行号取提交时的文件状态；若之后有新提交导致行号漂移，请以 `git log` 定位提交、以 `grep` 重新取行号为准。

## git diff --stat 原文

```text
14 files changed, 609 insertions(+), 275 deletions(-)

AUDIT.md 81/14   CHANGELOG.md 65/0   README.md 46/53
bigfish/__init__.py 1/1   backup.py 14/6   cli.py 32/13   core.py 6/3
gui.py 9/9   locks.py 202/145   roles.py 18/7   server.py 91/16
tests/test_v011_p2.py 29/4   test_v011_rc4.py 9/2   test_v011_rc5_review_audit.py 6/2
```

## 差异口径

任务书与 `docs/.rc7-facts.md` 记的是 `14 文件 +598/−271`，实测 `+609/−275`（差 `+11/−4`）。原因见 `FIX-MATRIX-v0.1.1-rc7.md` 补充说明一：`--stat` 的图形列是改动块长度，不是增删行数。本文一律以 `git diff --numstat` 为准。

| 口径 | 文件数 | insertions | deletions |
| --- | --- | --- | --- |
| 全部 | 14 | 609 | 275 |
| `bigfish/` + `tests/`（代码） | 11 | 417 | 208 |
| `bigfish/` 单独 / `tests/` 单独 | 8 / 3 | 373 / 44 | 200 / 8 |
| 三份文档（AUDIT + CHANGELOG + README） | 3 | 192 | 67 |

另有 4 个未跟踪项不在 diff 里，属本轮产物：`requirements.lock`、`.github/workflows/ci.yml`、`tests/test_v011_rc7.py`、`docs/.rc7-facts.md`（末项是写文档的数据源，不是交付物）。

**平台状态**：平台 A（Debian 12 / Python 3.11.2 / SQLite 3.40.1）跑出 `295 项 / OK / RC=0 / 60.507s`，`pyflakes` 与 `compileall` RC=0，`selfcheck` 16/16，`gui_smoke` 9/9；平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）**rc7 的 294 项也已跑通**：`Ran 294 tests in 74.232s` / `OK (skipped=1)` / 退出码 0（收尾补的第 295 项未在 Windows 重跑）。本轮最大的一处改动（`locks.py` 整文件重写）正好落在 Windows 专属分支上——槽位模型、`_LOCAL` 共享句柄、`msvcrt` 区间锁全在 `if os.name == "nt"` 里面，这部分现在有真机执行记录。rc6 已经证明这类路径不可能靠 Linux 验出来（缺陷 17 就是 Windows 首跑才炸），所以平台 B 这一跑才算把 BFC-01 的验证闭环补上。

**用例构成**：`test_api` 6、`test_core` 15、`p0` 16、`p1` 24、`p2` 26、`p3` 20、`rc3` 26、`rc4` 33、`rc5_cli_guard` 4、`rc5_dispatch_id` 3、`rc5_key_heal` 8、`rc5_proxy` 9、`rc5_receipts` 24、`rc5_restore_authority` 24、`rc5_review_audit` 13、`rc6` 16、`rc7` 28 = **295**。rc6 是 265，`+30` = 新增 `test_v011_rc7.py` 28 项 + `test_v011_p2.py` 净增 2 项；另两个测试文件是改写既有用例，项数不变。

---

## 0. 怎么读这篇 diff

| 审计项 | 主改文件 | 节号 |
| --- | --- | --- |
| BFC-01 Windows 恢复 TOCTOU | `bigfish/locks.py`（整文件重写） | 1 |
| BFC-02 MCP 字段级 maxLength | `bigfish/server.py` | 2.3 / 2.4 |
| BFC-03 create_app 绕过监听闸门 | `bigfish/server.py`、`bigfish/gui.py` | 2.2 / 2.5 / 7 |
| BFC-04 manifest 布尔承诺 | `bigfish/backup.py` | 6 |
| BFC-05 / BFC-06 CLI 关库与预留端口 | `bigfish/cli.py` | 5 |
| BFC-07 发布工程不闭环 | `requirements.lock`、`.github/workflows/ci.yml` | 10 |
| BFC-08 tag 指向根提交 | 无代码改动 | 13 |
| MOBILE 手机端只读角色 | `bigfish/roles.py`、`bigfish/core.py` | 3 / 4 |

片段一律从工作树复制、行号是真实行号，不做简化；比正文需要的长，是为了让「改前是什么样」可核对。

---

## 1. `bigfish/locks.py`（202 / 145，整文件重写）

265 行 / 10092 字节（旧版 208 行）。本轮唯一一处结构性重写。

### 1.1 旧实现：检查与上锁在两把不同的锁上

```python
140|def _probe_other_writers(db_path: Path) -> list:
141|    """Windows：列出还活着的写者租约文件。"""
144|    for f in p.parent.glob(p.name + ".access.*.lock"):        # pragma: no cover
151|                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

172|            others = _probe_other_writers(db_path)
173|            if others:
174|                raise LeaseError(
175|                    "还有进程在用这个数据库（" + ", ".join(others) +
176|                    "），拒绝替换。请先关闭 GUI / 停掉 headless 服务。")
180|                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
```

（以上是 `git show HEAD:bigfish/locks.py` 的行号，rc7 前版本。）

第 172 行扫的是 `*.access.<pid>.lock`（一堆按 pid 分开的文件），第 180 行锁的是 `council.db.access.lock`（**另一个文件**）。两个 inode、两套锁状态。第 172 行返回「没人」之后、第 180 行上锁之前，新 `Database` 完全可以在自己那个 pid 文件上建成锁——扫描结论在拿到手那一刻就过期了。这不是概率，是逻辑上必然的窗口。旧版写者退出时还靠 `unlink` 清租约文件（旧 `:122-127`），删文件本身又是一个竞态源。

### 1.2 新实现：一个内核对象

```python
37|SLOTS = 64
43|_LOCAL: dict[str, list] = {}
44|_LOCAL_LOCK = threading.Lock()
51|def access_lock_path(db_path: Path) -> Path:
53|    return p.with_name(p.name + ".access.lock")
56|def restore_gate_path(db_path: Path) -> Path:
58|    return p.with_name(p.name + ".restore.lock")
66|def _posix_flock(fh, exclusive: bool) -> bool:
70|        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
71|        fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
85|def _win_try_lock(fh, offset: int, nbytes: int) -> bool:
90|        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, nbytes)
```

只剩一个锁文件（`:51-53`）：POSIX 上写者 `LOCK_SH`、恢复 `LOCK_EX` 是同一把锁的两种模式；Windows 没有共享锁，于是把「共享」翻译成「区间不重叠」——访问锁文件预留 64 字节，每个写者占 1 字节，恢复端独占 `[0, 64)`。冲突由内核判定，「能不能占一个槽」与「占槽成功」是同一件事。

### 1.3 `WriterLease`

```python
112|    def __init__(self, db_path: Path):
114|        self._lock_path = access_lock_path(self.path)
118|        self.acquire()
121|    def acquire(self) -> None:
124|        if os.name == "nt":                       # pragma: no cover - 仅 Windows
125|            self._acquire_windows()
127|        fh = _open_rw(self._lock_path)
128|        if not _posix_flock(fh, exclusive=False):
130|            raise LeaseError(
131|                "数据库正在恢复或维护中，暂时不能打开（"
132|                + str(self._lock_path) + "）。请稍后重试。")
```

Windows 分支要处理 rc6 平台 B 踩出来的坑：`msvcrt.locking` 的区间锁按**文件句柄**记，同进程第二个句柄锁同一区间必 `EACCES`。所以先查 `_LOCAL`，已有人持锁就共享句柄、只加计数：

```python
136|    def _acquire_windows(self) -> None:           # pragma: no cover - 仅 Windows
138|        with _LOCAL_LOCK:
139|            held = _LOCAL.get(key)
140|            if held is not None:
141|                held[1] += 1                  # 同进程已在用，共享句柄
142|                self._fh = held[0]
144|                self._slot = held[2]
149|        start = os.getpid() % SLOTS
150|        for i in range(SLOTS):
152|            if _win_try_lock(fh, slot, 1):
154|                    _LOCAL[key] = [fh, 1, slot]
```

release 按同一套引用计数收尾，计数归零才真正解锁（`:171-180`）。POSIX 分支走 `_posix_unlock`，不需要计数，因为 `flock` 允许多句柄各自 `LOCK_SH`。

### 1.4 踩坑记录：锁文件绝不删除

```python
187|        # 绝不删除锁文件：unlink 之后新来的实例会在**新的 inode** 上加锁，
188|        # 而老实例还锁着旧 inode，两边各用各的锁，互斥就此失效。
189|        # （rc7 一度这么写过，tests.test_v011_rc6 里「另开实例阻止恢复」
190|        # 当场失败，就是这个原因。）
```

不是事后美化：第一版 release 沿用了 rc6 的 `unlink`，结果新实例在新 inode 上加锁、老实例锁着旧 inode，`test_second_database_on_same_file_blocks_restore` 直接失败。改法是彻底删掉删除逻辑，并加 `tests/test_v011_rc7.py:153 test_lock_files_are_never_deleted` 钉住「关库后 `.access.lock` 仍在」。

### 1.5 `exclusive_access`：两道锁，各管一件事

```python
200|def exclusive_access(db_path: Path, timeout: float = DEFAULT_RESTORE_TIMEOUT):
205|    1. restore_gate 排他 —— 只用来区分「另一个恢复在跑」和「有写者在用」，
206|       给的是能看懂的报错，不是正确性来源。
207|    2. 访问锁排他 —— 正确性在这里。它与写者的共享锁是同一个内核对象。
224|    fh = _open_rw(lock_path)
225|    deadline = time.monotonic() + max(0.0, timeout)
227|    while True:
229|            ok = _win_try_lock(fh, 0, SLOTS)
231|            ok = _posix_flock(fh, exclusive=True)
232|        if ok or time.monotonic() >= deadline:
234|        time.sleep(0.05)
```

第 229 行是 Windows 的关键：恢复端一次锁 `[0, 64)`，与任何写者的 1 字节都冲突，所以「有写者在用」与「拿不到整段」是同一件事。失败文案（`:243-244`）：「还有进程在用这个数据库，拒绝替换。请先关闭 GUI / 停掉 headless 服务。」收尾逆序释放，gate 同样不删（`:265`）。

---

## 2. `bigfish/server.py`（91 / 16）

### 2.1 `LoopbackGuardMiddleware`（`:121-172`，新增）

回环中间件要自己拼 403 的 JSON body，所以模块顶部还补了 `import json`（旧版所有响应都走 `JSONResponse`，没有这个导入）。

```python
121|class LoopbackGuardMiddleware:
122|    """连接级回环闸门（BFC-03）。
124|    审计指出：create_app() 只返回 app，第三方仍可用
125|    uvicorn.run(create_app(...), host="0.0.0.0") 绕开 check_bind_host()。
127|    这样端口就算被误绑到全网卡，外部请求也拿不到任何数据。
129|    - scope["client"][0]：对端 IP，局域网/公网来客必然不是回环。
131|    - scope["server"][0]：本端 IP（绑 0.0.0.0 时是该连接实际落到的本地地址）。
134|    两者任一非回环就 403。
140|    TRUSTED_PEERS = frozenset({"testclient", "testserver"})
147|        if not addr:  return True          # 无对端信息（unix socket）交由上游
149|        if addr in cls.TRUSTED_PEERS:  return True
151|        return is_loopback_host(addr)
164|    async def __call__(self, scope, receive, send):
165|        if scope["type"] == "http":
166|            peer = (scope.get("client") or (None,))[0]
167|            local = (scope.get("server") or (None,))[0]
168|            for addr in (peer, local):
169|                if not self._ok(addr):
170|                    await self._reject(send, addr)
171|                    return
172|        await self.app(scope, receive, send)
```

改前完全没有这一层：`create_app()` 只组装 app，闸门只在 `serve()` 里。改后即使 app 被硬绑 `0.0.0.0`，外部也只能拿到 403（`_reject` 在 `:153-162`）。`TRUSTED_PEERS` 只放两个进程内常量是刻意的——放进 `127.0.0.1` 之类真实地址，中间件就等于没有。

### 2.3 `_validate_args` 补 `maxLength`（`:431-441`）

```python
432|        spec_k = props.get(k) or {}
433|        want = spec_k.get("type")
434|        if want == "string" and not isinstance(v, str):
435|            return f"参数 {k} 必须是字符串"
436|        # BFC-02：自由文本必须有字段级上限。MCP 请求整体虽然受 MAX_BODY_BYTES
437|        # 限制，但一个字段就能独占几乎整个请求体，在 JSON 解析和业务处理之前
438|        # 就已经进内存了。REST 侧 Pydantic 早就有 max_length，这里补上同一道。
439|        limit = spec_k.get("maxLength")
440|        if limit is not None and isinstance(v, str) and len(v) > limit:
441|            return f"参数 {k} 超过长度上限 {limit} 字符（实际 {len(v)}）"
```

改前是 `want = (props.get(k) or {}).get("type")`，只查类型、没有 `maxLength` 分支，`enum` 那行又重取了一次 `props.get(k)`。第 440 行用 `>` 而不是 `>=`：`tests/test_v011_rc7.py:196 test_at_limit_field_is_accepted` 就是钉这个边界的。

### 2.4 `tool_specs` 里 5 处自由文本补上限

```python
508|                    "detail": {"type": "string", "maxLength": MAX_TEXT_FIELD},
525|                    "log": {"type": "string", "maxLength": MAX_TEXT_FIELD},
526|                    "diff": {"type": "string", "maxLength": MAX_TEXT_FIELD},
527|                    "tests": {"type": "string", "maxLength": MAX_TEXT_FIELD},
528|                    "note": {"type": "string", "maxLength": MAX_TEXT_FIELD},
```

改前这五个字段都只有 `{"type": "string"}`。上限值是 `MAX_TEXT_FIELD = int(os.environ.get("BFC_MAX_TEXT_FIELD") or 4 * 1024 * 1024)`（`:41`），与 REST 侧 `HeartbeatBody.detail` / `ResultBody.log` 同一个常量——两条入口的安全水位从此是同一条线。

### 2.5 新增 `create_production_app`，`serve` 改用它

```python
599|def create_production_app(council: Council, host: str | None = None,
600|                            watchdog_interval: float | None = None) -> tuple[FastAPI, str]:
603|    与 create_app() 的唯一区别：host 先过 check_bind_host()，非回环直接抛
604|    BindError。这样「谁调这个工厂，谁就不可能拿到一个准备绑 0.0.0.0 的 app」。
607|    return create_app(council, watchdog_interval=watchdog_interval), check_bind_host(host)
618|    app, host = create_production_app(
619|        council, host, watchdog_interval=watchdog_interval)
```

改前 `serve()` 是分开两行：`host = check_bind_host(host)` 然后 `app = create_app(...)`。分开写的问题不是「容易忘」这么轻——是「拿 app」与「过闸门」两个动作可以独立调用，每加一个入口就多一次漏掉的机会（GUI 就自己写了一遍 `check_bind_host`）。合并成返回 tuple 的工厂后，想拿 app 就得选工厂：官方工厂一定过闸门，`create_app` 一定不过，没法「一半」。

`create_app()` 的 docstring 同时从「**本函数不负责监听地址**（rc6 F-08）」改成「**本函数不校验监听地址**（供测试/进程内挂载）」并写出两道防线分层（`:216-226`）。「不负责」读起来像免责声明，「不校验」是能力描述——同一个事实，行动指引不同。

---

## 3. `bigfish/roles.py`（18 / 7）

```python
13|# BFC-RC7: mobile end (remote chat roaming / read-only watching). Lower than executor:
14|# cannot claim, heartbeat, or submit. Naming deliberately avoids executor.
15|MOBILE_VIEWER = "mobile_viewer"
17|ALL_ROLES = (ARCHITECT_GPT, REVIEWER_GEMINI, HUMAN_OWNER, EXECUTOR_DSH,
18|             MOBILE_VIEWER)
27|    MOBILE_VIEWER: "手机端（只读）",
44|    # 只读 + 可留言。刻意不含 task.claim / run.* / approve / backup.*：
45|    # 手机丢了也不至于能把任务领走或把库导出去。
46|    MOBILE_VIEWER: {"task.read", "event.read", "review.create"},
54|_DB_ROLE_MAP = {"executor": EXECUTOR_DSH, "reviewer": None, "admin": HUMAN_OWNER,
55|                "mobile": MOBILE_VIEWER}
```

改前 `ALL_ROLES` 是四元组、`_DB_ROLE_MAP` 三键，上面那段注释写的是「mobile 尚未实现，故意不登记」。实现之后那句注释就成了错误文档，所以整段重写为「未登记 = 拿到执行器权限」这条铁律 + 新增角色必须同时改三处的清单（`:48-52`）。还有一处判定写法变化：`:85` 从 `if base is not None: return base` 改成 `if db_role in _DB_ROLE_MAP and base is not None`。行为等价（未登记时 `base` 必为 `None`），但旧写法读起来像「碰巧有值就返回」，新写法是「先确认它在白名单里」——这段代码正是「未登记会被兜底成执行器」的地方，读错代价最大。

---

## 4. `bigfish/core.py`（6 / 3）

```python
163|        if role not in R.DB_ROLES:
164|            raise CouncilError("role 只能是 " + " / ".join(sorted(R.DB_ROLES)))
173|        prefix = {"executor": "bfc_ex_", "reviewer": "bfc_rv_", "admin": "bfc_ad_",
174|                  "mobile": "bfc_mb_"}[role]
```

改前是硬编码 `if role not in ("executor", "reviewer", "admin"): raise CouncilError("role 只能是 executor / reviewer / admin")` 与三项前缀字典。硬编码的问题在于它与 `_DB_ROLE_MAP` 是两份独立的真相：加 mobile 时漏改前者 = 功能不可用，漏改前缀字典 = `KeyError`，漏改映射表 = 提权。改成读 `R.DB_ROLES` 之后只剩一份真相，错误文案也自动跟着变。

---

## 5. `bigfish/cli.py`（32 / 13）

```python
143|    try:
144|        return _dispatch(a, s, db, council)
145|    finally:
146|        _close_quietly(db)
149|def _close_quietly(db) -> None:
150|    """退出前收掉数据库（BFC-05）。
152|    以前只有 restore 分支关库，其他分支靠进程退出来释放句柄。这在 Windows 上
153|    会让紧接着调用本 CLI 的脚本（移动/删除/再次恢复）撞上文件占用；库没关还
154|    可能留下没 checkpoint 的 WAL。统一在 main() 的 finally 里关。
168|        from .config import PORT_RESERVED, PORT_ROLES
169|        dump({
170|            "implemented": {k: {"port": v[0], "role": v[1]}
171|                            for k, v in PORT_ROLES.items()},
172|            # BFC-06：预留端口也要打出来，否则看 allowed-ports 的人会以为
173|            # 3099-3101 是空着的，随手拿去用。
174|            "reserved": {str(p): {"name": v[0], "note": v[1]}
175|                         for p, v in sorted(PORT_RESERVED.items())},
176|        })
```

两处改动各有前因。**BFC-05**：改前没有 `_dispatch`，所有分支直接摊在 `main()` 里，只有 restore 自带 `try/finally + db.close()`（旧 `:222-233`），注释理由是「其他分支是短命只读操作，交给进程退出即可」。这个理由在 Linux 上成立、Windows 上不成立，而且它把关库写成了每个分支的自觉——新增分支的人根本不会想到。现在关库属于 `main()`，restore 分支只剩 `dump(...)` 两行（`:250-252`）。**BFC-06**：改前只有 `dump({k: {"port": v[0], "role": v[1]} for k, v in PORT_ROLES.items()})` 一行，输出只有三项已实现端口，于是 `allowed-ports` 这个名字在骗人——它列的是「已用」，读起来像「允许用」。3099/3100/3101 分别预留给 mobile REST / mobile SSE / MCP bridge，被误占就要改端口或改契约。

---

## 6. `bigfish/backup.py`（14 / 6）

```python
135|            # BFC-04：布尔名叫 contains_plaintext_credentials 时，写 False 等于
136|            # 对外宣称「我检查过了、保证没有」。这不能由「白名单里没有 token 列」
137|            # 推出来，属于过强声明。改成枚举，只描述**实际观测到的事实**：
138|            #   none                          —— 明确不含任何凭据形态
139|            #   hashes-only                   —— 只含不可逆哈希，无明文（本包）
140|            #   pattern-scrubbed-unverified   —— 按已知模式脱敏过，未证明干净（审计包）
141|            "credential_status": "hashes-only",
142|            "credential_detail": (
143|                "含 executors.token_hash 与 tickets.token_hash（SHA-256，不可逆）；"
144|                "不含明文 token、不含执行票原文、不含密钥文件。"),
221|            "credential_status": "pattern-scrubbed-unverified",
229|                "manual_review_required": True,
```

改前两处都是布尔：操作包 `contains_plaintext_credentials: False` + `contains_credential_hashes: True`；审计包两个都是 `False`。操作包的结论其实对（确实只含哈希），问题在表达形式：布尔字段读起来像承诺，而那个承诺并不能由「白名单里没导出 token 列」推出来。审计包更糟——同一份 manifest 里挂着 `manual_review_required: True`，「我保证没有凭据」和「请你人工再看一遍」不可能同时成立。改后 `credential_scan` 块（`:222-230`）保留 rc6 的成果，只把布尔承诺换成枚举。

本轮没改、但必须一起读的两段（它们是 `locks.py` 的调用方）：`backup.py:487-495` 的 `db.enter_maintenance(...)` → `db.pause_lease()` → `with _translate_lease(exclusive_access(target))` → `captured_epoch = db.restore_epoch()`，以及 `:668-669` 把 `LeaseError` 翻成 `RestoreError`。`pause_lease()` 是新互斥模型能工作的前提：恢复自己也是写者，不先放开自己那份共享锁（Windows 上是自己占的槽位）就永远抢不到整段排他。

---

## 7. `bigfish/gui.py`（9 / 9）

改前第 27 行有 `from .server import create_app`。函数里又 import 了一次，模块级这条从未被使用，`pyflakes` 报 F401——而「pyflakes 零输出」是发布门槛之一。删掉不算洁癖：rc7 之后 `create_app` 已是「拿不到闸门的工厂」，留在文件顶部等于给后来人一个默认会选错的引用。

```python
 96|    def start(self):
 98|        from .server import BindError, create_production_app
 99|        # 纵深防御（SECURITY-REVIEW rc4 R10 / BFC-03）：直接构造 MainWindow 的脚本/
100|        # 测试绕过了 gui.run() 的闸门，所以这里再校验一次。闸门现在由
101|        # create_production_app() 统一执行，GUI 不再自己写一套判断。
102|        if self.thread is not None and self.thread.is_alive():
103|            return
105|        try:
106|            app, self.host = create_production_app(
107|                self.council, self.host, watchdog_interval=self.watchdog_interval)
108|        except BindError as exc:
109|            self.started.emit(False, f"拒绝启动：{exc}")
110|            return
```

改前是 `self.host = check_bind_host(self.host)` → 判线程 → `app = create_app(...)`。四处变化各有理由：① 闸门判断不再自己写（只有一份实现）；②「既过闸门又用无闸门工厂」的组合消失；③ `thread.is_alive()` 判断提到工厂调用之前——否则重复调用 `start()` 会先白跑一次闸门才在下一行发现线程已在跑；④ 注释改成说明「为什么删掉这段判断」，否则下一个读代码的人会以为这里漏了。

---

## 8. `bigfish/__init__.py`（1 / 1）

版本号一行：`__version__ = "0.1.1-rc7"`（`:6`）。

---

## 9. `tests/`（44 / 8，三个既有文件）

### 9.1 `tests/test_v011_p2.py`（29 / 4）

改前 `test_four_canonical_roles` 断言 `len(R.ROLES) == 4`。改成：

```python
43|    def test_canonical_roles(self):
49|        for role in (R.ARCHITECT_GPT, R.REVIEWER_GEMINI, R.HUMAN_OWNER,
50|                     R.EXECUTOR_DSH):
51|            self.assertIn(role, R.ROLES)
52|        self.assertIn(R.MOBILE_VIEWER, R.ROLES)
53|        self.assertEqual(len(R.ROLES), len(set(R.ALL_ROLES)))
```

不选「把 4 改成 5」：那只是把同一个脆弱性推迟到下一次加角色。`len(R.ROLES) == len(set(R.ALL_ROLES))` 守的是另一件事——`ROLES` 与 `ALL_ROLES` 不能有重复项（重复意味着某个角色名被写错成另一个）。新增两项都是「不能」型断言：`:55 test_mobile_viewer_cannot_execute`（9 个不该有 + 3 个该有）、`:67 test_mobile_db_role_maps_to_viewer_not_executor`。

### 9.2 `tests/test_v011_rc4.py`（9 / 2）

改前断言 `assertNotIn("mobile", R.DB_ROLES, "未实现的 mobile 角色不该被默认当成执行器")`。实现落地后必须反过来，但要守的性质没变：

```python
473|        self.assertIn("mobile", R.DB_ROLES)
474|        self.assertEqual(R.role_for_db_row("mobile"), R.MOBILE_VIEWER)
475|        self.assertFalse(R.can(R.role_for_db_row("mobile"), "task.claim"),
476|                         "登记 mobile 不等于给执行权")
```

这类改写的风险是「为了变绿而改断言」。这里不是：改前守「别让未实现的角色拿到执行器权限」，改后守「登记了也不给执行权」——后者更严，它直接验了 `task.claim`。

### 9.3 `tests/test_v011_rc5_review_audit.py`（6 / 2）

改前两条 `assertFalse(m["contains_plaintext_credentials"])` / `assertFalse(m["contains_credential_hashes"])`，改后三条：

```python
231|        self.assertEqual(m["credential_status"], "pattern-scrubbed-unverified")
232|        self.assertNotIn("contains_plaintext_credentials", m)
233|        self.assertTrue(m["credential_scan"]["manual_review_required"])
```

第 232 行是关键：只断言新字段存在，旧字段可能被顺手留下当「兼容」，而留下的那一刻矛盾就又回来了。`assertNotIn` 让「删除」成为被测试约束的事。

---

## 10. 未跟踪的新文件

### 10.1 `tests/test_v011_rc7.py`（438 行 / 19555 字节，28 项）

7 个测试类，一类对一条审计意见：`TestLeaseAtomicity` 6（`:87`）、`TestMcpFieldLimit` 4（`:228`）、`TestBindGate` 5（`:283`）、`TestManifestCredentialStatus` 3（`:318`）、`TestCliClosesDatabase` 2（`:351`）、`TestReleaseEngineering` 3（`:378`）、`TestMobileViewerRole` 5（`:408`）。两个用例决定了这 28 项是不是「真能失败」：

```python
 93|    def test_writer_cannot_open_while_exclusive(self):
100|        entered, release = threading.Event(), threading.Event()
102|        def hold():
103|            with exclusive_access(path, timeout=5.0):
104|                entered.set()
105|                release.wait(10)
248|        self.assertEqual(LoopbackGuardMiddleware.TRUSTED_PEERS,
249|                         frozenset({"testclient", "testserver"}))
250|        for bad in ("127.0.0.1", "0.0.0.0", "localhost", "192.168.1.1"):
251|            self.assertNotIn(bad, LoopbackGuardMiddleware.TRUSTED_PEERS)
```

前者用真线程 + 两个 `Event` 把「排他锁已持有」这一刻卡住，再断言新 `Database` 抛 `LeaseError`——不是 mock，也不是「函数被调用过」。后者守中间件的价值观：受信名单里一旦出现真实地址，整套连接级判定就失效，而这种退化不会被其它用例发现。

### 10.2 `requirements.lock`（53 行 / 1348 字节，39 个精确 pin）

文件头把生成方式与边界写死，防止下一轮有人拿它当依赖声明去改：生成命令 `pip freeze | grep -viE "^(pyflakes|pip|setuptools|wheel)=="`，生成环境 Debian 12 / Python 3.11.2 / 2026-11-20，并注明「这是全部传递依赖的精确版本，不是声明式范围」。正文 39 行全部形如 `uvicorn==0.54.0`，无 `>=`、无 extras。排除的四个包都是引导或开发期工具——把 `setuptools` 锁死会让新环境装不上，这是 lockfile 最常见的自伤方式。

### 10.3 `.github/workflows/ci.yml`（106 行 / 3689 字节）

四格矩阵：`os: [ubuntu-latest, windows-latest]` × `python: ["3.11", "3.13"]`，`fail-fast: false`——一个平台挂了不该把另一个平台的结论藏起来，而本轮最大的风险恰好是「只在 Windows 上会坏」。步骤是 pyflakes / compileall / 单测（`QT_QPA_PLATFORM=offscreen`）/ selfcheck / gui_smoke。另有 `security-note` job，刻意不装依赖，只做两件不需要依赖的事：

```yaml
101|          bad=$(git ls-files | grep -Ei '\.(db|db-wal|db-shm|key|pem|pfx|zip|exe|msi)$' || true)
104|          hits=$(git grep -nE '(ghp_[A-Za-z0-9]{20,}|bfc_(tk|ex|rv|ad)_[A-Za-z0-9_-]{10,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)' -- . || true)
```

跑得最快、能最早给结论。`tests/test_v011_rc7.py:337` 反过来验证那条正则真能命中——写一条永远匹配不上的正则等于没扫。

---

## 11. 文档改动（AUDIT.md 81/14、CHANGELOG.md 65/0、README.md 46/53）

不计入代码侧 417/208。性质：`AUDIT.md` 版本号与测试总数升到 rc7 / 295，新增「rc6 → rc7 的 schema 变化：无」、测试表加 `test_v011_rc7.py` 28 项、平台 B 那行在实跑前写的是「rc7 的 294 项尚未重跑」（已于 2026-11-23 更新为 294 项全绿）；`CHANGELOG.md` 新增 rc7 段（八条 + 手机端 + 验证 + 仍未做）；`README.md` 第 0 节改为「唯一下载渠道指向 GitHub Release v0.1.1-rc6」。**口径统一**：CHANGELOG 与 AUDIT.md 原先写的 `58.5s` / `58.236s` 已在发布前统一为 `59.793s`（与 `docs/.rc7-facts.md` 一致）；`TEST-REPORT` 里的 `61.458s` 是子代理复跑值，文中已注明是负载差异。

---

## 12. 回归实测

```bash
cd /home/dsh/bigfish-council
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
# Ran 295 tests in 60.507s / OK / RC=0

.venv/bin/python -m pyflakes bigfish tests tools       # 零输出，RC=0
.venv/bin/python -m compileall -q bigfish tests tools  # RC=0
.venv/bin/python tools/selfcheck.py                    # 16 通过 / 0 失败
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py                  # 9 通过 / 0 失败
```

以上是**平台 A**。平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）已补跑（294 项那一版）：`Ran 294 tests in 74.232s` / `OK (skipped=1)` / 退出码 0，`pyflakes` 零输出、`compileall` 退出码 0、`selfcheck` 16/16、`gui_smoke` 9/9。唯一跳过的是 `test_graceful_shutdown_on_sigint`（Windows 无 SIGINT 语义，标了 `skipIf(os.name == "nt")`）。

---

## 13. schema 与兼容性

- `SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`，本轮均未改，没有新增 `MIGRATIONS` 项；rc6 的库直接用，不需要迁移也不需要回滚。
- `executors.role` 新增合法取值 `mobile`（原 `executor` / `reviewer` / `admin`）。这是**值域扩张**不是结构变更，但回滚 rc7 → rc6 之前必须确认库里没有 `mobile` 行——老代码读到它会走兜底拿到 `EXECUTOR_DSH`（提权）。这条顺序应写进 MIGRATION/ROLLBACK，本轮没写。
- manifest 删了两个字段（`contains_plaintext_credentials`、`contains_credential_hashes`）、加了三个（`credential_status`、`credential_detail`、`credential_scan`）。按旧字段名读 manifest 的外部脚本会拿不到值——本轮没有消费者，但这是真实的接口破坏。
- `allowed-ports` 输出从扁平字典变成 `{implemented, reserved}` 两层，按旧结构解析的脚本读不到端口。
- BFC-08 的「在真实提交上打带注释 tag」**尚未执行**：`git tag -l` 为空，14 个改动 + 4 个未跟踪项全在工作树。

---

## 14. 本节没做的事

1. ~~没跑平台 B（U5）~~ **已完成（2026-11-23）**：`Ran 294 tests in 74.232s / OK (skipped=1)`，退出码 0。`locks.py` 的 Windows 分支（`_acquire_windows`、`_win_try_lock` 槽位寻址、`_LOCAL` 引用计数）现在有真机执行记录，当时 5 项 `TestLeaseAtomicity` 全过。收尾时在 Linux 侧补了第 6 项：用 `subprocess` 起**真正独立的第二个进程**持写者租约，`exclusive_access` 必须被拒；Windows 侧这一项还没跑（U9）。
2. **没打 tag、没提交。** BFC-08 只定下了流程。
3. **CI 没在真实 runner 上跑过。** `ci.yml` 只做了本地自检（测试断言它含 ubuntu/windows/unittest/security-note、且凭据正则真能命中），没有任何 GitHub Actions 执行记录。
4. **exe / apk / 开机自启三件产物都没做**（U2 / U7 / U3）。
5. **移动端只有角色，没有端点**（U6）：手机端身份能以只读权限注册并通过鉴权，但 REST/SSE 接口一个都没实现。
6. **`locks.py` 的极端路径没压测**：64 槽位用满、恢复等待满 `DEFAULT_RESTORE_TIMEOUT = 5.0` 秒这两条都没有用例；`test_exclusive_refused_while_writer_alive` 走的是「立即失败」。
7. **`server.py` 与 `core.py` 没重构**（U8）：本轮还往 620 行的 `server.py` 里继续加东西，分层问题更重了。
8. **审计原文不在仓库里**：`docs/AUDIT-CODEX-v0.1.1-rc6.md` 与 commit `f5e955930c` 在本仓库都不存在，本文对八条的表述是转述，不是引用。
