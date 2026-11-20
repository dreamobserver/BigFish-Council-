# 交付说明：bigfish-council v0.1.1-rc4

> 本文所有数字来自 2026-11-19 在本机（平台 A）实跑，原始输出在 `/home/dsh/bfc-rc4-logs/`。
> rc3 的交付说明见 [DELIVERY-v0.1.1.md](DELIVERY-v0.1.1.md)，本文不改写它。
> rc4 的四份专项文档：[Diff 摘要](DIFF-SUMMARY-v0.1.1-rc4.md)、[测试报告](TEST-REPORT-v0.1.1-rc4.md)、
> [迁移说明](MIGRATION-v0.1.1-rc4.md)、[回滚手册](ROLLBACK-v0.1.1-rc4.md)。

## 0. 一页速览

| 项目 | v0.1.1-rc3 | v0.1.1-rc4 | 依据 |
| --- | --- | --- | --- |
| 单元测试总数 | 129 | **162**（+33） | `Ran 162 tests ... OK`，见 §4.1 |
| 测试文件 | 7 | 8（新增 `tests/test_v011_rc4.py`） | `ls tests/*.py` |
| 自检脚本 | 16 项 | 16 项（默认端口 3099 → 3094） | `通过 16 项，失败 0 项`，见 §4.2 |
| GUI 冒烟 | 9 项 | 9 项 | `通过 9 项，失败 0 项`，见 §4.3 |
| SCHEMA_VERSION | 3 | **3（不变，免迁库）** | `bigfish/db.py` 常量 |
| 备份格式版本 | 2 | 2（恢复流程重写，格式未变） | `backup.py` |
| 平台 A | 129/129 | **162/162** | §4.1 |
| 平台 B（Windows） | 129/129 已跑 | **本轮未跑** | §5.1 |
| 生产部署 | 未部署 | **未部署** | §6 |

rc4 的主题：把「幂等」「多轮执行」「恢复」「协议」四件事在边界情况下钉死，
并给未来的 exe / apk 预留位置（只预留，未实现）。

## 1. 状态块

| 字段 | 值 |
| --- | --- |
| 版本号 | `0.1.1-rc4`（`bigfish/__init__.py:6` 的 `__version__`） |
| 分支 | `fix/v0.1.1-rc4-hardening` |
| 定性 | **发布候选（release candidate），不是生产版** |
| 平台 A | Debian 12.15 / Python 3.11.2 / SQLite 3.40.1 / Linux 5.15.0-187-generic —— **162 项全绿** |
| 平台 B | Windows 11 / Python 3.14.6 / SQLite 3.50.4 —— **rc4 本轮未跑**。rc3 的 129 项在平台 B 跑过（25.263s / 退出码 0），rc4 不能引用该结果 |
| SCHEMA_VERSION | 3（rc3 → rc4 不变，老库免迁移） |
| 提交状态 | rc3 发布点 `832f699`；其后 4 个提交（B2 主体在 `2ea5d1b`）；C / D / E 与部分 A 改动仍在工作区未提交 |
| 工作区规模 | `git diff --stat`：21 个文件 / 2421 insertions / 219 deletions；另有 `AUDIT.md`、四份 rc4 文档、`tests/test_v011_rc4.py` 未纳管 |
| 回滚 | 见 §7，指向 [ROLLBACK-v0.1.1-rc4.md](ROLLBACK-v0.1.1-rc4.md) |

三句话交代清楚：

1. 代码层面 rc4 已完成，平台 A 全部验证脚本退出码 0。
2. Windows 侧这轮一项没跑，跨机闭环这轮没重跑，类型检查与长稳没做。
3. 现役服务仍然是 rc3，本文不包含任何部署动作。

## 2. 交付物清单

### 2.1 源码 `bigfish/`（`ls` 核对：14 个 `.py`，5327 行）

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `__init__.py` | 8 | 版本号 `0.1.1-rc4` |
| `__main__.py` | 69 | 入口：回环校验 + headless / GUI 分支 |
| `backup.py` | 621 | 导出 / 检查 / 恢复；rc4 重写恢复原子性 |
| `cli.py` | 206 | 子命令 |
| `config.py` | 178 | 端口与路径唯一真源；rc4 新增 `PORT_RESERVED` |
| `core.py` | 1303 | 业务核心（`Council` + `Watchdog`） |
| `db.py` | 543 | schema 3 + 迁移 + 审计；rc4 改「先探测版本再动文件」 |
| `executor_client.py` | 185 | 执行器侧 SDK；rc4 改 `RECONCILED_OK` 与三个 safe_* |
| `gui.py` | 1339 | PySide6 五页界面；rc4 加运行所有权保护与草稿槽位 |
| `roles.py` | 83 | 角色权限表；rc4 新增 `DB_ROLES` 白名单 |
| `runtime.py` | 40 | `open_council()` 装配；rc4 接入密钥一致性自愈 |
| `security.py` | 212 | 令牌 / 密钥 / 派生票 |
| `server.py` | 456 | FastAPI + 手写 JSON-RPC；rc4 补协议语义 |
| `state.py` | 84 | 状态常量 |

### 2.2 测试 `tests/`（`ls` 核对：9 个 `.py`，其中 8 个测试文件，2430 行）

| 文件 | 行数 | 用例数 | 来源 |
| --- | --- | --- | --- |
| `__init__.py` | 1 | — | 包标记 |
| `test_api.py` | 110 | 6 | v0.1.0 |
| `test_core.py` | 166 | 15 | v0.1.0 |
| `test_v011_p0.py` | 209 | 15 | rc1 |
| `test_v011_p1.py` | 287 | 24 | rc1 |
| `test_v011_p2.py` | 219 | 24 | rc1 |
| `test_v011_p3.py` | 306 | 19 | rc1 |
| `test_v011_rc3.py` | 660 | 26 | rc3 |
| `test_v011_rc4.py` | 472 | **33** | **rc4 新增** |
| 合计 | 2430 | **162** |  loader 实测计数，见 §4.1 |

rc4 的 33 项分六个类：`TestIdempotencyContract` 6 / `TestMcpProtocol` 7 /
`TestMultiRoundTickets` 6 / `TestReviewBinding` 4 / `TestExecutorClient` 4 /
`TestAuxiliary` 6。既有 129 项断言零改动。

### 2.3 工具与脚本

| 文件 | 行数 | 用途 | rc4 状态 |
| --- | --- | --- | --- |
| `tools/selfcheck.py` | 151 | 16 项端到端自检 | 已改（端口 3094 + 服务身份校验），**已实跑 16/16** |
| `tools/gui_smoke.py` | 76 | GUI 离屏冒烟 | **已实跑 9/9** |
| `tools/e2e_loopback.py` | 157 | 同机回环端到端 | **已实跑 22/22（同机，非跨设备）** |
| `tools/net_check.py` | 166 | 分层网络探测 | 已改（无服务记 SKIP），**未在 VPS 跑** |
| `tools/remote_check.py` | 262 | 远端执行器检查 | 已改（默认只读），**跨机未跑** |
| `tools/dev_setup.sh` | 64 | Linux/WSL 开发环境 | 已改（修转义 bug），**本轮未执行** |
| `tools/bfc-proxy.mjs` | 48 | VPS 回环跳板 | 未改，未跑 |
| `tools/start-headless.ps1` | 203 | Windows 无窗口启动 | 已改（WMI 派生 + 强制重定向），**真机未跑通** |
| `tools/stop-headless.ps1` | 114 | Windows 停止进程 | 已改（特征串收紧），**真机未跑** |
| `setup.ps1` | 65 | Windows 建 venv + 装依赖 | 已改（`Assert-Ok`），**未执行** |
| `build.ps1` | 93 | PyInstaller 打包 | 已改（参数数组 + 断言），**未打包** |
| `start-gui.ps1` | 29 | 启动 GUI | 未改，未跑 |
| `install-task.ps1` | 39 | 注册登录自启 | 未改，**未执行（需授权，见 §6）** |
| `dsh-mcp-bigfish.json` | 13 | DSH MCP 连接模板 | 未改，**未导入（需授权，见 §6）** |

### 2.4 文档 `docs/`（`ls` 核对：22 个 `.md`，367721 字节）

| 文件 | 行数 | rc4 状态 |
| --- | --- | --- |
| `DELIVERY-v0.1.1-rc4.md` | 本文件 | **新增** |
| `DIFF-SUMMARY-v0.1.1-rc4.md` | 488 | 新增 |
| `TEST-REPORT-v0.1.1-rc4.md` | 428 | 新增 |
| `MIGRATION-v0.1.1-rc4.md` | 221 | 新增 |
| `ROLLBACK-v0.1.1-rc4.md` | 253 | 新增 |
| `ARCHITECTURE-FUTURE.md` | 889 | 新增（exe / apk 预留，草案） |
| `NETWORK-TOPOLOGY.md` | 408 | 已改（新增 §9 讲 rc4 网络层） |
| `DELIVERY-v0.1.1.md` | 834 | rc3 版，未改 |
| 其余 14 个 | — | rc1 / rc2 / rc3 历史文档，未改 |

### 2.5 根目录

| 文件 | 字节 | 状态 |
| --- | --- | --- |
| `README.md` | 16853 | 已改到 rc4，指向 rc4 测试报告 |
| `CHANGELOG.md` | 13007 | 新增 v0.1.1-rc4 完整条目 |
| `AUDIT.md` | 20580 | 新增，外部审计入口（**git 未纳管**） |
| `requirements.txt` | 353 | fastapi / uvicorn / pydantic / PySide6 / mcp 五条 |
| `LICENSE` | — | **不存在**（根目录与 `git ls-files` 均无） |

### 2.6 与任务书清单的差异

| 任务书写法 | 实际 `ls` 结果 | 处理 |
| --- | --- | --- |
| bigfish/ 12 个模块 | 14 个 `.py` | 按 14 个列，见 §2.1 |
| tests/ 8 个文件 | 8 个测试文件 + 1 个 `__init__.py` | 按 8 个测试文件列，见 §2.2 |
| tools/ 9 个 | 9 个文件 | 一致 |
| README / CHANGELOG / LICENSE / AUDIT | LICENSE **不存在** | 如实标缺失，未新建 |

LICENSE 缺失是历史状态，rc1 起就没有。本轮不改（新建许可证属法律决定）。

## 3. rc4 逐批次交付

### 3.1 总表

| 批次 | 主题 | 改动文件 | rc4 新增测试 | 状态 |
| --- | --- | --- | --- | --- |
| A | 运行时所有权 | `gui.py` `core.py` `server.py` | 0（由 `test_v011_p3.py` 既有 19 项覆盖） | 代码完成，平台 A 回归通过；Windows 未跑 |
| B1 | 数据库识别 | `db.py` | 0（由 `test_v011_rc3.py` 26 项覆盖） | 代码完成，另做只读探针实测；Windows 未跑 |
| B2 | 恢复原子性 | `backup.py` `runtime.py` `db.py` | 0（同上） | 代码完成，另做四态探针实测；**恢复中断场景未做故障注入** |
| C | 审查绑定 | `core.py` `gui.py` | **4**（`TestReviewBinding`） | 代码完成，4 项通过 |
| D | 多轮执行 | `core.py` | **6**（`TestMultiRoundTickets`） | 代码完成，6 项通过 |
| E | 幂等与协议 | `core.py` `server.py` `executor_client.py` | **17**（`TestIdempotencyContract` 6 + `TestMcpProtocol` 7 + `TestExecutorClient` 4） | 代码完成，17 项通过 |
| F | 辅助与预留 | `config.py` `roles.py` `selfcheck.py` `setup.ps1` `build.ps1` `dev_setup.sh` `remote_check.py` `net_check.py` `start-headless.ps1` `stop-headless.ps1` | **6**（`TestAuxiliary`） | 代码完成，6 项通过；**PS1 与 shell 脚本真机均未执行** |

新增测试合计 0 + 0 + 0 + 4 + 6 + 17 + 6 = **33**，与 129 → 162 的增量一致。

### 3.2 批次 A：运行时所有权

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `gui.py:99` | `if self.thread is not None and self.thread.is_alive(): return` | 重复点「启动」不再起第二个 uvicorn 线程 |
| `gui.py:113` | `except BaseException as exc` | 端口被占用时 uvicorn 抛 `SystemExit`，`except Exception` 接不住 |
| `gui.py:138` | `_resolve_endpoint()` 用 `servers[0].sockets[0].getsockname()` | 状态栏显示真实绑定端点，不再猜 |
| `gui.py:151` | `def stop_watcher(self, timeout: float = 5.0)` | 关窗时给线程一个有限等待，不无限 join |
| `gui.py:1267` | `self._closed = True`，先 `disconnect` 再 `stop` | 关窗时 `sqlite3.ProgrammingError: Cannot operate on a closed database` |
| `core.py:1181` | `stop_watchdog` 只停 `Watchdog._instances.get(key) is mine` 的那个 | 此前无条件 `stop_for` 会连外部 GUI / CLI 的看门狗一起停掉 |
| `server.py` lifespan | 加 `started` 标志；`watchdog_interval=None` 时不调 `stop_watchdog` | 服务关闭时不再误停不属于自己的看门狗 |

验证方式：`test_v011_p3.py` 的 `TestGuiSmoke` 19 项里包含 `test_close_without_tray_shuts_down_cleanly`，
本轮全量运行时该项为 `ok`（日志第 96–98 行，Qt 噪音把描述行与 `ok` 分在了三行）。

### 3.3 批次 B1：数据库识别

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `db.py:227` | `ver = self._peek_schema_version()` 提到 `PRAGMA journal_mode=WAL` **之前** | 打开未来版本的库时先只读判断，不再先把人家文件改成 WAL |
| `db.py:242 / :297` | `_peek_schema_version()` / `_peek_schema_version_locked()` | 只读查 `meta.schema_version`，0 表示全新库，不建表、不写 `meta` |
| `db.py:268` | `init_schema()` 改「先 `_migrate_locked(current)` 后 `executescript(SCHEMA)`」 | v1 库此前会在 `CREATE INDEX ... idempotency(expires_at)` 处炸 `no such column` |
| `db.py` 末尾 | 断言版本等于 `SCHEMA_VERSION`，否则抛 `DatabaseVersionError` | 迁移没跑完就启动会被当场拦下 |
| `db.py:432 / :447` | `close()` 幂等、`_reopen()` 重置 `_depth` 与 `_closed` | 恢复流程「关库 → 换文件 → 重开库」可安全重入 |

只读探针实测（本轮构造 `meta.schema_version=99` 的库）：

    DatabaseVersionError: 数据库 schema 版本 99 高于本程序支持的 3，
    拒绝打开（升级程序，不要降级数据库）

同时核对：抛出后 `journal_mode` 仍是 `delete`、文件头 100 字节未变、目录下无 `-wal` 文件。
这证明「拒绝」发生在任何写操作之前。

### 3.4 批次 B2：恢复原子性

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `backup.py:312` | `_restore_lock()`（POSIX `fcntl.flock` / Windows `msvcrt.locking(LK_NBLCK)`） | 两个进程同时恢复会互相覆盖 |
| `backup.py:357` | `_prepare_staging(...) -> tuple[int, dict]` | 纪元与计数在替换前算准，失败时按返回的 `cleanup` 精确回填 |
| `backup.py:504 / :512` | `os.replace(staging, target)` 与 `os.replace(key_new, key_path)` 成对 | 库与密钥不能只换一个 |
| `backup.py:557 / :562` | 失败时成对回滚（`swapped` / `key_installed` 两个标志分别管） | 回滚不能只回一半 |
| `backup.py:588` | `_count_blocked(db)` 真实计数 | 原写法把 `idempotency_purged` 写死 0，是假数据 |
| `backup.py:593` | `ensure_key_consistency(db, key_path) -> dict` | 两次 `os.replace` 之间被杀，下次启动自愈 |
| `runtime.py:18` | 启动时调用上述函数，包在 try/except 里 | 自愈失败不阻断启动 |

`ensure_key_consistency` 四态探针实测（本轮）：`consistent` → 手动改密钥 → `rotated` →
再跑 `consistent` → 删密钥 → `no-key`。rotated 之后 `audit` 表出现
`db.key_healed`，`target=ticket.key`。

未覆盖的部分：**没有做故障注入**。「`os.replace` 之间被 SIGKILL」这个真实场景没有构造过，
自愈路径靠代码审读与四态探针，不靠中断实验。

### 3.5 批次 C：审查绑定

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `core.py:36` | `EFFECTIVE_APPROVAL_STATES = {APPROVED, DISPATCHED, RUNNING, CANCELLING, SUBMITTED, DONE}` | 正向白名单 |
| `core.py:684` | `and task["status"] in EFFECTIVE_APPROVAL_STATES` | 原黑名单漏排 `PENDING_APPROVAL` / `REVIEW_PASSED` / `BLOCKED`，撤票退回议事后的历史批准仍显示「有效」 |
| `core.py:521` | 投票汇总去掉 `revoked_at IS NULL` 过滤，改「每个模型只看最新一张票（含已撤销）」 | 撤掉最新赞成票 = 立场撤退，不能回落到更早那张凑「双通过」 |
| `core.py:595` | `_vote_summary_locked` 同步 | 事务内与事务外两处必须一致 |
| `gui.py` 投票徽标 | 读 `votes[model].revoked`，显示「已撤票」 | 界面不再继续显示 approve |

新增 4 项：撤最新赞成票不回落、撤票后 `latest_approval` 非 effective、
提交前投票直达待审、派发后撤票打回。

### 3.6 批次 D：多轮执行

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `core.py:746` | `ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)` | 无幂等键时不再派生同一张票；「BLOCKED → 重批 → 再领」旧票持有者能继续用 |
| `core.py:760` | 先 `UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL` 再 INSERT | 不用 `INSERT OR REPLACE`，避免静默丢吊销证据 |
| `core.py:1124` `sweep_timeouts` | 先 `SELECT id FROM tickets WHERE task_id=? AND revoked_at IS NULL` 取当前活票，再按 `ticket_id` 查 `runs.last_beat_at` | 多轮执行后按 `task_id` 取最新 run 会读到历史心跳，把真失联误判成还活着 |
| `core.py:1092` | `allowed = (S.SUBMITTED,) if accept else (S.SUBMITTED, S.BLOCKED)` | BLOCKED 是「副作用不明」，点一下通过就变 DONE 是污染 |
| `core.py:358` | `update_prompt` 的 UPDATE 里加 `cancel_requested_at=NULL, blocked_reason=NULL, executor_id=NULL`，并清 `control` 里的 `cancel_requested` / `pause_requested` | 「取消后改一版 Prompt 再审批」此前永远领不到（`claim` 的 `WHERE cancel_requested_at IS NULL` 一直挡住） |

新增 6 项：BLOCKED 后重批再领发新票、新票吊销旧票、无幂等键用随机 nonce、
同 nonce 派生确定、verify 只收 SUBMITTED、verify 不能收 BLOCKED。

### 3.7 批次 E：幂等与协议

| 位置 | 改动 | 解决的场景 |
| --- | --- | --- |
| `core.py:1215` | `parts = key.split(":", 3)` | 原全量 split 取 `parts[3]`，含冒号的客户端幂等键被截断，重放派生出另一张票 |
| `server.py:191` | 强制 `jsonrpc == "2.0"`，否则 `-32600` | 畸形请求不该进正常流程 |
| `server.py:194` | `has_id = "id" in msg`；无 id 才是通知 | 此前缺 method 的畸形请求被当通知回 202，客户端一直等 |
| `server.py:196` | 缺 `method` → `-32600`「缺少 method」 | 同上 |
| `server.py:219` | `if method == "ping": return ok({})` | ping 带 id 是请求，回 202 会让客户端等到超时（代码注释标 E3） |
| `server.py:270` | `_validate_args` 校验必填 / 未知字段 / 字符串类型 / enum，错误走 `result.isError` | MCP 语义要求错误在 result 里，不在 HTTP 层 |
| `server.py:240` | `await run_in_threadpool(dispatch_tool, ...)` | 同步 SQLite 调用不再卡事件循环 |
| `executor_client.py:142` | `RECONCILED_OK = ("SUBMITTED", "DONE", "CANCELLED")` | 心跳超时也会打成 BLOCKED，拿它当提交成功是误判 |
| `executor_client.py:144` | `safe_submit(self, ticket, task_id: str | None = None, **kwargs)` | 旧写法 `kwargs.get("task_id")` 恒为 `None`（`submit()` 没有该形参） |
| `executor_client.py:164 / :175` | `safe_claim(task_id, idem=None, retries=1)` / `safe_heartbeat(ticket, status, detail, idem, retries)` | 复用同一幂等键重试 |

新增 17 项，其中并发用例一律真线程 + `threading.Barrier`（不用 mock 模拟并发）。

### 3.8 批次 F：辅助与预留

| 文件 | 改动 | 状态 |
| --- | --- | --- |
| `config.py:37` | `PORT_RESERVED`：3099 mobile_rest / 3100 mobile_sse / 3101 mcp_bridge / 8765 windows_mcp / 18765 wmcp_proxy_a / 18766 wmcp_proxy_b | 与 `PORT_ROLES` 互不相交，有测试守 |
| `roles.py:48` | `DB_ROLES = frozenset(_DB_ROLE_MAP)`（executor / reviewer / admin） | mobile 故意不登记，有测试守 |
| `tools/selfcheck.py:22` | 默认端口 3099 → 3094；新增「`health.service != 'bigfish-council'` 直接退」 | 已实跑 16/16 |
| `setup.ps1` / `build.ps1` | 每条原生命令后跟 `Assert-Ok` 断言 `$LASTEXITCODE`；`build.ps1` 改参数数组调用 PyInstaller 并在末尾 `exit 0` | **未执行** |
| `tools/dev_setup.sh` | 修 `\$LOG` 反斜杠转义（日志落到字面量文件、退出码取不到）；`set -uo pipefail` | **未执行** |
| `tools/remote_check.py` | 未指定 `--task-id` 绝不自动挑任务；`--allow-submit` 才提交且只提交 `cancelled` | **跨机未跑** |
| `tools/start-headless.ps1` | 改 WMI `Win32_Process.Create` 派生；强制 `>> log 2>&1` | **真机未跑通** |
| `tools/stop-headless.ps1` | 特征串收紧为 `-m bigfish` / `bigfish\\__main__.py` / `BigFishCouncil.exe` / `bfc-proxy.mjs` / `bigfish-council\\` 五个精确模式 | **真机未跑** |
| `tools/net_check.py` | 本机没起服务记 SKIP 不记 FAIL | **未在 VPS 跑** |
| `docs/ARCHITECTURE-FUTURE.md` | 889 行：端口预留表、`ClientTransport` 三层抽象、apk 八端点契约草案、onedir vs onefile、漫游三方案、`/api/v1` 版本策略、五阶段迁移、九条排除项 | 草案，**未实现** |

F 批的 6 项新增测试覆盖的是 `list_tasks` 过滤 / 翻页 / 排序、`get_task_status_row` 轻查询、
`PORT_RESERVED` 与 `PORT_ROLES` 无交集、`DB_ROLES` 不含 mobile。
PS1 与 shell 脚本没有任何自动化测试，只有人工在 Windows 上跑才算验证。

## 4. 验证证据

全部命令在 `/home/dsh/bigfish-council` 下执行，Python 用仓库自带 `.venv`。
本机无 `libGL.so.1`，GUI 相关必须在 `LD_LIBRARY_PATH=/home/dsh/.local/libbfc` 下跑。

### 4.1 全量单元测试：162 项

    export LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen
    .venv/bin/python -m unittest discover -s tests -t .

本轮多次运行结果：

| 次数 | 输出末尾 | 耗时 | 退出码 |
| --- | --- | --- | --- |
| 第 1 次 | `Ran 162 tests` / `OK` | 24.978s | 0 |
| 第 2 次（落盘 `unittest-linux-delivery.txt`） | `Ran 162 tests` / `OK` | 22.351s | 0 |
| 第 3 次（`-v`，落盘 `unittest-linux-verbose.txt`） | `Ran 162 tests` / `OK` | 24.333s | 0 |

`-v` 日志 179 行：`... ok` 行 **161** 个，另 1 个 `ok` 单独一行
（第 98 行，`test_close_without_tray_shuts_down_cleanly` 的描述行被 Qt 噪音拆开），合计 **162 个通过标记**。
`FAIL:` 与 `ERROR:` 各 0 行。

用例分布（loader 实测，非手数）：

| 文件 | 用例数 |
| --- | --- |
| `test_api` | 6 |
| `test_core` | 15 |
| `test_v011_p0` | 15 |
| `test_v011_p1` | 24 |
| `test_v011_p2` | 24 |
| `test_v011_p3` | 19 |
| `test_v011_rc3` | 26 |
| `test_v011_rc4` | 33 |
| 合计 | **162** |

不加 `LD_LIBRARY_PATH` 时是 `Ran 157 tests` + `skipped=1`：
`TestGuiSmoke.setUpClass` 因 `ImportError: libGL.so.1` 主动 `SkipTest`，
GUI 那一类的 5 项不计数。这是环境缺库，不是测试缺项。

### 4.2 自检：16 项

    .venv/bin/python tools/selfcheck.py

    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

退出码 0。默认端口 3094。16 项覆盖：服务在线、`/health` 不含任务信息、看门狗随服务启动、
`initialize` 成功、五个执行器工具、没有审批类工具、能列出已批准任务、列表不含 Prompt、
能领取并拿到 Prompt、重复领取被拒、心跳上报、回传只到待验收、状态可查、坏 Token 被拒、
HTTP 坏 Token 被拒、审批只能在进程内。

### 4.3 GUI 冒烟：9 项

    .venv/bin/python tools/gui_smoke.py

    通过 9 项，失败 0 项
    GUI 冒烟通过。

退出码 0。9 项覆盖主窗口构造、五个页面都在、页面逐个切换、状态栏文案、待审批任务被识别等。
**这是离屏渲染（`QT_QPA_PLATFORM=offscreen`），不是真显示器下的人工验收。**

### 4.4 静态检查

    .venv/bin/python -m pyflakes bigfish tests tools ; echo "RC=$?"
    RC=0
    （无输出，日志 0 字节）

    .venv/bin/python -m compileall -q bigfish tests tools ; echo "RC=$?"
    RC=0
    （无输出，日志 0 字节）

分目录跑（`bigfish` / `tools` / `tests`）同样 0 行输出。
这两项只证明「没有未定义名 / 未使用名」和「语法可编译」，**替代不了类型检查**（见 §5.7）。

### 4.5 回环端到端：22 项（同机）

    .venv/bin/python -m bigfish.cli --data-dir /tmp/e2e-rc4-d serve --port 3093 &
    .venv/bin/python tools/e2e_loopback.py --url http://127.0.0.1:3093/mcp --data-dir /tmp/e2e-rc4-d

    通过 22 项，失败 0 项

脚本自己打印的收尾语：

    注意：以上全部在**同一台机器**的回环上完成。
    这证明协议链路、权限边界与状态机正确；跨设备联调需另按 NETWORK-TOPOLOGY.md 执行。

跑完 `pkill`，`ps aux | grep -c '[b]igfish.cli'` 为 0，无残留进程。
**本项不代表跨设备联调成功**，见 §5.6。

### 4.6 复现清单

    cd /home/dsh/bigfish-council
    export LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen
    .venv/bin/python -m unittest discover -s tests -t .      # 162 项
    .venv/bin/python tools/selfcheck.py                      # 16 项
    .venv/bin/python tools/gui_smoke.py                      # 9 项
    .venv/bin/python -m pyflakes bigfish tests tools ; echo "RC=$?"
    .venv/bin/python -m compileall -q bigfish tests tools ; echo "RC=$?"

### 4.7 已知噪音（不影响结论）

| 文本 | 出现位置 | 来源 |
| --- | --- | --- |
| `Fontconfig error: Cannot load default config file: No such file: (null)` | 首次构造 `QApplication` 时 | 本机没有 `/etc/fonts/fonts.conf`，Qt 打这行后走内置回退 |
| `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` | 每个 import `TestClient` 的模块首行 | 第三方 `fastapi/testclient.py:1`，弃用提示 |
| `This plugin does not support propagateSizeHints()` | `test_close_without_tray_shuts_down_cleanly` | Qt offscreen 插件不实现窗口尺寸提示 |

三条都落在 stderr 或描述行，不进 `Ran N tests` 计数，退出码不受影响。

## 5. 明确未完成

这一节是本文最重要的部分。以下全部**没有做**或**没做成**。

### 5.1 Windows 侧 162 项未跑

平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）本轮一项都没执行：162 项全量、
33 项新增、16 项自检、9 项 GUI 冒烟、pyflakes、compileall，全部未跑。
rc3 的 129 项在 Windows 上跑过（25.263s / 退出码 0），那份结果**不能代指 rc4**：
新增 33 项里 `TestMcpProtocol` 与并发用例都涉及平台差异（`msvcrt.locking`、
`subprocess.terminate()` 返回码、路径分隔符、进程 ACL）。

### 5.2 GUI 真窗口人工验收未做

只做了离屏冒烟。真显示器下的 DPI 缩放、托盘图标交互、关窗确认框、任务栏图标、
高对比度主题，都要人点。`test_close_without_tray_shuts_down_cleanly` 断言的正是
「无托盘环境关窗要真退出」，但那是 offscreen 下的结论。

### 5.3 开机自启未注册

`install-task.ps1`（`Register-ScheduledTask`，任务名变量 `$taskName`）本轮**没有执行过**。
注册会写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 或计划任务，属系统级变更，
见 §6.3。

### 5.4 `start-headless.ps1` 真机未跑通

rc4 把它从「起进程不重定向」改成 WMI `Win32_Process.Create` 派生 + 强制
`>> log 2>&1`。这套改动在 Linux 上无法验证，本轮没有任何一次在 Windows 上成功启动过。
schtasks 回退路径同样未跑。

### 5.5 DSH MCP 连接未导入

实测核对：`/home/dsh/.dsh/` 下只有 `logs/` 与 `redteam/` 两个目录，
没有任何文件提到 `bigfish-council`。`dsh-mcp-bigfish.json` 里的
`Authorization` 仍是占位符：

    "Authorization": "Bearer <把 executor-add 打印出来的 bfc_ex_... 填在这里>"

也就是说 DSH 现在**没有**连接本服务的能力。导入属对外可见的配置变更，见 §6.2。

### 5.6 跨机 Tailscale 闭环 rc4 未重跑

rc3 期间用 `tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096` 打通过一次跨机闭环
（11/11）。rc4 这轮**没有重跑**，原因是要先把新代码部署到对端并重启 headless，
属于变更现役服务（见 §6.1）。本轮唯一跑通的「端到端」是 §4.5 的同机回环 22 项。

### 5.7 mypy / pyright 未做

- `python -m mypy --version` → `No module named mypy`，.venv 里没装。
- `npx --no-install pyright` → 缺包被 npx 取消，项目从未引入 pyright。

项目从 rc1 起就没有类型检查依赖，本轮也没加。`pyflakes` + `compileall` 不等于类型检查。

### 5.8 24h soak 未做

没有 24 小时窗口。只做了 3 次全量连跑（每次 22–25s）与 1 次回环 e2e。
长稳相关的项（SQLite 文件增长、看门狗长时间空转、GUI 长时间驻留内存）全部未观测。

### 5.9 其他未做

| 项 | 状态 | 说明 |
| --- | --- | --- |
| `build.ps1` 打包 | 未执行 | 没有产出 exe，`BigFishCouncil.exe` 只出现在 `stop-headless.ps1` 的特征串里 |
| `tools/dev_setup.sh` | 未执行 | `--test` 会重装依赖，本轮跳过 |
| `tools/net_check.py` 在 VPS | 未跑 | SKIP 分支没有真实环境验证 |
| `tools/remote_check.py` 跨机 | 未跑 | 需远端执行器在线 |
| 恢复中断故障注入 | 未做 | B2 的自愈路径只做了四态探针，没做 SIGKILL 实验 |
| `core.py` 第 4 / 9 / 10 / 11 条 | 无专用用例 | 仅靠全量回归兜底 |
| `db.py` / `backup.py` 的 rc4 增量 | 无 rc4 专用用例 | 由 `test_v011_rc3.py` 的 26 项覆盖 |
| pytest | 未引入 | 本项目用 `unittest`，没装 pytest |
| exe / apk 真机形态 | 未实现 | `ARCHITECTURE-FUTURE.md` 第 4、7 章是草案 |

## 6. 需人类授权才能做的事 🔸

以下四件本轮**一律没有执行**。代码与文档已就绪，等你决定。

### 6.1 🔸 把 rc4 推到现役服务

现役仍是 rc3（`832f699`）。推 rc4 意味着覆盖正在跑的 headless 进程、重启服务、
让执行器重新连。rc4 的 C / D / E 与部分 A 改动还在工作区**未提交**，推之前至少要：

1. 决定是否先 `git add` + `commit`（本文不代替你做版本管理决策）；
2. 在目标机备份 `council.db` 与 `ticket.key`（用 `bigfish.cli` 的备份子命令）；
3. 关掉旧进程再上新版本，不要并行混跑（混跑危害见 ROLLBACK §7）。

风险：如果平台 B 没先跑一遍 162 项就直接上，Windows 专属路径（WMI、`msvcrt.locking`、
ACL）出问题时没有基线可对照。

### 6.2 🔸 导入 DSH MCP 连接

    # dsh-mcp-bigfish.json 当前内容（Authorization 是占位符）
    {
      "mcpServers": {
        "bigfish-council": {
          "type": "http",
          "url": "http://127.0.0.1:3098/mcp",
          "headers": {
            "Authorization": "Bearer <把 executor-add 打印出来的 bfc_ex_... 填在这里>",
            "Accept": "application/json, text/event-stream"
          },
          "timeoutMs": 30000
        }
      }
    }

导入前必须先在目标机跑 `executor-add` 生成真实 `bfc_ex_...` Token，并确认吊销方式。
影响：DSH 从此能领取已批准的任务（五个工具都是领取 / 心跳 / 回传 / 查询，**不含审批**）。

### 6.3 🔸 注册开机自启

    .\install-task.ps1

会写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 或注册计划任务
（`Register-ScheduledTask -TaskName $taskName`）。影响：每次登录自动起 GUI + API 监听回环端口。
卸载方式在脚本注释里：`Unregister-ScheduledTask -TaskName $taskName -Confirm:$false`。

### 6.4 🔸 Tailscale Serve 变更

rc3 期间开过的转发：

    |-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
    |--> tcp://127.0.0.1:3096

rc4 若要用它重跑跨机闭环，需要确认这条还在、端口对上（rc4 默认端口没改）、
对端跑的是 rc4。关闭命令 `tailscale serve --tcp=3096 off`。
**没有开 Funnel，没有公网入口**；tailnet 内任何设备都能访问这个 API，除 `/health` 外都要 Bearer Token。

## 7. 回滚

完整步骤、命令与实测输出见 **[ROLLBACK-v0.1.1-rc4.md](ROLLBACK-v0.1.1-rc4.md)**（9 节，253 行）。
这里只讲三条硬事实：

1. **SCHEMA_VERSION 仍是 3**，rc3 与 rc4 互读同一个库不用迁移。这是 rc4 可以原地回滚的前提。
2. **密钥轮换过就不能回滚**。`ensure_key_consistency` 一旦自愈（`rotated`），
   旧密钥对应的执行票全部作废；回滚代码不能把密钥回滚回去，见 ROLLBACK §3。
3. **不要混跑**。rc3 与 rc4 交替读写同一库会互相踩幂等表与票吊销记录，见 ROLLBACK §7。

数据层面的回滚走 rc4 的恢复流程（`backup.py` 的 `restore_bundle`），
恢复前先在临时目录验证，别直接对真实数据目录动手。

## 8. 下一步建议

按优先级排，前两条不做完，rc4 就只能停在「平台 A 通过」。

| 优先级 | 事项 | 为什么排这里 | 前置 |
| --- | --- | --- | --- |
| P0 | 在 Windows 上跑一遍 162 项 + 16 自检 + 9 GUI 冒烟 | rc4 的 A / B1 / B2 与 F 批大量改 Windows 专属路径，没跑过就是没验证 | 先在 Windows 建 venv 装 `requirements.txt`；先设 `PYTHONIOENCODING=utf-8` 并重定向到文件（rc3 踩过 GBK 转码的坑） |
| P0 | 真显示器下人工验收 GUI | 离屏冒烟测不到 DPI、托盘、关窗确认框 | 无 |
| P1 | 在 Windows 上跑 `start-headless.ps1` / `stop-headless.ps1` | 本轮改动最大、风险最高的两块，一次都没跑通 | Windows 主机 |
| P1 | 提交 rc4 的 C / D / E 与 A 改动 | 工作区还有 2421 行未提交，回滚与审计都依赖提交点 | 决定提交切分方式 |
| P2 | 重跑跨机 Tailscale 闭环 | 只在 rc3 通过过一次，rc4 网络层有改动（`PORT_RESERVED` / selfcheck 校验） | 对端先上 rc4 |
| P2 | 补 LICENSE | 仓库从 rc1 起就没有 | 法律决定，本文不代做 |
| P3 | 引入 mypy 或 pyright | `pyflakes` 查不出类型错误；rc4 改了大量函数签名（`safe_submit` 的 `task_id`、`_prepare_staging` 返回元组） | 先定要不要加依赖 |
| P3 | 24h soak | 长稳、文件增长、内存驻留全未观测 | 需要一个空转的稳定环境 |
| P4 | B2 的故障注入（`os.replace` 之间 SIGKILL） | 当前自愈路径只有代码审读 + 四态探针 | 可在临时目录做，不碰真实数据 |
| P4 | 实现 exe / apk 形态 | `ARCHITECTURE-FUTURE.md` 是草案 | 前四条先落地 |

## 9. 一句话

平台 A 的 162 项、16 项自检、9 项冒烟、22 项回环 e2e 与两项静态检查全部退出码 0，代码层面 rc4 可以交；
Windows 这轮一项没跑、跨机没重跑、类型检查与长稳没做，所以它是**发布候选**，还不是生产版。
