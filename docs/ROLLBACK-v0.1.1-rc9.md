# 回滚说明：v0.1.1-rc9 → v0.1.1-rc8

## 0. 结论先行

- **库结构没有变化**（schema 4 / backup format 4），回滚**不需要**降级数据库、不需要恢复备份、不需要迁移。
- 回滚 = 换代码。已有数据（`council.db`、`ticket.key`、审计包）全部继续可用。
- **但有一条例外要掂量**：如果客户端是官方 MCP SDK（DSH connector、`mcp` Python 客户端），回滚到 rc8 后鉴权失败会退化成 `HTTP 200 + error`，客户端会**挂住不返回**。这类环境不建议降级。

---

## 1. 代码回滚（三种方式）

### 1.1 从 tag 检出

```bash
cd /home/dsh/bigfish-council
git fetch --tags
git checkout v0.1.1-rc8
```

会进入 detached HEAD。回 rc9 用 `git checkout main`（或对应的硬化分支）。

### 1.2 反向打补丁（保留 rc9 的其他提交）

```bash
git revert --no-commit <rc9 提交 sha>
git commit -m "revert: 回滚 rc9 的 MCP 传输层"
```

### 1.3 从源码包解包（最保险，不碰 git）

```bash
mkdir -p /tmp/rc8 && cd /tmp/rc8
unzip /path/to/bfc-src-v011rc8.zip
# 用解出来的 bigfish/ tests/ tools/ 覆盖现役目录
```

---

## 2. 数据侧：什么都不用做

| 对象 | rc9 是否改过 | 回滚影响 |
| --- | --- | --- |
| `council.db` 结构 | 否（schema 4） | 无 |
| `ticket.key` | 否 | 无 |
| 备份包格式 | 否（backup format 4） | 无 |
| 审计导出白名单 | 否 | 无 |
| 恢复流程 / epoch | 否 | 无 |

**不要**为了回滚删 `council.db`。rc9 没写过新表、新列，rc8 读同一个库不会有任何问题。

---

## 3. 回滚前必须检查的事项

### 3.1 未知审查身份

```sql
SELECT id, name, role, model FROM executors
WHERE role = 'reviewer' AND (model IS NULL OR lower(model) NOT IN ('gpt','gemini'));
```

有结果就说明：rc9 期间这些行被拒了，回滚到 rc8 后它们会**拿到 Gemini 票权**（凭空多一个审查身份）。

处理（二选一）：

```sql
UPDATE executors SET model = 'gpt' WHERE id = '...';   -- 确认真实身份
UPDATE executors SET enabled = 0   WHERE id = '...';   -- 或直接停用
```

### 3.2 MCP 客户端类型

如果 DSH 那边的 `bigfish-council` 连接已经在用官方 SDK 客户端握手，回滚会让它在令牌出错时挂死。**要么不降级，要么同时把 token 换对。**

---

## 4. 回滚验证

```bash
cd /home/dsh/bigfish-council
.venv/bin/python -c "import bigfish; print(bigfish.__version__)"   # 应为 0.1.1-rc8

LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
# 预期：Ran 322 tests, OK (skipped=1)

.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096 &
curl -s http://127.0.0.1:3096/health
```

rc8 的 `tools/selfcheck.py` 里坏 Token 断言是 `st == 200`，回滚后要连同 tools 一起回滚，否则自检红。

---

## 5. 回滚后已知会退化的行为

| 行为 | rc9 | 回滚后（rc8） |
| --- | --- | --- |
| 未知审查身份 | 拒绝 + 审计 | **默认 Gemini，有票权** |
| MCP 鉴权失败 | `401`，客户端立刻报错 | `200 + error`，官方 SDK 客户端**挂死** |
| MCP 维护期 | `503 + Retry-After` | `200 + 错误文案`，执行器可能误判成功 |
| 协议版本 | 协商（四个版本） | 固定回 `2025-03-26` |
| `/mcp` 精确路径 | 直接命中 | 手写实现，无 307 问题（因为它不是挂载的子应用） |
| 协议层维护 | 第三方 SDK | 自己维护，SDK 升级要手跟 |

---

## 6. 更划算的做法：只回滚一部分

rc9 的改动分成三块，可以单独反：

| 想解决的问题 | 只回滚哪些 | 保留哪些 |
| --- | --- | --- |
| SDK 出问题、要退回手写协议 | `bigfish/mcp_adapter.py` + `bigfish/server.py` | **保留** `roles.py` / `core.py` 的失败关闭修复 |
| 某个旧客户端不认 401 | 只能改 `_auth` 的 status 参数 | 其余全保留 |
| 审查身份被误拒 | `bigfish/roles.py` + `bigfish/core.py` | **保留** SDK 迁移（但这样等于重新打开提权口子，不建议） |

整版回滚只在「无法定位」或「影响面不明」时用。rc9 的改动面是 11 文件，其中 533 行是新文件，反起来很干净。
