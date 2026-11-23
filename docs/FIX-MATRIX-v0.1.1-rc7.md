# 修复总表：bigfish-council v0.1.1-rc7

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc7`（`bigfish/__init__.py:6`） |
| 日期 | 2026-11-23（工作树写文档日；`CHANGELOG.md` 的 rc7 段标题写的是 2026-11-20，两处不同源，以文件 mtime 为准） |
| 相对基线 | rc6 的工作树，HEAD `c0c54bc`（`docs: README 新增第 0 节——唯一下载渠道指向 GitHub Release v0.1.1-rc6`）。rc6 的**发布点**是 tag `v0.1.1-rc6` → commit `a2e9275751`，也就是 BFC-08 说的那个 `parents=0` 根提交 |
| 审计来源 | `docs/AUDIT-CODEX-v0.1.1-rc6.md`（Codex / GPT 在 `main` 上提交，commit `f5e955930c`），BFC-01～BFC-08 共八条。**该文件在本仓库工作树内没有副本**（`ls docs/AUDIT*` 为空），本文只能引用文件名与 commit，不能引用它的行号 |
| 分支 / HEAD | `fix/v0.1.1-rc7-hardening` / `c0c54bc`，**本轮全部改动未提交**（git commit 被明确禁止） |
| 差异规模 | 实测 `14 files changed, 609 insertions(+), 275 deletions(-)`（见补充说明一：任务书与事实清单记的是 +598/−271，差 +11/−4）。其中代码侧 `bigfish/`+`tests/` 11 文件 `417/208`；另新增 4 个未跟踪项（`requirements.lock`、`.github/workflows/ci.yml`、`tests/test_v011_rc7.py`、`docs/.rc7-facts.md`） |
| 库结构 | `SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`，**rc7 未改**，没有新增 `MIGRATIONS` 项，rc6 的库直接能用，不需要迁移也不需要回滚 |
| 全量测试 | `Ran 294 tests in 59.793s` / `OK` / RC=0（Debian 12 / Python 3.11.2 / SQLite 3.40.1 / offscreen） |
| 自检与冒烟 | `tools/selfcheck.py` 通过 16 项、失败 0 项；`tools/gui_smoke.py` 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes` `PF_RC=0` 零输出；`compileall -q` `CC_RC=0` |
| 平台 | 平台 A（Debian 12 / Python 3.11.2 / SQLite 3.40.1）已跑 294 项全绿；平台 B（Windows 11 / Python 3.14.6）**rc7 的 294 项未跑**，仍是 U5 |

状态口径：

- `已修复`：代码已改，且有能失败的回归测试，本批次在平台 A 上实测通过。
- `已修复（未覆盖）`：代码已改，但自动化证据只覆盖了一部分，缺什么在正文里点名。
- `已修复（文档级）`：改的是契约、声明或发布流程，本身没有可失败的自动化断言，只能靠人工复核兜。

严重度口径沿用 Codex 审计给的档，不自行改级：P1 = 直接让一条安全保证落空（恢复互斥、入口闸门、字段上限）；P2 = 不直接破保证，但会让声明与事实不一致，或把误导写进接口输出。

---

## 一、BFC-01～BFC-08 + MOBILE 修复总表

| 编号 | 级别 | 原始问题（一句话） | 修改文件（行号 / 函数） | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| BFC-01 | P1 | Windows 恢复互斥有 TOCTOU：旧的 `_probe_other_writers()` 先扫 pid 租约文件、再锁另一把锁，两把不是同一个内核对象 | `bigfish/locks.py` **整文件重写**（265 行）：`:37` `SLOTS = 64`、`:51` `access_lock_path`、`:56` `restore_gate_path`、`:66` `_posix_flock`、`:85` `_win_try_lock`、`:105` `WriterLease`（`:121` `acquire`、`:136` `_acquire_windows`、`:165` `release`）、`:200-265` `exclusive_access` | 写者身份与恢复排他压回同一个文件、同一个内核对象：POSIX 写者 `flock(LOCK_SH)` / 恢复 `flock(LOCK_EX)`；Windows 在访问锁文件里预留 64 字节，每个写者非阻塞独占 1 字节，恢复端一次独占 `[0,64)`。没有「检查→上锁」分离的两步，也就不存在可插队的窗口 | `tests/test_v011_rc7.py::TestLeaseAtomicity` 5 项（`:93`/`:118`/`:134`/`:144`/`:153`） | 已修复 |
| BFC-02 | P1 | MCP 没有字段级长度上限：REST 侧 Pydantic 早有 `max_length`，MCP 的 `tool_specs()` 只声明 `type: string` | `bigfish/server.py:431-441` `_validate_args`、`:508` `heartbeat.detail`、`:525-528` `log/diff/tests/note` | `_validate_args()` 读 `spec_k.get("maxLength")` 并校验；`tool_specs()` 给 5 处自由文本补 `MAX_TEXT_FIELD`（4 MiB，env `BFC_MAX_TEXT_FIELD` 可调）。超限走 `isError: true`，且在写库之前拒 | `tests/test_v011_rc7.py::TestMcpFieldLimit` 4 项（`:178`/`:186`/`:196`/`:207`） | 已修复 |
| BFC-03 | P1 | `create_app()` 只返回 app，第三方用 `uvicorn.run(create_app(...), host="0.0.0.0")` 能整条绕过 `check_bind_host()` | `bigfish/server.py:121-172` `LoopbackGuardMiddleware`、`:216-226` `create_app` docstring、`:242-244` 挂中间件、`:599-607` `create_production_app`、`:618-619` `serve`；`bigfish/gui.py:98`、`:106-110` | 两层：① 新增 `create_production_app()`，返回 `(app, host)` 且 host 先过 `check_bind_host()`，`serve`/`cli serve`/`gui.ApiServer` 全切过去，「忘写闸门」在代码结构上被消掉；② `LoopbackGuardMiddleware` 对**每一根连接**再判 `scope["client"][0]` 与 `scope["server"][0]`，任一非回环即 403 | `tests/test_v011_rc7.py::TestBindGate` 5 项（`:223`/`:228`/`:234`/`:241`/`:246`） | 已修复 |
| BFC-04 | P2 | 审计包 manifest 里 `contains_plaintext_credentials: false` 与同文件 `manual_review_required: true` 自相矛盾，布尔字段读起来像承诺 | `bigfish/backup.py:135-144`（操作包）、`:218-230`（审计包）；`tests/test_v011_rc5_review_audit.py:231-233` | 去掉布尔承诺，改枚举 `credential_status`：`none` / `hashes-only`（操作包）/ `pattern-scrubbed-unverified`（审计包），另加 `credential_detail` 说明哈希算法与不可逆性 | `tests/test_v011_rc7.py::TestManifestCredentialStatus` 3 项（`:262`/`:270`/`:278`） | 已修复 |
| BFC-05 | P2 | CLI 只有 `restore` 分支关库，其余分支靠进程退出来释放句柄 | `bigfish/cli.py:143-146`（`main` 的 try/finally）、`:149-159` `_close_quietly`、`:162` `_dispatch`、`:250-252`（restore 分支只剩 `dump`） | `main()` 拆成「打开 → `try: return _dispatch(...)` / `finally: _close_quietly(db)`」，关库从「每个分支的自觉」提升为「main 的生命周期」；restore 分支的内联 `finally` 删除，避免两处判定漂移 | `tests/test_v011_rc7.py::TestCliClosesDatabase::test_cli_returns_and_releases_lease`（`:291`） | 已修复 |
| BFC-06 | P2 | `allowed-ports` 只打印已实现端口，不打印 3099-3101 等预留端口 | `bigfish/cli.py:167-177`（`_dispatch` 的 `allowed-ports` 分支） | 输出分两块：`implemented`（`PORT_ROLES` 三项）与 `reserved`（`PORT_RESERVED` 六项，key 是端口号字符串，value 是 `{name, note}`），让「这个号有主」成为输出的一部分 | `tests/test_v011_rc7.py::TestCliClosesDatabase::test_allowed_ports_lists_reserved`（`:301`） | 已修复 |
| BFC-07 | P2 | 发布工程不闭环：没有 lockfile、没有 CI、没有 exe 产物 | 新增 `requirements.lock`（53 行，39 个精确 pin）、`.github/workflows/ci.yml`（106 行） | lock 文件是全传递依赖的精确版本，来自 rc7 验证过的 venv；CI 是 ubuntu-latest × windows-latest × Python 3.11/3.13 四格矩阵，跑 pyflakes / compileall / 单测 / selfcheck / gui_smoke，另加一个不装依赖的 `security-note` job 扫后缀与凭据正则。exe 产物仍未做（U2） | `tests/test_v011_rc7.py::TestReleaseEngineering` 3 项（`:318`/`:328`/`:337`） | 已修复（未覆盖） |
| BFC-08 | P2 | tag `v0.1.1-rc6` 指向根提交 `a2e9275751`（`parents=0`），与 `main` 历史不连通，溯源链断了 | 无代码改动（发布流程） | 本版起改为在**真实提交**上打带注释 tag，不再用「导出重写」的方式造发布点。当前仓库 `git tag -l` 为空，**tag 尚未打，待执行** | 无自动化断言，人工核对 | 已修复（文档级） |
| MOBILE | P1 | 移动端权限模型未落地；而 `role_for_db_row()` 对未知 db_role 兜底成执行器，直接注册 `mobile` 身份会当场拿到执行器权限 | `bigfish/roles.py:13-18` `MOBILE_VIEWER`、`:27` 标签、`:44-47` 权限集、`:50-55` `_DB_ROLE_MAP`、`:83-86` `role_for_db_row`；`bigfish/core.py:160-174` `register_executor` | 先登记再放行：`MOBILE_VIEWER` 只给 `task.read` / `event.read` / `review.create`（刻意不含 `task.claim` / `run.*` / `approve` / `backup.*`）；`_DB_ROLE_MAP` 加 `"mobile"`；`register_executor` 的白名单从硬编码三元组改成 `R.DB_ROLES`，让「能注册什么角色」与「角色表里有什么」永远同源；token 前缀 `bfc_mb_` | `tests/test_v011_rc7.py::TestMobileViewerRole` 5 项（`:348`/`:354`/`:361`/`:366`/`:371`）+ `tests/test_v011_p2.py:55`/`:67` + `tests/test_v011_rc4.py:465` | 已修复（未覆盖） |

### 补充说明一（差异规模口径，与任务书 / 事实清单不一致）

任务书与 `docs/.rc7-facts.md` 记的是 `14 文件 +598/−271`，本仓库实测不成立。用 `git diff --shortstat` 与 `git diff --numstat` 两种算法都是：

```text
14 files changed, 609 insertions(+), 275 deletions(-)
```

逐文件 numstat（`+`/`−`）与实测偏差如下，为了后面对账方便，把口径全列出来：

| 文件 | 实测 | 事实清单 |
| --- | --- | --- |
| `AUDIT.md` | 81 / 14 | 95 行区间（stat 图） |
| `CHANGELOG.md` | 65 / 0 | 65 |
| `README.md` | 46 / 53 | 84 行区间 |
| `bigfish/__init__.py` | 1 / 1 | 2 行区间 |
| `bigfish/backup.py` | 14 / 6 | 20 行区间 |
| `bigfish/cli.py` | 32 / 13 | 45 行区间 |
| `bigfish/core.py` | 6 / 3 | 9 行区间 |
| `bigfish/gui.py` | 9 / 9 | 18 行区间 |
| `bigfish/locks.py` | 202 / 145 | 347 行区间 |
| `bigfish/roles.py` | 18 / 7 | 25 行区间 |
| `bigfish/server.py` | 91 / 16 | 107 行区间 |
| `tests/test_v011_p2.py` | 29 / 4 | 33 行区间 |
| `tests/test_v011_rc4.py` | 9 / 2 | 11 行区间 |
| `tests/test_v011_rc5_review_audit.py` | 6 / 2 | 8 行区间 |

为什么会差 +11/−4：`git diff --stat` 那一列是「改动块总行数的图形化显示」，不是 insertions/deletions；把 stat 图上的数字当成增删行数就会系统性偏大。本文正文的行号与用例数一律按工作树实际文件核对。

代码侧与文档侧要分开看：`bigfish/`+`tests/` 共 11 文件 `417/208`，其中 `bigfish/` 单独 8 文件 `373/200`、`tests/` 3 文件 `44/8`；三份文档（AUDIT + CHANGELOG + README）合计 `192/67`，它们不计入代码改动。

另有 4 个未跟踪项，不在上面 14 个文件里，但确实是本轮产物：`requirements.lock`（53 行 / 1348 字节）、`.github/workflows/ci.yml`（106 行 / 3689 字节）、`tests/test_v011_rc7.py`（377 行 / 16703 字节）、`docs/.rc7-facts.md`（97 行，本轮数据源）。

### 补充说明二（审计原文不在仓库里）

`docs/AUDIT-CODEX-v0.1.1-rc6.md` 按 Codex 的说法提交在 `main` 上（`f5e955930c`），本仓库工作树里既没有这个文件也没有这个对象。因此本文对八条意见的「原始问题」只能做转述，不能标注审计原文行号；转述时尽量保留审计用的原词（TOCTOU、maxLength、contains_plaintext_credentials 等），方便回头比对。

---

## 二、逐条：为什么这样修

这一节写的是「为什么选这个修法」，不是「改了什么」。改了什么见第三节与 `DIFF-SUMMARY-v0.1.1-rc7.md`。

### BFC-01：把「判断」和「动作」压进同一个内核对象

旧实现的问题不是「没加锁」，而是把判断与动作分到了两个内核对象上：`_probe_other_writers()` 去扫 `*.access.<pid>.lock`，`exclusive_access()` 去锁 `council.db.access.lock`。扫描返回「没人」之后、上锁之前，新的 `Database` 完全可以在**另一个文件**上建成自己的锁——扫描的结论在那一刻就已经过期了。这就是 TOCTOU 的完整形态：不是概率问题，是逻辑上必然存在的窗口。

为什么不做成「探测 + 重试到稳定」：这类方案在并发下永远存在最后一刻的窗口，且失败率随进程数上升，最坏情况是恢复永远抢不到窗口。互斥的正确性只能来自「判断与动作是同一个原子操作」，也就是同一个内核对象上的同一次加锁。

于是把写者身份和恢复排他压回同一个文件：

- POSIX 上这件事是现成的——`flock` 的 `LOCK_SH`（可多份）与 `LOCK_EX`（排他）天然互斥，写者与恢复是同一把锁的两种模式（`locks.py:66` `_posix_flock`）。
- Windows 的 `msvcrt` 只有排他锁、没有共享锁。于是换成区间模型：访问锁文件里预留 `SLOTS = 64` 字节，每个写者非阻塞独占 1 字节（`:85` `_win_try_lock`），恢复端一次独占 `[0, 64)`（`:229`）。区间是否冲突由内核判定，同样是原子的——「能不能占到一个槽」与「占槽成功」是一件事。

代价要写清楚：Windows 上同时最多 64 个写者（超了明确抛 `LeaseError`，不静默放行），槽位从 `os.getpid() % SLOTS` 起找以减少碰撞（`:149`）。

还有一处代价是踩出来的：`release()` 里 `unlink` 锁文件会让新来的实例在**新的 inode** 上加锁，而老实例还锁着旧 inode，两边各用各的锁，互斥当场失效。rc7 一度就是这么写的，`tests.test_v011_rc6` 里「另开实例阻止恢复」那条用例当场失败。现在明确不删，并把原因写成注释（`:187-190`），再加一条 `test_lock_files_are_never_deleted` 钉住。

同进程多实例那条属于 Windows 特有的坑：`msvcrt.locking` 的区间锁是**按文件句柄**记的，同进程用第二个句柄锁同一区间必 `EACCES`（这就是 rc6 平台 B 的缺陷 17）。所以 Windows 分支用模块级 `_LOCAL` 共享句柄 + 引用计数 + 槽位号（`:41-44`、`:136-162`、`:171-180`），POSIX 不需要，因为 `flock` 允许多个句柄各自 `LOCK_SH`。

### BFC-02：让两条入口的安全水位对齐

整体的 `MAX_BODY_BYTES`（8 MiB）挡不住「单字段吃掉几乎整个请求体」：一个 7.9 MiB 的 `detail` 在 JSON 反序列化阶段就已经进内存了，而 `scrub` 和截断发生在业务层、在它后面。REST 侧之所以安全，是因为 Pydantic 的 `Field(max_length=...)` 是**声明式**的，在解析阶段就拒；MCP 侧 `inputSchema` 里只有 `type`，`_validate_args` 也只查类型，于是同一个执行器「用 REST 被 422、用 MCP 畅通」——安全水位取决于走哪条入口，这本身就是缺陷。

修法不是「加个 if」，而是让 schema 与校验器共用同一个字段级上限：`tool_specs()` 里声明 `maxLength: MAX_TEXT_FIELD`，`_validate_args()` 读同一个 key 来校验（`server.py:439-441`）。声明与执行同源，以后新增工具只要填 `maxLength` 就自动被校验，不需要记着去改校验器。

两个刻意的选择：

1. 边界值必须放行。`test_at_limit_field_is_accepted`（`:196`）用恰好 `MAX_TEXT_FIELD` 的字符串断言不报错，防止写成 `>=`。
2. 超限必须在写库之前拒。`test_oversized_result_log_is_error_and_nothing_written`（`:207`）断言 `get_task(tid)["runs"] == []`——如果校验放在业务层之后，会留下半条 artifact。

### BFC-03：不封死入口，改为让绕过路径也拿不到数据

rc6 的 F-08 选择了「写清边界」（在 docstring 里劝人别 `uvicorn.run(create_app(...))`），理由是 `TestClient` 依赖 `create_app` 不 bind，封死会连带废掉全部 API 测试与 GUI 内嵌用法。这个理由本身成立，但它把风险留在了「使用者的自觉」上：只要有人绕过 `serve()`，`check_bind_host()` 根本不在这条调用路径上。

rc7 的做法是承认「入口守不住所有人」，于是把闸门做成两层：

1. **启动前**：`create_production_app()` 成为唯一官方工厂，返回 `(app, host)`，host 已被校验/归一化（`server.py:599-607`）。`serve()`（`:618`）、`cli serve`、`gui.ApiServer.start()`（`gui.py:106`）全部切过去。这一步的意义在于把「忘写闸门」从人的失误变成结构上做不到：想拿 app 就得选工厂，选官方工厂就拿不到未校验的 host。
2. **每根连接**：`LoopbackGuardMiddleware` 同时看 `scope["client"][0]`（对端）与 `scope["server"][0]`（本端实际落到的地址），任一非回环就 403。这两个值都由传输层填充，客户端伪造不了。即使有人硬把 app 绑到 `0.0.0.0`，外部请求也只能拿到 403。

为什么保留 `create_app()`：测试与进程内挂载需要「不校验地址」的入口。所以不封死入口，而是让绕过路径拿不到数据——这是比「劝你别用」更硬的保证。

为什么 `TRUSTED_PEERS` 只放 `testclient` / `testserver` 两个常量：它们是 Starlette `TestClient` 在进程内写死的名字，不经过任何 socket，外部连接不可能把自己的对端地址变成这个值。一旦把 `127.0.0.1` 之类放进名单，中间件就等于没有——`test_trusted_peers_are_only_the_in_process_constants`（`:246`）专门钉这一条。

同时把 `create_app()` 的 docstring 从「本函数不负责监听地址（rc6 F-08）」改成「**本函数不校验监听地址**（供测试/进程内挂载）」并写出两道防线的分层（`:216-226`）。改法不同，读法就不同：前者读起来像免责声明，后者读起来像说明+分工。

### BFC-04：布尔字段读起来像承诺，枚举只能陈述事实

问题不在「`false` 是错的」，而在布尔字段的语义：`contains_plaintext_credentials: false` 在机器读法是「这个 key 的值不是 true」，在人读法里是「我保证没有明文凭据」。而审计包的白名单只证明「没导出 `executors` / `tickets` 那几列」，加上几条正则没命中，推不出「没有凭据」——换行拆分、编码变形、私有格式都不在正则覆盖范围内。更糟的是同一份 manifest 里还写着 `manual_review_required: true`，两句话互斥。

改成枚举之后，字段只回答「我做了什么」：

- `none`：明确不含任何凭据形态；
- `hashes-only`：只含不可逆哈希、无明文（操作包）；
- `pattern-scrubbed-unverified`：按已知模式脱敏过、未证明干净（审计包）。

操作包之所以能给较强结论，是因为它确实只含 `executors.token_hash` 与 `tickets.token_hash`（SHA-256、不可逆），这一点写进 `credential_detail`（`backup.py:142-144`），读者不必再翻代码。审计包则老实自报「模式脱敏、未验证」（`:221`），并保留 `credential_scan` 块的 `manual_review_required`。

这是「诚实性」类修复的通用形态：当一件事无法证明时，把声明从「结论」降级为「过程」。

### BFC-05：关库是 main 的生命周期，不是每个分支的自觉

rc6 只给 `restore` 分支补了 `finally: db.close()`，理由是「其他分支是短命只读操作，交给进程退出即可」。这个理由在 Linux 上成立（进程退出即释放 fd），在 Windows 上不成立：进程未退出前文件仍被占用，而「退出」的时机不归 CLI 控制——它是被脚本调用的，调用方紧接着可能移动、删除、再次恢复。

更根本的问题是「关库」被写成了每个分支的自觉：分支越多越容易漏，而新加分支的人根本不会想到这件事。所以修法不是再补一个 `finally`，而是把关库提升到 `main()` 的生命周期：`main` 只做 parse_args → 打开 → `try: return _dispatch(...)` / `finally: _close_quietly(db)`（`cli.py:143-146`）。restore 分支里那段内联 `finally` 整段删掉（`:250-252` 只剩 `dump`），避免两处判定漂移。

`_close_quietly` 吞掉关闭异常（`:149-159`），是不让收尾失败掩盖原始返回值或异常——收尾可以静默，主流程不能。

### BFC-06：`allowed-ports` 的输出在误导人

`allowed-ports` 是「我要开端口，先看哪些号是合法的」这个动作的唯一入口。rc6 只 `dump` `PORT_ROLES`（三项已实现），把 `PORT_RESERVED` 六项留在代码里不打印。读者的合理推论就是「3099-3101 空着，我拿去用」——而这三个号分别预留给 mobile REST / mobile SSE / MCP bridge，被占用之后这些功能上线时就要改端口或改契约。

这不是文档问题，是接口在误导。修法是让输出自带语义：`implemented` 与 `reserved` 分成两块，`reserved` 的 key 是端口号字符串、value 是 `{name, note}`（`cli.py:167-177`）。测试同时钉两头——`implemented` 的键集恰为三项、`reserved` 含 3099/3100/3101（`:301`），防止以后有人又把 `reserved` 删掉。

### BFC-07：把「我这台机器绿」变成「谁都能复现、每次提交都有人跑」

没有 lockfile 意味着「我这边 294 项全绿」不能复现到别人机器：`requirements.txt` 是范围声明，传递依赖会漂，而本轮改动恰好包括并发与文件锁——这类东西对依赖版本比业务代码更敏感。CI 意味着「每次提交都有人跑一遍」这件事之前完全依赖人肉，靠自觉。

rc7 补的是最小闭环，不是完备发布工程：`requirements.lock` 39 个精确 pin（来自 rc7 验证过的 venv，生成命令写在文件头）+ `.github/workflows/ci.yml` 的 ubuntu/windows × 3.11/3.13 四格矩阵，跑 pyflakes / compileall / 单测 / selfcheck / gui_smoke。

两个细节值得写下来：锁文件里排除 `pyflakes` / `pip` / `setuptools` / `wheel`，因为前者是开发期工具、后三个是引导工具，锁死它们会让新环境装不上；`security-note` job 刻意不装依赖，只做两件不需要依赖的事——按后缀拦 `.db/.key/.pem/.zip/.exe`、按正则拦 `ghp_` / `bfc_*` / PEM 私钥头——跑得最快，先给结论。`test_ci_secret_scan_actually_matches_tokens`（`:337`）反过来验证那条正则真的能命中，因为「写了一条永远匹配不上的正则」等于没扫。

状态只能是`已修复（未覆盖）`：这条工作流从未在真实 GitHub runner 上执行过（本仓库没有远端 CI 记录），exe 产物仍是 U2。

### BFC-08：发布点不能是一条孤立历史

tag 指向 `parents=0` 的根提交，意味着这个 tag 的祖先链里没有 `main` 的任何一段。拿 tag 去比对、去 cherry-pick、去追「这个版本到底改了哪些文件」，全都断在第一步。

这不是代码缺陷，是发布流程缺陷：用「导出重写」的方式造发布点，每次都会产生一条孤立历史，而且看起来一切正常——直到有人真的要溯源。

修法只能是流程：本版起在真实提交上打**带注释** tag（annotated tag 带 tagger、日期、说明，可被 `git verify-tag`、`git describe` 追溯）。当前仓库 `git tag -l` 为空，这条**尚未落地**，所以状态记 `已修复（文档级）`，并且这里如实写「待执行」而不是「已生效」。

### MOBILE：先登记，再放行

移动端第一版只需要「能看见、能留言」，不需要「能干活」。真正的风险不在权限表，而在 `roles.role_for_db_row()` 的兜底：未知 `db_role` 一律返回 `EXECUTOR_DSH`（`roles.py:94`），也就是「没登记 = 拿到执行器权限」。如果直接注册一个 `role="mobile"` 的身份而不改映射表，手机端会当场变成执行器——能领任务、能心跳、能提交。

所以顺序必须是「先登记，再放行」，三步：

1. `roles.py` 加 `MOBILE_VIEWER`，权限只给 `task.read` / `event.read` / `review.create`（`:44-47`），并在注释里写明「刻意不含 `task.claim` / `run.*` / `approve` / `backup.*`：手机丢了也不至于能把任务领走或把库导出去」；
2. `_DB_ROLE_MAP` 加 `"mobile"`（`:55`），并重写那段警告注释——旧注释写的是「mobile 尚未实现，故意不登记」，实现之后不改就成了错误文档；
3. `core.register_executor` 的白名单从硬编码 `("executor", "reviewer", "admin")` 改成 `R.DB_ROLES`（`core.py:163-164`），让「能注册什么角色」与「角色表里有什么」永远同源。

`role_for_db_row` 的判定同时从 `if base is not None` 改成 `if db_role in _DB_ROLE_MAP and base is not None`（`roles.py:83-86`）。行为等价（未登记时 `base` 必为 `None`），但把「登记」这件事显式写进条件，读代码的人能看出这里有一道白名单，而不是一个可能为 `None` 的取值。

状态 `已修复（未覆盖）`：角色、鉴权、注册、映射四层都有测试，但 REST/SSE 端点本身没实现（U6）、apk 没打（U7）。

---

## 三、本轮顺手修的两条真缺陷

两条都不在 BFC 清单里，是改 rc7 时撞见、并当场确认「不修就会误导后来人」的。列出来是因为它们改变了文件内容，不写会让对账的人对不上。

| # | 问题 | 修改文件（行号） | 修法 | 为什么算真缺陷（而不是洁癖） | 验证 |
| --- | --- | --- | --- | --- | --- |
| S1 | `bigfish/gui.py` 顶部有 `from .server import create_app`，而 `ApiServer.start()` 里又 `from .server import ...`，模块级那条成了未使用导入 | `bigfish/gui.py:22-26`（导入区）、`:96-113` `ApiServer.start` | 删掉模块级 `create_app` 导入；`start()` 内改成 `from .server import BindError, create_production_app`，并把 `thread.is_alive()` 判断提到工厂调用之前 | 两层：① 它让 `pyflakes` 报 F401，而「pyflakes 零输出」是发布门槛之一；② 更要紧的是，rc7 之后 `create_app` 已经是「拿不到闸门的工厂」，把它留在文件顶部等于给后来人一个默认会选错的引用 | `pyflakes bigfish tests tools` 零输出；`gui_smoke` 9/9 |
| S2 | `bigfish/server.py` 的 `create_production_app()` 原标注 `-> FastAPI`，实际 `return` 的是 `(app, host)` 元组 | `bigfish/server.py:599-600`（签名） | 标注改为 `-> tuple[FastAPI, str]`，并在 docstring 里写明「返回的 tuple 第二项是校验后的 host」 | 这是本轮新加的**唯一官方工厂**。标注错误会让第一个照着用的人把 tuple 当 app 传给 `uvicorn.Config`，症状是启动时报难以理解的属性错误；静态检查与 IDE 也会把 `app, host = create_production_app(...)` 标成解包错误 | `pyflakes` 零输出；`TestBindGate::test_production_factory_accepts_loopback` 直接解包使用 |

---

## 四、按文件汇总

| 文件 | 承担的项 | 关键落点 |
| --- | --- | --- |
| `bigfish/locks.py` | BFC-01（整文件重写，265 行） | `:37` `SLOTS`、`:51`/`:56` 两个锁文件名、`:66`/`:77` POSIX、`:85`/`:96` Windows 区间锁、`:105-196` `WriterLease`、`:200-265` `exclusive_access` |
| `bigfish/server.py` | BFC-02、BFC-03 | BFC-02：`:416-445` `_validate_args`、`:508`、`:525-528`；BFC-03：`:121-172` 中间件、`:216-226` docstring、`:242-244` 挂载、`:599-607` 工厂、`:618-619` `serve` |
| `bigfish/roles.py` | MOBILE | `:13-18`、`:27`、`:44-47`、`:50-55`、`:83-86`、`:94`（兜底未动，只加注释说明） |
| `bigfish/cli.py` | BFC-05、BFC-06 | `:143-146` try/finally、`:149-159` `_close_quietly`、`:162` `_dispatch`、`:167-177` `allowed-ports`、`:250-252` restore 分支瘦身 |
| `bigfish/backup.py` | BFC-04 | `:135-144` 操作包 `credential_status`/`credential_detail`、`:218-230` 审计包枚举 + `credential_scan` |
| `bigfish/core.py` | MOBILE | `:160-164` 白名单改 `R.DB_ROLES`、`:173-174` `bfc_mb_` 前缀 |
| `bigfish/gui.py` | BFC-03、S1 | `:22-26` 删未使用导入、`:96-113` 改用 `create_production_app` 并把 thread 判断提前 |
| `bigfish/__init__.py` | 版本号 | `:6` `__version__ = "0.1.1-rc7"` |
| `tests/test_v011_rc7.py` | 全部八条 + MOBILE（新增，377 行） | 7 类 27 项，见第五节 |
| `tests/test_v011_p2.py` | MOBILE | `:43` 去掉硬编码 4、`:55` 新增「不能执行」、`:67` 新增「不兜底成执行器」 |
| `tests/test_v011_rc4.py` | MOBILE | `:465` 从「mobile 不该在 DB_ROLES」改成「在、但映射到只读且无 task.claim」 |
| `tests/test_v011_rc5_review_audit.py` | BFC-04 | `:231-233` 断言枚举值 + 断言布尔 key 已消失 |
| `requirements.lock` | BFC-07（新增） | 53 行，39 个精确 pin |
| `.github/workflows/ci.yml` | BFC-07（新增） | 106 行，四格矩阵 + `security-note` |

---

## 五、用例分布与增量

本轮实测 `grep -c 'def test_'` 计数（共 294 项）：

`test_api` 6、`test_core` 15、`test_v011_p0` 16、`test_v011_p1` 24、`test_v011_p2` 26、`test_v011_p3` 20、`test_v011_rc3` 26、`test_v011_rc4` 33、`test_v011_rc5_cli_guard` 4、`test_v011_rc5_dispatch_id` 3、`test_v011_rc5_key_heal` 8、`test_v011_rc5_proxy` 9、`test_v011_rc5_receipts` 24、`test_v011_rc5_restore_authority` 24、`test_v011_rc5_review_audit` 13、`test_v011_rc6` 16、`test_v011_rc7` 27。

265 → 294 的 +29 项拆开：

| 来源 | 项数 | 对应条目 |
| --- | --- | --- |
| `tests/test_v011_rc7.py`（新文件，7 类） | 27 | 租约原子性 5（BFC-01）、MCP 字段上限 4（BFC-02）、监听闸门 5（BFC-03）、manifest 枚举 3（BFC-04）、CLI 关库与端口 2（BFC-05/06）、发布工程 3（BFC-07）、手机角色 5（MOBILE） |
| `tests/test_v011_p2.py` | +2 | 「手机端不能执行」「不兜底成执行器」（MOBILE），同时把原 `test_four_canonical_roles` 改名并去掉硬编码 4（改名不增减项数） |
| 合计 | **+29** | 265 → 294 |

BFC-08 没有对应用例（改的是发布流程，不是行为），所以九条里八条有测试、一条靠人工。Note：版本对比口径是 rc6 的 265 项（见 `FIX-MATRIX-v0.1.1-rc6.md` 第五节）。

---

## 六、残余风险与未完成（U1～U8）

| 编号 | 项 | 状态 | 影响 | 下一步 |
| --- | --- | --- | --- | --- |
| U1 | Windows 真机 GUI 窗口验收 | 未做 | BFC-03 的 GUI 侧只到「工厂调用正确」这一层，没有人真的在 Windows 上点一次启动/恢复，看服务是否真停、真起 | 在一台真 Windows 11 上手工走一遍启动 → 恢复备份 → 看 API 线程与 watchdog 是否按预期停/起 |
| U2 | exe 产物（PyInstaller） | 未做 | BFC-07 只补了 lockfile 与 CI，没有可分发的 exe；「双击就能用」这条承诺仍未兑现 | 先用 PyInstaller 出单文件包，再在干净的 Windows 上验证 `PySide6` 插件与证书加载 |
| U3 | 开机自启注册 | 未做 | 需要常驻的组件（watchdog）目前要手动启动，重启机器后不会自己回来 | 注册表 `Run` 键或计划任务，做幂等写入 + 卸载清理 |
| U4 | 跨设备 MCP 闭环 rc7 重跑 | 未做（rc2 时期 11/11 通过） | BFC-02 改的是 MCP 校验路径，未在 Tailscale 两侧真机之间重跑 claim → heartbeat → submit 全链路 | 两侧真机重跑 11 项，并专门验一次「超长字段被拒」在真实网络下的表现 |
| U5 | 平台 B 跑 rc7 的 294 项 | 未做 | 本轮 BFC-01 的重写正好落在 Windows 分支（槽位模型 + `_LOCAL`），而这条分支**没有任何真机记录**。rc6 已经证明过这类路径只靠 Linux 不可能验出来（缺陷 17 就是这么暴露的） | 在 Windows 11 / Python 3.14.6 上跑全量 294 项，先看 5 项 `TestLeaseAtomicity` 与 `_acquire_windows` 槽位寻址 |
| U6 | 移动端 REST/SSE 端点 | 未做 | MOBILE 只落地了「角色 + 鉴权 + 注册 + 映射」，手机端真正要调的接口一个都没有；`docs/ARCHITECTURE-FUTURE.md` 里只有草案 | 先定只读端点集（任务列表、事件流、留言），再定 3099/3100 的鉴权模型 |
| U7 | apk 打包 | 未做 | 没有分发形态 | 依赖 U6 先有端点，再选壳（WebView 或原生） |
| U8 | `core.py`/`server.py` 分层重构 | 未做 | `core.py` 1379 行、`server.py` 620 行，职责已经明显混在一起（`server.py` 里既有 ASGI 组装、中间件、还有 `tool_specs` 与业务分发） | 先切出 MCP 层，再切出传输层；本轮只在 `server.py` 里加东西，没有动结构 |

一句总结：**九条里八条有能失败的回归测试（BFC-08 靠人工），但其中 BFC-01 的关键路径、BFC-07 的 CI 都还没有真机/真实 runner 记录，U5 是本轮最大的窟窿。**

---

## 七、复现与复核命令

```bash
cd /home/dsh/bigfish-council

# 全量 294 项
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 自检与 GUI 冒烟
.venv/bin/python tools/selfcheck.py
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py

# 静态检查
.venv/bin/python -m pyflakes bigfish tests tools
.venv/bin/python -m compileall -q bigfish tests tools
```

按审计项定位测试：

- BFC-01 → `tests/test_v011_rc7.py::TestLeaseAtomicity`（5 项）
- BFC-02 → `tests/test_v011_rc7.py::TestMcpFieldLimit`（4 项）
- BFC-03 → `tests/test_v011_rc7.py::TestBindGate`（5 项）
- BFC-04 → `tests/test_v011_rc7.py::TestManifestCredentialStatus`（3 项）+ `tests/test_v011_rc5_review_audit.py::test_manifest_declares_allowlist_policy`
- BFC-05 → `tests/test_v011_rc7.py::TestCliClosesDatabase::test_cli_returns_and_releases_lease`
- BFC-06 → `tests/test_v011_rc7.py::TestCliClosesDatabase::test_allowed_ports_lists_reserved`
- BFC-07 → `tests/test_v011_rc7.py::TestReleaseEngineering`（3 项）
- BFC-08 → 无（人工核对 tag）
- MOBILE → `tests/test_v011_rc7.py::TestMobileViewerRole`（5 项）+ `tests/test_v011_p2.py::TestRoleTable` 两项 + `tests/test_v011_rc4.py::test_roles_db_whitelist`

单跑某一类：

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest tests.test_v011_rc7 -v
```

核对本轮差异规模（本文用的口径）：

```bash
git diff --shortstat
git diff --numstat | awk '{a+=$1; d+=$2} END {print "all", a, d}'
git diff --numstat -- bigfish tests | awk '{a+=$1; d+=$2} END {print "code", a, d}'
git status --short
```
