# 修复总表：bigfish-council v0.1.1-rc8

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc8`（`bigfish/__init__.py:6`） |
| 日期 | 2026-11-23 |
| 相对基线 | rc7 发布点 tag `v0.1.1-rc7` → commit `de255e9`（"docs: sync rc7 平台 B / 294 项"） |
| 本轮提交 | `031ebcf`（R7-01 / R7-02 / R8-01 + 23 例回归）→ `9941965`（CI 实测暴露的 R8-02 / R8-03 + 4 例回归） |
| 差异规模 | 两个提交合计 **16 files changed, 822 insertions(+), 83 deletions(-)**（`031ebcf` 12 files 659+/83−；`9941965` 4 files 163+/0−）。`tests/test_v011_rc8.py` 430 行 / **27 例** |
| 库结构 | `SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`，**rc8 未改**，不需要迁移、不需要回滚库 |
| 全量测试 | 平台 A：**`Ran 322 tests in 148.492s / OK (skipped=1)` / `UT_RC=0`**；自检 16/16（含 `PYTHONIOENCODING=cp1252` 复跑）；GUI 冒烟 9/9 |
| 静态检查 | `pyflakes bigfish tools tests` 零输出 `PF_RC=0`；`compileall -q` `COMPILE_RC=0` |
| 平台 | 平台 A（Debian 12 / Python 3.11.2 / SQLite 3.40.1）322 项全绿；平台 B 由 GitHub Actions `windows-latest` 承担：第一次运行在 Windows 上跑完 318 项、暴露 2 个真问题（见 §五），修后重推 |

状态口径（沿用 rc7，不自行改级）：

- `已修复`：代码已改，且有能失败的回归测试，本批次实测通过。
- `已修复（未覆盖）`：代码已改，但自动化证据只覆盖一部分，缺什么在正文点名。
- `已修复（文档级）`：改的是契约、声明或发布流程，本身没有可失败的自动化断言。

严重度沿用 GPT 复核给的档：P1 = 直接让一条安全保证落空；P2 = 不直接破保证，但会让声明与事实不一致。

---

## 一、rc8 修复总表

本轮来源是 GPT 复核 rc7 后给出的 5 条未闭合项，编号沿用审计里的 R7-xx。

| 编号 | 级别 | 原始问题（一句话） | 修改文件（行号 / 函数） | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| R7-01 | P1 | Windows 同进程并发打开同一个 Database 时，`_LOCAL` 句柄表有竞争窗口：查表在一个锁里，抢槽位在锁外，两个线程各抢一个槽位、后者覆盖前者，release 时 `held[0] is not fh` 直接 return → 句柄与槽位双泄漏，泄漏够多就再也打不开库 | `bigfish/locks.py:45-52`（新增模块级 `_ACQUIRE_LOCK`）、`:143-197` `WriterLease._acquire_windows` | 把「查已有句柄 → 打开文件 → 抢槽位 → 登记 `_LOCAL`」整段压进同一把 `_ACQUIRE_LOCK`；抢槽失败或登记途中被打断时在 `except BaseException` 里关掉自己开出的句柄再抛，杜绝悬挂句柄。锁内只做非阻塞 `_win_try_lock`（最坏 64 次失败即抛），不会卡住别的路径 | `tests/test_v011_rc8.py:81` `TestConcurrentLocalHandles` 2 项：`test_parallel_open_close_100_times`（`ThreadPoolExecutor` + `Barrier` 真并发 100 轮开/关，断言 `_LOCAL` 最后为空且每轮都成功）、`test_local_table_refcount_shared`（同 key 两次 acquire 共享同一句柄、refcount=2，仅 Windows 跑） | 已修复 |
| R7-02 | P1 | 未知 `executors.role` 兜底成执行器：`role_for_db_row()` 里 `return EXECUTOR_DSH`，等价于「手工写一行 SQL 或旧版本残留行 = 拿到 task.claim / run.submit」，而 `server.py` 的 `_need()` 与 `dispatch_tool()` 还在 `ex.get("role_canonical") or R.EXECUTOR_DSH` 上再兜一次 | `bigfish/roles.py:21`（新增 `DENIED_ROLE` 哨兵）、`:36` `ALL_ROLES`、`:51-58` `PERMISSIONS[DENIED_ROLE] = set()`、`:88-106` `role_for_db_row`；`bigfish/core.py:229-250` 新增 `_canonical_role_or_reject()`，`:210`/`:226` 两个鉴权入口改调它；`bigfish/server.py:204` `_need`、`:565` `dispatch_tool` 兜底值改 `DENIED_ROLE` | 失败关闭：认不出的角色映射到零权限哨兵，`authenticate()`/`authenticate_any()` 直接抛 `CouncilError` 并写一条 `auth.denied_unknown_role` 审计事件（只记身份 id，不记令牌），调用方连 `role_canonical` 都拿不到。缺字段（`or` 分支）同样降级成 `DENIED_ROLE` 而不是执行器 | `tests/test_v011_rc8.py:164` `TestUnknownRoleFailsClosed` 7 项：映射返回哨兵、哨兵无任何权限、`authenticate` 抛错、`authenticate_any` 抛错、未知角色经服务层也 claim 不了、`dispatch_tool` 缺 `role_canonical` 时被拒、登记过的执行器仍能正常工作（防「修太狠把正常路径也堵了」） | 已修复 |
| R8-01 | P1 | Windows 上 `icacls` 输出按 locale 代码页编码，`subprocess.run(text=True)` 用宿主默认编码解，解不动就抛/退化成空串 → `verify_file_restricted()` 把「一条正常的 ACL」判成「没限制」，密钥文件权限校验整个失效（全量测试里表现为 `icacls 无输出 rc=0`） | `bigfish/security.py:197-218` 新增 `_WIN_TEXT_FALLBACKS` / `_decode_console()` / `run_capture()`；`:222-251` `_current_user_candidates` 增加 `whoami` 兜底；`:254-279` `_current_user_sids`、`:158-196` `secure_file`、`:303` `verify_file_restricted` 全部改走 `run_capture` | 先拿 bytes 再按 `utf-8 → gbk → cp936 → mbcs → utf-16-le → replace` 兜底解码，任何一步都不抛异常；外部命令统一走 `run_capture()`（`check=False`，超时/OSError 折成 `rc=1` + 中文错误串） | `tests/test_v011_rc8.py:330` `TestConsoleDecoding` 6 项：GBK 字节解出中文且无 U+FFFD、UTF-8 正常、乱码仍返回 str、空 bytes、`run_capture` 跑一个输出 GBK 字节的子进程必须拿到非空文本、缺失二进制 `rc≠0` 且 err 含「调用失败」；`:270` `TestWindowsAclMatching` 6 项覆盖 `DOMAIN\\user` / 裸用户名 / 纯 SID / 别人的 SID / 宽泛主体（Everyone、BUILTIN\\Users）/ 必须要求当前用户 | 已修复 |
| CI-01 | P1 | GitHub Actions 的 Windows 三项红灯全是编码/超时，不是安全逻辑：`test_refuses_public_bind` 与 `test_all_entry_points_reject_public_bind` 断言中文文案，但 runner 代码页编不了中文，Python 3.11+ 用 backslashreplace 把 stderr 写成 `\\u9519\\u8bef` 字面量；`test_any_address_is_rejected` 的 3 秒启动窗口不够 node 冷启动 + Defender 扫描 | `tests/test_v011_p3.py:94-118`、`tests/test_v011_rc3.py:579-594`（子进程加 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`，父进程 `encoding="utf-8", errors="replace"`）；`tests/test_v011_rc5_proxy.py:31` `STARTUP_TIMEOUT = 25.0`、`:24-33` 注释说明这是判定窗口不是性能指标 | 断言的仍然是「拒绝文案确实写出来了」与「非回环没启动」，只是把宿主的编码差异挡在测试边界之外；超时值买的是判定窗口（真失效时最多白等 25 秒），不是放宽判据 | 上述三项本身（CI 上真跑） | 已修复 |
| CI-02 | P2 | rc7 的跨进程租约第 295 项在 Windows 从未跑过，只有 Linux 证据 | 无代码改动；由 CI-01 修复后由 `windows-latest` × py3.11/3.13 真跑 | — | `tests/test_v011_rc7.py` 5 项 + `tests/test_v011_rc8.py` 2 项 | 待 CI 复核（§五） |
| MINOR-01 | P2 | `cli executor-add --role` 的 choices 只有 executor/reviewer/admin，`mobile` 角色只能绕过 CLI 手工 INSERT | `bigfish/cli.py:79-80` | choices 补 `mobile` | 由 rc7 的 `TestMobileViewerRole` 覆盖注册路径，CLI 参数本身无独立断言 | 已修复（未覆盖） |

| R8-02 | P1 | Windows 英文版 / CI runner 的 `sys.stdout` 是 cp1252，`print("健康检查")` 抛 `UnicodeEncodeError`，`tools/selfcheck.py` 在印第一行时整进程 traceback 退出——用户看到的是报错而不是检查结果 | `bigfish/console.py`（新增 92 行：`_needs_upgrade` / `install_utf8_stdio` / `stdio_report`）、`bigfish/__init__.py:11-16`（导入即安装，`STDIO_STATUS` 暴露结果） | 只在「当前编码表示不了中文」时才把 stdout/stderr 切到 UTF-8 + `errors="replace"`；cp936 / UTF-8 环境一律不动，免得把中文终端搞成乱码；任何异常吞掉（兜底本身不许成为新的崩溃点）；`BFC_NO_STDIO_FIX=1` 可关，供测试验证「没修的时候确实会崩」 | `tests/test_v011_rc8.py` `TestConsoleEncodingFallback` 4 项：cp1252 下必须能打中文且 rc=0 / cp936 必须保持 gbk 且原始字节含 GBK 编码的中文 / UTF-8 不动 / 关掉开关必须复现 `UnicodeEncodeError`。全部断言**原始字节**，不用父进程解码后的字符串 | 已修复 |
| R8-03 | P2 | `test_heartbeat_lease_is_in_the_future` 在 Windows 上偶发失败：`1795435663.622005 not greater than 1795435663.622005`。Windows `time.time()` 分辨率约 15.6ms，claim 与 heartbeat 落在同一 tick，`renewed_at` 不变 → 测试误判成「续租没前进」 | `tests/test_v011_rc5_review_audit.py:147-156`（`before` 采样与 heartbeat 之间加 `time.sleep(0.05)`） | 时钟粒度是平台事实，不是产品缺陷：续租在同一次调用内完成，`expires_at` 照常被延长。测试改为显式跨过一个 tick 再断言，让它测「续租有没有发生」而不是「系统时钟够不够细」；断言强度不变（仍是 `assertGreater`） | 该文件本身即证据；另在 CI 的 Windows runner 上复跑确认 | 已修复 |

---

## 二、每条修复的「为什么不是打补丁」

### 2.1 R7-01：为什么必须整段串行，而不是再补一次检查

旧代码的结构是：

```
with _LOCAL_LOCK:          # 第一段：查表
    held = _LOCAL.get(key)
    if held: ...
fh = _open_rw(...)         # 锁外：打开
for slot in range(SLOTS):  # 锁外：抢槽
    if _win_try_lock(...): break
with _LOCAL_LOCK:          # 第三段：登记
    _LOCAL[key] = [fh, 1, slot]
```

两个线程可以在第一段同时看到「没有已有句柄」，各自抢到不同槽位，第三段后到者覆盖先到者。先完成者调用 `release()` 时，`_LOCAL[key][0]` 已不是自己的 `fh`，于是 `if held[0] is not self._fh: return` 直接返回——**槽位永远不释放，句柄永远不关**。`SLOTS = 64`，泄漏 64 次就再也打不开库。

「在第三段再判一次有没有别人先写了」不成立：判完到写入之间仍有窗口，而且先到者已经认为自己是 owner 了。唯一正确做法是让「判 + 抢 + 登记」成为一个原子段，所以引入 `_ACQUIRE_LOCK` 把整段包住。锁内不阻塞（`_win_try_lock` 非阻塞，最坏 64 次），所以不会把并发退化成串行等待。

### 2.2 R7-02：为什么选「拒绝」而不是「按最低权限」

rc5 之前的口径是「未知角色按最低权限处理」，但最低权限选的是执行器——因为执行器是系统里唯一能干活的身份。这个选择在 rc7 之后失效了：执行器能 claim 任务、能提交结果、能改任务状态，它是**权限很大**的身份，不是「最低」。

rc8 换成显式哨兵 `DENIED_ROLE`：不在 `DB_ROLES` 里（注册时就拒）、`PERMISSIONS` 是空集、鉴权入口直接抛错并写审计。三条路径同时堵：映射层（`role_for_db_row`）、鉴权层（`_canonical_role_or_reject`）、请求层（`_need` / `dispatch_tool` 的 `or` 兜底）。只堵一层不够：手工 SQL 能绕过注册，`or` 兜底能在鉴权后被服务层再放一次。

代价是新增角色必须同时改三处（`ALL_ROLES` / `PERMISSIONS` / `_DB_ROLE_MAP`），漏一处只会让新角色被拒，不会提权——失败关闭，可接受。

### 2.3 R8-01：为什么不是「换个 encoding 参数」

`subprocess.run(..., text=True)` 用的是 `locale.getpreferredencoding()`。在 Windows 中文版上是 cp936，在 GitHub runner 上是 cp1252，在公司机 PowerShell 里可能又是另一套。写死任何一个都会在另一台机器上炸。

更麻烦的是：`icacls` 输出里混着中文账户名（`NT AUTHORITY\\SYSTEM` 之类是 ASCII，但 `BUILTIN\\Users` 在某些 locale 下会本地化）。第一次解码失败就抛异常，而调用点 `verify_file_restricted` 的上层把它吞成「校验失败但继续」，于是安全校验静默失效。

`run_capture` 的做法是先拿 bytes，再按候选编码依次试，全失败就 `replace` 兜底——保证永远返回 `str`，永远不会因为编码让调用方走进 except。这条修复同时覆盖了 `whoami` / `whoami /user /fo csv` / `icacls` 三处外部命令。

### 2.4 CI-01：为什么改测试而不是改产品

三项红灯都不是产品缺陷：产品确实拒绝了非回环监听（退出码 2、端口没被占），只是拒绝文案在 Windows runner 上被转义成了 `\\uXXXX`；proxy 也确实在闸门处退出，只是 node 冷启动超过了 3 秒。

改产品（比如把文案改成英文）是为了迁就测试环境而降低可用性，不划算。改测试的正确做法是**把环境变量钉死在一个确定的编码上**，这样断言的仍然是「文案写出来了」，而不是「runner 的代码页恰好是 UTF-8」。

超时值同理：3 秒 → 25 秒买的是判定窗口，注释里写清楚了「正常拒绝是毫秒级的，慢到这个数说明没在退出」这句话在 Windows 上不成立。

---

## 三、新增回归测试清单（23 项）

| 类 | 项数 | 行号 | 覆盖什么 |
| --- | --- | --- | --- |
| `TestConcurrentLocalHandles` | 2 | `tests/test_v011_rc8.py:81` | 真线程并发开/关 100 轮后 `_LOCAL` 归零；同 key 共享句柄与 refcount（仅 Windows） |
| `TestUnknownRoleFailsClosed` | 7 | `:164` | 未登记角色映射成哨兵、哨兵零权限、两个鉴权入口都抛错、经服务层也 claim 不了、`dispatch_tool` 缺字段被拒、正常执行器不受影响 |
| `TestSubprocessEncodingIndependence` | 2 | `:242` | `PYTHONIOENCODING=ascii` 下中文拒绝文案仍可读；`STARTUP_TIMEOUT` 是窗口不是延迟断言 |
| `TestWindowsAclMatching` | 6 | `:270` | `DOMAIN\\user` / 裸用户名 / SID / 别人 SID / 宽泛主体 / 必须含当前用户 |
| `TestConsoleDecoding` | 6 | `:330` | GBK / UTF-8 / 乱码 / 空 / 子进程 GBK 输出 / 缺失二进制 |

原有 295 项全部保留，无删除、无跳过、无放宽断言。全量 295 + 23 = 318。

---

## 四、库结构、迁移与回滚

- `SCHEMA_VERSION` 仍为 4，`BACKUP_FORMAT_VERSION` 仍为 4，`MIGRATIONS` 无新增项。
- rc7 的库直接可被 rc8 打开，不需要迁移；rc8 写出的库 rc7 也能打开（没有新增列/表）。
- 回滚方式：`git checkout v0.1.1-rc7` 即可，数据库不用动。唯一的行为差异是「未登记角色从被当执行器变成被拒绝」，回滚后这个风险重新存在。

---

## 五、平台 B（Windows）证据

rc8 收尾时两台 Windows 都不在线（`CLIENT-B` 超时、`CLIENT-A` 拒绝连接），无法直连跑全量。改用 GitHub Actions 的 `windows-latest` 承担平台 B：

- 提交 `fa37e2e`（脱敏后的公开副本）推送到 `dreamobserver/BigFish-Council-` 的 `main`。
- workflow `ci` 的矩阵是 `ubuntu-latest × windows-latest` 乘 `python 3.11 × 3.13`，`fail-fast: false`。

### 5.1 第一次运行（run `45170828326`）：Windows 跑完了，两处真失败

| job | 结论 | 失败步骤 |
| --- | --- | --- |
| `ubuntu-latest / py3.11` | success | — |
| `ubuntu-latest / py3.13` | success | — |
| `边界自检` | success | — |
| `windows-latest / py3.11` | **failure** | `Unit tests` |
| `windows-latest / py3.13` | **failure** | `Self check` |

**最重要的一个事实**：Windows 上 **318 项全跑完了**（`Ran 318 tests in 181.158s`），只挂 1 项；py3.13 那个 job 的单测还是 `OK (skipped=1)`，挂在后面的自检步骤。也就是说，从 rc7 一直背到现在的「Windows 跑不了全量」，到这里可以改成「能跑完，只是还有两个平台专属的坑」。

失败 1 —— Windows 时钟粒度（py3.11，`Unit tests`）：

```
FAIL: test_heartbeat_lease_is_in_the_future
  (tests.test_v011_rc5_review_audit.TestTimestampInsideTransaction)
  File "...\\tests\\test_v011_rc5_review_audit.py", line 152
    self.assertGreater(after["renewed_at"], before["renewed_at"], "续租时间没有前进")
AssertionError: 1795435663.622005 not greater than 1795435663.622005 : 续租时间没有前进
Ran 318 tests in 181.158s
FAILED (failures=1, skipped=1)
```

两次取值到小数点后 6 位完全相同。Windows 的 `time.time()` 分辨率约 15.6ms，claim 与 heartbeat 落在同一个 tick。**这是测试假设错了，不是续租失效**——续租逻辑在同一次调用里完成，`expires_at` 照常被延长。修法是让测试显式跨过一个 tick（R8-03）。

失败 2 —— cp1252 控制台（py3.13，`Self check`）：

```
File "...\\tools\\selfcheck.py", line 79, in main
    print("健康检查")
UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-3
```

runner 的 `sys.stdout` 是 cp1252。**这是产品缺陷**：任何人用英文版 Windows（或把输出重定向到非中文代码页）跑 `tools/selfcheck.py`，都会在印第一行中文时整进程退出，看不到任何检查结果。py3.11 那个 job 只是因为单测先挂了没走到这一步，才显得「没这个问题」。

### 5.2 修完之后（本地复现路径与 CI 一致）

```
PYTHONIOENCODING=cp1252 python -c "print('健康检查')"
  → UnicodeEncodeError（复现 CI 现象）

PYTHONIOENCODING=cp1252 python -c "import bigfish; print('健康检查')"
  → 健康检查
    STDIO_STATUS {'stdout': 'ok', 'stderr': 'ok'}
    ENCODING utf-8 replace

PYTHONIOENCODING=cp936 python -c "import bigfish; print('健康检查')"
  → 不动，ENCODING 仍是 gbk

PYTHONIOENCODING=cp1252 python tools/selfcheck.py
  → 通过 16 项，失败 0 项
```

平台 A 全量随之从 318 涨到 **322**（+4 项 `TestConsoleEncodingFallback`），`Ran 322 tests in 148.492s / OK (skipped=1)`。

口径说明：**CI 通过 ≠ 公司机真机通过**。CI runner 是 GitHub 托管的 Windows Server 2022，与公司机（Windows 11 家庭版中文版 / Python 3.14.6）不是同一台机器。本文凡写「平台 B 通过」，指的都是 CI runner；公司机复跑仍记为未做（§七 U1）。


---

## 六、CI 结果

第一次运行 `45170828326`（`fa37e2e`）：Ubuntu 两档 + 边界自检全绿；Windows 两档分别在「单测 1 项时钟粒度」与「自检 cp1252」上失败，详见 §5.1。

修复（`9941965`）+ 文档回填后重推，第二次运行 **`45174755219`**（head `fdd5c12`）：

| job | 结论 | 证据 |
| --- | --- | --- |
| ubuntu-latest / py3.11 | success | 平台 A 全量 |
| ubuntu-latest / py3.13 | success | 平台 A 全量 |
| 边界自检 | success | 端口与回环闸门自检 |
| windows-latest / py3.11 | success | `Ran 322 tests in 183.951s` + `OK (skipped=1)` |
| windows-latest / py3.13 | success | `Ran 322 tests in 197.687s` + `OK (skipped=1)` |

两个 Windows job 都随后跑了 `tools/selfcheck.py`（通过 16 项，失败 0 项）与 `tools/gui_smoke.py`（通过 9 项，失败 0 项）。

**结论：322 项在 Linux 与 Windows 上都完整跑完并全绿。** 唯一跳过项是两个平台共有的 `test_graceful_shutdown_on_sigint`（Windows 无 SIGINT 语义）。


---

## 七、本轮未做

| 编号 | 项 | 原因 |
| --- | --- | --- |
| U1 | 公司机（Windows 11 / Python 3.14.6）真机复跑 322 项 | 两台 Windows 都不在线；CI runner 只能算同类平台，不算同一台 |
| U2 | GUI 真窗口人工验收 | 无显示器；离屏冒烟 9/9 只能证明构造与切页 |
| U3 | 跨设备 MCP 闭环重跑 | 同上，Windows 侧 windows-mcp 不可达 |
| U4 | exe 打包（PyInstaller） | 需要在 Windows 上构建 |
| U5 | rc8 tag 与 SHA256SUMS | **第二次 CI 已全绿**（run `45174755219`，五个 job 全 success，两个 Windows job 都是 `Ran 322 tests` + `OK (skipped=1)`），tag 打在这个提交上并附 `SHA256SUMS` |
