# 安全复审：bigfish-council v0.1.1-rc8

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc8` |
| 上一版 | `0.1.1-rc7`（`de255e9`） |
| 库结构 | schema 4 / backup format 4，未变 |
| 本轮改动 | 12 文件 659+/83−，其中安全相关 4 处（R7-01、R7-02、R8-01、CI-01 的测试侧） |
| 结论 | 修复了 2 条可直接提权/DoS 的 P1 缺陷、1 条会静默失效的权限校验缺陷；未引入新的对外入口、未新增凭据存储、未改变信任边界 |

---

## 1. 本轮三条安全修复

### 1.1 R7-02：未知角色从「失败开放」改成「失败关闭」

**威胁模型。** 攻击面是 `executors` 表本身。谁能写这张表？

1. 有过数据库写权限的人（本机任意进程，数据库文件默认 `0666` 减 umask）；
2. `register_executor()` 的合法调用方（人类所有者 / CLI）；
3. 旧版本残留：rc6 之前没有 `mobile` 角色，如果有人手工 INSERT 了 `role='mobile'`，rc6 之前它会被当成执行器。

在 rc7 及之前，路径 1/3 的收益是**直接拿到执行器全部权限**：`task.claim`（领走任务）、`run.heartbeat`、`run.submit`（伪造结果）、`task.read`（读所有任务正文）。也就是说，能改数据库的人不需要知道任何令牌，只要写一个不存在的 role 字符串 + 一个已知 token 哈希即可。

**rc8 之后。** 同样是写一个不存在的 role，结果是 `CouncilError` + 一条 `auth.denied_unknown_role` 审计事件，令牌无法完成任何操作。

**为什么不是「按最低权限」**：执行器不是最低权限。它是唯一能改变任务状态的非人类身份。把它当兜底等于把「最大的钥匙」挂在「默认」上。

**仍存在的风险**：能写数据库的人可以直接把 `role` 改成 `executor`（合法值），那就不会被拒。这条防线挡的是**误配置与旧数据**，不是有写权限的攻击者。后者需要文件 ACL 保护（见 §3 R2）。

**证据**：`tests/test_v011_rc8.py::TestUnknownRoleFailsClosed` 7 项；代码位置 `bigfish/roles.py:21,88-106`、`bigfish/core.py:229-250`、`bigfish/server.py:204,565`。

### 1.2 R7-01：Windows 同进程句柄泄漏 → 拒绝服务

**威胁模型。** 不是外部攻击，是**可用性自伤**：任何在 Windows 上并发打开同一个 `Database` 的程序（GUI 主线程 + ApiServer 线程 + Watchdog 线程就是三个）都可能触发。

后果不是数据损坏（锁仍然保护数据），是**再也打不开库**：`SLOTS = 64`，每泄漏一次少一个槽位；泄漏完 64 次后所有新的 `Database()` 都抛 `LeaseError`，用户看到的是「数据库正在被恢复」——一个误导性的错误。

**根因**：查表与抢槽不在同一个临界区。修复方式是把整段包进 `_ACQUIRE_LOCK`，并在异常路径关闭自己打开的句柄。

**为什么这条算 P1**：它是可复现的、无告警的、最终导致服务不可用的缺陷。rc7 的测试里 `TestLeaseAtomicity` 5 项全绿，因为那些测试都在单线程或跨进程场景下跑，从来没有「同进程两个线程同时打开」。

**证据**：`tests/test_v011_rc8.py::TestConcurrentLocalHandles` 2 项（Barrier 真并发 100 轮）；代码位置 `bigfish/locks.py:52,143-197,200+`。

### 1.3 R8-01：ACL 校验的静默失效

**威胁模型。** 密钥文件（`ticket.key`）的权限是本产品的核心假设之一：数据库泄露不足以伪造执行票，因为派生密钥不在库里。这个假设成立的前提是「密钥文件只有当前用户可读」。

`verify_file_restricted()` 负责在启动时/恢复后检查这条。它依赖 `icacls` 的输出。如果解码失败：

- rc7 的实现用 `subprocess.run(text=True)`，locale 解码失败会抛 `UnicodeDecodeError`；
- 调用点把它包在 `try/except` 里，异常被吞成「校验失败但继续」；
- 结果是**校验看起来做了，实际什么都没检查**。

这不是理论风险：rc8 全量测试里就出现了「icacls 返回 rc=0 但输出为空」，于是 ACL 判定为「未包含当前用户」，测试红。生产环境里同样的解码失败会走向另一条分支（静默通过）。

**修复**：`run_capture()` 永远返回 `str`，永远不会因编码抛异常；`_decode_console` 按 utf-8 → gbk → cp936 → mbcs → utf-16-le → replace 依次尝试。

**仍存在的问题**：解码成功不等于 ACL 正确。如果 `icacls` 输出被本地化成完全不认识的语言，`acl_mentions_user` 仍然匹配不到当前用户 —— 这时候失败方向是**判否**（拒绝启动 / 告警），不是判是。失败关闭，可接受。

**证据**：`tests/test_v011_rc8.py::TestConsoleDecoding` 6 项 + `TestWindowsAclMatching` 6 项；代码位置 `bigfish/security.py:197-218,222-251`。

---

## 2. 与 rc7 安全模型的差异

| 维度 | rc7 | rc8 |
| --- | --- | --- |
| 未登记角色 | 执行器权限 | 拒绝 + 审计 |
| 鉴权失败后的 `role_canonical` | 总是有值（兜底） | 可能直接抛错，调用方拿不到 |
| 外部命令解码 | `text=True`，可能抛 | 强类型兜底，不抛 |
| Windows 并发打开 | 可能泄漏槽位 | 整段串行 |
| 对外入口 | 无变化 | 无变化 |
| 凭据存储 | 无变化 | 无变化 |

---

## 3. 残余风险（按严重度）

| 编号 | 风险 | 严重度 | 现状 | 建议 |
| --- | --- | --- | --- | --- |
| R1 | 有数据库写权限的人仍可把 role 改成合法值绕过 R7-02 | 中 | 未修 | 依赖文件 ACL（R2）；长期可考虑 `executors` 增加签名列 |
| R2 | Windows 上 `ticket.key` 的 ACL 依赖 `icacls` 成功执行 | 中 | 已加固解码，真实 ACL 未在真机断言 | 公司机在线时跑一次 `verify_file_restricted` 实测 |
| R3 | 解码兜底用了 `replace`，极端情况下会把乱码当合法文本 | 低 | 失败方向是判否 | 保持 |
| R4 | `_current_user_candidates` 放宽匹配，可能匹配到同名不同用户的条目 | 低 | 只用于匹配不用于授权 | 保持 |
| R5 | CI runner 不是公司机，Windows 11 家庭版 / Py3.14 的真实行为未验 | 中 | 未做 | 机器在线时补跑 |
| R6 | GUI 真窗口验收未做，托盘/最小化/退出路径只有离屏证据 | 低 | 未做 | 人工验收 |
| R7 | 跨设备 MCP 闭环未重跑，rc8 的网络行为只有本地证据 | 中 | 未做 | 机器在线时用 `tools/remote_check.py --task-id=...` 跑 |

---

## 4. 本轮明确没做的事

- 没有新增任何对外监听端口。
- 没有修改 `ticket.key` 的生成/轮换逻辑。
- 没有修改恢复流程（`restore_bundle` / `restore_from_bundle`）。
- 没有修改审计包的导出白名单。
- 没有为了过测试放宽任何鉴权（唯一的语义变更是「未知角色更严」，见 `DIFF-SUMMARY` §7.4）。

---

## 5. 人工复核建议（按性价比排序）

1. **看 `bigfish/locks.py:143-200`**：确认 `_ACQUIRE_LOCK` → `_LOCAL_LOCK` 的获取顺序在全文件里只有这一个方向（反向会死锁）。
2. **看 `bigfish/roles.py:51-106`**：确认 `DENIED_ROLE` 的 `PERMISSIONS` 是空集，且 `DB_ROLES` 不含它。
3. **看 `bigfish/security.py:211-218`**：确认 `run_capture` 没有传 `text=True`（传了兜底就白写）。
4. **看 `tests/test_v011_rc8.py:81-163`**：确认并发测试真的用了 `Barrier`，不是「起两个线程各自跑完」。
5. **看 `tests/test_v011_rc5_restore_authority.py` 的改动**：确认它是「旧断言在新语义下不再成立」，不是「为了让测试变绿把断言删了」。
