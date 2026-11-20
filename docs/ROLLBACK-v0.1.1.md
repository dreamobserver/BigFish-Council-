# 回滚说明（v0.1.1-rc1 → v0.1.0）

## 0. 先说结论

| 场景 | 是否可直接回滚 | 正确做法 |
| --- | --- | --- |
| 从未跑过 v0.1.1-rc1（库还是 schema 1） | 可以，直接换回 v0.1.0 | 停进程 → 换代码/可执行文件 → 启动 |
| 跑过 v0.1.1-rc1（库已被迁到 schema 2） | **不能只换代码** | 先用备份恢复到 schema 1，再换回 v0.1.0 |
| 只是 GUI 出问题、服务正常 | 可以 | 换 GUI 代码即可，库不用动 |

原因：schema 2 只加了列和 audit 表，v0.1.0 的**读**不受影响（实测 v0.1.0 的
task-list / overview 能正常跑 schema 2 的库，新列默认为 NULL），
但 v0.1.0 的**写**不会维护 max_expires_at / request_hash / actor 这些新列，
混合运行会产生难以解释的数据状态。实测见第 4 节。

## 1. 回滚前的三件准备（必做）

1. **停掉所有进程**：GUI、headless 服务、所有 watchdog、VPS 跳板。

       Get-Process | Where-Object { $_.Path -like "*BigFishCouncil*" } | Stop-Process
       # VPS 侧
       pkill -f 'bfc-proxy.mjs'; pkill -f 'bigfish --headless'

2. **备份整个数据目录**（含 council.db / council.db-wal / secrets/）：

       $src = "$env:LOCALAPPDATA\BigFishCouncil"
       $dst = "$env:USERPROFILE\BigFishCouncil-backup-$(Get-Date -f yyyyMMdd-HHmmss)"
       Copy-Item $src $dst -Recurse -Force

   **注意 secrets/ticket.key 一定要一起备份**，否则回滚后旧票全部无法复算。

3. **导出一份 v0.1.1 格式的备份包**（用于将来想再回到 rc1）：

       .\.venv\Scripts\python.exe -m bigfish.cli export .\backup-v011.zip

## 2. 标准回滚步骤（推荐）

### 2.1 停服务

见 1.1。

### 2.2 数据回滚

**如果之前有 v0.1.0 时期的数据备份**（最干净）：

    Remove-Item "$env:LOCALAPPDATA\BigFishCouncil" -Recurse -Force
    Copy-Item <v0.1.0 时期的备份目录> "$env:LOCALAPPDATA\BigFishCouncil" -Recurse

**如果只有 v0.1.1 的备份包**（做不到完全退回 schema 1，只能保留数据）：

    # 先看包里是什么
    .\.venv\Scripts\python.exe -m bigfish.cli inspect .\backup-v011.zip

    # 恢复（会自动留 council.db.prerestore 副本）
    .\.venv\Scripts\python.exe -m bigfish.cli restore .\backup-v011.zip

⚠️ 这一步恢复的是 schema 2 的库，回滚到 v0.1.0 代码后它会以「只读兼容」状态运行 —— 
新列永远是 NULL，幂等/续租/审计全部失效。只应作为数据抢救手段，不作为正常运行状态。

### 2.3 代码回滚

    cd <项目目录>
    git checkout 1a97f5a -- .          # 基线 commit：baseline: BigFish Council v0.1.0
    # 或直接用完整基线快照
    robocopy /home/dsh/bfc-baseline-v0.1.0 . /E

Windows 上的实际做法（没有 git 时）：

    Remove-Item .igfish, .	ests, .	ools -Recurse -Force
    Expand-Archive .igfish-council-v0.1.0-src.zip -DestinationPath . -Force

### 2.4 重建环境并验证

    ..venvScriptspython.exe -m pip install -r requirements.txt
    ..venvScriptspython.exe -m unittest discover -s tests     # 应 21 项全过
    ..venvScriptspython.exe toolsselfcheck.py                # v0.1.0 的 14 项
    ..venvScriptspython.exe -m bigfish.cli overview

### 2.5 重启

    .start-gui.ps1

## 3. 回滚不需要动的东西

- **不需要**卸载依赖、不需要重装 Python；
- **不需要**动 DSH 容器、不需要改 /home/dsh 下任何东西；
- **不需要**动 Tailscale 配置（本版本从没改过 Tailscale）；
- **不需要**动 VPS 上的 bfc-proxy.mjs（回滚后它指向的端口仍是 3096）。

## 4. 实测记录（2026-11-18）

脚本 /tmp/rollback_check.py，用同一份 venv 交替运行两套代码：

```
=== A. 用 v0.1.0 代码建库建任务 ===
rc= 0
{ "id": "task-8fd3bdbb92", "title": "回滚验证任务", "status": "DRAFT", ... }
回滚点已备份 = /tmp/rollback-test/data-v0.1.0-snapshot

=== B. 用 v0.1.1-rc1 打开（自动迁移到 schema 2）===
rc= 0
schema_version = 2
tasks 数 = 2
audit 表存在 = True

=== C. 直接拿 v0.1.0 代码去跑 schema2 库（诚实测试）===
task-list rc= 0
stdout: [ {"id":"task-8d533c0566","title":"RC1 新任务","status":"DRAFT", ...},
          {"id":"task-8fd3bdbb92","title":"回滚验证任务","status":"DRAFT", ...} ]
stderr: (空)
task-create rc= 0 | stderr: (无)

=== D. 标准回滚：恢复数据快照 + 恢复代码 ===
回滚后 v0.1.0 task-list rc= 0
回滚后 schema_version = 1
回滚后 tasks 数 = 1
```

结论：

- **数据回滚干净**：恢复 v0.1.0 快照后 schema_version 回到 1，任务数回到 1，无残留；
- **代码层混跑不崩**：v0.1.0 读写 schema 2 的库都返回 rc=0，不报错；
- 但这不等于「可以混跑」——见下一节。

## 5. 混跑会产生什么（实测）

```
v0.1.0 写出来的 reviews: [('gpt', 'human_owner', None)]
```

v0.1.0 用旧代码投票时，reviews.actor 拿到的是 DEFAULT 值 'human_owner'。
这意味着：**执行器伪造的票在这条路径上会被当成人工票**（v0.1.0 本来就没有身份概念）。
所以：

- 混跑只允许出现在「回滚过程中间态」；
- 回滚完成后必须确认 schema_version 已回到 1，或者干脆用 v0.1.0 重建库；
- **不允许长期让 v0.1.0 与 rc1 交替写同一个库**。

## 6. 紧急情况下的一键检查

    # 1. 当前库是哪个 schema
    ..venvScriptspython.exe -c "import sqlite3;print(sqlite3.connect(r'%LOCALAPPDATA%BigFishCouncilcouncil.db').execute("SELECT value FROM meta WHERE key='schema_version'").fetchone())"

    # 2. 完整性
    ..venvScriptspython.exe -c "from pathlib import Path;from bigfish.db import Database;d=Database(Path(r'%LOCALAPPDATA%BigFishCouncilcouncil.db'));print(d.integrity(), d.table_counts())"

    # 3. 有没有 prerestore 残留（说明曾经恢复过）
    Get-ChildItem "$env:LOCALAPPDATABigFishCouncil" -Filter "*.prerestore"

## 7. 回滚后仍存在的问题（诚实说明）

v0.1.0 本身的缺陷不会因为回滚而消失：

- 备份包不完整（WAL 不在包里）；
- 审批可跨版本漂移；
- 幂等表存明文执行票与 Prompt；
- 心跳不续租、不幂等；
- MCP 业务失败仍返回 isError=false；
- 看门狗不随服务启动。

回滚是**止损手段**，不是修复。

---

## 7. rc3 回滚（v0.1.1-rc3 → v0.1.1-rc2）

### 7.1 先说结论

| 场景 | 能否直接换代码 | 正确做法 |
| --- | --- | --- |
| 从没启动过 rc3（库还是 schema 2） | 可以 | 停进程 → 换回 rc2 代码 → 启动 |
| 启动过 rc3（库已迁到 schema 3） | **不能** | 必须先把库恢复到 schema 2，再换回 rc2 |

原因：rc2 的 `Database._migrate_locked()` 发现库版本 3 > 程序支持 2 会直接
`raise DatabaseVersionError`，拒绝打开。这是 rc3 新加的防降级，**故意的**——
让老代码带着半懂的 schema 跑比启动失败更危险。

### 7.2 回滚步骤（库已到 schema 3）

    # 1) 停掉一切    
    #    GUI 关掉；headless 用 tools/stop-headless.ps1；确认 3096 没有 python 在听

    # 2) 保留现场（出事能再回来）
    copy 数据目录 -> 数据目录.rc3-backup-YYYYMMDD

    # 3) 用 rc3 自己的恢复功能退回 rc2 时期的备份
    python -m bigfish.cli --data-dir <data_dir> restore <rc2时期的 operational 包>
    #    注意：必须是 KIND_OPERATIONAL 包；KIND_AUDIT 审计包会被拒绝（这是设计）

    # 4) 换回 rc2 代码
    git checkout <rc2 的 commit>   # 或解压 rc2 源码包

    # 5) 启动验证
    python -m bigfish.cli --data-dir <data_dir> overview
    python -m unittest discover -s tests -t .     # 期望 Ran 103 tests + OK

### 7.3 没有 rc2 时期备份怎么办

只能手工降级到 schema 2。**这会丢数据列**，务必先整目录备份。

    sqlite3 <data_dir>/council.db <<'SQL'
    BEGIN;
    ALTER TABLE tickets DROP COLUMN epoch;
    ALTER TABLE tasks   DROP COLUMN cancel_requested_at;
    ALTER TABLE executors DROP COLUMN model;
    DROP INDEX IF EXISTS idx_tickets_executor;
    DROP INDEX IF EXISTS idx_idem_expires;
    UPDATE meta SET value='2' WHERE key='schema_version';
    COMMIT;
    SQL

前置条件：sqlite ≥ 3.35（本机 3.40.1，支持 `DROP COLUMN`）。
**丢掉的东西**：`executors.model`（审查身份绑定）、`tickets.epoch`（旧票防复活）、
`tasks.cancel_requested_at`（取消请求时间）。回滚后 rc2 代码不读这三列，所以能跑；
但 rc3 引入的三项安全保证同时失效，属于**安全降级**，只在必须时用。

### 7.4 只回滚部分功能（推荐做法）

不想降 schema，但某个 rc3 行为不合用？优先用配置而非回滚：

| 想恢复的行为 | 做法 |
| --- | --- |
| 取消立即生效（不走 CANCELLING） | 把 `state.CANCEL_NEEDS_ACK` 置空，重启生效。**不建议**：会让远程实际还在跑的任务被标成已取消 |
| 令牌不轮换 | `restore_bundle(db, path, key_path=None)`，即不传密钥路径。**强烈不建议**：旧票会复活 |
| 心跳不续租 | 把 `settings.heartbeat_lease_sec` 调小到接近 `ticket_ttl_sec` |

### 7.5 回滚后必须检查

1. `SELECT value FROM meta WHERE key='schema_version'` → 应为 `2`；
2. 没有任何任务卡在 `CANCELLING`（rc2 不认识这个状态）；
3. `secrets/ticket.key` 在位且权限正确；
4. 执行器能用旧 token 重新 claim 成功；
5. 跑一遍 rc2 的 103 项测试。

### 7.6 混跑危害（再说一遍）

**绝不要**让 rc2 和 rc3 同时读写同一个数据库文件。
rc3 会写 `epoch` / `cancel_requested_at`，rc2 不认识但不会清空它们；
然而 rc3 的取消语义、票校验、恢复阻断在 rc2 侧完全不存在，
两个进程对「这张票还能不能用」会给出相反答案。

