# 回滚说明：v0.1.1-rc6 → v0.1.1-rc5

| 项 | 值 |
| --- | --- |
| 当前 | `0.1.1-rc6`（`ae2fe82`，`SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`） |
| 目标 | `0.1.1-rc5`（发布点 `c6d663d`，`SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`） |
| 能不能只换代码 | **能**。schema 两边都是 4，数据库不用动 |
| 能不能只换库 | 不需要。本版没有动过库结构 |
| 正确形态 | 停干净所有实例 → 代码整树回到 `c6d663d` → 删 `locks.py`（回退时自动消失）→ 清遗留 `.access.lock` 文件 → 启动验证 schema 仍是 4 |
| 已验证平台 | Debian 12 / Python 3.11.2 / SQLite 3.40.1 与 Windows 11 / Python 3.13.11 / SQLite 3.50.4（265 项全绿）。**注意**：Windows 侧恢复相关的两条路径本轮改动过——`bigfish/locks.py` 的同进程句柄复用（缺陷 17）与 `cli.py` restore 分支的 `finally: db.close()`（缺陷 18）——回滚到 rc6 之前的版本会把这两条修复一起退掉 |

本文只描述「从 rc6 退回 rc5」这一个方向。升级见 `docs/MIGRATION-v0.1.1-rc6.md`。

与 rc5→rc4 那次回滚相比，这一次**容易得多**：那次要连库一起换（schema 4→3），这次 schema 没变。

---

## 0. 先说结论

| 场景 | 能否直接换代码 | 正确做法 |
| --- | --- | --- |
| rc6 只启动过、没恢复过 | **能** | 停进程 → 代码回 `c6d663d` → 删 `.access.lock` → 启动 |
| rc6 期间恢复过备份 | **能**，但要知道后果 | 恢复会轮换密钥、作废票、禁用身份；回 rc5 之后这些**不会自动恢复**，需要 `reenable` |
| 手上有 rc6 导出的 operational 包 | 可以给 rc5 用 | 实测 rc5 `restore` rc6 的包：`restored=True epoch=1`、退出码 0 |
| 手上有 rc6 导出的审计包 | 可以给 rc5 看 | 实测 rc5 `inspect`：`kind=audit`、能解析 manifest（`credential_scan` 被忽略） |
| 想保留 rc6 期间产生的新数据 | 可以直接保留 | schema 没变，rc5 读得懂 |
| 只想把 `locks.py` 删掉、其余留 rc6 | **不行** | `db.py:22` 有 `from .locks import WriterLease`，删了直接 `ModuleNotFoundError`（实测） |

核心原因只有一条：**schema 没变，所以库不用动**。这跟 rc5→rc4 完全不同——那次 rc4 打不开 schema 4 的库，只能换库；这次 rc5 打不开 rc6 的库这件事根本不存在。

---

## 1. 回滚前必须做的三件事

| # | 事项 | 命令 | 为什么 |
| --- | --- | --- | --- |
| 1 | **停掉所有写库的进程** | GUI 全关；Windows `powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1`；Linux `pkill -f "python -m bigfish"`；再 `ss -lntp` 确认 3096 无监听 | rc5 不看锁文件，所以「rc6 还在跑、rc5 直接开」不会报错，但两边会同时写同一个库。停干净是唯一可靠的办法 |
| 2 | **整份备份当前数据目录** | `cp -a ~/.bigfish-council ~/.bigfish-council.before-rollback` | 回滚本身也可能失败，要能回到 rc6 的状态 |
| 3 | **导出一份 rc6 格式的包** | `.venv/bin/python -m bigfish.cli --data-dir <dir> export ~/bfc-rc6-before-rollback.zip` | 留着是为了「万一还想回 rc6」，rc5 也能恢复它（实测） |

备份范围（默认 `~/.bigfish-council`，Windows 上 `%LOCALAPPDATA%\BigFishCouncil`）：

```
council.db   council.db-wal   council.db-shm   config.json   secrets/ticket.key   logs/
council.db.access.lock            # rc6 新增，0 字节，备份不备份都行
council.db.prerestore             # rc6 恢复过的才有
council.db.restore-new.access.lock  # rc6 恢复过的才有，0 字节
```

后三个是 rc6 才有的东西，加起来几百字节。整目录备份会一起带上，无害；不想要就单独排除。

---

## 2. 回滚步骤

### 2.1 前置条件

| 项 | 判据 |
| --- | --- |
| 当前版本 | `bigfish.__version__` 是 `0.1.1-rc6` |
| 目标提交存在 | `git cat-file -t c6d663d` → `commit` |
| 没有进程在跑 | `ss -lntp` 无 3096；`ps` 里没有 `python -m bigfish` |
| 工作区状态已知 | `git status --porcelain` 的输出你认得（**这是最容易踩的坑**，见 2.3） |

### 2.2 步骤

```bash
cd /home/dsh/bigfish-council

# 1) 停服务（GUI 全关；headless 用 pkill）
pkill -f "python -m bigfish" || true
ss -lntp | grep 3096 || echo "3096 无监听，OK"

# 2) 备份数据目录
cp -a ~/.bigfish-council ~/.bigfish-council.before-rollback

# 3) 用 rc6 导出一份留档包
.venv/bin/python -m bigfish.cli --data-dir ~/.bigfish-council \
  export ~/bfc-rc6-before-rollback.zip

# 4) 处理工作区（有未提交改动时必须先做，见 2.3）
git status --porcelain

# 5) 代码整树回到 rc5 发布点
git checkout c6d663d

# 6) 确认锁模块已经随代码消失
ls bigfish/locks.py || echo "locks.py 已不存在，OK"
grep -n '__version__' bigfish/__init__.py     # 0.1.1-rc5

# 7) 清掉遗留的锁文件（可选，见第 3 节）
rm -f ~/.bigfish-council/council.db.access.lock
rm -f ~/.bigfish-council/council.db.restore-new.access.lock

# 8) 启动验证
.venv/bin/python -m bigfish.cli --data-dir ~/.bigfish-council overview
```

第 5 步之后 `bigfish/locks.py` 会自动消失——它只存在于 rc6 的提交里，`git checkout c6d663d` 会把工作树换成 rc5 的树，那个文件不在其中。**不需要手工 `rm`**（但确认一下没坏处，见第 6 步）。

### 2.3 脏工作区是这一步唯一的高概率失败点

实测：当前仓库有未提交改动（`AUDIT.md` / `CHANGELOG.md` / `README.md` 被修改，`docs/` 下几个未跟踪的 rc6 文档）。在这种状态下直接 `git checkout c6d663d` 会失败：

```
error: Your local changes to the following files would be overwritten by checkout:
	CHANGELOG.md
Please commit your changes or stash them before you switch branches.
Aborting
```

HEAD 不动，什么都没坏，但也什么都没回滚。三条出路，选一条：

| 做法 | 命令 | 适用 |
| --- | --- | --- |
| 暂存改动 | `git stash push -u -m "pre-rollback"` → `git checkout c6d663d` | 想保留这些改动，之后还要 pop 回来 |
| 丢弃改动 | `git checkout -- <file>` 或 `git restore <file>` | 确认这些改动不要了 |
| 换个目录回退 | `git clone /home/dsh/bigfish-council /tmp/bfc-rc5 && cd /tmp/bfc-rc5 && git checkout c6d663d` | 完全不想动当前工作区 |

实测第二条路：脏工作区 → `git checkout` 失败（rc=1，HEAD 仍是 `ae2fe82`）→ `git stash push -u` 成功 → `git checkout c6d663d` 成功（rc=0，版本变 `0.1.1-rc5`、`locks.py` 消失）→ 切回分支 `git stash pop` 也成功。

**不要 `git reset --hard`**：它会连未跟踪文件一起处理，而且不打 stash 的话改动就真没了。

---

## 3. 遗留的锁文件怎么处理

### 3.1 是什么

| 文件 | 谁建的 | 内容 |
| --- | --- | --- |
| `council.db.access.lock` | rc6 `Database.__init__` 里的 `WriterLease` | 0 字节纯标记，靠 `flock` 的 inode 生效 |
| `council.db.access.<pid>.lock` | 同上，Windows 分支 | 0 字节，一个进程一个 |
| `council.db.restore-new.access.lock` | rc6 恢复时打开 staging 库留下的 | 0 字节 |

### 3.2 rc5 会不会被它们绊倒

不会。实测：rc6 建的库（目录里留着 0 字节锁文件）给 rc5 `overview`，成功、任务可见、退出码 0，锁文件原样留着——rc5 既不看也不删。

所以**删不删都不影响功能**。删的理由只有两个：目录干净、少一个让人误会的文件。

### 3.3 怎么删才安全

前提：**没有任何进程在用这个库**。

```bash
# 先确认没进程
pgrep -af "python -m bigfish" || echo "没有实例在跑"

# 再删
rm -f ~/.bigfish-council/council.db.access.lock
rm -f ~/.bigfish-council/council.db.access.*.lock
rm -f ~/.bigfish-council/council.db.restore-new.access.lock
```

删除是安全的，因为它们是标记文件不是数据文件：实测「关库 → `rm` → 重新 `Database(path)`」正常打开、锁文件自动重建；「带着别人留下的 0 字节锁文件拷到新目录 → 打开」也正常。

**不要**在 rc6 还在跑的时候删：实测库开着时手工 `unlink` 锁文件，`exclusive_access()` 会立刻成功，等于主动把那道跨进程保护关掉。

### 3.4 `.prerestore` 不要删

`council.db.prerestore` 不是锁文件，它是 rc6 每次恢复前打的活库副本。回滚到 rc5 之后它依然有用——「万一 rc6 那次恢复恢复错了」，这是唯一能回去的素材。留到确认数据没问题再删。

---

## 4. 回滚后必做的三项确认

| # | 确认 | 命令 | 期望 |
| --- | --- | --- | --- |
| 1 | 版本回到 rc5 | `.venv/bin/python -c "import bigfish; print(bigfish.__version__)"` | `0.1.1-rc5` |
| 2 | schema 仍是 4 | 见下方脚本 | `4` |
| 3 | 业务数据还在 | `overview` 与 `task-list` | 任务数、状态与回滚前一致 |

环境里没有 `sqlite3` 命令行工具（实测 `sqlite3: command not found`），用 Python 读：

```bash
.venv/bin/python - <<'PY'
import sqlite3, pathlib
db = pathlib.Path.home() / ".bigfish-council" / "council.db"
conn = sqlite3.connect(str(db))
print("schema_version =", conn.execute(
    "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
print("restore_epoch  =", conn.execute(
    "SELECT value FROM meta WHERE key='restore_epoch'").fetchone()[0])
print("integrity      =", conn.execute("PRAGMA integrity_check").fetchone()[0])
print("tasks          =", conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
conn.close()
PY
```

期望输出：

```
schema_version = 4
restore_epoch  = <回滚前的值，不回退>
integrity      = ok
tasks          = <回滚前的任务数>
```

### 4.1 确认 244 项还能跑

这是「回滚是否真的成功」最硬的判据。rc5 发布点 `c6d663d` 的测试是 244 项（rc6 是 265 项，多出来的 21 项测的是 rc6 才有的行为，回滚后自然不存在）。

```bash
cd /home/dsh/bigfish-council
git log --oneline -1                    # 必须是 c6d663d

LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
```

期望：

```
Ran 244 tests in 50.561s

OK
```

实测过四次，都在 Debian 12 / Python 3.11.2 上：

| 树 | 命令 | 结果 |
| --- | --- | --- |
| `git archive c6d663d` 解出的 rc5 树 | `unittest discover` | `Ran 244 tests in 70.198s` / `OK` |
| `git clone` 后 `checkout c6d663d`（第一次） | 同上 | `Ran 244 tests in 58.287s` / `OK` |
| `git archive` 那棵树，重跑 | 同上 | `Ran 244 tests in 50.561s` / `OK` |
| `git clone` 后 `checkout c6d663d`（第二次，干净 clone） | 同上 | `Ran 244 tests in 73.948s` / `OK` |

耗时随机器负载浮动，**项数 244 与 `OK` 不变**。如果跑出 265，说明代码没真的回到 `c6d663d`。

### 4.2 顺带跑自检与静态检查

```bash
.venv/bin/python tools/selfcheck.py
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py
.venv/bin/python -m pyflakes bigfish tests tools
.venv/bin/python -m compileall -q bigfish tests tools
```

rc5 上的期望值：自检通过 16 项、GUI 冒烟通过 9 项、pyflakes 零输出、compileall 退出码 0。这四项在 rc5 与 rc6 上数值相同（本批没增删自检项），所以它们不能用来区分版本——区分版本只能靠 `bigfish.__version__`、`locks.py` 是否存在、以及全量项数是 244 还是 265。

### 4.3 收尾

```bash
# 确认锁文件已经不在了（可选清理已做的话）
ls ~/.bigfish-council/council.db.access.lock 2>/dev/null || echo "无锁文件"

# 确认没有 -wal / -shm 残留（正常关闭后应该没有）
ls ~/.bigfish-council/council.db-wal 2>/dev/null || echo "无 WAL 残留"
```

---

## 5. 回滚的边界

### 5.1 rc6 期间产生的数据不会丢

schema 未变，所以 rc6 写进去的任务、事件、审计记录，rc5 原样能读。反方向也实测过：rc5 建的库 rc6 打开成功，rc6 建的库 rc5 打开成功。这是本版回滚比 rc5→rc4 轻松的根本原因。

### 5.2 恢复过的库，凭据状态不会自动回到 rc6 之前

如果在 rc6 期间做过恢复，那次恢复做的事**不会因为回滚而撤销**：

| 恢复时发生的 | 回滚后仍然成立 |
| --- | --- |
| 执行票密钥被轮换 | 旧票仍失效 |
| 所有执行票 `revoked_at` 置位 | 仍作废 |
| 全部远端身份 `enabled=0` + `disabled_reason` | 仍需 `reenable` |
| 在途任务 → `BLOCKED` | 仍需人工确认后重新审批 |
| `restore_epoch` +1 | 保持新值（不回退，这是设计） |

要恢复执行端，仍然用 rc5 的 `reenable`：

```bash
.venv/bin/python -m bigfish.cli --data-dir ~/.bigfish-council reenable <exec_id>
```

新 `token` 只出现这一次，要手工发给执行端。

### 5.3 回滚不会退回「没有锁」的世界

回到 rc5 之后，跨进程恢复互斥这道保护就没了——rc5 的 `_restore_lock()` 只和另一个 restore 互斥，管不住普通写者。这是 rc6 想修的东西（F-06），回滚等于放弃它。如果回滚的原因不是「rc6 有 bug」，而是「rc6 太严」，那要想清楚：**严的那部分是防止数据分叉的，不是添麻烦。**

### 5.4 混跑危害

| 组合 | 后果 |
| --- | --- |
| rc5 与 rc6 同时开同一个库 | 目前不会报错（rc5 不看锁，rc6 只管自己那一份），但两边写同一个库 = 数据和预期不一致的风险自负 |
| rc5 持库时 rc6 恢复 | **恢复成功**（实测 `epoch=1`）——rc5 没登记租约，排他锁拿得到（S7） |
| rc6 持库时 rc5 恢复 | rc5 没有任何租约检查，照常执行 |

结论：**任何版本混跑都不安全，回滚必须停干净**。

### 5.5 不受支持的做法

| 做法 | 为什么不行 |
| --- | --- |
| 只删 `bigfish/locks.py`，其余留 rc6 | `db.py:22` 的 `from .locks import WriterLease` 直接 `ModuleNotFoundError`（实测） |
| 只把 `db.py` 换回 rc5 版 | 其余 rc6 文件（`backup.py` 用 `exclusive_access`）会 import 失败 |
| 手工改 `meta.schema_version` | 没必要——本来就是 4 |
| 手工删 `.prerestore` 以外的锁文件并同时跑着 rc6 | 库开着时删锁文件会让排他锁失效（实测） |

---

## 6. 回滚也失败怎么办

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `git checkout c6d663d` 报 `local changes would be overwritten` | 工作区有未提交改动（2.3） | `git stash push -u` 或换目录 clone |
| `ModuleNotFoundError: No module named 'bigfish.locks'` | 代码没整树回退，混了 rc6 的文件 | `git status` 看有没有遗留；重新 `git checkout c6d663d` |
| 启动报 `PermissionError` | 数据目录不可写 | `chmod u+w <data-dir>`；这条是 rc6 才有的检查路径，rc5 不会报 |
| 启动报 `FileExistsError: ... council.db.access.lock` | 锁路径被同名目录占了（rc6 时期建的） | `rmdir <data-dir>/council.db.access.lock` |
| 全量跑出 265 项 | 代码没回到 `c6d663d` | `git log --oneline -1` 与 `grep __version__ bigfish/__init__.py` 双重确认 |
| 全量有失败项 | 数据目录被测试污染 / `LD_LIBRARY_PATH` 没设 | 测试用临时目录；确认 `.local/libbfc` 存在 |
| 执行端全部报「该身份已被禁用」 | rc6 期间恢复过（5.2） | 逐个 `reenable` |
| 想回到 rc6 但 `git checkout` 失败 | 同样是脏工作区 | 先 `git stash`，或用第 1 节备份的目录重建 |

最后一条给个兜底：**整目录备份是唯一能回到「rc6 + 当时数据」的手段**。第 1 节第 2 步不要省。

---

## 7. 一键检查清单

```bash
set -u
DIR=~/.bigfish-council
cd /home/dsh/bigfish-council

echo "1. 版本：$(.venv/bin/python -c 'import bigfish;print(bigfish.__version__)')"
echo "2. HEAD：$(git log --oneline -1)"
echo "3. locks.py：$(test -f bigfish/locks.py && echo 存在 || echo 不存在)"
echo "4. schema：$(.venv/bin/python -c "
import sqlite3,sys
c=sqlite3.connect('$DIR/council.db')
print(c.execute(\"SELECT value FROM meta WHERE key='schema_version'\").fetchone()[0])")"
echo "5. 锁文件：$(ls $DIR/council.db.access.lock 2>/dev/null || echo 无)"
echo "6. 端口：$(ss -lntp 2>/dev/null | grep -c 3096) 个监听"
echo "7. 进程：$(pgrep -fc '[p]ython -m bigfish' || echo 0) 个"
```

rc5 的期望值：版本 `0.1.1-rc5`、HEAD `c6d663d`、`locks.py` 不存在、schema `4`、锁文件「无」（清理过的话）、端口 0 个监听、进程 0 个。

---

## 8. 仍存在的问题

1. **Windows 侧本次未跑。** 所有实测（244 项全量、回滚演练、跨版本互开）都在 Debian 12 / Python 3.11.2 / SQLite 3.40.1 上完成。Windows 上的 `msvcrt` 分支、`os.replace` 撞文件占用、NTFS ACL 三条路径只有代码，没有实机验证。上真机前先跑 `tools/selfcheck.py` 与 `tools/gui_smoke.py`。
2. **`c6d663d` 不是 HEAD 的祖先。** 它在远端分支 `ghrc5` 上，`git merge-base --is-ancestor c6d663d HEAD` 返回非零（实测）。所以 `git checkout c6d663d` 是「切到另一条历史线」，不是「往回退几个提交」。仓库里也没有 tag 指向它，回滚时请用完整的提交号，别用分支名猜。
3. **`c6d663d` 与另一条 rc5 发布点 `b8f6de1` 有 6 个文件的小差异**（`bigfish/roles.py` 的角色标签「执行器（VPS DSH）」→「执行器（VPS DSH）」、`tests/test_v011_p3.py` 与三个 `tools/` 脚本里的占位 IP 换成了真实 IP、`tools/dev_setup.sh` 的模式位）。这些差异与 rc6 无关，但回滚后如果你看到角色标签是「VPS DSH」或 IP 是真实的，那就是 `c6d663d` 这一支的内容，不是回滚出了问题。
4. **回滚后 rc6 的 21 项新测试不再存在。** 244 项里没有 `tests/test_v011_rc6.py`，也没有 `test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot` 这类用例。也就是说，回滚之后「epoch 单调」这条保证就回到了 rc5 的受损状态（F-02 之前的行为：恢复旧快照可能让 `restore_epoch` 变小）。这是回滚的真实代价，写在这里不含糊。
5. **`council.db.prerestore` 的版本不匹配时不可当回滚素材。** 它要么是 rc6 的库（schema 4 × rc6 代码），要么在更早的恢复里是更早的库。拿它当「回到某版本」用之前，先 `inspect` 确认它的 `schema_version` 与 `app_version`。
