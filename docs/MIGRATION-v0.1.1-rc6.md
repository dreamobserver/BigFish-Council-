# 迁移说明：v0.1.1-rc5 → v0.1.1-rc6

| 项 | 值 |
| --- | --- |
| 起点 | `0.1.1-rc5`（发布点 `c6d663d`，`SCHEMA_VERSION = 4`） |
| 终点 | `0.1.1-rc6`（`ae2fe82`，`SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`） |
| 分支 | `fix/v0.1.1-rc4-hardening` |
| 库结构变化 | **没有**。`SCHEMA_VERSION` 仍是 4，`MIGRATIONS` 没有新增键，`SCHEMA` 建表块一行没改 |
| 是否需要人工介入 | 数据库不用动。要做的是：停干净所有旧实例再启动、知道多出来的锁文件是什么、下游若有脚本解析审计包 manifest 则适配 `credential_scan` |
| 能否降级 | 能，比 rc4→rc5 容易得多：schema 没变，回 rc5 只需换代码 + 删 `locks.py` + 清锁文件。见 `docs/ROLLBACK-v0.1.1-rc6.md` |
| 已验证平台 | Debian 12 / Python 3.11.2 / SQLite 3.40.1（265 项全绿）与 Windows 11 / Python 3.13.11 / SQLite 3.50.4（`Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0）。Windows 侧首跑 2 错，修掉两个 Windows 专属缺陷后才全绿（见 `TEST-REPORT-v0.1.1-rc6.md` 6.1.1 / 6.1.2） |

本文只描述「从 rc5 迁到 rc6」这一个方向。回滚见 `docs/ROLLBACK-v0.1.1-rc6.md`，安全语义见 `docs/SECURITY-REVIEW-v0.1.1-rc6.md`。

---

## 1. 结论先说：数据库不需要迁移

这一版最容易误判的地方是「版本号变了，是不是要跑迁移」。答案是不用，理由不是「看起来没改」，而是三个常量与两份结构定义都没动：

| 项 | rc5 | rc6 | 说明 |
| --- | --- | --- | --- |
| `SCHEMA_VERSION` | 4（`bigfish/db.py:22`） | 4（`bigfish/db.py:24`） | 常量值不变，只是上方多了 `from .locks import WriterLease` 导致行号下移 2 行 |
| `BACKUP_FORMAT_VERSION` | 4（`bigfish/db.py:23`） | 4（`bigfish/db.py:25`） | 备份包格式不变，rc5 与 rc6 的 `.zip` 互相认得 |
| `MIGRATIONS` 的键 | `2 / 3 / 4` | `2 / 3 / 4` | 没有 `MIGRATIONS[5]` |
| `SCHEMA` 建表块 | — | — | 一行没改 |
| `REQUIRED_TABLES` | 12 张 | 12 张 | 没变 |

也就是说 `init_schema()` 在 rc6 上打开一个 rc5 建的库时，走的是「已有库」分支：`_migrate_locked(4)` 发现当前版本已经是 4，一步都不执行，然后 `executescript(SCHEMA)`（全是 `IF NOT EXISTS`，幂等）与 `INSERT OR IGNORE` 补 `restore_epoch`。**没有任何 ALTER、没有任何建表、没有数据搬运。**

反方向也实测过：rc6 建的库（schema 4）rc5 打开成功，`exit 0`，任务列表正常。两边对同一个库的读写完全兼容。

---

## 2. 新增的磁盘文件：`locks.py` 与两类锁文件

### 2.1 代码侧新增一个文件

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `bigfish/locks.py` | 157 | 跨进程「谁在用这个库」的写者租约 + 恢复期排他锁（F-06） |

它是**新文件**，不是改名：rc5 里 `backup.py` 的 `_restore_lock()`（约 30 行）在 rc6 被删除，职责由 `locks.py` 完全取代。这一点对回滚很关键——rc6 的 `db.py` 第 22 行写着 `from .locks import WriterLease`，把 rc6 的 `db.py` 单独拷进 rc5 的树会直接 `ModuleNotFoundError: No module named 'bigfish.locks'`（实测）。所以**代码必须整树换，不能只换文件**。

### 2.2 数据目录里新增两个锁文件

| 文件 | 出现条件 | 大小 | 作用 |
| --- | --- | --- | --- |
| `council.db.access.lock` | 每次打开库（POSIX） | 0 字节、内容为空 | 写者持 `LOCK_SH`、恢复持 `LOCK_EX` 的那把 `flock` 的载体 |
| `council.db.access.<pid>.lock` | 每次打开库（Windows） | 0 字节 | Windows 没有共享锁，退化成「一个进程一个文件」，恢复时逐个试探 |

实测：`open_council()` 建库后目录里是

```
council.db   council.db-shm   council.db-wal   council.db.access.lock   logs   secrets
```

关库之后 `council.db.access.lock` **仍然在**（0 字节）。POSIX 分支的 `release()` 只做 `LOCK_UN` 与 `close()`，不 `unlink`；只有 Windows 分支在 `release()` 末尾 `os.unlink`。

### 2.3 能不能删

能，前提只有一条：**没有任何进程正在用这个库**。

实测三条，都是真跑过的：

| 做法 | 结果 |
| --- | --- |
| 关掉所有实例后手工 `unlink` 锁文件，再 `Database(path)` | 正常打开，锁文件自动重建 |
| 库还开着时手工 `unlink` 锁文件，然后 `exclusive_access()` | **成功**——屏障失效（这是 S5 那条残余风险，不是「删了没事」的许可） |
| 把 rc6 建的库 + 别人留下的 0 字节锁文件一起拷到新目录，再打开 | 正常打开，任务数正确，恢复也成功（`restored=True epoch=1`） |

所以：**锁文件是纯标记，不含任何业务数据，删掉不会丢数据**；但「库还开着的时候删」等于主动放弃跨进程保护，别在服务运行时做这件事。

### 2.4 备份 / 恢复时会不会被当成垃圾打包

不会进备份包。实测两个包的成员：

| 包类型 | zip 成员 |
| --- | --- |
| operational（`export`） | `manifest.json`、`council.db` |
| audit（`audit-export`） | `manifest.json`、`tasks.json`、`task_versions.json`、`reviews.json`、`approvals.json`、`events.json`、`audit.json` |

原因是快照走 SQLite 的 backup API（`bigfish/db.py:587` → `self._conn.backup(target)` + `PRAGMA journal_mode=DELETE`），只复制库内容，不复制目录。**锁文件不在任何包里。**

只有一种情况会把它带走：整目录 `cp -a` / `tar` / 资源管理器复制。带走了也无害——目标机上没有进程持有它，最多是个 0 字节的空文件，rc6 打开时会照常在上面的 `flock`（新 inode）工作。真要干净，拷贝后删一下即可。

### 2.5 恢复还会多出两个文件

恢复一次之后，目录里会多：

| 文件 | 来源 | 处置 |
| --- | --- | --- |
| `council.db.prerestore` | 替换前的活库副本（`restore_bundle` 里 `db.snapshot()` 生成） | 留着，这是「恢复错了还能回去」的唯一素材 |
| `council.db.restore-new.access.lock` | staging 库 `<db>.restore-new` 被 `Database(staging)` 打开时登记的租约，POSIX 不 unlink | 0 字节，可删；重复恢复不会累加 |

### 2.6 版本控制

仓库的 `.gitignore` 忽略 `data/`、`*.db`、`*.db-wal`、`*.db-shm`、`logs/`，**没有 `*.access.lock``。默认数据目录（`~/.bigfish-council` / `%LOCALAPPDATA%\BigFishCouncil`）本来就不在仓库里，所以平时看不到；只有把数据目录指到仓库内时，锁文件才会以未跟踪文件的形式出现。这种情况加一条 `*.access.lock` 到 `.gitignore` 即可，与程序行为无关。

---

## 3. 审计包 manifest 结构变了：新增 `credential_scan`

这一版**唯一需要下游适配的地方**。包格式没变（`backup_format` 仍是 4），但 `manifest.json` 多了一个键，`handling` 的措辞也改了。

### 3.1 逐键对照

| 键 | rc5 | rc6 | 变化 |
| --- | --- | --- | --- |
| `backup_kind` | 有 | 有 | — |
| `backup_format` | 4 | 4 | — |
| `app_version` | `0.1.1-rc5` | `0.1.1-rc6` | 值变 |
| `schema_version` | 4 | 4 | — |
| `created_at` / `created_at_iso` | 有 | 有 | — |
| `tables` / `table_counts` | 有 | 有 | — |
| `redaction` | 5 个子键 | 5 个子键 | 不变（`policy` / `allowed_columns` / `dropped_tables` / `dropped_columns` / `string_scrub`） |
| `contains_plaintext_credentials` | `false` | `false` | **布尔没删**，仍然是 `false` |
| `contains_credential_hashes` | `false` | `false` | — |
| `credential_scan` | **无** | **有** | 新增 |
| `handling` | 「可外发：无 token…」 | 「可外发（需人工复核）：…」 | 措辞变 |

键的**顺序**也变了：`credential_scan` 插在 `contains_credential_hashes` 与 `handling` 之间。rc5 是 12 键，rc6 是 13 键（实测 `list(manifest)`）。

operational 包（`export`）的 manifest **没有** `credential_scan`，仍是 16 键（含 `restore_epoch` / `db_file` / `db_sha256` / `db_size` / `includes_key_file` / `note`）。所以「manifest 多了键」只影响审计包。

### 3.2 `credential_scan` 的实际内容

实测（一个空库导出的审计包）：

```json
{
  "mode": "pattern-based",
  "patterns": ["bfc-token", "bearer", "openai-key", "github-token", "private-key",
                "aws-key", "google-key", "slack-token", "stripe-key",
                "key-value", "uri-userinfo"],
  "fields_scanned": "全部导出字符串列（含 task_versions.prompt 正文）",
  "coverage": "只覆盖上述已知形态；换行拆分、编码变形、私有格式的密钥无法保证命中",
  "hits": {},
  "manual_review_required": true
}
```

`hits` 是**脱敏之后**逐字段回扫的真实统计（键是模式名，值是命中次数）。空对象的意思是「导出内容里没命中这 11 类模式」，**不是**「保证没有凭据」。

### 3.3 下游脚本怎么改

三件事，按风险从高到低：

1. **不要再用 `contains_plaintext_credentials` 单独下结论。** 这个布尔在 rc6 仍然写着 `false`，但它的含义已经从「我们保证没有凭据」收缩成「11 类已知模式已脱敏」。正确读法是看 `credential_scan`：`mode` 是 pattern-based、`hits` 是空还是有东西、`manual_review_required` 是不是 `true`。
2. **把 `credential_scan` 当成可选键读，不要用「键数 == 12」这类判据。** 老包（rc5 导的）没有这个键，新包有；两种都要能处理。
3. **外发流程要加一道人工复核。** `manual_review_required: true` 不是装饰：它明确要求外发前有人过一遍 `hits` 与 `fields_scanned` 覆盖到的自由文本。

一个可以直接抄的判据片段：

```python
import json

def audit_bundle_is_safe_to_send(manifest: dict) -> tuple[bool, str]:
    """返回 (能不能直接外发, 理由)。注意：任何情况下都不等于「保证无凭据」。"""
    scan = manifest.get("credential_scan")
    if scan is None:
        # rc5 及更早的包：只有一句布尔，按「需人工复核」处理
        return False, "老包没有 credential_scan，必须人工复核"
    if scan.get("manual_review_required") is not False:
        hits = scan.get("hits") or {}
        if hits:
            return False, f"脱敏后仍命中：{', '.join(sorted(hits))}"
        return False, "模式扫描未命中，但仍是 pattern-based，需人工复核"
    return True, "manifest 显式声明无需复核"
```

（这段是示例写法，不是仓库里的代码。仓库里没有这个函数，别去 `grep`。）

### 3.4 `inspect` 命令不用改

`python -m bigfish.cli --data-dir <dir> inspect <pkg.zip>` 对两种包都照常工作，rc6 会把 `credential_scan` 原样打出来。实测 rc6 读 rc5 的审计包：`kind=audit`、manifest 可解析、没有 `credential_scan` 也不报错；rc5 读 rc6 的审计包：同样能解析，只是忽略那个新键。

---

## 4. 升级前必须知道的运行约束

### 4.1 必须先停干净旧实例

rc6 打开库时会登记写者租约，rc5 不会。所以**混跑期是单向不对称的**：

| 场景 | 结果 | 实测 |
| --- | --- | --- |
| rc6 持库，rc5 去打开 | 正常打开，因为 rc5 根本不看锁文件 | 通过（`exit 0`，任务可见） |
| rc5 持库，rc6 去恢复 | **恢复照样成功**——rc5 没登记租约，排他锁拿得到 | 通过（`epoch=1`，旧进程仍在跑） |

第二种就是 `docs/SECURITY-REVIEW-v0.1.1-rc6.md` 的 S7。它意味着：升级流程里「先停掉所有 rc5 实例，再起 rc6」这一步是**必须的**，不能靠 rc6 的租约替你拦。

### 4.2 病态目录会让 rc6 打不开库（rc5 能开）

两条实测出来的边界，都是环境问题不是数据问题：

| 情形 | rc6 表现 | rc5 表现 |
| --- | --- | --- |
| 数据目录不可写（`chmod 0555`）且锁文件不存在 | `PermissionError`（`bigfish/locks.py:51` 的 `open()` 抛出，不是 `LeaseError`） | 正常打开 |
| 锁路径被同名**目录**占住 | `IsADirectoryError`，CLI 外层表现 `FileExistsError: [Errno 17] File exists`、退出码 1 | 正常打开 |

处置：把数据目录权限改回可写、把误建的目录删掉，再启动。这两条在 `docs/MIGRATION-v0.1.1-rc4.md` 之后的版本里都没出现过，是 rc6 新增的行为，因此单独列。

### 4.3 请求体上限是新约束

rc6 加了 ASGI 层 body 上限与 REST 字段长度限制。默认值是 8 MiB / 4 MiB，可以用环境变量抬：

```bash
export BFC_MAX_BODY_BYTES=$((16*1024*1024))   # 默认 8 MiB
export BFC_MAX_TEXT_FIELD=$((8*1024*1024))    # 默认 4 MiB
```

如果你的执行器会回传很大的 `diff` 或 `log`（比如整份依赖树 diff），升级后可能第一次收到 413 / 422。这不是 bug，是新增的保护；按需抬上限，别直接关掉。**MCP 路径目前只有 body 上限、没有字段上限**（S9），所以同样的 4 MiB `log` 走 REST 会 422、走 `/mcp` 会 200。

---

## 5. 升级步骤

前提：你已经拿到 rc6 的代码（`git checkout ae2fe82` 或等价的分发包），并且**没有在跑任何旧的 bigfish 进程**。

| # | 步骤 | 命令 | 验证 |
| --- | --- | --- | --- |
| 1 | 停掉所有实例 | GUI 全关；Windows `powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1`；Linux `pkill -f "python -m bigfish"` | `ss -lntp` 里 3096 无监听 |
| 2 | 确认没有残留锁的持有者 | `ls -la <data-dir>/council.db.access.lock` | 文件存在也没关系（0 字节）；关键是没进程 |
| 3 | 整份备份数据目录 | `cp -a ~/.bigfish-council ~/.bigfish-council.before-rc6` | 目录存在、`council.db` 大小对得上 |
| 4 | 用 rc5 导出一份留档包 | `python -m bigfish.cli --data-dir <dir> export ~/bfc-rc5-before-rc6.zip` | `inspect` 能看到 `app_version: 0.1.1-rc5` |
| 5 | 切到 rc6 代码 | `git checkout ae2fe82`（或解压新包） | `.venv/bin/python -c "import bigfish; print(bigfish.__version__)"` → `0.1.1-rc6` |
| 6 | 确认 `locks.py` 存在 | `ls bigfish/locks.py` | 157 行 |
| 7 | 启动一次（会自动建锁文件） | `python -m bigfish.cli --data-dir <dir> overview` | `exit 0`，schema 仍是 4 |
| 8 | 看锁文件是否生成 | `ls -la <data-dir>/council.db.access.lock` | 0 字节 |
| 9 | 跑全量测试 | 见第 7 节 | `Ran 265 tests` / OK |
| 10 | 起 GUI 或 headless | 按平常方式 | `/health` 正常，任务列表可见 |
| 11 | 若执行端在用 `ExecutorClient` | 一并更新到 rc6 版本 | 收据日志照常写 |

第 7 步的「会自动建锁文件」是 rc6 与 rc5 唯一可见的启动差异。如果你看到 `PermissionError` 或 `FileExistsError`，对照 4.2 那两条。

---

## 6. 升级前检查清单

| # | 检查项 | 判据 |
| --- | --- | --- |
| 1 | 当前版本 | `bigfish.__version__` 是 `0.1.1-rc5` |
| 2 | 当前 schema | `sqlite3 <db> "SELECT value FROM meta WHERE key='schema_version'"` → `4` |
| 3 | 没有进程在跑 | `ss -lntp` 无 3096；`ps` 里没有 `python -m bigfish` |
| 4 | 数据目录可写 | `touch <data-dir>/.probe && rm <data-dir>/.probe` 成功 |
| 5 | `council.db.access.lock` 不是目录 | `test -f` 为真（不存在也算过） |
| 6 | 已整目录备份 | `~/.bigfish-council.before-rc6` 存在 |
| 7 | 已导出 rc5 格式留档包 | `inspect` 显示 `0.1.1-rc5` |
| 8 | 磁盘余量够两份库 | `df -h`；库通常几十 MB 量级 |

---

## 7. 升级后验证

```bash
cd /home/dsh/bigfish-council

# 版本与文件
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"   # 0.1.1-rc6
ls -l bigfish/locks.py                                            # 157 行

# schema 未变
.venv/bin/python -m bigfish.cli --data-dir <data-dir> overview    # exit 0，任务可见
sqlite3 <data-dir>/council.db "SELECT value FROM meta WHERE key='schema_version'"   # 4

# 锁文件已生成、且是空文件
ls -l <data-dir>/council.db.access.lock                            # 0 字节

# 全量回归（265 项）
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

| 检查 | rc6 的期望值 |
| --- | --- |
| 全量测试 | `Ran 265 tests` / `OK` / 退出码 0 |
| `tools/selfcheck.py` | 通过 16 项、失败 0 项 |
| `tools/gui_smoke.py`（offscreen） | 通过 9 项、失败 0 项 |
| `pyflakes bigfish tests tools` | 零输出、退出码 0 |
| `compileall -q bigfish tests tools` | 退出码 0 |
| 打开库后的目录 | 多一个 `council.db.access.lock`（0 字节） |
| 恢复一次后的目录 | 再多 `council.db.prerestore` 与 `council.db.restore-new.access.lock` |

---

## 8. 升级失败怎么办

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'bigfish.locks'` | 只换了部分文件（比如只拷了 `db.py`），没换整树 | 整树切到 rc6，别手工拼文件 |
| `PermissionError` 打不开库 | 数据目录不可写（4.2） | 改权限；这是 rc6 新增的检查路径 |
| `FileExistsError: [Errno 17] File exists: '.../council.db.access.lock'` | 锁路径被同名目录占了（4.2） | 删掉那个目录再启动 |
| `LeaseError: 无法登记数据库写者租约（…）` | 锁文件所在目录不可写或磁盘满 | 查 `df -h` 与目录权限 |
| `RestoreError: 还有进程在用这个数据库（写者租约仍在生效）…` | 恢复时还有 rc6 实例（含 GUI）开着 | 全关掉再恢复；别用 `--force` 绕（它只跳端口探测，跳不过租约） |
| 恢复或提交开始返回 413 | 请求体超过 8 MiB | 抬 `BFC_MAX_BODY_BYTES`（4.3） |
| 恢复或提交开始返回 422 | 字段超过 4 MiB | 抬 `BFC_MAX_TEXT_FIELD`（4.3） |
| 启动时报「执行票密钥一致性校验失败，拒绝启动」 | 密钥指纹不一致且无法自愈（F-03：rc6 不再静默继续） | 查 `secrets/ticket.key` 与 `meta.key_sha256`；这条是 rc6 明确要求的 fail-closed |
| 下游脚本解析 manifest 报 `KeyError: 'handling'` 之类 | 按固定键序读 manifest | 改成按键名读，见 3.3 |

---

## 9. 实测记录

全部来自本批次在 Debian 12 / Python 3.11.2 / SQLite 3.40.1 上的真实执行。

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| rc5 库 → rc6 打开 | rc5 建的库（schema 4、1 个任务）给 rc6 `overview` | 成功，schema 4，任务可见，`exit 0`；目录里新出现 `council.db.access.lock` |
| rc6 库 → rc5 打开 | rc6 建的库（已留锁文件）给 rc5 `overview` | 成功，打印 `0.1.1-rc5`，任务正常，`exit 0`；锁文件原样留着 |
| rc5 持库 → rc6 恢复 | rc5 进程打开库并 `sleep`，rc6 子进程调 `restore_from_bundle` | **恢复成功**（`epoch=1`）——S7，混跑期必须停干净 |
| rc6 持库 → rc5 恢复 | rc6 进程持库，rc5 恢复同一个包 | rc5 没有任何租约检查，照常执行 |
| 锁文件大小与生命周期 | 打开 → 看目录 → 关库 → 再看 | 打开后 0 字节；关库后仍存在、仍 0 字节 |
| 删锁文件后重开 | 关库 → `unlink` → `Database(path)` | 正常打开，锁文件自动重建 |
| 带残留锁文件拷贝 | 把库 + 别人留下的 0 字节锁拷到新目录再打开 | 正常打开，任务数正确，恢复成功 |
| 备份包成员 | `zipfile` 读两个包 | operational: `manifest.json` + `council.db`；audit: manifest + 6 个 json；**都不含锁文件** |
| manifest 键数 | `list(manifest)` | audit 13 键（含 `credential_scan`）；operational 16 键（无 `credential_scan`） |
| rc5 restore rc6 的包 | rc5 `restore` 一个 rc6 导出的 operational 包 | `restored=True epoch=1`，`exit 0` |
| rc6 restore rc5 的包 | rc6 `restore` 一个 rc5 导出的 operational 包 | `restored=True epoch=1`，`exit 0`，返回 manifest 16 键无 `credential_scan` |
| 混合文件 | 只把 rc6 的 `db.py` 拷进 rc5 树 | `ModuleNotFoundError: No module named 'bigfish.locks'` |
| 恢复后的目录 | 恢复一次、再恢复一次 | 第一次多 `.prerestore` 与 `.restore-new.access.lock`；第二次不累加 |
| 全量测试（rc6） | `unittest discover` | `Ran 265 tests` / `OK` |
| 全量测试（rc5） | 同上，在 rc5 树上 | `Ran 244 tests` / `OK` |

Windows 侧（含 `msvcrt` 租约分支）本批次**未跑**，升级流程在 Windows 上属于未验证。上真机时先跑 `tools/selfcheck.py` 与 `tools/gui_smoke.py`，再动业务数据。

---

## 10. 与其它行为变化的交叉引用

升级除了多一个锁文件，还带来几处行为变化，迁移时要一并知道：

| 变化 | 影响 | 文档 |
| --- | --- | --- |
| 打开库即登记写者租约（F-06） | 恢复前必须没有别的 rc6 实例；混跑期 rc5 实例不被拦 | 本文第 2、4 节；`SECURITY-REVIEW-v0.1.1-rc6.md` S7 |
| GUI 停服超时改为报错（F-05） | 恢复时如果 API 线程 15 秒内没退出，会看到「停服失败，已中止本次操作」而不是静默继续 | `SECURITY-REVIEW-v0.1.1-rc6.md` F-05 |
| 审计包 manifest 加 `credential_scan`（F-07） | 下游解析要适配 | 本文第 3 节 |
| 请求体 / 字段上限（F-09） | 大 `diff` / `log` 可能第一次 413 或 422 | 本文 4.3 |
| 密钥自愈失败改为拒绝启动（F-03） | 以前静默继续，现在 `CouncilError` + 退出 | `SECURITY-REVIEW-v0.1.1-rc6.md` 第三节 Q4 附近 |
| 恢复世代读数修正（F-02） | `restore_epoch` 回到真正的单调递增；以前恢复旧快照会让它变小 | `SECURITY-REVIEW-v0.1.1-rc6.md` Q4 |
