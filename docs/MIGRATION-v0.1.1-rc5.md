# 迁移说明：v0.1.1-rc4 → v0.1.1-rc5

| 项 | 值 |
| --- | --- |
| 起点 | `0.1.1-rc4`（发布点 `72e8535`，`SCHEMA_VERSION = 3`） |
| 终点 | `0.1.1-rc5`（`b8f6de1`，`SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`） |
| 分支 | `fix/v0.1.1-rc4-hardening` |
| 库结构变化 | 只加 1 列（`executors.disabled_reason`）+ 1 个索引（`idx_executors_enabled`），不重建表、不删列、不改既有值 |
| 是否需要人工介入 | 需要。升级本身自动完成；升级后若执行过恢复，必须人工 `reenable` 重新授权 |
| 能否降级 | 不能。库版本高于程序版本时 `Database.__init__` 直接拒绝打开 |
| 已验证平台 | Debian 12 / Python 3.11.2（244 项全绿）。Windows 11 / Python 3.14.6 本批次未跑 |

本文只描述「从旧库迁移到 rc5」这一个方向。回滚见 `docs/ROLLBACK-v0.1.1-rc5.md`。

---

## 1. rc4 → rc5 的结构变化

### 1.1 常量

| 常量 | rc4 | rc5 | 位置 |
| --- | --- | --- | --- |
| `SCHEMA_VERSION` | 3 | 4 | `bigfish/db.py:22` |
| `BACKUP_FORMAT_VERSION` | 3 | 4 | `bigfish/db.py:23` |
| `REQUIRED_TABLES` | 12 张 | 12 张（未变） | `bigfish/db.py:25-28` |

### 1.2 迁移体只有两条 SQL

`MIGRATIONS[4]`（`bigfish/db.py:205-208`）：

```sql
ALTER TABLE executors ADD COLUMN disabled_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled);
```

- 只加一列 + 建一个索引。没有 `CREATE TABLE ... AS SELECT`、没有 `DROP TABLE`、不搬数据、不改既有行的值。
- `disabled_reason` 是 `TEXT`、无默认值：存量行读出 `NULL`，语义正好是「没有被禁用」，不需要回填。
- 索引服务于 R1 的 `UPDATE executors SET enabled=0 ... WHERE enabled=1` 与身份列表查询。

### 1.3 一个必须说清楚的事实：这条索引只在升级库上存在

`SCHEMA` 建表块（`bigfish/db.py:163-170`）里的索引只有 8 个：

`idx_tasks_status` / `idx_reviews_task` / `idx_runs_task` / `idx_events_task` / `idx_tickets_task` / `idx_tickets_executor` / `idx_idem_expires` / `idx_audit_created`

**`idx_executors_enabled` 不在 `SCHEMA` 块里，只写在 `MIGRATIONS[4]` 里**。后果：

| 库的来源 | 升级到 schema 4 后有几个索引 | 有没有 `idx_executors_enabled` |
| --- | --- | --- |
| 旧库升级而来 | 9 个 | 有（由 `MIGRATIONS[4]` 建出） |
| 全新安装（`init` 或首次 `Database`） | 8 个 | 没有 |

两边的查询结果与行为一致（只影响 `WHERE enabled=?` 的扫描方式），但核对「升级是否成功」时不要拿这条索引当判据——全新库里它本来就不存在。实测见第 9 节。

---

## 2. 迁移在什么时候执行、怎么执行

### 2.1 打开数据库的先后顺序

`Database.__init__`（`bigfish/db.py:228`）：

1. 先 `_peek_schema_version()` 只读探测（`bigfish/db.py:262`）：只查 `sqlite_master` 与 `meta.schema_version`，不建表、不写 meta、不改 `journal_mode`；
2. 版本高于程序支持时关连接、置 `_closed=True`、抛 `DatabaseVersionError`（`bigfish/db.py:254`），一个字节都不写；
3. 通过之后才 `PRAGMA journal_mode=WAL` / `synchronous=NORMAL` / `foreign_keys=ON`。

### 2.2 `init_schema()` 的顺序（`bigfish/db.py:345`）

| 库的状态 | 做什么 |
| --- | --- |
| `meta` 表都不存在（`current == 0`） | 直接 `executescript(SCHEMA)`，写 `schema_version` 与 `restore_epoch=0`，不跑任何迁移 |
| 已有库（`current > 0`） | （1）`_migrate_locked(current)` 逐版本迁移；（2）再 `executescript(SCHEMA)` 补最新索引/表；（3）`INSERT OR IGNORE restore_epoch=0` |

「先迁移、再跑最新 SCHEMA」这个顺序是缺陷修复的结果：历史上先跑 SCHEMA 会导致 v1 库在「`CREATE INDEX ... idempotency(expires_at)`」处直接炸 `no such column`，迁移根本没机会执行。

### 2.3 `_migrate_locked` 的原子性（`bigfish/db.py:412-441`）

```python
for ver in range(current + 1, SCHEMA_VERSION + 1):
    self._conn.execute("BEGIN IMMEDIATE")
    try:
        for sql in MIGRATIONS.get(ver, []):
            ...
        self._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(ver),))
        self._conn.execute("COMMIT")
    except Exception:
        self._conn.execute("ROLLBACK")
        raise
```

- **每个版本一个单独事务**：失败就停在原版本，不留半迁移状态。
- `current == SCHEMA_VERSION` 直接 `return`，不产生任何写入。
- `duplicate column name` 视为成功（可重复执行）。
- 全部版本跑完后 `init_schema` 会再复核一次版本，不到 4 就抛错。

### 2.4 失败会怎样（实测）

把 `MIGRATIONS[4]` 换成一条必然失败的语句 `ALTER TABLE no_such_table ADD COLUMN boom TEXT`，用 rc5 打开一个 schema 3 的库：

| 检查项 | 结果 |
| --- | --- |
| 异常 | `OperationalError: no such table: no_such_table` |
| `meta.schema_version` | 仍是 **3**（没有前进） |
| `executors` 列 | 没有 `disabled_reason`（同一事务里的第一条 ALTER 已一起回滚） |
| `idx_executors_enabled` | 不存在 |
| `PRAGMA integrity_check` | `ok` |
| 换回正确 `MIGRATIONS` 后重跑 | 成功，schema → 4，索引建好，原有数据一行不少 |

结论：**迁移失败不会把库改坏，可以原地重试**。数据库停在旧版本，旧版本程序照样能打开。

---

## 3. 更早版本（v0.1.0 / rc1 / rc2 / rc3）怎么升上来

### 3.1 各发布点的版本号

| 发布点 | 提交 | `SCHEMA_VERSION` | `BACKUP_FORMAT_VERSION` |
| --- | --- | --- | --- |
| v0.1.0 | `1a97f5a` | 1 | 无此常量 |
| v0.1.1-rc1 | `d492b85` | 2 | 2 |
| v0.1.1-rc2 | `8c1aaf9` | 2 | 2 |
| v0.1.1-rc3 | `832f699` | 3 | 3 |
| v0.1.1-rc4 | `72e8535` | 3 | 3 |
| v0.1.1-rc5 | `b8f6de1` | 4 | 4 |

### 3.2 一次启动内按版本号连续跳

`_migrate_locked` 从 `current + 1` 一直循环到 4，中间版本一个都不跳。用 rc5 打开 v0.1.0 的库（schema 1），实际跑的是 `2 → 3 → 4` 三步：

| 迁移键 | 语句数 | 明细 |
| --- | --- | --- |
| `MIGRATIONS[2]` | 10 | 8 条 `ADD COLUMN`（`reviews.actor` / `reviews.revoked_at` / `approvals.actor` / `approvals.evidence` / `tickets.max_expires_at` / `tickets.renewed_at` / `idempotency.request_hash` / `idempotency.expires_at`）+ 1 条 `CREATE TABLE audit` + 1 条 `CREATE INDEX idx_audit_created` |
| `MIGRATIONS[3]` | 5 | 3 条 `ADD COLUMN`（`executors.model` / `tickets.epoch INTEGER NOT NULL DEFAULT 0` / `tasks.cancel_requested_at`）+ 2 条 `CREATE INDEX`（`idx_tickets_executor` / `idx_idem_expires`） |
| `MIGRATIONS[4]` | 2 | 见 1.2 节 |
| 合计 | 17 | 全部是 `ADD COLUMN` / `CREATE TABLE` / `CREATE INDEX`，没有一条 `DROP` 或重建 |

关于条数的口径：有些材料把 `MIGRATIONS[2]` 记成 9 条、`MIGRATIONS[3]` 记成 4 条，那是漏算了索引语句（rc1 时代的 `MIGRATIONS[2]` 确实只有 9 条，`idx_audit_created` 是后来补进去的第 10 条）。本文按 rc5 代码的实际条数写：**10 / 5 / 2**。字段集与既有文档一致，计数差异只是历史版本残留。

### 3.3 多跳迁移实测

用 v0.1.0 建的库（`SCHEMA_VERSION=1`，11 张表，没有 `audit` 表，目录里没有 `secrets`）交给 rc5 打开，打桩记录到的步骤是：

```
[('start', 1), ('step', 2, 10), ('step', 3, 5), ('step', 4, 2)]
```

结果：

| 检查项 | v0.1.0 升级前 | 升级后 |
| --- | --- | --- |
| `meta.schema_version` | 1 | 4 |
| 表数 | 11 + `sqlite_sequence` | 12 + `sqlite_sequence`（补出 `audit`） |
| `executors` 列 | 无 `model`、无 `disabled_reason` | 两列都有 |
| `idx_executors_enabled` | 无 | 有 |
| `integrity_check` | — | `ok` |
| 旧任务数据 | 有 | 原样保留 |

rc1 / rc2 的库（schema 2）只跑 `3 → 4` 两步；rc3 / rc4 的库（schema 3）只跑 `4` 一步。**不需要先升到 rc4 再升 rc5**，rc5 一次就能从任何旧版本直接上来。

---

## 4. 首次以 rc5 启动旧库：密钥指纹自愈

### 4.1 这一步发生在迁移之后

`runtime.open_council`（`bigfish/runtime.py:13`）的顺序：

```
settings.ensure_dirs() → Database() → init_schema()（迁移在这里） → ensure_key_consistency() → load_or_create_key() → Council()
```

`ensure_key_consistency`（`bigfish/backup.py:672`）的异常会被吞掉，**不会阻止服务启动**——密钥自愈失败不该让整个控制中心起不来。

### 4.2 四种动作

| 库里的状态 | 动作 | 会不会吊销执行票 |
| --- | --- | --- |
| `meta.key_sha256` 与磁盘密钥一致 | `consistent`，什么都不做 | 不会 |
| `meta` 里**没有 `key_sha256`**、磁盘密钥在 | `recorded`：只补记指纹，`rotate=False` | **不会** |
| 指纹在、磁盘密钥被换过 | `rotated`：重新对齐密钥 | 会，吊销全部活票 + 清 `claim/heartbeat/submit` 幂等 |
| 磁盘密钥文件不存在 | `new-key`：生成新钥 | 会，同上 |

只有 `rotate=True` 的分支才会 `UPDATE tickets SET revoked_at ...` 并 `DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')`，同时写 `audit`（actor=`human_owner`、action=`db.key_healed`、target=`ticket.key`）。

### 4.3 为什么「补记指纹」刻意不吊销任何票

这是 rc5 里最重要的一条设计取舍，原文写在 `_heal_key`（`bigfish/backup.py:695`）的注释里：

> 只有真的换了密钥才动票。仅仅补记指纹（老库首次升级到 rc5）时票还是用同一把密钥签的，作废它们等于无故中断在途执行。

展开说三层理由：

1. **票本身没有变得不可信。** 执行票是 HMAC 确定性派生的，签名用的还是磁盘上那把 `secrets/ticket.key`。`key_sha256` 只是「库记得的那把钥匙的指纹」，它缺失不代表钥匙换了，只代表这一版程序还没来得及记录。此时吊销票，等于在没有任何凭据泄漏的前提下主动打断正在跑的任务。
2. **这不是一次安全事件，只是一次版本升级。** 真正需要作废票的语义是「密钥世代变了，旧票不可复算」。`recorded` 分支里世代没变（`restore_epoch` 也没动），所以在途执行器的心跳、提交、续租都应该照常工作。
3. **如果升级即吊销，每次升级都会强制中断生产。** `key_sha256` 是 rc4 发布点前一个提交（`2ea5d1b`）才引入的，rc1 / rc2 / rc3 / rc4 的库里都没有这个键，v0.1.0 的库连密钥文件都没有。若 `recorded` 也吊销，等于「任何一次跨版本升级都要把所有在途任务重来一遍」，这不可接受。

反过来，`rotated` / `new-key` 必须吊销：那时票确实不可复算（密钥变了），旧票既不能验签也不该再放行。

### 4.4 历史对照：哪些旧库走哪条分支

| 旧库来源 | 目录里有 `secrets/ticket.key` 吗 | 首次被 rc5 打开走的分支 | 执行票 |
| --- | --- | --- | --- |
| v0.1.0（`1a97f5a`） | **没有**（那一版没有 `runtime.py`、没有 `load_or_create_key`） | `new-key` | 全部吊销 |
| rc1 / rc2 / rc3 / rc4 | 有（首启就会 `load_or_create_key` 生成） | `recorded` | 全部保留 |
| 已经跑过 rc4 `restore` 的库 | 有，且 `meta` 已有 `key_sha256` | `consistent` | 保留（但恢复时已被吊销过一轮） |

### 4.5 实测

| 场景 | 返回 / 审计 detail | 活票变化 |
| --- | --- | --- |
| rc4 库（schema 3、1 张活票、meta 无 `key_sha256`）首次 rc5 打开 | `action=recorded`，`tickets_revoked=0`，`idempotency_purged=0`；审计写「库内尚无 key 指纹（recorded），已补记、未改动任何执行票」 | 1 → 1（不变） |
| 同上，rc4 签发的票在 rc5 下心跳 | `ok=True`，`task_status=RUNNING` | 任务照常跑 |
| 同上，再调一次 `ensure_key_consistency` | `action=consistent` | 不变 |
| rc1 / rc2 / rc3 的库（各有 1 张活票）首次 rc5 打开 | 都是 `recorded`，审计同一句 | 1 → 1，老票心跳 `ok=True` |
| v0.1.0 的库（无密钥文件、1 张活票）首次 rc5 打开 | `action=new-key`；审计写「库内 key 指纹与磁盘密钥不一致（new-key），已重新对齐密钥并吊销 1 张执行票、清理 1 条幂等记录」 | 1 → 0，旧票心跳报 `执行票已被撤销` |
| 指纹在、密钥被换 | `action=rotated`，`tickets_revoked=1` | 1 → 0 |
| 指纹在、密钥文件被删 | `action=new-key`，`tickets_revoked=1` | 1 → 0 |

结论：**rc4 → rc5 这条主路径不会吊销任何执行票，在途任务不中断。** 只有从 v0.1.0 直跳（或密钥真的变了）才会吊销。

---

## 5. 禁止降级

### 5.1 启动期硬闸门

库版本高于程序版本时，`Database.__init__`（`bigfish/db.py:254`）抛：

```
DatabaseVersionError: 数据库 schema 版本 4 高于本程序支持的 3，拒绝打开（升级程序，不要降级数据库）
```

用 rc4 的代码打开一个 schema 4 的库，实测：

| 检查项 | 结果 |
| --- | --- |
| 异常类型 | `DatabaseVersionError` |
| CLI 退出码 | 2，stderr 为上面那句话 |
| 库文件 sha256 | 前后**完全一致** |
| 目录里是否多出 `council.db-wal` / `council.db-shm` | 没有 |
| `PRAGMA journal_mode` | 没被改写 |

闸门之所以能这么干净，是因为探测用的是 `_peek_schema_version`：它只 `SELECT`，不建表、不写 meta、不设 WAL。而 `PRAGMA journal_mode=WAL` 要等闸门通过之后才执行——这正是「历史缺陷：一上来就改 WAL」的修复点。

### 5.2 备份包层面也拦

`_verify_version`（`bigfish/backup.py:230`）在 `inspect_bundle` 与 `restore_bundle` 两条路径上都做同一件事：

```
备份 schema 版本 4 高于当前程序支持的 3，请先升级程序，禁止把高版本库恢复进低版本程序
```

实测 rc4 对 rc5 导出的包：`inspect` RC=2、`restore` RC=2，两条路径都拒，且发生在替换文件之前。

### 5.3 所以「降级」的唯一合法形态

**连库一起回滚**：程序换回 rc4，同时把数据目录里的库换成一份 schema 3 的副本（升级前备份，或 rc4 时代导出的包恢复出来）。没有 schema 3 的副本，就只能停在 rc5。

具体步骤见 `docs/ROLLBACK-v0.1.1-rc5.md`。

---

## 6. 升级前检查清单

| # | 事项 | 怎么做 | 为什么 |
| --- | --- | --- | --- |
| 1 | 确认当前版本与库版本 | `.venv/bin/python -c "import bigfish;print(bigfish.__version__)"`；再用 Python 读 `meta` 里的 `schema_version` | 先知道自己在哪个版本，出问题时才有对照 |
| 2 | 停掉所有写库的进程 | GUI、headless、计划任务全停；用 `ss -lntp`（Windows 用 `netstat -ano`）确认 3096 上没有监听 | 半开连接会让 `os.replace` 撞文件占用，Windows 上直接失败 |
| 3 | 整份备份数据目录 | `cp -a <data-dir> <data-dir>.bak`（Windows 用 `robocopy /E` 或压缩整个目录） | 回滚的唯一依靠，见第 5 节 |
| 4 | 备份必须含密钥 | 确认副本里有 `secrets/ticket.key` | 缺了它，已签发的票无法复算 |
| 5 | 导出一份 rc4 格式的 operational 包 | `.venv/bin/python -m bigfish.cli --data-dir <dir> export <pkg>.zip` | **必须在升级前用 rc4 导**：rc5 导出的包 schema_version=4，rc4 会拒收 |
| 6 | 记录升级前状态 | 活票数、`restore_epoch`、executors 列表 | 升级后核对「票有没有被误吊销」 |
| 7 | 确认磁盘有余量 | 迁移只需几 KB，但备份目录要放得下整库 | — |
| 8 | 记住 Windows 侧本批次未跑 | 见 `docs/TEST-REPORT-v0.1.1-rc5.md` | 新机器上先跑自检再上业务 |

---

## 7. 升级步骤

### 7.1 标准步骤（rc4 → rc5）

| # | 命令 | 期望 |
| --- | --- | --- |
| 1 | 停服务：关 GUI，Windows 上 `powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1`，Linux 上 `pkill -f "python -m bigfish"` | 端口无监听 |
| 2 | 备份：`cp -a ~/.bigfish-council ~/.bigfish-council.bak` | 副本含 `secrets/ticket.key` |
| 3 | 导包：`.venv/bin/python -m bigfish.cli export ~/bfc-rc4-before-upgrade.zip` | 返回包路径 |
| 4 | 换代码：`git checkout fix/v0.1.1-rc4-hardening`（或解压 rc5 源码覆盖） | `bigfish.__version__ == "0.1.1-rc5"` |
| 5 | 装依赖（若有变化）：`.venv/bin/pip install -e .` | RC=0 |
| 6 | 触发迁移：`.venv/bin/python -m bigfish.cli init` 或 `overview` | `init` 打印三行（数据目录 / 数据库 / 密钥），RC=0 |
| 7 | 核对 schema | `meta.schema_version` 读出来应为 `4` |
| 8 | 核对票没被误伤 | `select count(*) from tickets where revoked_at is null` 与升级前**相同** |
| 9 | 跑自检：`.venv/bin/python tools/selfcheck.py` | 通过 16 项、失败 0 项 |
| 10 | 跑全量测试：`.venv/bin/python -m unittest discover -s tests -t .` | `Ran 244 tests` + `OK` |
| 11 | 起服务 | `/health` 返回 `status=ok` |

第 6 步也可以直接用 GUI 启动——迁移发生在 `Database` 打开库的那一刻，`init` / `overview` / GUI 都一样。

Linux 上跑测试要先给 Qt 库路径与离屏后端：

```bash
export LD_LIBRARY_PATH=/home/dsh/.local/libbfc
export QT_QPA_PLATFORM=offscreen
cd /home/dsh/bigfish-council
.venv/bin/python -m unittest discover -s tests -t .
```

不加 `LD_LIBRARY_PATH` 会有部分用例跳过，数字对不上不是代码问题。

### 7.2 升级后的 schema 应该长什么样

以 rc4 库升级为例（实测）：

| 检查项 | 升级前 | 升级后 |
| --- | --- | --- |
| `meta` 键 | `schema_version=3`、`restore_epoch=0` | 多出 `key_sha256`（64 位 hex） |
| `executors` 列 | `id,name,role,model,token_hash,enabled,created_at,last_seen` | 尾部多出 `disabled_reason` |
| 索引 | 8 个 | 9 个（多 `idx_executors_enabled`） |
| 表数 | 13（12 必需 + `sqlite_sequence`） | 13（不变） |
| 活票 | 1 | 1（不变） |
| `integrity_check` | — | `ok` |
| `audit` 新增 | — | actor=`human_owner`、action=`db.key_healed`、target=`ticket.key`，detail 为「库内尚无 key 指纹（recorded），已补记、未改动任何执行票」 |

---

## 8. 需要人做的事：`reenable`

### 8.1 什么时候会碰到

schema 4 之后 `executors` 多了一列 `disabled_reason`。**只要在 rc5 上执行过一次 `restore`（恢复备份），`_block_inflight` 就会把所有 `enabled=1` 的身份改成 `enabled=0`**，并写入 `DISABLED_REASON_RESTORE`（`bigfish/backup.py:72`）：

> 数据库已从备份恢复，恢复前的凭据全部作废，需人工重新授权

理由很简单：快照里带着 `token_hash`，恢复之后那些早就发到外面的旧 token 会立刻重新可用。禁用的不是「人」，是「已外发的凭据」。

### 8.2 怎么重新授权

```bash
.venv/bin/python -m bigfish.cli --data-dir <data-dir> reenable <exec_id>
```

输出（JSON，RC=0）：

```json
{
  "id": "executor-xxxxxxxx",
  "name": "dsh-cloud",
  "role": "executor",
  "token": "bfc_ex_....",
  "model": null
}
```

要点：

- **`token` 只在这一刻出现一次**，明文不落库（库里存的是哈希）。
- **重新启用会同时轮换凭据**：`UPDATE executors SET enabled=1, disabled_reason=NULL, token_hash=?, last_seen=NULL`。旧 token 的哈希被直接替换掉，物理上无法再用。
- 前缀按角色取：`executor` → `bfc_ex_`，`reviewer` → `bfc_rv_`，`admin` → `bfc_ad_`。
- 审计写 `executor.reenabled`，detail 为「角色:名字（凭据已轮换）」。
- 身份不存在时：stderr `错误: 身份不存在`，exit 2。
- 换发的新 token 要人工交给执行端（客户端配置里替换）。

**不要用 `UPDATE executors SET enabled=1` 绕过它**——那等于让泄露过的那份 token 复活，`reenable` 存在的意义就是强制换钥。

### 8.3 同一次恢复里还有什么要人处理

| 恢复后的状态 | 需要人做什么 |
| --- | --- |
| 在途任务（DISPATCHED / RUNNING / CANCELLING）→ `BLOCKED`，原因「数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批」 | 人工核对副作用，再重新走审批 |
| 已批准待派工的任务（APPROVED）→ 冻结成 `BLOCKED`，原因含「该任务的派工许可已冻结；人工核对后需重新提交审批才可再次派工」 | 重新提交审批 |
| 所有执行票 `revoked_at` 置位 | 无需操作，执行端重新 `claim` 即可 |
| `claim/heartbeat/submit` 幂等缓存被清 | 无需操作，重放会重新计算 |
| `restore_epoch` +1 | 无需操作，旧票的 epoch 不匹配会自动失效 |
| 所有身份 `enabled=0` | **逐个 `reenable`**，并分发新 token |

---

## 9. 实测记录

所有数字都来自本批次在 Debian 12 / Python 3.11.2 上的真实执行，不是推演。

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| rc4 → rc5 升级 | 用 rc4 建的库（schema 3、1 张活票、meta 无 `key_sha256`）跑 rc5 `overview` | schema 3→4，RC=0 |
| 列与索引 | 同上，读 `PRAGMA table_info(executors)` 与 `sqlite_master` | 多出 `disabled_reason` 与 `idx_executors_enabled` |
| 存量数据 | 同上，读 `enabled` 与活票数 | `enabled` 仍为 1、`disabled_reason` 为 `NULL`、活票仍为 1 |
| 数据完整性 | `PRAGMA integrity_check` | `ok` |
| 幂等 | 同一个库再打开一次 | schema 仍 4，无报错、无重复写入 |
| 迁移失败回滚 | 把 `MIGRATIONS[4]` 换成必然失败的 SQL | `OperationalError`；schema 停在 3、无新列无新索引、`integrity_check=ok`；换回正确语句后重跑成功 |
| 多跳迁移 | v0.1.0 的库（schema 1）给 rc5 打开 | 打桩得到 `start 1 / step 2(10) / step 3(5) / step 4(2)`；最终 schema 4、12 表、`integrity_check=ok` |
| 全新库 | 空目录跑 `init` | schema 4；索引只有 8 个（无 `idx_executors_enabled`）；`executors` 列里 `disabled_reason` 在 `enabled` 之后 |
| 禁止降级 | rc4 代码打开 schema 4 的库 | `DatabaseVersionError`，CLI RC=2，库文件 sha256 前后一致，无 `-wal` / `-shm` |
| 备份包层面 | rc4 `inspect` / `restore` 一个 rc5 导出的包 | 两条路径都 RC=2，文案同为「备份 schema 版本 4 高于当前程序支持的 3…」 |
| `recorded` 分支 | rc4 库删掉 `meta.key_sha256` 后再跑自愈 | 返回 `action=recorded`、`tickets_revoked=0`、`idempotency_purged=0` |
| `rotated` 分支 | 指纹在、换掉磁盘密钥 | 返回 `action=rotated`、`tickets_revoked=1`，活票 1→0 |
| `reenable` | 对恢复后的身份执行 | 返回 `id/name/role/token/model`；旧 token 认证报 `执行器令牌无效`，新 token 可用 |
| 自检 | `tools/selfcheck.py` | 通过 16 项、失败 0 项 |
| GUI 冒烟 | `tools/gui_smoke.py` | 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes bigfish tests tools` / `compileall` | 零输出 / RC=0 |
| 全量测试 | `unittest discover -s tests -t .` | `Ran 244 tests` / `OK` |

Windows 侧（Python 3.14.6）本批次没有跑，升级流程在 Windows 上属于**未验证**。上真实机器时先跑 `tools/selfcheck.py` 与 `tools/gui_smoke.py`，再动业务数据。

---

## 10. 升级失败怎么办

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 启动时报「数据库 schema 版本 4 高于本程序支持的 3」 | 代码没换过来（还在跑 rc4）或混用了旧 venv | 确认 `bigfish.__version__` 是 `0.1.1-rc5`，确认没有第二个进程在用旧代码 |
| 启动时报「schema 版本未达到预期：当前 N / 期望 4」 | 迁移中途被强杀，某个版本没提交成功 | 再启动一次（迁移可重复执行）；仍然不行就看 stderr 的具体 SQL 错误 |
| 迁移抛 `no such column` / `no such table` | 库结构被人工改过，或从更早的畸形库升上来 | 用备份回退到升级前，再排查库结构；不要手工补列 |
| `database is locked` | 还有别的进程在写库 | 停掉所有实例再试；`PRAGMA busy_timeout` 是 5000ms |
| 升级后执行端全部报「该身份已被禁用」 | 升级期间跑过恢复（`_block_inflight` 生效了） | 逐个 `reenable`，见第 8 节 |
| 升级后活票突然变 0 | 库原来是 v0.1.0（无密钥文件），走了 `new-key` | 查 `audit` 里 `db.key_healed` 的 detail；这是预期行为 |
| 备份包被拒「备份 schema 版本 4 高于当前程序支持的 3」 | 在 rc4 上恢复 rc5 导出的包 | 这个包只能给 rc5 用；回滚请用手上 schema 3 的那份 |
| Windows 上 `os.replace` 失败 | 有进程占着库文件 | 关掉所有 GUI / headless 实例再试 |

---

## 11. 与 rc5 其它行为变化的交叉引用

升级除了 schema，还带来三处行为变化，迁移时要一并知道（细节见 `docs/FIX-MATRIX-v0.1.1-rc5.md`）：

| 变化 | 影响 | 文档 |
| --- | --- | --- |
| 恢复后禁用全部身份 + 必须 `reenable`（R1） | 升级后第一次恢复会让所有执行端掉线，必须人工重新授权 | 本文第 8 节 |
| 恢复期进入维护模式（R8） | 恢复期间任何写请求返回 503 + `Retry-After: 15`；CLI `restore` 会探端口拒绝 | `docs/ROLLBACK-v0.1.1-rc5.md` 第 1 节 |
| `bfc-proxy.mjs` 强制回环（R7） | VPS 上如果设过 `BFC_PROXY_LISTEN_HOST` 且不是 `127.0.0.1` / `localhost` / `::1`，跳板会在 `listen` 前 `exit(1)` | `docs/SECURITY-REVIEW-v0.1.1-rc5.md` |

另外 R5 新增了执行端收据日志 `bigfish/executor_client.py`（`ReceiptLog`，rc4 里没有这个文件）。执行端如果在用 `ExecutorClient`，升级后需要一并更新到 rc5 版本，否则服务端下发的 `dispatch_id` 用不上（不影响兼容：旧客户端会回退用 `ticket_id`）。
