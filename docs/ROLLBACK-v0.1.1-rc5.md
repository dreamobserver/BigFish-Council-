# 回滚说明：v0.1.1-rc5 → v0.1.1-rc4

| 项 | 值 |
| --- | --- |
| 当前 | `0.1.1-rc5`（`b8f6de1`，`SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`） |
| 目标 | `0.1.1-rc4`（`72e8535`，`SCHEMA_VERSION = 3`、`BACKUP_FORMAT_VERSION = 3`） |
| 能不能只换代码 | 不能。schema 4 的库 rc4 打不开 |
| 能不能只换库 | 不够。换库会把升级前的旧票重新变活，必须同时吊销 |
| 正确形态 | 代码与库成对回退，回退后立刻清算凭据 |
| 已验证平台 | Debian 12 / Python 3.11.2。Windows 11 本批次未跑 |

本文只描述「从 rc5 退回 rc4」这一个方向。升级见 `docs/MIGRATION-v0.1.1-rc5.md`。

---

## 0. 先说结论

| 场景 | 能否直接换代码 | 正确做法 |
| --- | --- | --- |
| rc5 只启动过、库被升到 schema 4 | **不能** | 停进程 → 换回 rc4 代码 → **同时**把库换成 schema 3 的副本 |
| 升级前整份备份过数据目录 | 可以，走路线 B | 整目录回退，再清算票与身份 |
| 升级前用 rc4 导出过 operational 包 | 可以，走路线 A | 空目录 + rc4 `restore` 那个包 |
| 手上只有 rc5 导出的包（schema 4） | **不能** | rc4 会在 `_verify_version` 拒绝，见 2.1 |
| rc5 期间跑过 `restore`（密钥已轮换） | **不能完整回滚** | 见第 5 节「回滚的边界」 |
| rc5 期间产生过新数据且没有备份 | **不能无损回滚** | 见第 5 节 |

核心原因只有一条：**schema 4 的库不能被 rc4 打开**。

用 rc4 的代码打开一个 schema 4 的库，实测：

```
DatabaseVersionError: 数据库 schema 版本 4 高于本程序支持的 3，拒绝打开（升级程序，不要降级数据库）
```

CLI 退出码 2，库文件 sha256 前后完全一致（实测 `36778b19...` 未变），目录里不会多出 `-wal` / `-shm`，`journal_mode` 也没被改写。**rc5 没有提供降级迁移**（`MIGRATIONS` 里只有正向语句），所以「把 schema 4 改回 3」这件事只能靠换库。

---

## 1. 回滚前必须做的三件事

| # | 事项 | 命令 | 为什么 |
| --- | --- | --- | --- |
| 1 | **停掉所有写库的进程** | GUI 全关；Windows 上 `powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1`；Linux 上 `pkill -f "python -m bigfish"`；再确认 `ss -lntp` 里 3096 没有监听 | 半开连接会让 `os.replace` 撞文件占用（Windows 直接失败），POSIX 上另一个实例会继续写旧 inode，造成数据静默分叉 |
| 2 | **整份备份当前数据目录** | `cp -a ~/.bigfish-council ~/.bigfish-council.before-rollback` | 回滚本身也可能失败，要能回到「rc5 + schema 4」的状态 |
| 3 | **导出一份 rc5 格式的包** | `.venv/bin/python -m bigfish.cli --data-dir <dir> export ~/bfc-rc5-before-rollback.zip` | 这份包 schema_version=4，只能给 rc5 用；留着是为了「万一将来还想回 rc5」 |

备份范围（默认 `~/.bigfish-council`，Windows 上 `%LOCALAPPDATA%\BigFishCouncil`）：

```
council.db   council.db-wal   council.db-shm   config.json   secrets/ticket.key   logs/
```

`secrets/ticket.key` 必须一起备份：rc4 认的是这把钥匙，只回退库不回退密钥会让所有已签发的票验不过。

### 1.1 CLI `restore` 现在会拒绝在服务运行时替换库

rc5 的 `restore` 子命令（`bigfish/cli.py:211-222`）在替换前先探 `api_host:api_port`（`_port_busy`，`bigfish/cli.py:41`，回环 `create_connection`，超时 0.4s）。有服务在监听就打：

```
拒绝恢复：127.0.0.1:3096 上还有服务在监听，请先停掉它（tools/stop-headless.ps1 或关闭 GUI），
确认没有其他实例后再重试；确实要强行恢复请加 --force。
```

退出码 2，一个字节都不动。

`--force`（`bigfish/cli.py:126`，help 原文「本机回环端口上还有服务在跑时，仍然强行恢复（危险）」）是人工逃生门，风险是实打实的：

- 另一个进程仍持有旧连接，POSIX 上会继续往被 `os.replace` 掉的旧 inode 写，数据静默分叉；
- Windows 上替换会因文件占用直接失败，可能停在「库换了、密钥没换」的中间态；
- 恢复期间的维护模式（`db.enter_maintenance`）**只管得住本进程**的连接，管不住另一个实例。

**只有当你能确认那个监听是本地另一个不写这个库的东西时，才用 `--force`。** 拿不准就停服务，别赌。

---

## 2. 路线 A：用备份包在 rc4 上重建库

### 2.1 前置条件（整条路线的硬约束）

**必须有一份 schema 3 的 operational 包。**

原因在 `_verify_version`（`bigfish/backup.py:230`）：它拦的是「包里的库版本 > 当前程序支持的版本」，文案为：

```
备份 schema 版本 4 高于当前程序支持的 3，请先升级程序，禁止把高版本库恢复进低版本程序
```

而 rc5 导出的包 manifest 里 `schema_version = 4`、`backup_format = 4`（实测 `inspect` 输出如此）。所以：

| 手上的包 | 在 rc4 上 `inspect` | 在 rc4 上 `restore` |
| --- | --- | --- |
| rc4 时代导出的（`backup_format=3`、`schema_version=3`） | RC=0 | RC=0，恢复成功 |
| rc5 导出的（`backup_format=4`、`schema_version=4`） | **RC=2** | **RC=2** |

两条路径都实测过，拒绝发生在替换文件之前，原库不受影响。

一个小副作用要知道：rc4 的 `build()` 会先打开（必要时创建）目标目录的库，所以对一个空目录执行 `restore` 而被版本闸门拒绝时，目录里会多出一个 126976 字节的 schema 3 空库。它无害，但不要误以为「恢复了一半」。

### 2.2 步骤

```bash
# 1) 停服务（见第 1 节），确认端口无监听

# 2) 把 schema 4 的库移出数据目录（不要删，先隔离）
mkdir -p ~/bfc-schema4-frozen
mv ~/.bigfish-council/council.db     ~/bfc-schema4-frozen/
mv ~/.bigfish-council/council.db-wal ~/bfc-schema4-frozen/ 2>/dev/null || true
mv ~/.bigfish-council/council.db-shm ~/bfc-schema4-frozen/ 2>/dev/null || true

# 3) 换回 rc4 代码（整棵树切换，不要局部 checkout）
cd /home/dsh/bigfish-council
git stash -u
git checkout 72e8535

# 4) 用一个 schema 3 的包恢复（此时目录里没有库，rc4 会先建一个空的）
.venv/bin/python -m bigfish.cli --data-dir ~/.bigfish-council restore ~/bfc-rc4-before-upgrade.zip
```

`restore` 返回（rc4 口径，实测）：

```
{
  "restored": true,
  "restore_epoch": 1,
  "key_rotated": true,
  "blocked_inflight": 1,
  "idempotency_purged": 0,
  "table_counts": { ... 12 张表的行数 ... },
  "manifest": { "backup_format": 3, "schema_version": 3, "app_version": "0.1.1-rc4" },
  "prerestore_path": ".../council.db.prerestore",
  "key_backup_path": ".../ticket.key.prerestore"
}
```

注意 rc4 的返回值里**没有** `executors_disabled` 与 `frozen_approved`（那是 rc5 才加的），这也意味着它没做这两件事。

### 2.3 恢复之后必须做的清算（rc4 不会替你做）

rc4 的 `_block_inflight`（`bigfish/backup.py:292`）只做三件事：

1. 在途任务（DISPATCHED / RUNNING / CANCELLING）→ `BLOCKED`；
2. 吊销全部活票（置 `revoked_at`）；
3. 清 `claim/heartbeat/submit` 幂等缓存。

它**不禁用 executors，也不冻结 APPROVED**（那是 rc5 的 R1 才补上的）。实测 rc4 恢复后：

| 检查项 | 实测结果 |
| --- | --- |
| `restore_epoch` | 1 |
| 活票 | 0 |
| `executors.enabled` | **仍为 1** |
| 同一个旧 token 还能认证吗 | **能**（`authenticate` 返回原来的 executor id） |

所以回退到 rc4 之后，**必须手工清算身份**。rc4 没有 `reenable`，也没有任何 `revoke` 的 CLI 入口（`revoke_executor` 在 rc4 的 `core.py:224` 有定义，但全仓没有任何调用方），只能跑这段：

```python
import time
from pathlib import Path
from bigfish.config import Settings
from bigfish.runtime import open_council

s = Settings.load(Path.home() / ".bigfish-council")
db, c = open_council(s)
with db.tx() as conn:
    t = time.time()
    n_t = conn.execute(
        "UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL", (t,)).rowcount
    n_e = conn.execute(
        "UPDATE executors SET enabled=0 WHERE enabled=1").rowcount
print("吊销活票:", n_t, "禁用身份:", n_e)
db.close()
```

这段在 rc4 上实测有效（`/tmp/rb-a3` 与另一份副本上各跑过一次）。注意 rc4 **没有轮换 token 的能力**：`enabled=0` 只挡住登录，旧 token 的哈希还在库里。这正是路线 A 的残余风险——想彻底换钥，只能删掉那个身份重建（`executor-add` 会生成新 id 和新 token）。

### 2.4 路线 A 的优点与代价

| 维度 | 说明 |
| --- | --- |
| 优点 | 得到的是一个干净的 schema 3 库；`restore_epoch` 由 rc4 提升到 1，旧票被吊销、在途任务被 BLOCKED；不需要手工剥列 |
| 代价 | 只能回到包里那个时间点，包里之后产生的数据全部丢失；必须有 schema 3 的包；身份要手工清算 |
| 容易踩的坑 | 拿 rc5 的包去恢复（RC=2）；在服务还开着时恢复（被拒，或 `--force` 造成分叉） |

---

## 3. 路线 B：整目录回退到升级前的副本

### 3.1 步骤

```bash
# 1) 停服务，确认端口无监听

# 2) 把当前数据目录整个挪走（不要删）
mv ~/.bigfish-council ~/.bigfish-council.rc5-schema4

# 3) 把升级前的整目录副本放回去
cp -a ~/.bigfish-council.bak ~/.bigfish-council

# 4) 换回 rc4 代码
cd /home/dsh/bigfish-council
git stash -u
git checkout 72e8535

# 5) 确认代码与库版本
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"
.venv/bin/python -m bigfish.cli --data-dir ~/.bigfish-council overview
```

`overview` 能返回 JSON（不抛 `DatabaseVersionError`）就说明库是 schema 3、代码是 rc4，对上了。

### 3.2 这条路线的陷阱：旧票会变活

整目录回退会把 `meta.restore_epoch` 一起退回升级前的值（通常是 0），而升级前签发的票里记的正是那个 epoch。`_check_ticket_row`（rc4 的 `bigfish/core.py:830`）只比对「票里的 epoch」与「库里的 epoch」，两边一起退回就匹配成功，**旧票重新可用**。

实测（rc4 建库并发活票 → 整目录副本 → rc5 升级到 schema 4 → 把副本换回来 → rc4 打开）：

| 检查项 | 回退后 |
| --- | --- |
| `schema_version` | 3 |
| `restore_epoch` | 0（退回旧值） |
| 活票 | 1 |
| `executors.enabled` | 1 |
| 旧票心跳 | **`ok=True`、`task_status=RUNNING`** |

也就是说：只换库不换钥、不清票，等于把「升级前那一刻的所有授权」原样复活，包括那些可能已经外流的 token。

**所以路线 B 做完必须立刻执行 2.3 节那段清算脚本**（吊销全部活票 + 禁用全部身份）。两件事都做，顺序不重要，但不能省。

### 3.3 路线 B 的优点与代价

| 维度 | 说明 |
| --- | --- |
| 优点 | 不依赖备份包；能精确回到升级前那一刻（包含 `config.json`、`logs/`、`secrets/ticket.key`）；不需要剥列 |
| 代价 | 升级后产生的所有数据全部丢失；`restore_epoch` 退回旧值，旧票会复活，必须手工清算；升级前没备份目录的话这条路不存在 |

### 3.4 关于 `council.db.prerestore`

`council.db.prerestore` 是 `restore_bundle` 在替换前对**当前库**做的快照（`bigfish/backup.py:556`）。

| 谁产生的 | 快照里的库是什么版本 |
| --- | --- |
| rc4 的 `restore` | schema 3（实测） |
| rc5 的 `restore` | **schema 4**（实测） |

所以在 rc5 → rc4 这个场景里，**rc5 自己产生的 `prerestore` 不能用来回滚**——rc4 打开它同样抛 `DatabaseVersionError`。只有当那份 `prerestore` 是 rc4 时代留下的（schema 3）时，它才能当路线 B 的替代品。

---

## 4. 回滚后必做的三项确认

| # | 确认什么 | 怎么查 | 期望 |
| --- | --- | --- | --- |
| 1 | `restore_epoch` 已经加过 1 | 读 `meta` 里 `restore_epoch` | 路线 A：≥ 1；路线 B：退回升级前的值（通常 0），此时靠第 2、3 项兜底 |
| 2 | 所有执行票已被吊销 | `select count(*) from tickets where revoked_at is null` | `0` |
| 3 | 所有身份已被禁用 | `select id, enabled from executors` | 每行 `enabled=0`，然后按业务需要重建或重新授权 |

关于第 1 项：`restore_epoch` 是「这次恢复拒绝旧票」的依据。rc5 的 `_prepare_staging` 用 `max(captured_epoch, staged_epoch) + 1`，rc4 也一样——实测把 `meta.restore_epoch` 人为置 7 再 `restore`，返回的就是 8，它会跨越手工设的高值，不会退回 1。

路线 B 的 `restore_epoch` 退回旧值属于预期，代价就是第 2、3 项必须人工做完——这条路上 `restore_epoch` 不提供任何保护。

### 4.1 验证清单

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 版本 | `.venv/bin/python -c "import bigfish; print(bigfish.__version__)"` | `0.1.1-rc4` |
| 库能打开 | `.venv/bin/python -m bigfish.cli --data-dir <dir> overview` | 返回 JSON，不抛 `DatabaseVersionError` |
| schema | 读 `meta.schema_version` | `3` |
| 活票 | `select count(*) from tickets where revoked_at is null` | `0` |
| 身份 | `select count(*) from executors where enabled=1` | `0`（除非刚重建了身份） |
| 完整性 | `PRAGMA integrity_check` | `ok` |
| 自检 | `.venv/bin/python tools/selfcheck.py` | 通过 16 项、失败 0 项 |
| 全量测试 | `.venv/bin/python -m unittest discover -s tests -t .` | rc4 是 `Ran 162 tests` + `OK` |

跑测试前先给 Qt 环境：

```bash
export LD_LIBRARY_PATH=/home/dsh/.local/libbfc
export QT_QPA_PLATFORM=offscreen
```

### 4.2 收尾

```bash
# 确认没问题后，才删掉隔离目录（先看清里面确实是要丢的 rc5 数据）
ls -la ~/.bigfish-council.rc5-schema4 ~/bfc-schema4-frozen

# 需要回到 rc5 分支时
git checkout fix/v0.1.1-rc4-hardening
git stash pop
```

---

## 5. 回滚的边界

有些东西回滚不了，或者回滚了也修不回来。动手前先对照这一节。

### 5.1 已经用 rc5 产生的新数据

- rc5 期间创建的任务、版本、审查、审批、收据，在路线 A 的包里 / 路线 B 的副本里都不存在，回滚就是**丢弃**。
- rc5 新增的 audit 记录（`db.restored`、`db.key_healed`、`executor.reenabled`）也会一起消失。
- 想留证据就先导审计包：`.venv/bin/python -m bigfish.cli --data-dir <dir> audit-export <audit.zip>`。这份包是脱敏的列白名单格式（R6），给 rc4 打开没有意义，但作为归档是有效的。

### 5.2 已经轮换过的密钥

rc5 在两种情况下会换掉 `secrets/ticket.key`：

| 触发条件 | 后果 |
| --- | --- |
| 执行过 `restore`（返回里 `key_rotated=true`） | 旧钥被新钥替换，用旧钥签的票全部不可复算 |
| `ensure_key_consistency` 走 `rotated` / `new-key` 分支 | 同上，并额外吊销全部活票、清幂等 |

这两件事**不可逆**：代码回滚不会把旧钥变回来。

能做的只有：回滚后把全部身份重建一遍，重新分发新 token。

### 5.3 已经重新派过的工

rc5 的 `claim` 返回 `dispatch_id = ticket_id`，重新派工会换新票，旧收据不会被覆盖（R5）。一旦在 rc5 上重新派工并用 `ReceiptLog` 记了收据：

- 回滚到 rc4 后，那批 `dispatch_id` 与收据文件都没有对应的服务端记录（rc4 没有 `executor_client.py`，也没有 `dispatch_id` 概念）；
- 靠收据判断「某次派工到底有没有开工」的能力随之消失，只能人工查日志。

### 5.4 已经被 rc5 冻结 / 阻断的任务

rc5 的 `_block_inflight` 把 APPROVED 冻结成 BLOCKED、把在途任务打成 BLOCKED（R1）。回滚后这些状态会各自按库里的样子回来，不要假设「状态一定是安全的」——逐条看 `BLOCKED` 任务的 `blocked_reason`。

### 5.5 混跑的危害

| 混跑方式 | 后果 |
| --- | --- |
| rc4 代码 + schema 4 库 | 起不来（`DatabaseVersionError`），无害但也没用 |
| rc5 代码 + schema 3 库 | 一启动就自动升到 4（迁移是自动的），等于又回到 rc5 |
| 两个版本同时开着同一个库 | POSIX 上数据分叉、Windows 上替换失败；两边对 `executors.disabled_reason` 的理解也不一致 |
| rc4 的包恢复进 rc5 | 允许（实测通过），会先有序迁移到 4，再吊销旧票、禁用身份 |
| rc5 的包恢复进 rc4 | 拒绝（`_verify_version`），RC=2 |

### 5.6 一条不受支持的路（写在这里只为说明它存在）

技术上可以在 SQLite 3.35+ 上手工剥列：`DROP INDEX idx_executors_enabled` + `ALTER TABLE executors DROP COLUMN disabled_reason` + 把 `meta.schema_version` 改回 3。在 3.40.1 上实测能成功，`integrity_check` 也是 `ok`，rc4 随后能正常打开。

**这条路不受支持，不要用于生产。** 原因：

- 它会丢掉 `disabled_reason`，也就是「哪些身份被禁、为什么被禁」的审计线索；
- 它绕过了版本闸门，等于用改数字冒充降级；一旦 rc5 再打开这个库，`MIGRATIONS[4]` 会因为列已存在而按「视为成功」放过，索引状态就取决于你手工做到哪一步；
- 只改 `meta.schema_version` 而保留列，实测 rc4 也能打开（schema 读回 3），但库里留着 rc4 不认识的列，属于半升级态；
- 中途出错的话，你既回不到 schema 4，也回不到干净的 schema 3。

正确做法永远是第 2、3 节的两条路线。

---

## 6. 回滚也失败怎么办

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| rc4 启动报「数据库 schema 版本 4 高于本程序支持的 3」 | 库没换，只换了代码 | 按第 2 或 3 节把库一起换掉 |
| rc4 `restore` 报「备份 schema 版本 4 高于当前程序支持的 3」 | 用了 rc5 导出的包 | 换成 rc4 时代的包；没有就只能走路线 B |
| rc4 `restore` 报「拒绝恢复：…上还有服务在监听」 | 还有实例在跑 | 停服务；确知那是无关进程时可加 `--force`（风险见 1.1） |
| rc4 `restore` 报「数据库原子替换失败」 | 文件被占用（Windows 常见） | 关掉所有实例、确认没有计划任务在拉服务，再重试 |
| rc4 起来后旧 token 还能登录 | 只做了 `restore`，没做 2.3 的清算 | 立即执行那段 SQL；`restore_epoch` 只拦旧票，不拦身份 |
| 回滚后发现票还能用 | 路线 B 把 `restore_epoch` 一起退回了 | 手工吊销全部活票（2.3 节），别只靠 epoch |
| 两条路线都失败 | 数据目录状态不明 | 用第 1 节第 2 步的 `before-rollback` 副本恢复回「rc5 + schema 4」，留在 rc5 上处理，别在数据上继续试 |

---

## 7. 一键检查清单

回滚前：

- 所有 GUI / headless / 计划任务已停，3096 无监听
- 当前数据目录已整份复制（含 `secrets/ticket.key`）
- 已用 rc5 `export` 导出一份包（仅留档）
- 手上有一份 schema 3 的 operational 包（路线 A）或升级前目录副本（路线 B）
- 已用 rc5 `audit-export` 归档审计数据

回滚中：

- schema 4 的库已移出数据目录（隔离，不删）
- 代码已整树切到 `72e8535`（不是局部 checkout）
- 库已通过 `restore`（路线 A）或整目录复制（路线 B）就位

回滚后：

- `bigfish.__version__ == "0.1.1-rc4"`
- `meta.schema_version == 3`
- `overview` 能返回 JSON
- `restore_epoch` 已确认（路线 A ≥1；路线 B 记下实际值）
- `select count(*) from tickets where revoked_at is null` 为 0
- `select count(*) from executors where enabled=1` 为 0
- `PRAGMA integrity_check` 为 `ok`
- `tools/selfcheck.py` 16/16
- `unittest discover` 回到 `Ran 162 tests` + `OK`
- 需要继续跑业务的执行端已重建身份并拿到新 token

---

## 8. 仍存在的问题

1. **rc4 没有 `reenable`。** 回滚后重新授权只能重建身份（新 id、新 token），历史审计里的 id 与新建的 id 对不上。这是 rc5 才补上的能力，回滚回去就得接受。
2. **rc4 的 `_block_inflight` 不禁用身份、不冻结 APPROVED。** 回滚后这些清算全靠人，脚本在 2.3 节，但它不覆盖 APPROVED 冻结——需要人工看任务状态。
3. **rc4 恢复也会把 epoch 加一，但不会通知你旧票已死。** 执行端只会收到「执行票已失效（数据库已从备份恢复），请重新领取」，看起来像偶发故障。回滚时最好提前告知执行端。
4. **Windows 侧本批次未跑。** 所有实测都在 Debian 12 / Python 3.11.2 上完成。Windows 的差异点在 `os.replace` 的文件占用行为与 `icacls` 密钥权限，回滚前请先在测试目录里走一遍路线 B。
5. **`prerestore` 在 rc5 → rc4 场景里不可用。** rc5 产生的 `prerestore` 是 schema 4，rc4 打不开。别把它当回滚素材。
