# BFC v0.1.1-rc7 迁移说明

| 项目 | 值 |
| --- | --- |
| 起点 | `v0.1.1-rc6`（也适用于 rc1-rc5，见 §5） |
| 终点 | `v0.1.1-rc7`（分支 `fix/v0.1.1-rc7-hardening`） |
| 库结构变化 | **无**。`SCHEMA_VERSION` 仍是 4，`BACKUP_FORMAT_VERSION` 仍是 4 |
| 是否需要人工介入 | 不需要跑迁移脚本；只有「想用 mobile 角色」时需要手工建一次身份 |
| 能否直接降级 | 能。库结构没动，rc6 可以原样打开 rc7 写过的库（例外见 ROLLBACK 文档的 mobile 段） |
| 已验证平台 | Debian 12 / Python 3.11.2 / SQLite 3.40.1（295 项 OK） |
| 已验证平台 | Windows 11 / Python 3.14.6 / SQLite 3.50.4（rc7 的 294 项全绿：`Ran 294 tests in 74.232s` / `OK (skipped=1)`；收尾补的第 295 项未在 Windows 重跑） |

## 1. 结论：这次不需要迁移数据库

rc7 的九条改动全在进程行为、文件格式、角色矩阵和发布工程上，**没有一条动 SQLite 结构**。
`bigfish/db.py:24` 的 `SCHEMA_VERSION = 4` 与 `bigfish/db.py:25` 的 `BACKUP_FORMAT_VERSION = 4` 和 rc6 完全一致，
`MIGRATIONS`（`bigfish/db.py:176-212`）里也**没有新增第 5 步**——最高一步仍是 4。

| 常量 | rc6 | rc7 | 差异 |
| --- | --- | --- | --- |
| `SCHEMA_VERSION` | 4 | 4 | 无 |
| `BACKUP_FORMAT_VERSION` | 4 | 4 | 无 |
| `MIGRATIONS` 最高版本 | 4 | 4 | 无 |
| 表 / 列 / 索引 | 同 rc6 | 同 rc6 | 无 |

### 1.1 为什么「打开 rc7 写过的库」不会触发迁移

`Database.init_schema()` 判断三步（`bigfish/db.py:429-439`）：

1. `current > SCHEMA_VERSION` → 抛 `DatabaseVersionError`，拒绝打开。
2. `current == SCHEMA_VERSION` → 直接 `executescript(SCHEMA)`（建表语句全是 `IF NOT EXISTS`，天然幂等）加 `INSERT OR IGNORE` 补元数据，**一条 `ALTER TABLE` 都不执行**。
3. 否则按 `range(current + 1, SCHEMA_VERSION + 1)` 逐条跑 `MIGRATIONS`。

rc7 写过的库停在 4，rc7 自己再打开时走的是第 2 条：不迁移。

### 1.2 一个容易被忽略的点：写者租约先于版本判断

`Database.__init__` 的顺序是「先登记写者租约，再读 schema 版本」（`bigfish/db.py:238` 与 `:255-263`）。
所以某个进程因版本不匹配被拒时，它也已经把租约登记过又释放了，不会留下僵尸锁。
排查「打开失败」时要知道：失败信息是 `DatabaseVersionError`，不是 `LeaseError`。

---

## 2. 依赖锁 `requirements.lock` 是新增的

rc7 之前项目只有 `requirements.txt`，里面是声明式范围（`fastapi>=0.115,<1.0`、`uvicorn>=0.30,<1.0`、`pydantic>=2.7,<3.0`、`PySide6>=6.7,<7.0`、`mcp>=1.9,<2.0`）。
同一份文件，今天装和三个月后装拿到的可能是两个 fastapi 版本。
`requirements.lock` 把整棵依赖树的传递依赖全部钉死成 `==`，并写清生成环境（Debian 12 / Python 3.11.2 / 2026-11-20）与生成方式（`pip freeze` 后剔掉 `pyflakes` / `pip` / `setuptools` / `wheel`）。

### 2.1 旧环境不改也能跑

**`requirements.txt` 没有被删、也没有被改。** 老机器上原来怎么装，rc7 之后照旧怎么装：

```bash
.venv/bin/python -m pip install -r requirements.txt   # 老办法，仍然有效
.venv/bin/python -m pip install -r requirements.lock  # 想要可复现时用这个
```

`tools/dev_setup.sh` 与 `setup.ps1` 两个一键脚本里装的也还是 `requirements.txt`——本轮**没有**把它们改成 lock，
所以「一键装完能不能跑」这条路径和 rc6 完全一样，不存在迁移断点。

### 2.2 三个已知点

| 点 | 说明 |
| --- | --- |
| PySide6 | lock 里是一个具体版本号，Windows 上装的是同一版本号的 `cp310-abi3` wheel，头注释里写了「已实测」 |
| Python 版本 | lock 是在 3.11.2 上 freeze 的。3.13 上部分包可能没有对应 wheel，CI 的 3.13 那一格因此装的是 `requirements.txt` 而不是 lock |
| 头注释 | lock 的头注释已同步为「重跑全量测试（295 项）」，与 rc7 实际数一致（早期一版写的是 265，已在发布前修正） |

---

## 3. 新增 mobile 角色：旧库不受影响

### 3.1 这条改的是什么

`bigfish/roles.py:15` 加了 `MOBILE_VIEWER = "mobile_viewer"`，权限只有 `task.read` / `event.read` / `review.create`（`:46`），
`_DB_ROLE_MAP` 加了 `"mobile" → MOBILE_VIEWER`（`:54-55`）。`core.py` 的白名单从硬编码三元组改成查 `R.DB_ROLES`（`bigfish/core.py:163-164`）。

### 3.2 旧库为什么不受影响

mobile 不是 schema 的一部分，它是 `executors` 表里 `role` 字段的一个**取值**。
`executors` 表本来就有 `role` 列（rc6 里存 `executor` / `reviewer` / `admin`），列类型、约束、索引一概没变。所以：

- 手上已有的库，`executors` 里一行 `mobile` 都没有 → 打开、读写、导出，行为与 rc6 完全一致。
- 只有当你**显式调用** `register_executor(name=..., role="mobile")` 时，库里才会多出一条 `role = "mobile"` 的行。
- 不调用就不会有，不存在「升级时自动建一个 mobile 身份」这种副作用。

### 3.3 怎么建、建了之后有什么

```python
from bigfish.core import Council
info = council.register_executor("我的手机", role="mobile")
# 返回 {"id": "mobile-xxxxxxxx", "name": "我的手机", "role": "mobile", "token": "bfc_mb_..."}
```

`token` 只在这一刻返回一次，库里存的是 `token_hash`（SHA-256），前缀是 `bfc_mb_`（`bigfish/core.py:173-174`）。
**CLI 还没开放这个参数**：`bigfish/cli.py:79` 的 `--role` 选项仍是 `executor / reviewer / admin`，命令行建不出来，只能用上面的代码或走 API。这是已知缺口，见交付文档 U6。

### 3.4 这个身份不能做什么

| 通道 | 结果 |
| --- | --- |
| `council_list_tasks` / `council_claim_task` | 拒（需要 `task.claim`） |
| `council_heartbeat` | 拒（需要 `run.heartbeat`） |
| `council_submit_result` | 拒（需要 `run.submit`） |
| 导出备份 / 审计包 | 拒（没有 `backup.*`） |
| 提 review | 允许（`review.create`） |
| 读任务、读事件 | 允许 |

`test_mobile_token_cannot_use_executor_channel` 钉的就是「拿 `bfc_mb_` 令牌去执行器通道会被挡」。

---

## 4. `.github/` 与 `requirements.lock` 只是仓库文件

这两个是 rc7 新增的**纯仓库产物**，不参与运行时：

| 文件 | 运行时影响 | 说明 |
| --- | --- | --- |
| `.github/workflows/ci.yml` | **零**。本地不会执行它 | 只有推到远端（push 到 `main` / `fix/**` / `review/**`，或开 PR、手动 dispatch）才会跑 |
| `requirements.lock` | **零**。没有任何代码 `import` 或读取它 | 只有人手动 `pip install -r requirements.lock` 时才起作用 |

换句话说：把一个 rc6 的部署目录原样换成 rc7 的 `bigfish/`，**不拷这两个文件也能跑**。
它们解决的是「别人能不能复现我的环境」和「改动有没有被自动检查」，不是「程序能不能启动」。

CI 里那个 `security-note` job 同性质：它扫的是**仓库内容**——`git ls-files` 拒绝 `.db` / `.key` / `.pem` / `.zip` / `.exe` 入库，
`git grep` 扫 `ghp_` 令牌、`bfc_` 前缀令牌与 PEM 头，命中即失败。和运行中的库没有关系。

---

## 5. 从 rc1-rc5 直接迁到 rc7

rc6 → rc7 是零迁移。但如果你手上是一个更老的库（schema 1 / 2 / 3），迁到 rc7 要一次跨过全部累积步骤。
好消息是：**你不需要手写任何迁移 SQL**，`init_schema()` 会按顺序跑完。

### 5.1 累积迁移表（`bigfish/db.py:176-212`）

| 从 → 到 | 做了什么 |
| --- | --- |
| 1 → 2 | `reviews` 加 `actor`（默认 `human_owner`）与 `revoked_at`；`approvals` 加 `actor` 与 `evidence`；`tickets` 加 `max_expires_at` 与 `renewed_at`；`idempotency` 加 `request_hash` 与 `expires_at`；新建 `audit` 表与 `idx_audit_created` |
| 2 → 3 | `executors` 加 `model`；`tickets` 加 `epoch`（默认 0）；`tasks` 加 `cancel_requested_at`；加 `idx_tickets_executor` 与 `idx_idem_expires` |
| 3 → 4 | `executors` 加 `disabled_reason`；加 `idx_executors_enabled` |

rc7 没有 4 → 5。所以一个 schema 1 的库打开时，跑的是 2、3、4 三步，跑完停在 4，正是 rc7 要的版本。

| 起点 | 需要经过 | 备注 |
| --- | --- | --- |
| schema 1（rc1/rc2 时期） | 1→2→3→4 | 三步连跑，中途任何一步失败都不会写入半个库 |
| schema 2（rc3 时期） | 2→3→4 | 两步 |
| schema 3（rc4/rc5 时期） | 3→4 | 一步 |
| schema 4（rc6 时期） | 不迁移 | 直接 `executescript(SCHEMA)`，全是 `IF NOT EXISTS` |

### 5.2 旧备份也能恢复进 rc7

`restore_bundle` 会把备份解到 staging，在 staging 上 `init_schema()`——**旧备份就在这一步被有序迁移**（`bigfish/backup.py:502-525`）：

```text
_verify_version → Database(staging) → staged.init_schema() → _verify_snapshot → _verify_relational
```

所以一个 rc3 时期导出的包，直接恢复到 rc7 上是可以的，中间几步由 staging 自动跑。
反过来不行：`_verify_version` 会拦住「备份 schema 高于当前程序支持」（`bigfish/backup.py:261`），
报「请先升级程序，禁止把高版本库恢复进低版本程序」。

---

## 6. 迁移步骤

rc6 → rc7 实际要做的只有「换代码 + 装依赖 + 重开服务」。

| 步骤 | 命令 | 验证 |
| --- | --- | --- |
| 1. 停服务 | 关 GUI、停 headless | 无 `python` 进程占着库 |
| 2. 备份 | `python -m bigfish.cli export before-rc7.zip` | zip 里有 `manifest.json` 与 `council.db` |
| 3. 记版本 | 读 `meta.schema_version`，应为 4 | 见 §7 检查清单 |
| 4. 换代码 | 切到 rc7 的 `bigfish/` | `bigfish/__init__.py` 里是 `0.1.1-rc7` |
| 5. 装依赖 | `pip install -r requirements.txt`（或 `-r requirements.lock`） | 无报错 |
| 6. 起服务 | `start-gui.ps1` 或 headless | 3096 能连，且只听 127.0.0.1 |
| 7. 跑测试 | `python -m unittest discover -s tests -t .` | 295 项 OK |
| 8. 自检 | `python tools/selfcheck.py` | 16 通过 0 失败 |
| 9. （可选）建 mobile | `register_executor(role="mobile")` | 见 §3.3 |

第 3 步值得单独说：**先记下旧库的 schema 版本**。万一后面出问题，你需要知道「本来应该是几」才能判断是迁移没跑还是跑错了。

---

## 7. 迁移前后检查清单

### 7.1 迁移前（八项判据，全过再动）

| # | 检查 | 判据 |
| --- | --- | --- |
| 1 | 服务真的停了 | 没有进程占着库；`exclusive_access` 能拿到锁 |
| 2 | 备份可读 | zip 能打开，`manifest.json` 里 `backup_kind` 不是审计包 |
| 3 | 备份的 `schema_version` | 应 ≤ 4；大于 4 说明这包来自更高版本程序，别往 rc7 恢复 |
| 4 | 密钥文件在位 | `data_dir/secrets/ticket.key`（`bigfish/config.py:105`）存在且权限收紧 |
| 5 | 库完整性 | `PRAGMA integrity_check` 返回 `ok` |
| 6 | 旧版本号记下来 | `meta.schema_version` 与 `meta.restore_epoch` 各记一份 |
| 7 | 端口没被占 | 3096 / 3098 上没有别的进程 |
| 8 | Python 版本够 | 3.11 以上；lock 是在 3.11.2 上生成的 |

### 7.2 迁移后（五项确认）

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `SCHEMA_VERSION` | 仍是 4 |
| 2 | `PRAGMA integrity_check` | `ok` |
| 3 | 全量测试 | 295 项 OK |
| 4 | 自检 | 16 通过 0 失败 |
| 5 | 监听地址 | 3096 只听 127.0.0.1，`allowed-ports` 打出 implemented 与 reserved 两段 |

读这几项用 Python 而不是 `sqlite3` CLI（很多环境没装 CLI）：

```python
import sqlite3
con = sqlite3.connect("council.db")
print(con.execute("PRAGMA integrity_check").fetchone()[0])
print(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
print(con.execute("SELECT value FROM meta WHERE key='restore_epoch'").fetchone()[0])
con.close()
```

---

## 8. 迁移失败怎么办

### 8.1 `DatabaseVersionError`

**现象**：启动即抛 `DatabaseVersionError`，消息是「数据库版本高于程序版本：拒绝打开，绝不静默降级」（`bigfish/db.py:215` 的 docstring）。
**原因**：库的 `schema_version` 大于程序支持的 4。最常见的是「把新版本写过的库拿给旧版本程序开」。
**处置**：换回产生这个库的那个（或更新的）程序版本。**不要**手工改 `meta.schema_version` 去骗过程序——那等于让旧代码读新结构。

### 8.2 迁移中途失败

**现象**：`init_schema()` 在某一步抛错，进程起不来。
**处置**：停掉所有进程 → 把出问题的库挪开（改名而不是删）→ 用迁移前的备份恢复，或用 `restore_bundle` 把 `before-rc7.zip` 恢复回来 → 换回旧程序先跑起来，再查为什么那一步会失败。

`restore_bundle` 自带回滚：替换失败时会把 `.prerestore` 快照写回去，并把 `keep_maintenance` 置真（`bigfish/backup.py:597-640`）。
如果回滚本身也没成，它会抛「恢复失败且回滚未完全成功：…」，这时别反复重试，按 ROLLBACK 文档 §6 的人工处置走。

### 8.3 恢复时报「还有进程在用」

**现象**：恢复时报错说还有进程在用这个库。
**原因**：还有别的 `Database` 实例活着。rc7 的互斥现在是**同一个内核对象**（`bigfish/locks.py:200-264`），
有写者占着共享锁（POSIX `LOCK_SH` / Windows 槽位）时，恢复端拿不到排他锁，会等到超时（默认 5 秒）才报错。
**处置**：把 GUI 和 headless 都关掉，确认没有残留进程，再重试。锁文件本身不会被删（设计如此，`test_lock_files_are_never_deleted` 钉着），别去手工删它。

---

## 9. 实测记录

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| rc6 库用 rc7 打开 | 直接 `Database(路径)` | 不触发迁移，`init_schema` 走 `current == SCHEMA_VERSION` 分支 |
| 全量单测 | `unittest discover -s tests` | `Ran 295 tests ... OK`，RC=0 |
| 旧备份恢复 | `restore_bundle` 在 staging 上 `init_schema()` | 旧版本在 staging 内被迁移，再整体替换 |
| 审计包误用于恢复 | `restore_bundle(审计包)` | 抛 `RestoreError("这是脱敏审计包，不能用于恢复运行数据库")` |
| 高版本备份往低版本恢复 | 构造 `schema_version > 4` 的包 | `_verify_version` 拦住，提示先升级程序 |

注意第一行：**这是本轮 295 项里覆盖到的路径**，不是在一台真实生产机上跑过的。生产机的首次升级仍建议按 §6 走一遍。

---

## 10. 交叉引用

| 想知道 | 去哪看 |
| --- | --- |
| rc7 到底改了什么、测试怎么跑 | `docs/DELIVERY-v0.1.1-rc7.md` |
| 怎么退回 rc6、mobile 身份的兜底风险 | `docs/ROLLBACK-v0.1.1-rc7.md` |
| 上一版的迁移说明 | `docs/MIGRATION-v0.1.1-rc6.md` |
| 备份/恢复的代码级行为 | `bigfish/backup.py`（`restore_bundle` 的 docstring 五步） |
| 迁移 SQL 原文 | `bigfish/db.py:176-212` 的 `MIGRATIONS` |
| 角色与权限矩阵 | `bigfish/roles.py` |

一句话：**rc6 → rc7 不需要迁移库**，只要换代码、装依赖、重开；
要迁移的只有更老的库，而那部分由 `init_schema()` 在打开时自动按序跑完，你只管先备份。
