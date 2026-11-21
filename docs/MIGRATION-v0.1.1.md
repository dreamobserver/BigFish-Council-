# 迁移说明（v0.1.0 → v0.1.1-rc3）

> **rc3 变更（2026-11-19）**：schema 从 **2** 升到 **3**。仍在 v0.1.0 的设备会
> 一次性走 1 → 2 → 3。第 9 节单独说明 rc3 新增的三列与两个索引。
> 下面第 1–8 节是 rc1 写的（schema 1 → 2），内容仍然有效，未改写。

## 1. 一句话

数据库 schema 从 **1** 升到 **2**，升级是**纯增量**（只 ADD COLUMN + 新建 audit 表），
不重建任何表、不删任何数据、不要求停机导出。旧库直接打开即可自动升级。

## 2. schema 变更清单

bigfish/db.py：SCHEMA_VERSION = 2、BACKUP_FORMAT_VERSION = 2、
REQUIRED_TABLES 从 11 张变 12 张（新增 audit）。

    MIGRATIONS: dict[int, list[str]] = {
        2: [
            "ALTER TABLE reviews ADD COLUMN actor TEXT NOT NULL DEFAULT 'human_owner'",
            "ALTER TABLE reviews ADD COLUMN revoked_at REAL",
            "ALTER TABLE approvals ADD COLUMN actor TEXT NOT NULL DEFAULT 'human_owner'",
            "ALTER TABLE approvals ADD COLUMN evidence TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tickets ADD COLUMN max_expires_at REAL",
            "ALTER TABLE tickets ADD COLUMN renewed_at REAL",
            "ALTER TABLE idempotency ADD COLUMN request_hash TEXT",
            "ALTER TABLE idempotency ADD COLUMN expires_at REAL",
            "CREATE TABLE IF NOT EXISTS audit (...)",
        ],
    }

_migrate_locked() 的关键行为：

- 逐版本执行，成功后把 meta.schema_version 写成新值；
- 对 duplicate column name 容忍（手工补过列的老库不会卡住）；
- 迁移全程在 self._lock 内，不与其他写入交错。

## 3. 各字段的用途（为什么必须加）

| 表 | 新列 | 用途 | 对应缺陷 |
| --- | --- | --- | --- |
| reviews | actor | 记录这条审查是谁提交的（human_owner / 执行器 / 模型角色） | P2-1 审查身份服务端绑定 |
| reviews | revoked_at | 改 Prompt 后旧版本的票软删除，不再参与计票 | P0-2 版本漂移 |
| approvals | actor | 审批人必须落库，且只允许 human_owner | P2-2 权限分离 |
| approvals | evidence | 记 votes:v<N>，证明这次批准基于哪一版双模型票 | P0-2 版本漂移 |
| tickets | max_expires_at | 租约绝对上限（默认 6h），续租不能突破 | P1-2 执行票续期 |
| tickets | renewed_at | 最后一次续租时间，便于排查 | P1-2 执行票续期 |
| idempotency | request_hash | 同一幂等键换了请求体必须报错，不能静默重放 | P1-3 心跳幂等 |
| idempotency | expires_at | 幂等记录 24h 过期，避免表无限增长 | P1-3 心跳幂等 |
## 4. 实测：用 v0.1.0 建的库升级到 v0.1.1-rc1

脚本 /tmp/mig_check.py：先用 /home/dsh/bfc-baseline-v0.1.0 的代码建库并写入
一条完整任务（送审 → 双 approve → 批准 → 领取），再切换 sys.path 到 v0.1.1-rc1 用
open_council() 打开同一个库：

    v1 建库完成: 102400 bytes
    schema_version = 1
    tickets 列 = ['id','task_id','version','executor_id','token_hash','issued_at','expires_at','revoked_at']

    升级后 schema_version = 2
    tickets 列 = [... 'revoked_at','max_expires_at','renewed_at']
    audit 表 = True
    reviews 列 = [... 'created_at','actor','revoked_at']
    idempotency 列 = ['key','scope','response','created_at','request_hash','expires_at']
    任务仍在 = DISPATCHED v 1
    密钥文件 = .../secrets/ticket.key True 600
    完整性 = (True, 'ok')
    表计数 = {'meta':1,'tasks':1,'task_versions':1,'reviews':2,'approvals':1,
              'executors':1,'tickets':1,'runs':0,'artifacts':0,'events':9,
              'idempotency':0,'audit':0}

结论：**升级成功，数据零丢失，完整性检查通过，密钥文件按 0600 新建。**

## 5. 升级行为上需要知道的三个变化

### 5.1 旧执行票继续有效，但会被新规则约束

旧票是随机 secrets.token_urlsafe 生成的，新代码不再生成这种票，但 _ticket_lookup
仍然按 token_hash 查表，所以**在途的旧票不会立刻失效**。
差别是：旧票的 max_expires_at 是 NULL，续租时按
min(now+lease, now+ticket_max_ttl) 兜底，不会无限续租。

### 5.2 新签发的票是确定性派生票

bfc_tk_ + HMAC-SHA256(密钥, 「执行器|任务|版本|幂等键」)[:43]。
同一「执行器, 任务, 版本, 幂等键」重放会得到**同一张票**，因此重试天然安全。
密钥在 <data_dir>/secrets/ticket.key（0600），**不写进数据库、不进导出包**。

> 代价：如果密钥文件丢失，所有已签发但未使用的票都无法复算。
> 这不是数据丢失（票本来就一次性），但已领取的任务需要重新领取才能续租。

### 5.3 旧版遗留的脏数据会被拒绝而不是被接受

- reviews.actor 老行默认填 human_owner（DEFAULT 值），
  所以旧的人工审查在 approve() 里不会被判成「执行器投票」；
- 如果旧库里有执行器伪造的审查票（v0.1.0 没有 actor 列所以无法区分），
  本版本**无法追溯纠正**。这是已知的、无法自动修复的历史数据问题，
  处置方式：对可疑任务执行 approve --decision return 退回重审。

## 6. 备份格式变更

v0.1.0 的 export_bundle 直接 zipfile.write(council.db) ——
WAL 模式下主库文件可能只有 4096 字节（数据全在 -wal 里），导出的包是**空库**。

v0.1.1 的 bigfish/backup.py：

- db.snapshot(dest) 用 sqlite3.Connection.backup()（在线一致性快照），
  完成后把目标库 PRAGMA journal_mode=DELETE，保证单文件自包含；
- 包内 council.db + manifest.json（含 backup_format、schema_version、
  db_sha256、table_counts、includes_key_file: false、
  contains_plaintext_credentials: false）；
- 恢复前先 inspect_bundle 校验（必需表齐全 + 关系完整性 + foreign_key_check），
  校验失败**不碰原库**；通过后把原库复制成 council.db.prerestore 再替换。

**v0.1.0 导出的旧备份可以直接 inspect 但可能缺 manifest.json，
这类包必须人工核对表计数后才能恢复。** 旧包里的 council.db 若只有 4096 字节，
恢复出来会是空库 —— 已由 tests/test_v011_p0.py::test_corrupted_bundle_does_not_touch_db 覆盖。

## 7. 迁移失败怎么办

迁移只做 ADD COLUMN 与 CREATE TABLE IF NOT EXISTS，正常不会失败。
若遇到异常（磁盘满 / 库文件损坏）：

1. **不要删库**。先把整个数据目录复制一份备份；
2. .venv/bin/python -m bigfish.cli --data-dir <dir> inspect <某个旧备份.zip> 确认备份可用；
3. 回滚到 v0.1.0 可执行文件（见 ROLLBACK-v0.1.1.md），
   但注意：**已升级到 schema 2 的库不能被 schema 1 的代码写入**
   （v0.1.0 不认识 audit 表不影响读，但它会按 schema 1 继续写入，导致新列永远为 NULL）。
   所以正确顺序是：**先用 v0.1.0 的备份恢复，再回滚代码**。

---

## 9. rc3 追加：schema 2 → 3

### 9.1 变更清单

    ALTER TABLE executors ADD COLUMN model TEXT;
    ALTER TABLE tickets   ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks     ADD COLUMN cancel_requested_at REAL;
    CREATE INDEX IF NOT EXISTS idx_tickets_executor ON tickets(executor_id);
    CREATE INDEX IF NOT EXISTS idx_idem_expires   ON idempotency(expires_at);

四条不会破坏既有数据：

| 列 | 为什么要加 | 老数据会怎样 |
| --- | --- | --- |
| `executors.model` | 审查身份（GPT/Gemini）不能靠 name 字符串猜，要绑到不可随意改的列 | 老行的 model 为 NULL，读取时按角色判定，不会误判 |
| `tickets.epoch` | 数据库从备份恢复后，旧执行票必须失效。epoch 不随快照回滚 | 老票 epoch=0，恢复后 epoch 变成 1，全部失效 |
| `tasks.cancel_requested_at` | CANCELLING 状态判断执行器是否失联要用它 | 老任务为 NULL，等价于「从未请求取消」 |

两个索引纯性能，建不建都不影响正确性。

### 9.2 迁移步骤（自动，无需手动 SQL）

1. 停掉所有进程（GUI、headless、任何打开数据库的东西）；
2. 备份整个数据目录，**含 `secrets/ticket.key`**；
3. 换代码；
4. 启动任意入口（GUI / `python -m bigfish` / `python -m bigfish.cli`），首次打开库时自动迁移；
5. 验证：

    $ python -m bigfish.cli overview
    $ sqlite3 <data_dir>/council.db "SELECT value FROM meta WHERE key='schema_version'"
    3

### 9.3 迁移失败会怎样

每个版本号的所有 DDL 在**一个事务**里执行，任一条失败 → `ROLLBACK` → 库停在原版本 → 抛异常。
程序**不会**带着半迁移的库继续跑。重复执行 DDL 遇到 `duplicate column name` 视为成功（幂等），
所以中途崩过一次也能重来。

### 9.4 不允许降级

    # bigfish/db.py _migrate_locked()
    if current > SCHEMA_VERSION:
        raise DatabaseVersionError("数据库 schema v{n} 高于程序支持的 v{m}，拒绝打开")

把 rc3 的库拿去给 rc2 代码打开，会直接抛错，不会静默降级把 v3 的列当不存在。
这一点有单测：`test_restore_future_schema_rejected`。

### 9.5 从备份恢复时的版本处理

| 备份里的 schema | 当前程序 | 结果 |
| --- | --- | --- |
| 相同 | 相同 | 直接替换 |
| 更旧 | 更新 | 恢复到临时库 → 迁移到当前版本 → 迁移成功才替换 |
| 更新 | 更旧 | **拒绝**（`DatabaseVersionError`），不降级 |
| 缺表/缺列/损坏 | 任意 | **拒绝**（`_verify_relational` + `PRAGMA integrity_check`） |

单测覆盖：`test_restore_older_schema_migrates`（2 → 3 实测）、
`test_restore_future_schema_rejected`（schema 99 被拒）、
`test_restore_corrupted_bundle_rejected`（假 ZIP、只有 manifest 的包）。

### 9.6 恢复会改密钥（重要）

`restore_from_bundle()` 成功时会：

1. 轮换 `secrets/ticket.key`（新 32 字节，0600 / icacls）；
2. `meta.ticket_epoch + 1`；
3. 清掉 `idempotency` 表里 claim / heartbeat / submit 三类记录；
4. 把 DISPATCHED/RUNNING/CANCELLING 的任务转 BLOCKED 并吊销全部票。

**这意味着恢复之后，所有执行器都要重新领取任务。** 不是 bug，是设计：
否则一份旧快照就能把已经撤销的执行授权复活。

### 9.7 回滚（rc3 → rc2）

schema 3 的库无法被 rc2 打开（会被拒绝）。回滚必须：

1. 停进程；
2. 用**升级前的备份**恢复（或用 `cli restore` 恢复到 rc2 时期的 operational 包）；
3. 换回 rc2 代码；
4. 启动验证。

详见 `docs/ROLLBACK-v0.1.1.md` 第 7 节。

