# 安全审查：bigfish-council v0.1.1-rc1

审查对象：`/home/dsh/bigfish-council`（相对 v0.1.0 baseline commit `1a97f5a` 的全部改动）。
审查方式：源码逐条对照 + 实际运行验证（不是纸面设计）。
**本文只描述本轮实际做到的事，没有做的部分在最后一节明确列出。**

## 1. 威胁模型

| 编号 | 威胁 | 现实程度 | 本轮对策 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 数据库/备份包泄露，攻击者拿到明文执行票 | 高（备份常被随便拷） | 执行票 HMAC 派生，库里只存哈希；密钥独立文件不进导出包 | 已修 |
| T2 | 执行器伪造审查票，让未审方案被批准 | 高（v0.1.0 无身份） | 服务端按 token 绑角色，投票身份由 token 决定 | 已修 |
| T3 | 执行器自己批准任务 | 高 | 执行器工具集不含 approve；approve 只认 human_owner | 已修 |
| T4 | 审批被跨版本降级（审 v1，跑 v2） | 中 | 事务内读版本 + 条件 UPDATE + expected_version | 已修 |
| T5 | 备份恢复把库搞坏 | 中 | 恢复前先 inspect，恢复失败自动回滚，留 prerestore 副本 | 已修 |
| T6 | 长期任务因租约到期被误判死 | 中 | 心跳续租（封顶 6h） | 已修 |
| T7 | 网络暴露到公网 | 高影响 | 强制回环绑定，非回环直接拒绝启动 | 已修 |
| T8 | 模型身份可被密码学证明 | — | **做不到，且未声称做到** | 明确不实现 |

## 2. 凭据处理

### 2.1 执行器 Token（bfc_ex_ / bfc_rv_ / bfc_ad_）

- 生成：`secrets.token_urlsafe(32)`，前缀按角色区分；
- 存储：数据库只存 `sha256(token)`（`executors.token_hash`），无明文列；
- 比对：`hmac.compare_digest`（`security.py:verify_token`），非常量时间的问题不存在；
- 返回：`register_executor()` 只在**注册那一刻**返回一次明文；
- `authenticate()` 返回的字典里 `pop("token_hash")`，业务层拿不到哈希。

实测：`test_v011_p2.TestExecutorBoundary.test_token_hash_not_returned` 断言认证结果里无 token 相关键。

### 2.2 执行票（bfc_tk_…）

这是 v0.1.1 改动最大的一处。v0.1.0 用 `new_token()` 随机生成，把明文写进 `tickets.token_hash`**以外的**幂等表响应里，
导致 `idempotency.response` 字段直接含 `bfc_tk_…` 明文和整段 Prompt（P0-4 实测确认）。

现在的做法（`security.py:derive_ticket`）：

```python
material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key or ''}".encode("utf-8")
digest = hmac.new(key, material, hashlib.sha256).hexdigest()
return "bfc_tk_" + digest[:43]
```

性质：

- **确定性**：同一 (执行器, 任务, 版本, 幂等键) 必然复算出同一张票 → 幂等重放可以安全重算，不用存明文；
- **不可反推**：密钥是 32 字节随机数，存在 `data_dir/secrets/ticket.key`，权限 `0600`；
- **不进备份包**：`backup.py` 的 manifest 里有 `includes_key_file: false`，导出 ZIP 里只有 `manifest.json` + `council.db`；
- **密钥轮换 = 旧票全废**：删掉 key 文件会重新生成，所有在途票立即失效（这是有意的，写进 MIGRATION 文档）。

### 2.3 敏感字段守护

`core.py:assert_no_secrets(obj, path)` 在写幂等表前做递归检查，遇到 `bfc_tk_` 前缀或键名恰好为 `prompt` 就抛
`拒绝持久化敏感字段：{path}`。这是**第二道闸**——即使以后有人改坏了 `_idem_put` 的调用方，也不会静默写明文。

实测：`test_v011_p0.TestNoPlaintextToken.test_guard_rejects_secret_fields`。

## 3. 身份与权限（P2）

### 3.1 四角色表

见 `bigfish/roles.py`（80 行，单一真源）：

| 角色 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| `architect_gpt` | 以 gpt 身份投审查票、读任务 | 以 gemini 身份投票、审批、执行 |
| `reviewer_gemini` | 以 gemini 身份投审查票、读任务 | 以 gpt 身份投票、审批、执行 |
| `human_owner` | 创建/修改/取消/验收任务、投票、审批、签发/吊销票、管理执行器、导出/恢复备份、读审计 | 被自动授予任何执行能力 |
| `executor_dsh` | 领任务、读自己任务的状态、心跳、回传结果 | 审查、审批、管理执行器、读审计、读别人的任务 |

### 3.2 服务端绑定，不信请求体

v0.1.0 的 `add_review(tid, model, ...)` 直接吃调用方给的 model 字符串。
现在 `add_review` 增加 `actor` 参数，服务端先用 token 解析出角色，再由 `roles.model_for_role(role)` 决定它能代表哪个模型：

```
gpt 角色投 gemini 票 → CouncilError("只能以 gpt 身份投票")
gemini 角色投 gpt 票 → CouncilError("只能以 gemini 身份投票")
```

GUI 里人工粘贴模型回复的路径，`actor` 记 `human_owner`，**不是**模型自证。这一点在 `roles.py` 文档串里写死了：

> 本版本不具备密码学意义上的模型来源证明，不得声称已实现。

### 3.3 执行器不能审批

三道闸：

1. **工具集**：MCP 只注册 5 个工具（list/claim/heartbeat/submit/status），物理上没有 approve；
2. **权限表**：`TOOL_PERMISSIONS` 里每个工具都绑一个权限，`dispatch_tool` 开头校验；
3. **业务层**：`approve()` 第一行就 `if actor not in (R.HUMAN_OWNER,)` 抛错。

另外 `approve()` 在事务内额外检查：当前版本的审查票里若出现 `actor == executor_dsh`，直接拒绝
（防御历史脏数据 —— v0.1.0 写过这种行）。

### 3.4 审计

新增 `audit` 表（actor / action / target / detail / created_at），记录：
`approval.approve|reject|return`、`executor.registered`、`executor.revoked`、`review.revoked`。

审计**不对外暴露**：没有 `/api/v1/audit` 端点（实测 `test_audit_not_exposed_over_http` 断言 404），只能本机 CLI/GUI 读。

## 4. 网络暴露面（P3 / 阶段 B）

| 面 | 状态 | 证据 |
| --- | --- | --- |
| Windows API | 只绑 `127.0.0.1:3096` | `config.DEFAULT_API_HOST`；`__main__` 非回环 `return 2` |
| VPS 跳板 | 只绑 `127.0.0.1:3098`，只放行 `/health`、`/mcp*`、`/api/*` | `tools/bfc-proxy.mjs` |
| 公网端口 | **零新增** | 未改防火墙、未开 Funnel、未开 Serve |
| Tailscale | 未做任何配置变更 | — |

非回环绑定的拒绝实测（`test_v011_p3.TestLoopbackOnly`）：

```
$ python -m bigfish --headless --host 0.0.0.0
拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。
returncode = 2

$ python -m bigfish --headless --host CLIENT-A-TAILNET-IP
（同一句，returncode = 2）
```

## 5. 数据安全（P0）

### 5.1 备份完整性

v0.1.0 的 `z.write(self.db.path, "council.db")` 会漏掉 WAL 里未 checkpoint 的数据。
实测：1 行任务在库内，但导出包里 `council.db` 只有 4096 字节，恢复后 `no such table: tasks`。

现在 `Database.snapshot(dest)` 用 sqlite3 的 `backup()` API（一致性快照，含 WAL），
目标库 `journal_mode=DELETE`（避免把 WAL 依赖带进包里）。

导出包结构：

```
bigfish-backup-YYYYmmdd-HHMMSS.zip
├── manifest.json   { backup_format: 2, app_version, schema_version, created_at,
│                     db_sha256, db_size, table_counts, includes_key_file: false,
│                     contains_plaintext_credentials: false }
└── council.db      一致性快照（12 张表齐全）
```

### 5.2 恢复

`restore_bundle()` 顺序：`inspect_bundle()`（只读校验 + 表完整性 + 关系完整性）→ 备份原库到 `council.db.prerestore`
→ 替换 → 复检 → 失败回滚并抛 `RestoreError`。

实测：故意喂一个损坏包，`test_corrupted_bundle_does_not_touch_db` 断言原库字节未变。

### 5.3 审批不可漂移

`approve()` 现在整段在一个 `BEGIN IMMEDIATE` 事务里：

```python
with self.db.tx() as c:
    row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    version = task["current_version"]
    if expected_version is not None and version != expected_version:
        raise CouncilError(f"任务版本已变（当前 v{version}，请求基于 v{expected_version}）")
    votes = self._vote_summary_locked(c, tid, version)   # 只看当前版本
    cur = c.execute(
        "UPDATE tasks SET status=?, updated_at=?, blocked_reason=NULL"
        " WHERE id=? AND current_version=? AND status=?",
        (target, t, tid, version, task["status"]))
    if cur.rowcount != 1:
        raise CouncilError("任务在审批过程中被修改，已拒绝本次审批")
```

关键点：`UPDATE` 带 `current_version` 条件，即使读与写之间版本被改，也会因 `rowcount != 1` 失败，而不会把未审版本推进。

## 6. 残余风险（诚实列表）

| 风险 | 严重性 | 现状 | 说明 |
| --- | --- | --- | --- |
| 模型身份无密码学证明 | 中 | **未解决** | GPT/Gemini 的票靠人工粘贴，谁也证明不了这段文本真是模型输出 |
| 执行器可谎报进度/结果 | 中 | 设计如此 | executor 本来就该被信任到「会说真话」的程度，人工验收是最后一道闸 |
| 密钥在明文文件里 | 低 | 部分缓解 | `0600` + 不与 DB 同目录层，但本机管理员仍可读 |
| Prompt 内容会进回传日志 | 低 | 未加过滤 | executor 回传的 log 可能包含敏感信息，人类验收时需自行判断 |
| 无速率限制 | 低 | 未做 | 只绑回环，攻击面是「本机已有权限的进程」 |
| SQLite 单写者 | 低 | 已知限制 | WAL 下并发读没问题，多进程写靠 `BEGIN IMMEDIATE` 串行化 |
| Windows 端防火墙 | — | **未知** | 本轮无法访问 CLIENT-A（CLIENT-A-TAILNET-IP:3096 与 :8765 全 TimeoutError），防火墙状态未验证 |

## 7. 明确没有做的事

- 没有做密码学签名/模型证书；
- 没有做 DSH 容器层的系统级沙箱（DSH 是执行框架，不是安全边界 —— 该条已写进 DELIVERY 文档）；
- 没有做审计日志的防篡改（只有 append，没有链式哈希）；
- 没有做备份加密（ZIP 是明文，含 Prompt 与任务正文）；
- 没有做跨设备联调（见 NETWORK-TOPOLOGY.md 的实测失败记录）。

---

## 12. rc3 补充（2026-11-19）

> 本文第 1–11 节写于 rc1，结论仍然有效。rc3 的安全复审已独立成文：
> **[SECURITY-REVIEW-v0.1.1-rc3.md](SECURITY-REVIEW-v0.1.1-rc3.md)**，
> 按任务书要求逐项回答 9 个问题（绕过审批 / 取消后领取 / 恢复后旧票复活 / Watchdog 误杀 /
> 幂等并发 / 非回环监听 / 凭据明文 / 跨版本恢复 / 执行器越权），每问都给代码位置与测试证据。
> 本文下面只补 rc3 相对 rc1 的**威胁模型增量**。

### 12.1 新增的攻击面（都是自己给自己挖的坑）

| 攻击面 | 怎么来的 | rc3 的处理 |
| --- | --- | --- |
| 备份恢复 = 权限复活 | 票是 HMAC(key,...) 确定性派生，恢复旧快照后旧票能重算出来 | 恢复时轮换密钥 + epoch+1 + 清 claim 幂等 |
| 取消语义 = 假取消 | rc2 直接标 CANCELLED，远程可能还在跑 | CANCELLING + 执行器确认 + 失联转 BLOCKED |
| 幂等在事务外 | 并发下两个请求都读到「没有」 | 检查与写入同一事务 |
| 监听校验只在入口 | `cli serve` 绕过 `__main__` | 校验下沉到 `serve()` |
| 脱敏只管一处 | 只处理幂等响应 | 统一闸门覆盖 events / runs / artifacts / audit |

### 12.2 rc3 的残余风险（7 条，不掩盖）

| # | 风险 | 是否可接受 |
| --- | --- | --- |
| R1 | `approve` 的 actor 由调用方传入，无生物特征证明 | 是（单机本地工具，威胁模型不含本机 root） |
| R2 | Windows ACL 实测未做 | **已验证**（2026-11-19 公司机实测：`secure_file` → acl granted，`verify_file_restricted` → `(True, "acl limited")`，KEYLEN 32） |
| R3 | 恢复时若有第二个 GUI 实例打开 db，`os.replace` 可能失败 | 是（抛错 + 回滚，不损坏） |
| R4 | Tailnet 内任何有执行器 token 的设备可操作控制台 | 是（仅限自己被分配的任务） |
| R5 | `heartbeat_ttl` 默认 120s，网络抖动超时会误阻断 | 是（进 BLOCKED 需人工，方向 fail-safe） |
| R6 | Windows 平台 129 项未跑 | **已验证**（2026-11-19：Ran 129 tests in 25.263s / OK，退出码 0） |
| R7 | 数据库文件未加密 | 是（本地单机，且已排除凭据） |

### 12.3 rc3 明确没做的 5 项

1. 类型检查（mypy / pyright）——项目未引入，本轮不新增依赖；
2. 24h soak / 长时间稳定性；
3. 数据库文件加密（SQLCipher 等）；
4. Windows 真机 ACL 与 GUI 真窗口验收；
5. 跨机 MCP 闭环的 rc3 重跑（公司机仍跑 rc2）。

