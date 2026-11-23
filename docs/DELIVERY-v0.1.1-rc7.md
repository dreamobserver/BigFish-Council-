# BFC v0.1.1-rc7 交付说明

> 本文每个数字都来自本轮实跑：全量单测 294 项在 Debian 12 / Python 3.11.2 上跑完 `RC=0`，
> 自检 16 项、GUI 冒烟 9 项全过，pyflakes 零输出。没有「应该能过」「大概跑过」这种话。
> 平台 B（Windows 11 / Python 3.14.6）本轮**没有**跑 rc7 全量测试，凡涉及平台差异的结论都标了来源平台。
> 唯一数据源是 `docs/.rc7-facts.md`；凡是那份清单里没有的，这里也不写。

## 0. 状态表

| 项目 | 值 |
| --- | --- |
| 版本号 | `0.1.1-rc7`（`bigfish/__init__.py:6`） |
| 分支 | `fix/v0.1.1-rc7-hardening` |
| 对比基线 | `v0.1.1-rc6`（发布点 commit `a2e9275751`） |
| 基线前最后一个提交 | `c0c54bc`（docs: README 新增第 0 节，把唯一下载渠道指向 GitHub Release v0.1.1-rc6） |
| 本批改动规模 | 14 个已跟踪文件 `+598 / -271`，另有 3 个未跟踪新增文件 |
| 测试数 | 294 项（18 个测试文件），本轮 `Ran 294 tests ... OK`，RC=0 |
| 源码规模 | `bigfish/` 15 个文件共 6462 行 |
| 平台 A（已实测） | Debian 12 / Python 3.11.2 / SQLite 3.40.1 |
| 平台 B（未实测 rc7） | Windows 11 / Python 3.14.6 |
| `SCHEMA_VERSION` | 4（未变） |
| `BACKUP_FORMAT_VERSION` | 4（未变） |
| 定性 | 加固版：修 8 条审计项 + 加一个只读角色，**不动库结构** |
| 现役部署 | 未推送，3096 上跑的仍是旧版 |

一句话：rc7 全是「让已有功能在边界上不塌」的加固，没有新表、没有新列、没有新迁移，
回滚时库可以被 rc6 直接打开；只有 mobile 这一个新角色名对 rc6 是未知值，处置写在回滚文档里。

---

## 1. rc7 交付内容

九条，每条一句话结论。BFC-01 到 BFC-08 来自本轮审计，MOBILE 是新增能力。

### 1.1 九条结论

| # | 一句话结论 | 落点 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- |
| BFC-01 | Windows 上「恢复」与「普通写库」的互斥原来有 TOCTOU 窗口，现在改成同一个内核对象上的原子语义，窗口消失。 | `bigfish/locks.py`（整文件重写） | `TestLeaseAtomicity` 5 项 | ✅ 完成 |
| BFC-02 | MCP 通道过去没有字段级长度上限，单个自由文本字段可以顶满整个请求体；现在 5 个自由文本字段都声明了 `maxLength` 并真的校验。 | `bigfish/server.py:183`、`:191-194`、`:508`、`:525-528` | `TestMcpFieldLimit` 4 项 | ✅ 完成 |
| BFC-03 | `create_app()` 可以被 `uvicorn.run(host="0.0.0.0")` 绕过监听闸门，现在生产入口换成 `create_production_app()`，外加逐连接复查的中间件，两道门。 | `bigfish/server.py`（工厂 + `LoopbackGuardMiddleware`） | `TestBindGate` 5 项 | ✅ 完成 |
| BFC-04 | 备份清单里 `contains_plaintext_credentials: false` 与 `manual_review_required: true` 自相矛盾，现在换成三值枚举 `credential_status`，把「没有明文凭据」和「没做过人工复核」拆成两件事。 | `bigfish/backup.py:122-147`、`:221-233` | `TestManifestCredentialStatus` 3 项 | ✅ 完成 |
| BFC-05 | CLI 过去只有 `restore` 分支会关库，其余命令靠进程退出来兜底；现在 `main()` 拆出 `_dispatch()` 并用 `try/finally` 无条件关库，写者租约当场释放。 | `bigfish/cli.py` | `TestCliClosesDatabase` 2 项 | ✅ 完成 |
| BFC-06 | `allowed-ports` 只打印在用的三个端口，预留的 3099-3101 看不见，容易被人当成「没人用」；现在输出分 implemented 与 reserved 两段。 | `bigfish/cli.py:167-176` | `TestCliClosesDatabase::test_allowed_ports_lists_reserved` | ✅ 完成 |
| BFC-07 | 发布工程不闭环：没有 lockfile、没有 CI。现在补上 `requirements.lock`（全传递依赖精确 pin）与 `.github/workflows/ci.yml`（双平台 × 双 Python 版本 + 密钥扫描）。exe 产物仍没做，见 U2。 | `requirements.lock`、`.github/workflows/ci.yml` | `TestReleaseEngineering` 3 项 | ⚠️ 部分完成（exe 未做） |
| BFC-08 | `v0.1.1-rc6` 那个 tag 指向根提交 `a2e9275751`（`parents=0`），和 main 历史不连通，按它 checkout 出来的是孤儿树。本版起改为在真实提交上打带注释 tag。 | 发布流程（人工） | 无自动化测试 | ⚠️ 流程已改，待下次发版验证 |
| MOBILE | 新增只读角色 `mobile_viewer`：能看任务和事件、能提 review，但**领不走任务、发不了心跳、交不了结果、导不出库**。 | `bigfish/roles.py:15`、`:46`、`:54-55`；`bigfish/core.py:163-174` | `TestMobileViewerRole` 5 项 | ✅ 完成（仅角色层） |

### 1.2 BFC-01：为什么必须整文件重写

rc6 的 Windows 实现是「先扫一遍别的 pid 的租约文件，再去锁另一把文件」。
扫描和上锁之间是两把不同的内核对象，新写者可以正好插进这个缝里——这就是审计说的 TOCTOU，
「我检查过了没人」这句话在上锁的那一刻已经过期。

rc7 换成一个内核对象 + 原子语义（`bigfish/locks.py:11-25` 的模块 docstring 写的就是这个推导）：

- POSIX：访问锁文件上写者持 `flock` `LOCK_SH`，恢复持 `LOCK_EX`，两者天然互斥，flock 本身原子，不需要扫谁。
- Windows：`msvcrt` 只有排他区间锁、表达不了共享锁，所以改**槽位模型**——访问锁文件预留 `SLOTS = 64`（`bigfish/locks.py:37`）个字节，每个写者独占其中 1 字节（从自己 pid 对应的槽开始往后找空槽，`bigfish/locks.py:149-151`），恢复端一次性锁住整段 64 字节（`bigfish/locks.py:229`）。任何一个写者占着槽，恢复就锁不上；反过来也成立。

`exclusive_access()`（`bigfish/locks.py:200`）里两道锁各管一件事，顺序固定：`restore_gate` 排他只为把「另一个恢复在跑」和「有写者在用」区分成两种看得懂的报错，正确性由访问锁保证（`bigfish/locks.py:203-208` 注释明写）。

### 1.3 BFC-02：MCP 的字段上限

REST 侧一直是 pydantic `Field(max_length=...)`，MCP 侧的 schema 只写了 `"type": "string"`，
于是 `council_heartbeat` 的 `detail`、`council_submit_result` 的 `log` / `diff` / `tests` / `note`
这 5 个自由文本字段可以单字段顶满请求体上限。rc7 在 MCP 的 tool schema 里补 `maxLength`（`bigfish/server.py:508`、`:525-528`），
并在 `_validate_args` 里真的执行——超限直接报错，**且一个字节都不落库**（`test_oversized_result_log_is_error_and_nothing_written`）。

### 1.4 BFC-03：监听地址的两道门

原来 `create_app()` 返回的 app 谁都能拿去 `uvicorn.run(host="0.0.0.0")`，闸门只在 `serve()` / GUI / CLI 里。
rc7 拆成两个工厂：`create_app()` 明确声明「不校验监听地址，供测试与进程内挂载」（`bigfish/server.py:217-226`），
生产路径一律走 `create_production_app()`——它返回 `(app, host)`，先过 `check_bind_host` 再把 host 交出去。
第二道门是 `LoopbackGuardMiddleware`：每根连接再判一次 peer / local 是不是回环，非回环 403。
`TRUSTED_PEERS` 只放 `testclient` / `testserver` 两个进程内常量，列表本身也被测试钉住（`test_trusted_peers_are_only_the_in_process_constants`）。

### 1.5 BFC-04：备份清单的措辞

操作包（`export_bundle`）里 `credential_status = "hashes-only"`，`credential_detail` 写明「含 `executors.token_hash` 与 `tickets.token_hash`（SHA-256，不可逆）；不含明文 token、不含执行票原文、不含密钥文件」（`bigfish/backup.py:122-147`）。
审计包（`export_audit_bundle`）里是 `pattern-scrubbed-unverified`，配套 `credential_scan` 说明扫描是**模式匹配**、覆盖了哪些字段、有没有命中，并且 `manual_review_required: true` 照样留着（`bigfish/backup.py:221-233`）。
两个包都不再声称「已证明干净」——脱敏不等于证明，这句话现在写在 `handling` 字段里，而不是靠一个布尔值暗示。

### 1.6 BFC-05 / BFC-06：CLI 的两处收口

`main()` 原来在 `restore` 分支手动关库，其余分支指望进程退出把 SQLite 连接和租约一起带走。rc7 把 `main()` 拆成「解析 → `_dispatch()` → `finally: _close_quietly(db)`」，任何分支返回都当场释放写者租约。

`allowed-ports` 现在打两段：implemented（`PORT_ROLES` 三项：`windows_api` 3096 / `vps_proxy` 3098 / `test_only` 3097，`bigfish/config.py:27-31`）与 reserved（`PORT_RESERVED` 六项：3099 `mobile_rest`、3100 `mobile_sse`、3101 `mcp_bridge`，以及外部进程的 8765 / 18765 / 18766，`bigfish/config.py:37-44`）。前三个明确标注「计划中，未实现」。

### 1.7 BFC-07：发布工程补了两块

`requirements.lock`：`.venv` 里 `pip freeze` 后剔掉 `pyflakes` / `pip` / `setuptools` / `wheel`，得到全传递依赖精确 pin。它和 `requirements.txt`（声明式范围：`fastapi>=0.115,<1.0` 等）并存，两者都可用，区别在可复现性。

`.github/workflows/ci.yml`：`test` job 是 `ubuntu-latest` × `windows-latest` 乘 `3.11` × `3.13`，`fail-fast: false`；步骤是 pyflakes → `compileall` → 单测 → selfcheck → GUI 冒烟，其中 3.11 装 lock、3.13 装 txt（用它顺带验证「旧装法在新解释器上还在」）。另有 `security-note` job 不装依赖，只做两件事：`git ls-files` 拒绝 `.db` / `.key` / `.pem` / `.zip` / `.exe` 之类入库，`git grep` 扫 `ghp_` 令牌、`bfc_` 前缀令牌与 PEM 头，命中即失败。

### 1.8 BFC-08：tag 的问题

`v0.1.1-rc6` 指向 `a2e9275751`，那个提交 `parents=0`，是根提交，与本仓库历史不连通——按 tag checkout 出来不是当时的代码，是一棵孤儿树。
本版起改为在真实提交上打**带注释** tag。这条没有自动化测试能证明，只能发版时人工核对，所以状态是「流程已改，待验证」。

### 1.9 MOBILE：只读角色的边界

`MOBILE_VIEWER = "mobile_viewer"`（`bigfish/roles.py:15`），权限集只有三项：`task.read`、`event.read`、`review.create`（`bigfish/roles.py:46`）。
上游注释写得很直白：刻意不含 `task.claim` / `run.*` / `approve` / `backup.*`——「手机丢了也不至于能把任务领走或把库导出去」。

`_DB_ROLE_MAP` 加 `"mobile" → MOBILE_VIEWER`（`bigfish/roles.py:54-55`），`DB_ROLES` 是它的 `frozenset`，所以 `core.py` 的白名单不用硬编码名单，改成 `if role not in R.DB_ROLES: raise`（`bigfish/core.py:163-164`）——加角色只改一处映射，少改一处就少一处提权机会。令牌前缀 `bfc_mb_`（`bigfish/core.py:173-174`），拿 `bfc_mb_` 去调执行器通道会被挡（`test_mobile_token_cannot_use_executor_channel`）。

需要点明的现状：`bigfish/cli.py:79` 的 `executor-add --role` 选项仍是 `executor / reviewer / admin`，**CLI 还没开放 mobile**；本轮 mobile 身份只能经 API 或代码 `register_executor(role="mobile")` 创建。移动端端点（3099/3100）也没实现，见 U6 / U7。

### 1.10 顺手修的两条真缺陷

这两条不是审计项，是跑 pyflakes 与读代码时撞上的：

1. `bigfish/gui.py` 顶部 `from .server import create_app` 与函数内 `import` 重复，导致 pyflakes 报未使用导入。已删重复导入，并让 `ApiServer` 改用 `create_production_app`——顺带把 GUI 也接进了 BFC-03 的第一道门。
2. `bigfish/server.py` 的 `create_production_app()` 原来标注返回 `FastAPI`，实际返回 `(app, host)` 元组。类型标注改成 `tuple[FastAPI, str]`。这条不影响运行时，但会骗过所有读签名的人。

---

## 2. 交付物清单

### 2.1 源码文件树（`bigfish/`，6462 行）

```text
bigfish/
├── __init__.py            8     版本号 v0.1.1-rc7
├── __main__.py           69     python -m bigfish 入口
├── config.py            178     端口常量 / 回环判定 / 数据目录
├── core.py             1379     Council 业务层（register_executor、authenticate、任务流转）
├── db.py                659     SCHEMA_VERSION=4、MIGRATIONS、写者租约登记
├── backup.py            741     export_bundle / export_audit_bundle / restore_bundle
├── locks.py             265     写者租约 + 恢复期排他锁（本版整文件重写）
├── roles.py              94     五角色 / 权限矩阵 / db_role 映射
├── security.py          233     令牌哈希、脱敏模式、密钥轮换
├── state.py              84     进程内状态
├── executor_client.py   408     执行器侧客户端
├── server.py            620     REST + MCP，两道监听门
├── gui.py              1419     PySide6 界面（ApiServer 走生产工厂）
├── cli.py               264     子命令与 _dispatch 收口
└── runtime.py            41     运行时装配
```

本轮动过的文件（已跟踪）：`config.py`、`core.py`、`db.py`、`backup.py`、`locks.py`、`roles.py`、`security.py`、`server.py`、`gui.py`、`cli.py`、`__init__.py`、`__main__.py`、`runtime.py`、`executor_client.py` 中的 14 个，合计 `+598 / -271`。

### 2.2 文档清单

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `docs/DELIVERY-v0.1.1-rc7.md` | 本文件：交付内容、清单、安装、验证、未完成、授权项 | 本轮新增 |
| `docs/MIGRATION-v0.1.1-rc7.md` | rc6 → rc7 与 rc1-rc5 → rc7 的迁移路径、检查清单、失败处置 | 本轮新增 |
| `docs/ROLLBACK-v0.1.1-rc7.md` | rc7 → rc6 代码/数据回滚、备份恢复回滚、最坏情况 | 本轮新增 |
| `docs/.rc7-facts.md` | 本轮的**唯一数据源**（隐藏文件，97 行实测记录） | 已有 |
| `docs/DELIVERY-v0.1.1-rc6.md` 等 rc6 全套 | 上一版交付、迁移、回滚、安全评审、测试报告 | 已有 |
| `docs/ARCHITECTURE-FUTURE.md` | 移动端与分层重构的草案（U6 / U8 的依据） | 已有 |

### 2.3 本轮新增的三个未跟踪文件

| 文件 | 作用 | 备注 |
| --- | --- | --- |
| `requirements.lock` | 全传递依赖精确 pin，用于复现同一套依赖 | 头注释已同步为「重跑全量测试（294 项）」 |
| `.github/workflows/ci.yml` | 双平台 × 双 Python 的 CI，外加密钥扫描 job | 只在仓库里，本地不执行；第一次真正跑要等推到远端 |
| `tests/test_v011_rc7.py` | rc7 的 27 项回归测试 | 7 个测试类，见 4.2 |

`requirements.txt` 保留不动——它和 lock 是两个用途，删掉它会破坏 `tools/dev_setup.sh` 与 `setup.ps1` 的既有路径。

---

## 3. 怎么装怎么跑

### 3.1 Linux / macOS：`tools/dev_setup.sh`

```bash
# 默认：建 .venv、按 requirements.txt 装依赖、打印常用命令
bash tools/dev_setup.sh

# 装完顺手跑一遍全量测试（输出会重定向到 ${TMPDIR:-/tmp}/bfc-dev-setup-test.log，只 tail 最后 6 行）
bash tools/dev_setup.sh --test

# 指定解释器
PYTHON=/usr/bin/python3.11 bash tools/dev_setup.sh
```

脚本做的事：建 `.venv` → 缺 pip 就用 `get-pip.py` 引导 → `pip install -r requirements.txt` → 打印跑单测 / 自检 / `net_check` / 启动服务的命令。`--test` 时如果测试非 0 退出，脚本也非 0 退出。

**它装的是 `requirements.txt` 不是 lock。** 想要可复现环境，手动补一步：

```bash
.venv/bin/python -m pip install -r requirements.lock
```

跑全量测试（本机实测用的就是这条，含 GUI 依赖路径与 offscreen 设置）：

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
```

### 3.2 Windows：`setup.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

五步，每步之后都用 `Assert-Ok` 查 `$LASTEXITCODE`，失败就 `Write-Error` 加 `exit 1`：

| 步骤 | 动作 |
| --- | --- |
| 1 | 检测 Python |
| 2 | 建 `.venv` |
| 3 | `pip install -r requirements.txt` |
| 4 | `python -m unittest discover -s tests` |
| 5 | `python tools\selfcheck.py` |

这个 `Assert-Ok` 不是装饰：rc4 那轮修的就是「PowerShell 调原生命令失败不抛异常，脚本一路绿到底」的假成功。

### 3.3 装完自检

```bash
.venv/bin/python tools/selfcheck.py     # 期望：16 项通过 0 失败
.venv/bin/python tools/gui_smoke.py     # 期望：9 项通过 0 失败（需 GUI 依赖）
.venv/bin/python -m bigfish.cli allowed-ports   # 期望：implemented 三项 + reserved 六项
```

---

## 4. 验证结果

### 4.1 全量单测：294 项 OK

```text
$ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .
Ran 294 tests in 59.793s

OK
RC=0
```

环境：Debian 12 / Python 3.11.2 / SQLite 3.40.1。

18 个测试文件的分布（逐文件 `grep` 数过 `def test`，与总数完全对得上）：

| 测试文件 | 项数 | | 测试文件 | 项数 |
| --- | --- | --- | --- | --- |
| `test_api` | 6 | | `test_v011_rc5_cli_guard` | 4 |
| `test_core` | 15 | | `test_v011_rc5_dispatch_id` | 3 |
| `test_v011_p0` | 16 | | `test_v011_rc5_key_heal` | 8 |
| `test_v011_p1` | 24 | | `test_v011_rc5_proxy` | 9 |
| `test_v011_p2` | 26 | | `test_v011_rc5_receipts` | 24 |
| `test_v011_p3` | 20 | | `test_v011_rc5_restore_authority` | 24 |
| `test_v011_rc3` | 26 | | `test_v011_rc5_review_audit` | 13 |
| `test_v011_rc4` | 33 | | `test_v011_rc6` | 16 |
| | | | `test_v011_rc7` | 27 |
| | | | **合计** | **294** |

### 4.2 rc7 新增的 27 项

| 测试类 | 项数 | 覆盖什么 |
| --- | --- | --- |
| `TestLeaseAtomicity` | 5 | 有写者时恢复被拒；有恢复时写者开不了库；同进程第二个 `Database` 不死锁；全关之后排他锁能拿到；锁文件用完不删 |
| `TestMcpFieldLimit` | 4 | 5 个自由文本字段都声明了上限；超限心跳报错；刚好卡在上限能过；超限提交报错且什么都没写进去 |
| `TestBindGate` | 5 | 生产工厂拒公网 host、收回环；中间件挡非回环客户端、放行回环；`TRUSTED_PEERS` 只有两个进程内常量 |
| `TestManifestCredentialStatus` | 3 | 操作包说 hashes-only；审计包不承诺「已干净」；状态值只从已知集合里取 |
| `TestCliClosesDatabase` | 2 | CLI 返回即释放租约；`allowed-ports` 打印 reserved 段 |
| `TestReleaseEngineering` | 3 | lock 是精确 pin；CI 覆盖双平台；CI 里的密钥正则真的能匹配样本令牌 |
| `TestMobileViewerRole` | 5 | mobile 用自己的前缀注册；认证出来是 viewer；mobile 令牌进不了执行器通道；mobile 的 db_role 绝不回落成执行器；注册未知角色被拒 |

第 4.2 表格里最要紧的是 `mobile_db_role_never_falls_back_to_executor`——它钉的正是 rc6 里会出事的那条兜底路径。

### 4.3 自检、冒烟、静态检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 自检 | `python tools/selfcheck.py` | 通过 **16**，失败 **0** |
| GUI 冒烟 | `python tools/gui_smoke.py` | 通过 **9**，失败 **0** |
| pyflakes | `python -m pyflakes bigfish tools tests` | 输出为空，`RC=0` |
| 字节码编译 | `python -m compileall -q bigfish tools tests` | `RC=0` |

pyflakes 从「有输出」变成「零输出」，靠的就是 1.10 那条重复导入的清理；编译检查 `RC=0` 说明没有语法层面的残留。

### 4.4 没验证到的部分（必须说清）

| 项目 | 现状 |
| --- | --- |
| 平台 B 跑 rc7 的 294 项 | **没跑**。Windows 11 / Python 3.14.6 上只跑到 rc6 |
| Windows 真机 GUI 窗口 | **没验**（一直用 offscreen 冒烟） |
| CI 工作流实际执行 | **没跑过**。`ci.yml` 只是仓库里的文件，第一次真跑要等推到远端 |
| mobile 端到端 | **没有**。只有角色与权限矩阵，服务端点和客户端都不存在 |
| 跨设备 MCP 闭环 | rc2 时期 11/11 通过过，rc7 没重跑（U4） |

---

## 5. 未完成项

| 编号 | 未完成的事 | 影响 | 卡在哪 |
| --- | --- | --- | --- |
| U1 | Windows 真机 GUI 窗口验收 | 界面只在 offscreen 下冒烟过，真实窗口的缩放 / DPI / 托盘行为没看过 | 需要一台能开桌面的 Windows 机 |
| U2 | exe 打包（PyInstaller） | 用户仍要装 Python 才能跑 | 依赖 BFC-07 的锁文件先稳定 |
| U3 | 开机自启注册 | 现在要手动 `start-gui.ps1` / `start-headless.ps1` | 需要管理员权限，见 §6.1 |
| U4 | 跨设备 MCP 闭环的 rc7 重跑 | 上一次通过是 rc2 时期的 11/11，rc7 改了 server 的工厂与中间件，可能影响通道 | 需要两台机器与 Tailscale |
| U5 | 平台 B 跑 rc7 的 294 项 | Windows 上 rc7 的结论全靠平台 A 外推 | 需要 Windows 11 + Python 3.14.6 环境 |
| U6 | 移动端 REST / SSE 端点 | 只有角色，没有接口。3099 / 3100 仍是「计划中，未实现」 | 见 `docs/ARCHITECTURE-FUTURE.md` 草案 |
| U7 | apk 打包 | 没有客户端可打 | 依赖 U6 |
| U8 | `core.py`、`server.py` 分层重构 | `core.py` 1379 行、`server.py` 620 行，改一处要读一整遍 | 没排期，纯技术债 |

U5 是这八条里最该先补的：台上只有一条腿站着，另一条腿的结论全是外推。

---

## 6. 需要 `human_owner` 授权的操作

下面四条**一条都没执行**。它们都会改变现役机器状态或对外可见面，必须由人类点头。
每条写清三件事：做了什么、影响是什么、怎么撤。

### 6.1 🔸 注册开机自启

- **做什么**：把 GUI 或 headless 服务注册成登录即启（`install-task.ps1` / 计划任务或启动项）。
- **影响**：机器一开机就有一份常驻进程占着数据库与 3096，且持有写者租约。多开一个实例会直接撞租约——这是设计如此，不是 bug。另一个后果是「重启后服务自己起来了」会让排障时误以为是手动起的。
- **怎么撤**：删掉计划任务 / 启动项（`Unregister-ScheduledTask` 或从启动目录移除），然后确认没有残留进程：`Get-Process python* | Where-Object { $_.Path -like "*bigfish*" }`，有就停掉；最后确认 `.access.lock` 没被占（正常关掉进程后锁会释放，锁文件本身按设计不删）。

### 6.2 🔸 导入 MCP 连接

- **做什么**：把 `dsh-mcp-bigfish.json` 导进 DSH 的 MCP 配置，让 agent 侧能调到 `council_*` 工具。
- **影响**：导入后 agent 侧就多了一条能创建/领取/提交任务的通道。这条通道受 BFC-02 的字段上限和 BFC-03 的回环闸门约束，但**权限是真的**——能调 `council_claim_task` 就能占任务。
- **怎么撤**：从 MCP 配置里删掉该条目并重载 DSH；随后在库里把相关执行器禁用（`executors.enabled = 0`），必要时轮换令牌。回滚文档的第 3 节有对应的检查脚本。

### 6.3 🔸 配置 Tailscale Serve

- **做什么**：用 `tailscale serve` 把本机 3096 暴露给 tailnet 内的其它设备。
- **影响**：服务从「只有本机能连」变成「tailnet 内都能连」。回环闸门会挡非回环 peer——所以要么走 Serve 的本地转发（peer 看到的是回环），要么这条路根本走不通；这一点必须在开之前实测确认，不能假设。
- **怎么撤**：`tailscale serve reset`（或删掉对应 serve 配置），然后从另一台设备 `curl` 一次确认已经连不上。撤完别忘了确认 3096 仍只听 127.0.0.1。

### 6.4 🔸 把 rc7 推到现役 3096

- **做什么**：在当前跑着旧版的机器上切到 rc7 的代码与依赖。
- **影响**：现役服务会短暂中断（要关库、换代码、重开）。库结构不变，所以不需要迁移；但如果这期间创建过 mobile 身份，回滚时会踩到 rc6 的兜底提权路径——处置写在 `docs/ROLLBACK-v0.1.1-rc7.md`。
- **怎么撤**：按回滚文档走：先 `restore_bundle` 或直接切回 rc6 的代码与依赖，再确认库能被 rc6 打开。**回滚前务必先导一个操作包**，因为恢复语义会阻断在途任务、吊销所有活票、禁用全部执行器。

四条的共同点：都能在一分钟内撤回，但撤回的代价不对称——6.1 和 6.3 撤起来无痛，6.4 撤起来要重导库。

---

## 7. 下一步建议

按「补证据 → 补能力 → 还技术债」排：

1. **先补 U5**：在 Windows 11 / Python 3.14.6 上把那 294 项跑通。这是唯一一条能靠一个人一台机器关掉的证据缺口，关掉之后 rc7 才算双平台成立。
2. **再补 U1**：真机开一次窗口，看缩放、DPI、托盘。offscreen 冒烟只能证明「不崩」，证明不了「能用」。
3. **把 CI 真跑一次**：`ci.yml` 现在是仓库里的文件，不是证据。推到远端跑通第一个绿灯，BFC-07 才能从「部分完成」变「完成」。
4. **BFC-08 在下次发版时验证**：打一个带注释 tag，`git cat-file -p` 看 `parents` 不为 0、`git merge-base` 能和 main 连上。这条只能靠流程，没有测试能兜。
5. **U6 之前先把 mobile 的 CLI 口子开出来**：现在只能经 API 或代码建 mobile 身份，运维上很别扭。开 `executor-add --role mobile` 是个小改动，但能让 U6 的联调少一堆手工步骤。
6. **U8 不急，但别再往里堆**：`core.py` 1379 行已经到了「改一处读一遍」的程度。真要动，先补测试再拆，别反过来。

---

## 8. 一句话收尾

rc7 没有新功能面，它做的是把 rc6 里那些「平时不出事、出事就是数据分叉或提权」的缝补上：
互斥换成同一内核对象、MCP 自由文本有了字段上限、监听闸门变成两道、备份清单不再自相矛盾、
CLI 不再靠进程退出兜底关库，另外加了一个连任务都领不走的只读角色。
库结构一动没动，所以它好回滚；也正因为没动结构，它没什么可炫的——这版的价值全在「不出事」上。
