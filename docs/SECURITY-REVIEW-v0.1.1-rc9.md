# 安全复审：bigfish-council v0.1.1-rc9

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc9` |
| 上一版 | `0.1.1-rc8` |
| 库结构 | schema 4 / backup format 4，未变 |
| 本轮改动 | 11 文件 1118+/293−；其中安全相关 3 处（AUDIT-RC8-02 提权、R9-02 鉴权状态码、R9-04 维护期语义），其余是协议层迁移 |
| 结论 | 关掉 1 条可凭空获得审查票权的 P1 提权；MCP 层从手写换成官方 SDK，错误语义按规范修正；**未新增对外入口、未改变信任边界、未新增凭据存储** |

---

## 1. 本轮三处安全修复

### 1.1 AUDIT-RC8-02：未知审查身份从「默认 Gemini」改成「拒绝」

**威胁模型。** 审查票权是本产品的信任根：双模型都投 `approve` 才能进 `PENDING_APPROVAL`，人类才能批准。也就是说，「多一个 Gemini 身份」等于「审批可以一个人刷两票」。

攻击面是 `executors` 表本身：

1. 有过数据库写权限的人（本机任意进程）；
2. 旧版本残留：rc2 之前的审查身份没有 `model` 列，升级后该列为空或异常值；
3. 有人手工写错一列。

rc8 及之前，路径 2/3 的收益是**直接拿到审查票权**：

```
role_for_db_row("reviewer", "weird", "unknown")  ->  reviewer_gemini
role_for_db_row("reviewer", "",      None)      ->  reviewer_gemini
```

**rc9 之后。** 同样输入得到 `DENIED_ROLE`（`PERMISSIONS` 是空集），`authenticate()` / `authenticate_any()` 直接抛 `CouncilError` 并写一条 `auth.denied_unknown_role` 审计事件（只记身份 id，不记令牌）。

**为什么是拒绝而不是「按最低权限」。** 审查身份不是低权限：它的票能让任务从 `IN_REVIEW` 走到 `PENDING_APPROVAL`。把它当默认值等于把审批的一半钥匙挂在「默认」上。

**仍存在的风险。** 能写数据库的人可以直接把 `model` 写成 `gpt` 或 `gemini`（合法值），那就不会被拒。这条防线挡的是**误配置与旧数据**，不是有写权限的攻击者——后者需要文件 ACL（见 §3 R2），长期可考虑给 `executors` 加签名列。

**证据**：`tests/test_v011_rc9.py` 15 项；代码位置 `bigfish/roles.py:97-112`、`bigfish/core.py:164-182`。

### 1.2 R9-02：MCP 鉴权失败改 HTTP 401（这条既安全也关乎可用性）

**问题不是「不够严」，是「报不出来」。** 旧实现在鉴权失败时回 `HTTP 200 + {"jsonrpc":"2.0","id":null,"error":{...}}`。这在 JSON-RPC 上是合法响应，但：

- 官方 SDK 客户端收到后走 `JSONRPCMessage.model_validate_json`，模型不接受 `id: null`，抛 9 条 pydantic 校验错；
- 错误发生在清理阶段，`streamablehttp_client` **卡住不返回**，实测挂满 3 分钟被外部超时砍掉；
- 日志里只有 pydantic 报错，**没有任何一句说「令牌错了」**。

对一个「执行器靠令牌认领任务」的系统，这意味着：令牌配错时，运维看到的是客户端无响应，而不是 401。

**修复**：`_mcp_error(rid, code, message, status=200)` 新增 status 参数，两处鉴权失败显式传 401；body 仍带 `-32001` 便于脚本解析。

**证据**：`tests/test_v011_rc9_socket.py::test_bad_token_rejected_over_real_socket` / `test_missing_token_rejected_over_real_socket`（断言拿到的是**异常**，不是挂死）；`tools/selfcheck.py` 实跑 16/16。

### 1.3 R9-04：维护期必须表达成「这次没执行」

恢复备份期间数据库处于维护态。旧实现把维护期错误塞进 JSON-RPC 错误体（200），执行器可能把「收到了响应」当成「票到手了」，然后在恢复后的新库里找不到那张票——这是 rc5 那轮「恢复之后世界还是不是原来那个世界」的同一个坑。

**修复**：进 SDK **之前**判 `council.db.in_maintenance()`，直接回 `503` + `{"maintenance": true}` + `Retry-After: 15`。503 在 HTTP 语义上就是「这次没处理」，客户端不会误判成功。

**证据**：间接由 rc5 的 `TestRestoreAuthority` 覆盖（恢复期拒绝操作）；**MCP 这条分支没有独立断言**，属「已修复（未覆盖）」。

---

## 2. 与 rc8 安全模型的差异

| 维度 | rc8 | rc9 |
| --- | --- | --- |
| 未知 reviewer model | 默认 Gemini（**有票权**） | 拒绝 + 审计（零权限） |
| `register_executor` 名称两可 | 默认 Gemini | 抛错，要求显式传参 |
| MCP 鉴权失败 | `200 + -32001` | `401 + -32001` |
| MCP 维护期 | `200 + 错误文案` | `503 + Retry-After: 15` |
| MCP 协议实现 | 手写 JSON-RPC | 官方 SDK（第三方实现） |
| 协议版本 | 固定 `2025-03-26` | 协商（四个版本） |
| 工具暴露面 | 5 个执行器工具 | **不变**（测试逐名断言 + 显式排除 approve/restore/add_executor） |
| 参数校验 | 自研 `_validate_args()` | **仍自研**（不因换 SDK 丢字段级上限） |
| 对外监听 | 只回环 | **不变** |
| 凭据存储 | `ticket.key` + 令牌哈希 | **不变** |

---

## 3. 残余风险（按严重度）

| 编号 | 风险 | 严重度 | 现状 | 建议 |
| --- | --- | --- | --- | --- |
| R1 | 有数据库写权限的人仍可把 `executors.model` 改成合法值绕过 AUDIT-RC8-02 | 中 | 未修 | 依赖文件 ACL（R2）；长期考虑签名列 |
| R2 | Windows 上 `ticket.key` 的 ACL 依赖 `icacls` 成功执行；rc3 实测 `acl: granted`，但 Py3.14 + Win11 家庭版未复验 | 中 | 部分验证 | 公司机在线时跑一次 `verify_file_restricted` |
| R3 | MCP 换 SDK 后，协议层行为由第三方库决定；SDK 大版本升级可能改变错误码 | 中 | 新增 | `requirements.lock` 钉死；升级时跑 `test_v011_rc9_socket.py` 全套 |
| R4 | `_sdk_supported_methods()` 用反射，SDK 结构大改时会退回保守清单（四方法） | 低 | 有兜底 | 保持 |
| R5 | 维护期 503 无独立断言 | 低 | 未覆盖 | 补一条 MCP 层断言 |
| R6 | GUI 真窗口验收未做（托盘/最小化/退出） | 低 | 未做 | 人工验收 |
| R7 | 跨设备 MCP 闭环未重跑；本轮所有 MCP 证据都在回环 | 中 | 未做 | 机器在线时 `tools/remote_check.py --task-id=...` |
| R8 | 公司机 Py3.14.6 未跑 345 项 | 中 | 未做 | CI 只覆盖 runner，不覆盖真机 |

---

## 4. 本轮明确没做的事

- 没有新增任何对外监听端口，没有改 `check_bind_host` / `LoopbackGuardMiddleware`。
- 没有修改 `ticket.key` 的生成 / 轮换 / 恢复逻辑，没有改恢复 epoch。
- 没有修改审计导出白名单（`AUDIT_COLUMNS`）。
- 没有为了过测试放宽任何鉴权。唯一被改的旧断言（`selfcheck.py` 的 `st == 200`）是**改对**，语义从 200 变 401。
- 没有把 `dispatch_tool()` 拆成两份实现——REST 与 MCP 仍共用同一个函数，这是「换传输不换契约」的关键。

---

## 5. 人工复核建议（按性价比排序）

1. **看 `bigfish/roles.py:97-112`**：确认只有「显式 model」和「名称唯一可辨」两条路径能返回 `ARCHITECT_GPT` / `REVIEWER_GEMINI`，其余全是 `DENIED_ROLE`。
2. **看 `bigfish/core.py:164-182`**：确认注册入口与上面同源，没有第二套判定。
3. **看 `bigfish/mcp_adapter.py:415-430`**：确认 `_auth` 的两个失败分支都传了 `status=401`。
4. **看 `bigfish/mcp_adapter.py:460-475`**：确认维护期判断在 `_precheck` 之前、在 SDK 之前。
5. **看 `bigfish/server.py` 的 `__all__`**：确认再导出的是 15 个历史名字，没有顺手把内部函数暴露出去。
6. **看 `tests/test_v011_rc9_socket.py:105-150`**：确认客户端真的是官方 `mcp` SDK，不是本仓库的 `executor_client.py`。
7. **看 `tests/test_v011_rc9.py` 的 `insert_executor`**：确认它写的是脏行（绕过白名单），否则整组测试是同义反复。
