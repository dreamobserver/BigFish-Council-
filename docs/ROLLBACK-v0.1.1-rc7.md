# BFC v0.1.1-rc7 回滚说明

| 项目 | 值 |
| --- | --- |
| 当前版本 | `0.1.1-rc7`（分支 `fix/v0.1.1-rc7-hardening`） |
| 目标版本 | `0.1.1-rc6`（发布点 commit `a2e9275751`） |
| 能不能只换代码 | 能。库结构没变，`SCHEMA_VERSION` 两边都是 4 |
| 能不能只换库 | 没有「只换库」这回事——rc7 没改结构，换库等于什么也没做 |
| 正确形态 | 换代码 + 处理 mobile 身份残留；库原样留着 |
| 已验证平台 | Debian 12 / Python 3.11.2（rc7 的 295 项全过） |
| 已验证平台 | Windows 11 / Python 3.14.6 / SQLite 3.50.4（rc7 的 294 项全绿：`Ran 294 tests in 74.232s` / `OK (skipped=1)`；收尾补的第 295 项未在 Windows 重跑） |

## 0. 先说结论

| 场景 | 能否回滚 | 正确做法 |
| --- | --- | --- |
| 只是代码有问题 | ✅ 能 | git 切回去 + 重装依赖 |
| 库被 rc7 写坏了 | ⚠️ 看情况 | 用恢复前的操作包 `restore_bundle` 回，不是 git 能解决的 |
| rc7 期间建过 mobile 身份 | ⚠️ 能，但要处置 | **先禁用再回滚**，否则这条身份在 rc6 下会拿到执行器权限 |
| 恢复中途失败 | ⚠️ 能 | 靠 `.prerestore` 快照回，见 §5 |
| 恢复失败且回滚也失败 | ❌ 不能自动 | 进维护态，人工按 §6 处置 |

一句最重要的：**rc7 没动库结构，回滚的主要风险不在数据，在角色**。
`mobile` 这个 `db_role` 在 rc6 里不存在，而 rc6 的 `role_for_db_row()` 末尾是 `return EXECUTOR_DSH`——未知角色一律当执行器。

---

## 1. 回滚前必须做的三件事

### 1.1 导一个操作包

```bash
python -m bigfish.cli export rollback-before.zip
python -m bigfish.cli inspect rollback-before.zip
```

`inspect` 会打印 manifest 里的 `backup_kind` / `schema_version` / `table_counts`，确认它是**操作包**不是审计包。
审计包恢复会被直接拒：`RestoreError("这是脱敏审计包，不能用于恢复运行数据库")`（`bigfish/backup.py:471-472`）。

### 1.2 记下三个值

```python
import sqlite3
con = sqlite3.connect("council.db")
for k in ("schema_version", "restore_epoch", "key_sha256"):
    print(k, con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()[0])
print(con.execute("PRAGMA integrity_check").fetchone()[0])
con.close()
```

回滚之后要用这三个值比对。`restore_epoch` 单调递增，是最可靠的那条线索。

### 1.3 确认有没有 mobile 身份

```python
rows = con.execute("SELECT id, name, role, enabled FROM executors WHERE role = 'mobile'").fetchall()
```

输出空列表 → 直接进 §2。有结果 → **先读 §4 再动手**。

---

## 2. 代码回滚

### 2.1 前置条件

- 所有服务停掉：GUI、headless、watchdog 全关。
- 工作区干净：`git status --short` 无输出。rc7 的改动若还没提交，直接切分支会撞「本地改动会被覆盖」。
- 依赖可回退：`requirements.txt` 没被改过；`requirements.lock` 是新增文件，删不删都不影响 rc6 运行。

### 2.2 git 步骤

```bash
git status --short                 # 必须为空

git checkout v0.1.1-rc6            # 或 git checkout a2e9275751

# 把 rc7 新增的三个未跟踪文件挪走（不删，留着以后用）
mkdir -p ../rc7-park
mv requirements.lock .github ../rc7-park/ 2>/dev/null || true
rm -f tests/test_v011_rc7.py

.venv/bin/python -m pip install -r requirements.txt   # 通常秒过
```

### 2.3 文件角度（不玩 git 时）

rc7 的改动全在 `bigfish/` 里（14 个文件），整目录一起换最稳。几个必须换的：

| 文件 | 为什么必须换 |
| --- | --- |
| `bigfish/locks.py` | BFC-01 整文件重写，槽位模型与 rc6 的租约互不认 |
| `bigfish/roles.py` | 含 `mobile_viewer`，与 rc6 的 `_DB_ROLE_MAP` 对不上 |
| `bigfish/server.py` | BFC-02 / BFC-03，`create_production_app` 返回元组而非 FastAPI |
| `bigfish/core.py` | 白名单与 `bfc_mb_` 前缀 |
| `bigfish/gui.py` | 改用 `create_production_app` |
| `backup.py`、`cli.py`、`db.py`、`config.py` 等 | 配套小改，一起换最稳 |

**不要混搭**：rc7 的 `roles.py` 配 rc6 的 `core.py`，白名单会拒掉 `mobile`；反过来又会放行一个 rc6 的 `_DB_ROLE_MAP` 不认识的角色。

### 2.4 回滚后立刻验证

```bash
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"   # 应为 0.1.1-rc6
.venv/bin/python -m unittest discover -s tests -t .                 # rc6 的项数且 OK
.venv/bin/python tools/selfcheck.py                                  # 16 通过 0 失败
```

---

## 3. 数据回滚：库可以直接被 rc6 打开

rc7 的 `SCHEMA_VERSION` 与 `BACKUP_FORMAT_VERSION` 都是 4（`bigfish/db.py:24-25`），rc6 也是 4；
`MIGRATIONS` 最高只到 4（`bigfish/db.py:176-212`），rc7 没有新增第 5 步。

所以 rc7 写过的库拿给 rc6 打开时，`init_schema()` 走「`current == SCHEMA_VERSION`」这条分支（`bigfish/db.py:429-439`）：
直接 `executescript(SCHEMA)`（全是 `IF NOT EXISTS`）+ `INSERT OR IGNORE` 补元数据，一条 `ALTER TABLE` 都不跑。

**结论：不需要恢复库，也不要改 `meta.schema_version`。动它反而是自找麻烦。**

结构没变不代表数据没变。rc7 可能写进去的、rc6 不认识的东西只有一个：`executors.role = "mobile"`。
其余字段（`disabled_reason`、`epoch`、`model` 等）rc6 都认识，因为它们在 schema 4 之前就已经存在。

---

## 4. mobile 执行器：回滚里唯一的真坑

### 4.1 会发生什么

rc6 的 `_DB_ROLE_MAP` 只有 `executor` / `reviewer` / `admin` 三个键，**没有 `mobile`**。
`_DB_ROLE_MAP.get("mobile")` 返回 `None`，`"mobile" in _DB_ROLE_MAP` 为假，于是落到 `role_for_db_row()` 末尾那行：

```python
# bigfish/roles.py（rc6 版本，第 94 行附近）
return EXECUTOR_DSH
```

结果：**这条身份在 rc6 下被当成执行器（`executor_dsh`）**。`server.py` 的 `_need()` 读的就是 `role_canonical`，而 rc6 的 `authenticate()` 会把它填成 `executor_dsh`。

| 能力 | rc7 下（mobile） | rc6 下（被当成 executor） |
| --- | --- | --- |
| `council_list_tasks` / `council_claim_task` | 拒 | **放行**（`task.claim`） |
| `council_heartbeat` | 拒 | **放行**（`run.heartbeat`） |
| `council_submit_result` | 拒 | **放行**（`run.submit`） |
| `council_task_status` | 拒 | **放行**（`task.read.own`） |

这正是 `roles.py:50-53` 那条注释警告的事：「未知 `db_role` 会落到 `EXECUTOR_DSH`，即『没登记 = 拿到执行器权限』」。
rc7 新增的 `test_mobile_db_role_never_falls_back_to_executor` 钉住了这条，但**它跑在 rc7 的代码上，救不了 rc6**。

### 4.2 处置 A：回滚前禁用（推荐）

```python
con.execute("UPDATE executors SET enabled = 0, disabled_reason = ? WHERE role = 'mobile'",
            ("rc7 回滚 rc6 前主动禁用",))
con.commit()
print(con.execute("SELECT COUNT(*) FROM executors WHERE role = 'mobile' AND enabled = 1").fetchone()[0])
```

`enabled = 0` 的身份在 rc6 里同样进不来——`authenticate()` 会先查 `enabled`。禁用后即使用 `bfc_mb_` 令牌也拿不到 `executor_dsh`。

### 4.3 处置 B：回滚前直接删

```sql
DELETE FROM executors WHERE role = 'mobile';
```

更干净，但以后要用就得重建（`token` 只在创建时返回一次）。`tickets` 里的旧票会变成孤儿，回滚后跑一次 `python -m bigfish.cli sweep` 清掉。

### 4.4 为什么不能用 `reenable` 补救

`reenable_executor()` 的 prefix 表在新旧两版里都只有 `executor` / `reviewer` / `admin` 三个键，`.get(role, "bfc_ex_")` 的默认值是 `bfc_ex_`。
所以**mobile 身份被重新启用后拿到的一定是 `bfc_ex_` 前缀的令牌**——这等于明着把它升成执行器。
在 rc6 下用 `reenable` 处理 mobile 是反向操作，越处理越糟。只有 A 和 B 是对的。

**一句话记住：回滚前先禁用，别等回滚后再补救。**

---

## 5. 备份恢复的回滚

### 5.1 `restore_bundle` 会留下的中间文件

`restore_bundle(db, path, key_path=None, before_replace=None, actor="human_owner")`（`bigfish/backup.py:449`）执行期间在库文件同目录产生：

| 文件 | 什么时候产生 | 失败时被怎么用 |
| --- | --- | --- |
| `council.db.restore-new` | 备份解包到 staging 的位置（`bigfish/backup.py:477`） | `finally` 里无条件删掉（`:641-655`） |
| `ticket.key.restore-new` | 新密钥先落这里（`write_key_atomic`，`:531-532`） | 未安装成功则 unlink（`:628-632`） |
| `council.db.prerestore` | 替换前给当前库拍的快照（`:541`） | **回滚的数据源**（`:610-616`） |
| `ticket.key.prerestore` | 替换前当前密钥的副本（`:544-548`） | 密钥回滚的数据源（`:621-623`） |
| `council.db.rollback` | 回滚时才产生（`:610`） | 从 `.prerestore` 拷一份再 `os.replace` 回原位 |
| `council.db.restore.lock` | 恢复闸门锁（`bigfish/locks.py:56-58`） | 区分「另一个恢复在跑」和「有写者在用」 |
| `council.db.access.lock` | 写者/恢复共用的访问锁（`bigfish/locks.py:51-53`） | 正确性来源，POSIX `flock` / Windows 64 槽位 |

后两个锁文件**按设计永不删除**（`test_lock_files_are_never_deleted` 钉着），看到它们不代表有残留进程。

### 5.2 失败自动回滚的逻辑

`bigfish/backup.py:597-640`：先 `db.close()`（Windows 上不关连接就 `os.replace` 会撞文件占用）；
库已换过去（`swapped`）且 `had_prerestore` 时，从 `.prerestore` 拷一份成 `.rollback`、删 `-wal`/`-shm`、`os.replace(rollback, target)`；
密钥已装（`key_installed`）且 `key_backup` 存在时 `os.replace(key_backup, key_path)` 换回去；最后 `db._reopen()` 并删掉残留的 `key_new`。

任一步失败会累积进 `rollback_errors`，非空时把 `keep_maintenance` 置真并抛：

```text
恢复失败且回滚未完全成功：<逐条错误>（原始错误：<原始异常>）
```

看到这句就别重试了，进 §6。

### 5.3 人工恢复（自动回滚也没成时）

```bash
ls -la council.db* ticket.key* 2>/dev/null      # 1. 看现场（先停掉一切）
mv council.db council.db.broken-$(date +%s)      # 2. 坏库挪开，不要删
cp council.db.prerestore council.db              # 3. 用快照回
rm -f council.db-wal council.db-shm
cp ticket.key.prerestore ticket.key              # 4. 密钥同理
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('council.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

最后一行期望打印 `ok`。不 `ok` 说明 `.prerestore` 本身也是坏的，只能退回 §1.1 那个操作包。

---

## 6. 回滚失败的最坏情况

| 情况 | 症状 | 人工处置 |
| --- | --- | --- |
| 库与密钥不匹配 | 能起服务但所有执行器令牌失效，`meta.key_sha256` 与磁盘密钥对不上 | 走 `ensure_key_consistency()` 自愈：`rotate_key` 并随轮换吊销全部未撤销票、清三类幂等、写 `audit(db.key_healed)`（`bigfish/backup.py:677-726`）。**代价是所有执行器要重新拿令牌** |
| 维护态卡住 | 服务起来就报 `MaintenanceError` | 上次恢复把 `keep_maintenance` 置真了（`bigfish/backup.py:633-639`）。确认库与密钥都已是想要的状态后，清掉维护标记再重启 |
| 两个中间文件都在 | `council.db` 与 `.prerestore` 内容不同，分不清哪个是好的 | 都别动。各自 `PRAGMA integrity_check`，再比 `restore_epoch`——大的那个是后写的。仍不确定就用 §1.1 的操作包 |

### 6.1 「全部都没了」时

1. 停服务，把整个 `data_dir` 打成 tar 留证，别在原地继续折腾。
2. 在**新目录**里用最新版本的 `restore_bundle` 再试（新目录能避免旧锁文件与旧 `-wal` 的干扰）。
3. 全失败就承认数据丢失，用更早的备份重建，然后把这次事故写进 `AUDIT.md`。

**不要做的三件事**：手工 `UPDATE meta SET schema_version=`、删 `.access.lock` 指望解锁、把 `.prerestore` 和 `council.db` 合并。
这三件事都会把「可以诊断的故障」变成「没法诊断的故障」。

---

## 7. 一键检查清单

```bash
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('council.db').execute('PRAGMA integrity_check').fetchone()[0])"
.venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("council.db")
for k in ("schema_version", "restore_epoch"):
    print(k, con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()[0])
print("启用的 mobile:", con.execute(
    "SELECT COUNT(*) FROM executors WHERE role='mobile' AND enabled=1").fetchone()[0])
con.close()
PY
ls -la council.db.restore-new council.db.rollback ticket.key.restore-new 2>/dev/null || echo "无残留（正常）"
.venv/bin/python -m bigfish.cli allowed-ports
```

期望：版本是 `0.1.1-rc6`；完整性 `ok`；`schema_version` 是 4 且 `restore_epoch` 不比回滚前小；
**启用的 mobile 必须是 0**；中间文件应为「无残留」；端口打印出 implemented 与 reserved 两段。

---

## 8. 仍存在的问题

| 问题 | 说明 |
| --- | --- |
| 兜底提权没有编译期防线 | `role_for_db_row()` 末尾的 `return EXECUTOR_DSH` 是新旧两版共有的设计，靠「加角色时记得改三处」的注释约束。rc7 加了测试钉住 mobile，但下一个新角色还是同样的风险 |
| `reenable_executor` 的 prefix 表没覆盖 mobile | 两版都只有三个键、默认 `bfc_ex_`。这条在 rc7 里也是不完整的，见交付文档 U6 |
| 回滚没有自动化验证 | 本文所有步骤都在 Debian 12 上按代码路径推演过，**没有在真实生产机上完整跑过一次 rc7 → rc6 的回滚演练** |
| Windows 侧已验证 | rc7 在 Windows 11 上 294 项全绿（`Ran 294 tests in 74.232s`，对应 294 项那一版），其中 CLI restore 关库与租约释放相关的用例都在真机跑过；「回滚后能否开库」不再是纯代码推断 |

---

## 9. 一句话总结

rc7 好回滚，因为它一动没动库结构——代码切回去，库直接就能开。
唯一需要动脑的是 mobile：它在 rc6 眼里是个不认识的字符串，而不认识就等于执行器。
回滚前把它禁掉，这件事就没了；回滚后再想补救，手里那把工具（`reenable`）只会把它升得更高。
