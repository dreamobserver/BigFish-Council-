# 迁移说明（v0.1.1-rc3 → v0.1.1-rc4）

> 结论：**rc3 → rc4 没有 schema 变化**，`SCHEMA_VERSION` 仍是 **3**，库结构、
> 备份格式（`BACKUP_FORMAT_VERSION = 3`）、12 张必需表都没动。
> 但有 **5 处行为不兼容**和 **1 处启动期版本闸门**，升级前需要把在途的执行票、
> BLOCKED 任务、写死 3099 的脚本先处理掉。

本文所有函数名、常量、行号都对着当前分支 `fix/v0.1.1-rc4-hardening` 的代码核过。

## 1. schema 现状（按代码逐条核对）

| 项 | rc3 | rc4 | 位置 |
| --- | --- | --- | --- |
| `SCHEMA_VERSION` | 3 | **3** | `bigfish/db.py:21` |
| `BACKUP_FORMAT_VERSION` | 3 | **3** | `bigfish/db.py:22` |
| `REQUIRED_TABLES` | 12 张 | 12 张 | `bigfish/db.py:24-27` |
| `MIGRATIONS` 最高键 | 3 | 3 | `bigfish/db.py:193-202` |
| `__version__` | `0.1.1-rc3` | `0.1.1-rc4` | `bigfish/__init__.py:6` |

12 张表：`meta`、`tasks`、`task_versions`、`reviews`、`approvals`、`executors`、
`tickets`、`runs`、`artifacts`、`events`、`idempotency`、`audit`。

版本 3 的迁移项（rc3 引入，rc4 未追加）：

    ALTER TABLE executors ADD COLUMN model TEXT;
    ALTER TABLE tickets   ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks     ADD COLUMN cancel_requested_at REAL;
    CREATE INDEX IF NOT EXISTS idx_tickets_executor ON tickets(executor_id);
    CREATE INDEX IF NOT EXISTS idx_idem_expires   ON idempotency(expires_at);

**升级动作：无需任何 SQL。** rc4 打开 rc3 的库时 `_migrate_locked()` 会发现
`current == SCHEMA_VERSION` 直接 return（`bigfish/db.py:335` 起的逻辑）。
实测：用 rc4 建库后 schema 仍是 3；rc3 打开 rc4 建的库，`schema_version()` 同样返回 3。

## 2. 启动期的版本闸门（新增，会影响「拿新库喂旧程序」）

`Database.__init__`（`bigfish/db.py:213`）现在先调 `_peek_schema_version()`
（`bigfish/db.py:242`）**只读**探测版本，再决定动不动这个文件。

| 情况 | rc3 行为 | rc4 行为 |
| --- | --- | --- |
| 库版本 == 程序版本 | 正常打开 | 正常打开 |
| 库版本 < 程序版本 | 迁移 | 迁移 |
| 库版本 > 程序版本 | 先 `PRAGMA journal_mode=WAL` 再报错，文件已被碰过 | 探测后立刻关连接、置 `_closed=True`、抛 `DatabaseVersionError`，文件字节不变、不生成 `-wal` |

实测（临时目录，schema 写 99 的库）：

    DatabaseVersionError: 数据库 schema 版本 99 高于本程序支持的 3，
    拒绝打开（升级程序，不要降级数据库）
    文件是否被改写: False | -wal 是否存在: False

对 rc4 的含义：**rc5 的库不能拿 rc4 打开**，会直接失败。这是有意的行为，
不要试图绕过（改 meta 里的版本号会让迁移在缺列时炸在更难排查的位置）。

## 3. 五处行为不兼容

### 3.1 `claim()` 无幂等键时改用随机 nonce 派生票

代码：`bigfish/core.py:705` `claim()`，票据派生在 `bigfish/core.py:746`：

    ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)
    ticket = self._derive_ticket(ex_id, tid, version, ticket_nonce)

rc3 的派生材料只有「执行器 + 任务 + 版本 + 幂等键」，同一任务多轮领取会算出**同一张票**；
rc4 在没有幂等键时塞随机 nonce，每轮都是新票。领取时先
`UPDATE tickets SET revoked_at ... WHERE task_id=? AND revoked_at IS NULL`
（`bigfish/core.py:760-761`）吊销同任务全部旧活票，再 INSERT 新票。

| 问题 | 升级后会发生什么 | 需要人做什么 |
| --- | --- | --- |
| 在途执行票 | 库里已存在、未被吊销的 rc3 票，rc4 仍按 `token_hash` 查表认（实测心跳、提交都成功）；一旦该任务被重新领取，旧票立刻报「执行票已被撤销」 | 按「旧票不再可信」处理：升级后**所有在途执行器重新领取**，不要复用升级前的票 |
| 幂等重放 | 不带幂等键的领取不再可复算；带幂等键的仍走 `_replay_claim`，但票面值依赖当前密钥 | 执行器调用 `claim` 时**始终带幂等键**，重试语义才有保证 |

### 3.2 `verify(accept=True)` 只接受 SUBMITTED

代码：`bigfish/core.py:1082`：

    allowed = (S.SUBMITTED,) if accept else (S.SUBMITTED, S.BLOCKED)

| 问题 | 升级后会发生什么 | 需要人做什么 |
| --- | --- | --- |
| BLOCKED 任务点「通过」 | rc3 能把它变成 DONE；rc4 抛 `CouncilError：当前状态「已阻断」不允许验收通过` | 先 `verify(tid, accept=False)` 退回 RETURNED（此路径实测可用），再由人决定重做或作废 |

实测：BLOCKED 上 `verify(True)` 报错，`verify(False)` 把任务置为 RETURNED 并写入
`blocked_reason`（note 为空时填「验收未通过」）。

口径上要接受的一件事：BLOCKED 表示「副作用不明」，rc4 不允许用一次点击把它洗成 DONE。

### 3.3 `vote_summary` 每个模型只取最新一票（含已撤销）

代码：`bigfish/core.py:520` / `bigfish/core.py:595` `_vote_summary_locked()`。
SQL 去掉了 `AND revoked_at IS NULL`，改为 `ORDER BY id DESC` 每个 model 只收第一条：

    gone = bool(r["revoked_at"])
    result[r["model"]] = {"verdict": None if gone else r["verdict"], ...,
                          "revoked": gone}

| 场景 | rc3 汇总结果 | rc4 汇总结果 |
| --- | --- | --- |
| 先 reject 再 approve | approve | approve（最新一票是 approve） |
| 先 approve 再撤票 | 回落到更早那张 approve | **无票**（`verdict=None`、`revoked=True`），`all_approved=False` |
| 撤票后退回议事重投 | 可能被更早的票兜住 | 必须重新投一张新票 |

历史票数据一条没改，改的只是汇总口径。GUI 侧已经跟着改：投票徽标读
`votes[model].revoked`，撤票后显示「已撤票」。

需要人做什么：如果升级时某任务恰好停在「撤了最新赞成票、还指望旧票兜底」的状态，
审批会被挡住。处置办法是让对应模型重新投一票，不要手工去改 `reviews` 表。

### 3.4 `update_prompt()` 会清取消/阻断/执行器残留

代码：`bigfish/core.py:319`，UPDATE 语句在 `bigfish/core.py:356-362`：

    ... status=?, updated_at=?, cancel_requested_at=NULL,
    blocked_reason=NULL, executor_id=NULL, control=? ...

同时 `control` 里的 `cancel_requested` / `pause_requested` 被 `pop` 掉。

| 问题 | 升级后会发生什么 | 需要人做什么 |
| --- | --- | --- |
| 「取消过的任务改一版 Prompt 再审批」 | rc3 的 `claim` 先撞 `CouncilError("任务已被请求取消，不能领取")`（`bigfish/core.py:673-674` 的 rc3 版本），再被 `WHERE cancel_requested_at IS NULL` 挡住，改完 Prompt 仍然领不到；rc4 改完就能领 | 无需额外操作；这是修复，行为上算兼容性变化 |
| 版本号 | `current_version` +1，状态回到 IN_REVIEW | 新版本会同时吊销旧版本全部执行票，执行器要重新领取 |

实测：BLOCKED 任务执行 `update_prompt` 后状态变 IN_REVIEW，三字段均为 None，版本 +1。

### 3.5 `tools/selfcheck.py` 默认端口 3099 → 3094

代码：`tools/selfcheck.py:22`

    PORT = int(os.environ.get("BFC_SELFCHECK_PORT", "3094"))

原因写在 `tools/selfcheck.py:19-21` 的注释里：3099 在 rc4 被 `config.PORT_RESERVED`
登记为手机端 REST 网关（`bigfish/config.py:38`，状态「计划中，未实现」），
3095 在本容器被 quota-hub 长期监听。

| 问题 | 升级后会发生什么 | 需要人做什么 |
| --- | --- | --- |
| 写死 3099 的脚本 | 自检会连到别的服务，或干脆连不上 | 把硬编码改成 3094，或用 `BFC_SELFCHECK_PORT` 覆盖；不要改 `selfcheck.py` 的默认值 |
| 只跑 `.venv/bin/python tools/selfcheck.py` | 自动用 3094 | 无需操作 |

`PORT_RESERVED` 是平行字典，**没有**并进 `PORT_ROLES`（`bigfish/config.py:27-31` 仍是三行），
所以 `bigfish.cli allowed-ports` 不会列出 3099。

## 4. 升级前检查清单

| # | 检查项 | 命令 / 判据 | 不满足怎么办 |
| --- | --- | --- | --- |
| 1 | 所有进程已停 | `pgrep -af 'bigfish|bfc-proxy'` 无输出 | 先停 GUI / headless / watchdog / VPS 跳板 |
| 2 | 数据目录已整份备份 | 备份里含 `council.db`、`council.db-wal`、`secrets/ticket.key` | 缺 `ticket.key` 的备份不算备份 |
| 3 | 知道当前 schema | `sqlite3 council.db "SELECT value FROM meta WHERE key='schema_version'"` 应为 3 | 是 4 及以上说明库已被更高版本碰过，禁止用 rc4 打开 |
| 4 | 有 BLOCKED 任务在等「点通过」 | `python -m bigfish.cli task-list --status blocked` | 先按 3.2 退回 RETURNED |
| 5 | 有在途执行票 | `SELECT count(*) FROM tickets WHERE revoked_at IS NULL` | 通知执行器升级后重新领取 |
| 6 | 脚本里写死 3099 | `grep -rn 3099` 排除 `.venv` | 改成 3094 或 `BFC_SELFCHECK_PORT` |
| 7 | 备份包能读 | `python -m bigfish.cli --data-dir <dir> inspect <pkg.zip>` 返回 `schema_version=3` | 包坏就别升级，先重导一份 |

## 5. 升级步骤

| 步骤 | 动作 | 验证 |
| --- | --- | --- |
| 1 | 停进程：GUI、headless 服务、watchdog、VPS 侧 `bfc-proxy.mjs` | `pgrep` 无输出 |
| 2 | 备份整个数据目录，**含 `secrets/ticket.key`**（默认 `~/.bigfish-council`，Windows 是 `%LOCALAPPDATA%\BigFishCouncil`） | `ls` 能看到 `council.db` 与 `secrets/ticket.key` |
| 3 | 换代码（`git checkout` 到 rc4 或解压 rc4 源码） | `python -c "import bigfish;print(bigfish.__version__)"` 输出 `0.1.1-rc4` |
| 4 | 启动任意入口（GUI / `python -m bigfish` / `python -m bigfish.cli overview`），首次打开自动过版本闸门 | `overview` 返回 JSON，不抛 `DatabaseVersionError` |
| 5 | 跑全量测试 | 见下方实测记录，应 `Ran 162 tests / OK` |
| 6 | 跑自检 | `python tools/selfcheck.py`，默认监听 3094 |
| 7 | 通知执行器重新领取 | 旧票不再复用 |

数据库本身在这一步没有任何 DDL 执行（`MIGRATIONS` 里没有版本 4）。

## 6. 实测记录（2026-11-19，临时目录）

环境变量固定为 `LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen`。

| 测项 | 做法 | 结果 |
| --- | --- | --- |
| rc4 建库版本 | `Database(p).init_schema()` | `schema_version() = 3` |
| rc4 打开 rc3 库 | rc3 建的库直接给 rc4 | 正常打开，不做任何 DDL |
| rc3 打开 rc4 库 | 反向 | 正常打开，`restore_epoch` 保留，rc4 签发的票在 rc3 心跳/提交通过 |
| rc3 票在 rc4 | rc3 签发的票拿给 rc4 心跳、提交 | 通过（按 `token_hash` 查表，不重算票面值） |
| 未来版本库 | schema 写 99 的库给 rc4 | 抛 `DatabaseVersionError`，文件字节不变、无 `-wal` |
| BLOCKED 验收 | BLOCKED 上 `verify(True)` / `verify(False)` | 前者报错，后者退回 RETURNED |
| 撤票口径 | 先 approve 再撤票 | `verdict=None`、`revoked=True`，`all_approved=False` |
| update_prompt 清残 | BLOCKED 任务改 Prompt | 状态 IN_REVIEW，三字段 None，版本 +1 |

全量测试（真实仓库）：

    cd /home/dsh/bigfish-council
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .
    # Ran 162 tests / OK

## 7. 升级失败怎么办

| 症状 | 大概率原因 | 处置 |
| --- | --- | --- |
| 启动抛 `DatabaseVersionError` | 库版本高于 3（被 rc5 或手工改过） | 换回新版本程序；不要改 meta |
| 执行器心跳报「执行票已被撤销」 | 任务被重新领取，旧票已吊销 | 重新 `claim` |
| 执行器心跳报「执行票无效」 | 票面值是用另一份密钥派生的（换过密钥） | 重新 `claim`，旧票无法复算 |
| BLOCKED 任务点通过被拒 | 3.2 的新规则 | `verify(False)` 退回 |
| 自检连到异常服务 | 还在用 3099 | 改 3094 或设 `BFC_SELFCHECK_PORT` |

失败后**不要删库**。先整份复制数据目录，再走 `docs/ROLLBACK-v0.1.1-rc4.md`。

## 8. 回滚到 rc3 的前置条件

| 前置条件 | 说明 |
| --- | --- |
| rc4 期间没跑过 `restore` | 跑过就会轮换密钥并把 `restore_epoch` +1，回 rc3 后 rc4 签发的票仍是新 epoch，旧票也不会复活 |
| 密钥没被手工替换 | 回滚密钥会让两边都认不出已签发的票，见 ROLLBACK 文档第 3 节 |
| 库版本仍是 3 | rc4 没升级 schema，所以**库可以直接给 rc3 用**（实测 rc3 打开 rc4 建的库正常） |
| 已备份 rc4 期间产生的数据 | 回滚只换代码，库不换；但 prerestore 文件会留在数据目录，别误删 |

**不允许回滚的情形：**

1. rc4 期间执行过 `restore`（密钥已轮换、`restore_epoch` 已 +1）——回 rc3 后旧票依然失效，
   回滚不能恢复票，只能重新领取；这属于「回滚也救不回来」，不要把它当止损手段。
2. 数据目录里已出现 `council.db.restore-new` 或 `council.db.restore.lock` 残留
   ——说明上一次恢复没走完，先把现场留存再判断，别急着换代码。
3. 有人把 schema 改到 4 ——rc3 会以同样的闸门拒绝打开，回滚等于让服务完全起不来。

回滚步骤与密钥处理细节见 `docs/ROLLBACK-v0.1.1-rc4.md`。
