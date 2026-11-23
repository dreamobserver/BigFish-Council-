# 回滚说明：v0.1.1-rc8 → v0.1.1-rc7

## 0. 结论先行

- **库结构没有变化**（schema 4 / backup format 4），所以回滚**不需要**降级数据库、不需要恢复备份、不需要迁移。
- 回滚 = 换代码。已有数据（`council.db`、`ticket.key`、审计包）全部继续可用。
- 唯一需要注意的是：rc8 期间**被拒绝**的未知角色，回滚到 rc7 后会重新变成执行器权限。如果有这类数据，回滚前先清理。

---

## 1. 代码回滚（三种方式，按场景选）

### 1.1 从 tag 检出（推荐，干净）

```bash
cd /home/dsh/bigfish-council
git fetch --tags
git checkout v0.1.1-rc7        # de255e9
```

注意：这会进入 detached HEAD。要回到 rc8 用 `git checkout fix/v0.1.1-rc7-hardening`。

### 1.2 反向打补丁（保留 rc8 的其他提交）

```bash
git revert --no-commit 031ebcf
git commit -m "revert: 回滚 rc8"
```

### 1.3 从源码包解包（最保险，不碰 git）

```bash
mkdir -p /tmp/rc7 && cd /tmp/rc7
unzip /path/to/bfc-src-v011rc7.zip
# 用解出来的 bigfish/ tests/ tools/ 覆盖现役目录
```

---

## 2. 数据侧：什么都不用做

| 对象 | rc8 是否改过 | 回滚影响 |
| --- | --- | --- |
| `council.db` 结构 | 否（schema 4） | 无 |
| `ticket.key` | 否 | 无 |
| 备份包格式 | 否（backup format 4） | 无 |
| 审计导出白名单 | 否 | 无 |
| 恢复流程 | 否 | 无 |

**不要**为了回滚去删除 `council.db`。rc8 没写过新表、新列，旧代码读同一个库不会有任何问题。

---

## 3. 回滚前必须检查的一件事

```sql
-- 有没有未登记角色
SELECT id, name, role FROM executors WHERE role NOT IN
  ('architect_gpt','reviewer_gemini','human_owner','executor','reviewer','admin','mobile');
```

有结果就说明：rc8 期间这些行被拒了，回滚到 rc7 后它们会拿到执行器权限。

处理方式（二选一）：

1. 把它们改成合法角色（`executor` 或 `mobile`）；
2. 或者直接 `enabled = 0` 停用。

```sql
UPDATE executors SET enabled = 0 WHERE role NOT IN (...);
```

---

## 4. 回滚验证

```bash
cd /home/dsh/bigfish-council
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"   # 应为 0.1.1-rc7

LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \\
  .venv/bin/python -m unittest discover -s tests -t .
# 预期：Ran 295 tests, OK（rc7 的测试数）
```

然后起服务确认健康：

```bash
.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096 &
curl -s http://127.0.0.1:3096/health
```

---

## 5. 回滚后已知会退化的行为

| 行为 | rc8 | 回滚后（rc7） |
| --- | --- | --- |
| 未登记角色 | 拒绝 + 审计 | **回落成执行器** |
| Windows 并发打开 64 次后 | 正常 | 可能永久 `LeaseError` |
| `icacls` 解码失败 | 兜底不抛 | 可能抛 `UnicodeDecodeError` |
| Windows CI | 应能跑全量 | 3 项红灯 |

如果回滚是为了「先恢复可用」，建议只回滚到 rc7 并把 `executors.role` 全部换成合法值，而不是长期停在 rc7。

---

## 6. 回滚它是最后手段

rc8 的改动面很小（12 文件 659+/83−），且都是加法式修复。如果只是某一个场景出问题，更划算的做法是：

1. 用 `git revert` 单独反掉 `locks.py` 或 `roles.py` 的改动；
2. 保留其余修复；
3. 在 `docs/FIX-MATRIX` 里记一条「已回退」。

整版回滚只在「无法定位」或「影响面不明」时用。
