# 迁移说明：v0.1.1-rc8 → v0.1.1-rc9

## 0. 一句话

**本版没有数据库迁移。** schema 4 / backup format 4 保持不变，旧库直接能用，不需要跑迁移脚本，也不需要重新导出备份。

---

## 1. 为什么没有

rc9 改的三件事都不碰数据：

| 修复 | 层 | 是否碰数据 |
| --- | --- | --- |
| AUDIT-RC8-01 MCP 传输层换 SDK | 协议 | 否，改的是请求怎么被解析 |
| AUDIT-RC8-02 未知审查身份 | 权限判定 | 否，改的是返回值（`REVIEWER_GEMINI` → `DENIED_ROLE`） |
| R9-02 / R9-03 / R9-04 | HTTP 状态码与路由注册 | 否 |

没有新增列、没有新增表、没有改字段类型、没有改索引。

---

## 2. 但有三处语义变化需要你知道

### 2.1 未知 `reviewer` 身份会立刻失效

rc9 之后，`executors` 表里 `role='reviewer'` 且 `model` 既不是 `gpt`/`gemini`、名称里也看不出是哪个的行，**无法再认证**。日志里会出现：

```
auth.denied_unknown_role  actor=executor-xxxx
```

升级前先查一遍：

```sql
SELECT id, name, role, model, enabled FROM executors
WHERE role = 'reviewer'
  AND (model IS NULL OR lower(model) NOT IN ('gpt','gemini'));
```

有结果时分两种：

- 名称里唯一可辨（只有 gpt 或只有 gemini）→ **不用管**，rc9 仍会推断出来，只是写一条日志说明「靠名称推断」。
- 名称也看不出来 → 明确指定：
  ```sql
  UPDATE executors SET model = 'gpt'    WHERE id = '...';   -- 或 'gemini'
  ```
  或者停用：`UPDATE executors SET enabled = 0 WHERE id = '...';`

### 2.2 MCP 客户端要能处理 401

如果外部脚本直接 `curl` 我们的 `/mcp`，以前鉴权失败是 `HTTP 200`，现在是 `HTTP 401`。判断「被拒」的脚本要把状态码判断一起改（body 里的 `-32001` 没变）。

官方的 MCP 客户端（`mcp` Python SDK、DSH 的 connector）本来就按规范处理 401，不受影响，反而是修好了。

### 2.3 `initialize` 必须带完整参数

自己写脚本调 MCP 的话，`initialize` 必须带 `protocolVersion` / `capabilities` / `clientInfo`：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-03-26","capabilities":{},
  "clientInfo":{"name":"my-script","version":"1.0"}}}
```

缺了会被 SDK 以 `-32602` 拒绝（旧手写版不校验，传空也能过）。`tools/e2e_loopback.py` 已按新要求改。

---

## 3. 升级步骤

```bash
cd /home/dsh/bigfish-council
git pull                      # 或解包到新目录

# 1) 备份（照旧，rc9 没改备份逻辑）
.venv/bin/python -m bigfish.cli export /path/to/backup-$(date +%F).zip

# 2) 检查未知审查身份（见 §2.1）
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/council.db")
rows = c.execute("""SELECT id,name,role,model,enabled FROM executors
  WHERE role='reviewer' AND (model IS NULL OR lower(model) NOT IN ('gpt','gemini'))""").fetchall()
print(rows or "no suspicious reviewer rows")
PY

# 3) 确认 mcp SDK 在环境里（rc9 起它是硬依赖，不再是可选）
.venv/bin/python -c "import mcp; print(mcp.__version__ if hasattr(mcp,'__version__') else 'ok')"

# 4) 跑测试
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 5) 起服务
.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096
```

---

## 4. 依赖变化

| 包 | rc8 | rc9 |
| --- | --- | --- |
| `mcp` | 声明在 `requirements.txt` 里，但代码零 import（**事实上的可选**） | **硬依赖**（`mcp_adapter.py` 直接 import） |
| 版本 | `requirements.txt` 未钉版本 | 建议 `>=1.9`；本机实测 `1.30.0`，`requirements.lock` 已含 |

降级（rc9 → rc8）不需要卸 mcp——rc8 只是不用它。

---

## 5. 降级

rc9 → rc8 同样是零迁移（结构没变）。见 `docs/ROLLBACK-v0.1.1-rc9.md`。

要注意的是：

- rc9 期间被拒的未知审查身份，降级后会**重新变成 Gemini**（有票权）。降级前先把它们改成合法值或停用。
- 降级后 MCP 鉴权失败又变回 `HTTP 200`，官方 SDK 客户端会挂住。**如果客户端是官方 SDK，不要降级到 rc8。**

---

## 6. 跨版本

| 从 | 到 | 需要做什么 |
| --- | --- | --- |
| rc8（schema 4） | rc9（schema 4） | 无 |
| rc7（schema 4） | rc9 | 无 |
| rc6（schema 4） | rc9 | 无 |
| rc5（schema 4） | rc9 | 无 |
| rc4（schema 3） | rc9 | 需要 schema 3→4 迁移（rc5 引入，`Database.init_schema()` 自动执行：`ALTER TABLE executors ADD COLUMN disabled_reason TEXT` + `CREATE INDEX idx_executors_enabled`） |
| rc3（schema 3） | rc9 | 同上 |

升级时 `init_schema()` 自动跑迁移，不需要手工 SQL。迁移失败会回滚并抛 `MaintenanceError`，库保持迁移前状态。
