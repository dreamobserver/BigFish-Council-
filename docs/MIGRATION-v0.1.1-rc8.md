# 迁移说明：v0.1.1-rc7 → v0.1.1-rc8

## 0. 一句话

**本版没有数据库迁移。** schema 4 / backup format 4 保持不变，旧库直接能用，不需要跑迁移脚本，也不需要重新导出备份。

---

## 1. 为什么没有

rc8 修的三件事都在「行为层」：

| 修复 | 层 | 是否碰数据 |
| --- | --- | --- |
| R7-01 句柄竞态 | 并发控制 | 否，改的是加锁范围 |
| R7-02 未知角色 | 权限判定 | 否，改的是返回值（`EXECUTOR_DSH` → `DENIED_ROLE`） |
| R8-01 解码兜底 | 子进程输出处理 | 否，改的是解码函数 |

没有新增列、没有新增表、没有改字段类型、没有改索引。

---

## 2. 但有一个语义变化需要你知道

rc8 之后，`executors.role` 里出现未登记值时，那个执行器**无法再认证**。

如果你现在的库里有这类行（旧版本手工插入的、或者拼错的），升级到 rc8 后它们会立刻变成「不可用」，日志里会出现：

```
auth.denied_unknown_role  actor=executor-xxxx  role=xxx
```

**升级前先查一遍**：

```sql
SELECT id, name, role, enabled FROM executors
WHERE role NOT IN ('architect_gpt','reviewer_gemini','human_owner',
                   'executor','reviewer','admin','mobile');
```

- 有结果且这些执行器还需要用 → 改成合法角色：
  ```sql
  UPDATE executors SET role = 'executor' WHERE id = '...';
  ```
- 有结果但不需要 → 不用管，rc8 会拒掉它们，这正是期望行为。

合法角色完整清单在 `bigfish/roles.py`：`ARCHITECT_GPT`、`REVIEWER_GEMINI`、`HUMAN_OWNER`、`EXECUTOR_DSH`、`MOBILE_VIEWER`、`DENIED_ROLE`，外加 `_DB_ROLE_MAP` 里的别名（`reviewer`、`admin`、`mobile`）。

---

## 3. 升级步骤

```bash
cd /home/dsh/bigfish-council
git checkout fix/v0.1.1-rc7-hardening
git pull                                  # 或解包到新目录

# 1) 备份（照旧，rc8 没改备份逻辑，用现成命令）
.venv/bin/python -m bigfish.cli export /path/to/backup-$(date +%F).zip

# 2) 升级前检查未知角色（见 §2）
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/council.db")
rows = c.execute("""SELECT id,name,role,enabled FROM executors
  WHERE role NOT IN ('architect_gpt','reviewer_gemini','human_owner',
                     'executor','reviewer','admin','mobile')""").fetchall()
print(rows or "no unknown roles")
PY

# 3) 跑测试
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \\
  .venv/bin/python -m unittest discover -s tests -t .

# 4) 起服务
.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096
```

---

## 4. 降级

rc8 → rc7 同样是零迁移（结构没变）。见 `docs/ROLLBACK-v0.1.1-rc8.md`。

唯一要注意：rc8 拒绝的那些未知角色，降级后会重新变成执行器。降级前把它们改成合法值或停用。

---

## 5. 跨版本

| 从 | 到 | 需要做什么 |
| --- | --- | --- |
| rc7（schema 4） | rc8（schema 4） | 无 |
| rc6（schema 4） | rc8 | 无 |
| rc5（schema 4） | rc8 | 无 |
| rc4（schema 3） | rc8 | 需要 schema 3→4 迁移（rc5 引入，`bigfish/db.py` 自动执行：`ALTER TABLE executors ADD COLUMN disabled_reason TEXT` + `CREATE INDEX idx_executors_enabled`） |
| rc3（schema 3） | rc8 | 同上 |

升级时 `Database.init_schema()` 会自动跑迁移，不需要手工执行 SQL。迁移失败会回滚并抛 `MaintenanceError`，此时库保持迁移前状态。
