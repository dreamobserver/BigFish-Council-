# 回滚说明（v0.1.1-rc4 → v0.1.1-rc3）

## 0. 先说结论

| 场景 | 能否直接换代码 | 正确做法 |
| --- | --- | --- |
| rc4 只启动过、库还是 schema 3 | **可以** | 停进程 → 换回 rc3 代码 → 启动 |
| rc4 期间执行过 `restore`（密钥已轮换） | **不能只换代码** | 换回 rc3 后旧票全部失效，所有执行器重新领取；密钥不要回滚 |
| 有人手工把 `secrets/ticket.key` 换回旧版本 | **不行** | 两边都认不出已签发的票，等于全部重新领取 |
| 库被改到 schema 4 | **不能** | rc3 会抛 `DatabaseVersionError`，服务起不来 |

rc3 → rc4 **没有 schema 变化**（`SCHEMA_VERSION` 仍是 3，`bigfish/db.py:21`），
所以库文件可以直接给 rc3 用：实测用 rc4 建库、签发执行票，再切到 rc3 打开同一份库，
`schema_version()` 返回 3、`restore_epoch` 保留、rc4 签发的票在 rc3 里心跳与提交都通过。

回滚的难点集中在**执行票与密钥**，不在数据库结构。

## 1. 回滚前必须做的三件事

| # | 事项 | 命令 | 为什么 |
| --- | --- | --- | --- |
| 1 | 停掉所有进程 | `pgrep -af 'bigfish|bfc-proxy'` 无输出 | 半开连接会让 `os.replace` 撞文件占用 |
| 2 | 整份备份数据目录 | 见下 | 回滚只换代码，但恢复残留文件要有原样副本 |
| 3 | 导出一份 rc4 格式的备份包 | `python -m bigfish.cli --data-dir <dir> export <pkg.zip>` | 将来想再回 rc4 时有可用包 |

备份范围（默认 `~/.bigfish-council`，Windows `%LOCALAPPDATA%\BigFishCouncil`）：

    council.db  council.db-wal  council.db-shm  config.json  secrets/ticket.key  logs/

`secrets/ticket.key` 必须一起备份。缺了它，已签发但尚未用完的票无法复算。

## 2. 标准回滚步骤

### 2.1 数据层

rc4 没有 schema 变化，**数据库不需要动**。直接把 `council.db` 留给 rc3 打开即可。

例外是 rc4 期间跑过 `restore`：库已被换过、`restore_epoch` 已 +1。此时：

| 想做 | 做法 | 结果 |
| --- | --- | --- |
| 回滚到恢复前的数据 | 用 `council.db.prerestore` 覆盖 `council.db`（见 4.2） | 数据回到恢复前，但 `restore_epoch` 也会回到旧值 |
| 只回滚代码、不碰数据 | 保留当前库 | rc3 能打开，旧票仍然全部失效 |

### 2.2 代码层

    cd /home/dsh/bigfish-council
    git switch --detach <rc3 的 commit>
    # 或把 rc3 源码包解压覆盖到项目目录

回滚后用只读命令确认版本：

    .venv/bin/python -c "import bigfish; print(bigfish.__version__)"   # 应为 0.1.1-rc3

rc3 的 `_migrate_locked()` 在 `bigfish/db.py:259`，看到 `current == SCHEMA_VERSION` 会直接返回，
不会对 rc4 建的库做任何 DDL。

### 2.3 验证

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 版本 | `python -c "import bigfish;print(bigfish.__version__)"` | `0.1.1-rc3` |
| 库能打开 | `python -m bigfish.cli --data-dir <dir> overview` | 返回 JSON，不抛 `DatabaseVersionError` |
| schema 未变 | `sqlite3 council.db "SELECT value FROM meta WHERE key='schema_version'"` | `3` |
| 自检端口 | `python tools/selfcheck.py` | rc3 默认端口是 **3099**，注意端口占用 |
| 全量测试 | `python -m unittest discover -s tests -t .` | rc3 是 129 项（rc4 为 162 项） |

## 3. 密钥：已经轮换过就不能回滚

### 3.1 轮换发生在哪里

恢复备份时，`bigfish/backup.py:486-487` 把当前密钥复制成 `ticket.key.prerestore`，
随后用 `secrets.token_bytes(KEY_BYTES)` 生成新密钥写入 `ticket.key.restore-new`，
并在库替换成功后 `os.replace` 成正式密钥。`KEY_BYTES = 32`（`bigfish/security.py:29`）。

票据派生（`bigfish/security.py:196`）：

    material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key or ''}"
    token = "bfc_tk_" + hmac_sha256(key, material)[:43]

### 3.2 为什么不能把密钥换回去

| 动作 | 实测结果 |
| --- | --- |
| 轮换后，用老票心跳 | **通过**（`_check_ticket_row` 按 `token_hash` 查表，不重算票面） |
| 把 `ticket.key.prerestore` 复制回 `ticket.key` | rc4 启动时 `runtime.ensure_key_consistency()`（`bigfish/runtime.py:18`）发现库里 `meta.key_sha256` 与磁盘密钥不一致，**再轮换一次**，并在 `audit` 里写一条 `db.key_healed` |
| 换密钥后走幂等重放 | `_replay_claim` 重新派生出的票面值与首发**不同**，用它心跳报「执行票无效」 |

所以密钥回滚在 rc4 上被自愈覆盖；在 rc3 上没有 `ensure_key_consistency`
（`bigfish/runtime.py` 里没有这个函数），自愈不会发生，但此时库里的 `meta.key_sha256`
仍是 rc4 写的新密钥指纹，回 rc4 又会被纠正。**结论：密钥不要手工回滚。**

### 3.3 回滚到 rc3 之后票的状态

| 票的来源 | 回 rc3 后 |
| --- | --- |
| rc4 恢复**前**签发、已被吊销 | 报「执行票已被撤销」，两边都不认 |
| rc4 恢复**后**签发（`epoch=1`） | rc3 接受（实测心跳、提交通过） |
| rc4 未做过恢复期间签发 | rc3 接受 |
| 手工换过密钥后 | 报「执行票无效」，只能重新 `claim` |

一句话：**回滚代码不会让票复活，只会让一部分新票继续可用。** 需要确定性的做法是
回滚后让所有执行器重新领取。

## 4. 数据恢复的回滚

### 4.1 文件名常量（核对自 `bigfish/backup.py`）

| 文件 | 生成位置 | 含义 |
| --- | --- | --- |
| `council.db` | `bigfish/backup.py:45` `DB_NAME` | 正式运行库 |
| `council.db.prerestore` | `bigfish/backup.py:483` `db.snapshot(original_dir / (target.name + ".prerestore"))` | 恢复前的原库快照 |
| `council.db.rollback` | `bigfish/backup.py:551` | 回滚时用的中转文件 |
| `council.db.restore-new` | `bigfish/backup.py:429` | 解包出来的待替换库 |
| `council.db.restore.lock` | `bigfish/backup.py:439` `_restore_lock` | 恢复互斥锁 |
| `ticket.key.prerestore` | `bigfish/backup.py:486` `Path(str(key_path) + ".prerestore")` | 恢复前的密钥副本 |
| `ticket.key.restore-new` | `bigfish/backup.py:475` | 恢复时生成的新密钥 |
| `manifest.json` | `bigfish/backup.py:44` `MANIFEST_NAME` | 备份包内的清单 |

### 4.2 用 prerestore 手工回退一次恢复

**先停进程，再复制**（`os.replace` 在进程持有句柄时可能失败）：

    DATA=~/.bigfish-council

    # 1) 留存现场
    cp -a "$DATA" "$DATA.before-undo-$(date +%Y%m%d-%H%M%S)"

    # 2) 库回退：把 prerestore 变成正式库
    rm -f "$DATA/council.db-wal" "$DATA/council.db-shm"
    cp -a "$DATA/council.db.prerestore" "$DATA/council.db"

    # 3) 密钥**不要**一起回退（见第 3 节）
    #    若确实要回退，需同时清理 meta.key_sha256，否则 rc4 启动会再轮换一次

`council.db.prerestore` 是 `db.snapshot()` 产出的单文件一致性快照，可以当普通 SQLite 库直接打开校验：

    .venv/bin/python -c "from pathlib import Path;from bigfish.db import Database
    d=Database(Path.home()/'.bigfish-council'/'council.db.prerestore');print(d.schema_version(), d.integrity(), d.table_counts())"

### 4.3 不用 prerestore 时的回退路径

| 手里有什么 | 做法 |
| --- | --- |
| 恢复前的整目录备份 | 停进程 → 用备份目录覆盖数据目录 → 启动 |
| 只有备份包（`*.zip`） | `python -m bigfish.cli --data-dir <dir> inspect <pkg.zip>` 校验后 `restore`，注意这会**再轮换一次密钥** |
| 只有 `council.db.prerestore` | 按 4.2 手工复制 |

审计包（`KIND_AUDIT`）不能用于恢复：`restore_bundle` 在 `bigfish/backup.py:423-424`
直接抛 `RestoreError("这是脱敏审计包，不能用于恢复运行数据库")`。

## 5. 备份 / 恢复的实测命令（临时目录，不碰真实数据）

以下命令都在 `/tmp` 下的临时目录执行，用 rc4 代码跑：

    export LD_LIBRARY_PATH=/home/dsh/.local/libbfc
    export QT_QPA_PLATFORM=offscreen
    cd /home/dsh/bigfish-council

    # 导出
    .venv/bin/python -m bigfish.cli --data-dir /tmp/bfc-demo export /tmp/bfc-demo-backup.zip

    # 只读校验
    .venv/bin/python -m bigfish.cli --data-dir /tmp/bfc-demo inspect /tmp/bfc-demo-backup.zip

    # 恢复（会留 council.db.prerestore 与 secrets/ticket.key.prerestore）
    .venv/bin/python -m bigfish.cli --data-dir /tmp/bfc-demo restore /tmp/bfc-demo-backup.zip

实测结果（`/tmp/tmpemvkfx67`）：

| 项 | 值 |
| --- | --- |
| 导出包大小 | 3766 bytes |
| `inspect` | `schema_version=3`、`backup_kind=operational`、`tasks=1` |
| `restore` 返回 | `{"restored": true, "restore_epoch": 1, "key_rotated": true, "blocked_inflight": 1, "idempotency_purged": 0}` |
| 目录内新增 | `council.db.prerestore` |
| `secrets/` 内新增 | `ticket.key.prerestore`（原密钥副本） |
| 密钥是否真的变了 | 是 |
| 恢复前那张票 | 心跳报「执行票已被撤销」，任务变 `BLOCKED` |

恢复会把 `DISPATCHED` / `RUNNING` / `CANCELLING` 全部转成 `BLOCKED`，原因是
`blocked_reason` 写入「数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批」。

## 6. 回滚也失败怎么办

`restore_bundle` 的失败分支在 `bigfish/backup.py:538-579`，实际行为如下：

| 阶段 | 代码动作 |
| --- | --- |
| 进入 except | 建 `rollback_errors: list[str]` |
| 库已替换且有 prerestore | `copyfile(prerestore → council.db.rollback)`，删 `-wal`/`-shm`，`os.replace(rollback → council.db)` |
| 密钥已安装且有副本 | `os.replace(ticket.key.prerestore → ticket.key)` |
| 重开连接 | `db._reopen()`（异常被吞掉） |
| 清理 | 删除 `ticket.key.restore-new` |
| 判定 | `rollback_errors` 非空 → `raise RestoreError("恢复失败且回滚未完全成功：" + "；".join(rollback_errors) + "（原始错误：…）")`；为空则原样 `raise` |

**注意**：源码注释写的是「把数据库置为只读保护」，但代码里没有额外的只读开关动作。
实际效果是「抛 `RestoreError` + 尝试重开连接」，数据库仍是可写状态。
不要把注释当成已实现的保护。

回滚失败后的手工处置顺序：

1. 立刻停掉所有进程，不再启动任何入口；
2. 整份复制数据目录（含 `-wal`/`-shm`、`secrets/`、`*.prerestore`、`*.rollback`）；
3. 用只读方式核对三个文件是否互相一致：

       sqlite3 council.db "PRAGMA integrity_check"
       sqlite3 council.db "SELECT value FROM meta WHERE key='schema_version'"
       ls -l secrets/ticket.key secrets/ticket.key.prerestore council.db.prerestore

4. 优先用 `council.db.prerestore` 覆盖 `council.db`（4.2），**不要**同时回退密钥；
5. 起服务后用 `python -m bigfish.cli overview` 确认能读，再跑全量测试。

## 7. 混跑危害（rc3 / rc4 交替读写同一库）

| 行为 | 后果 |
| --- | --- |
| rc3 写 `reviews` | rc4 的 `vote_summary` 按「最新一票含已撤销」统计，旧票可能被撤票挡住审批 |
| rc3 上 BLOCKED 点「通过」 | 任务变 DONE；换回 rc4 后这条历史仍算数，但新规不允许再这么干 |
| rc4 领票后 rc3 再领同一任务 | rc3 会算出与 rc4 不同（或相同）的票，双方互相吊销，最终只有最后一张有效 |
| 任一侧 `restore` | 密钥轮换 + `restore_epoch` +1，另一侧已签发的票全部失效 |
| 任一侧手工改密钥 | rc4 启动自愈会再轮换，rc3 不会；两边对票的判定不一致 |

回滚只允许出现在「换代码」这一个中间态，不要长期让两个版本交替写同一个库。

## 8. 一键检查清单

| # | 检查 | 命令 | 通过判据 |
| --- | --- | --- | --- |
| 1 | 版本对吗 | `python -c "import bigfish;print(bigfish.__version__)"` | 回滚后为 `0.1.1-rc3` |
| 2 | schema 对吗 | `sqlite3 council.db "SELECT value FROM meta WHERE key='schema_version'"` | `3` |
| 3 | 完整性 | `python -c "from pathlib import Path;from bigfish.db import Database;d=Database(Path('council.db'));print(d.integrity())"` | `(True, 'ok')` |
| 4 | 恢复残留 | `ls -la <data> \| grep -E 'prerestore\|rollback\|restore-new\|restore.lock'` | 有残留说明上次恢复/回滚没走完 |
| 5 | `restore_epoch` | `sqlite3 council.db "SELECT value FROM meta WHERE key='restore_epoch'"` | 与恢复次数一致，回滚不改变它 |
| 6 | 密钥指纹 | `sqlite3 council.db "SELECT value FROM meta WHERE key='key_sha256'"` | 非空即有指纹；空则 rc4 启动会走 `no-fingerprint` 分支 |
| 7 | 自检 | `python tools/selfcheck.py` | rc3 默认 3099，rc4 默认 3094 |
| 8 | 全量测试 | `python -m unittest discover -s tests -t .` | rc3 129 项全过 |

## 9. 回滚后仍然存在的问题（诚实说明）

回滚到 rc3 会把 rc4 修掉的缺陷一起带回来：

| 缺陷 | 回滚后的表现 |
| --- | --- |
| 无幂等键领取派生固定票 | 同一任务多轮领取会得到同一张票，旧票持有者能继续用 |
| `verify(accept=True)` 接受 BLOCKED | 一次点击就能把「副作用不明」洗成 DONE |
| 撤票后回落到更早的票 | 双通过可能被历史票凑出来 |
| `update_prompt` 不清取消残留 | 取消过的任务改完 Prompt 仍然领不到 |
| `_idem_get_locked` 全量 `split(":")` | 含冒号的幂等键会被截断，重放派生出另一张票 |
| `Database.__init__` 先改 WAL 再判断版本 | 打开未来版本的库会先动人家文件 |
| 无 `ensure_key_consistency` | 恢复时「换库」和「换密钥」之间被杀，密钥与库指纹会长期不一致 |

回滚是止损手段。要真正消除这些行为，还是得回到 rc4。
