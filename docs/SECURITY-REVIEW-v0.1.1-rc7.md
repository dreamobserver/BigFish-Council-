# 安全复审 · v0.1.1-rc7

> 版本：`0.1.1-rc7`（`bigfish/__init__.py:6`）· 分支 `fix/v0.1.1-rc7-hardening` · HEAD `c0c54bc`
> 沿用 rc6 的威胁模型编号（T1–T8），复用了 rc6 的残余风险编号（S1–S13），本轮新增风险从 R1 起单独编号。
> 体例与 rc6 一致：每条写清"问题 → 处理 → 代码位置 → 测试证据 → 残余"。**代码位置写行号，测试证据写用例名**，两者都可以被 `grep` 复核。

---

## 一、本轮输入与结论速览

### 1.1 本轮要处理的 8 条审计发现

| 编号 | 一句话 | 收敛点 |
|---|---|---|
| BFC-01 | 写者租约"先检查后加锁"，两把锁不是同一个内核对象，Windows 上 restore 存在 TOCTOU | `bigfish/locks.py` 整文件重写 |
| BFC-02 | MCP 工具对自由文本没有字段级上限，一个字段能撑爆内存 | `bigfish/server.py` 的 `_validate_args` + schema |
| BFC-03 | `create_app()` 可被第三方 `uvicorn.run(host="0.0.0.0")` 绕过绑定校验 | `create_production_app()` + `LoopbackGuardMiddleware` |
| BFC-04 | 审计 manifest 里 `contains_plaintext_credentials: false` 与 `manual_review_required: true` 自相矛盾 | 改枚举 `credential_status` |
| BFC-05 | CLI 只有 restore 分支关库，其余分支连接悬空 | `main()` 拆 `_dispatch()` + `try/finally` |
| BFC-06 | `allowed-ports` 不打印预留端口，用户以为 3099–3101 可用 | 输出分 implemented / reserved |
| BFC-07 | 发布工程不闭环：无依赖锁、无 CI | `requirements.lock` + `.github/workflows/ci.yml` |
| BFC-08 | `v0.1.1-rc6` 的 tag 打在根提交上，无法溯源 | 本版起在真实提交上打带注释 tag（人工） |

### 1.2 结论

- BFC-01/02/03/04/05/06/07 六条已在代码里闭合，且每条都有可执行的用例；BFC-08 是发布流程动作，代码侧无对应物，本轮只记录不改。
- BFC-03 是本轮唯一"改变攻击面"的修复：它把"监听地址校验"从**调用约定**（文档里说别这么绑）变成了**连接级判定**（每根连接都判对端与本端地址）。这是双重防线，下面第五节单独展开。
- 未做的验证全部列在第八节，不在正文里含糊过去。

---

## 二、威胁模型 T1–T8

编号与 rc6 完全一致，不新增、不重排。每条的"rc7 状态"只写本轮**有新证据**的部分。

| 编号 | 威胁 | rc6 状态 | rc7 变化 |
|---|---|---|---|
| **T1** | 库文件或备份包泄露，导致明文执行票外流 | 票只存 `token_hash`；审计包改列白名单 | **有加强**：manifest 不再做布尔承诺，改为 `credential_status` 三值枚举 + `credential_detail` 逐项说明含什么、不含什么。见 BFC-04 |
| **T2** | 执行器伪造审查票（冒充 gpt/gemini 投票） | `add_review` 校验角色与模型绑定 | **无新证据**，沿用 rc6 |
| **T3** | 执行器自己批准自己的任务 | `approve()` 只认 `human_owner`；执行器不得参与投票 | **无新证据**，沿用 rc6 |
| **T4** | 审批被跨版本降级绕过 | 恢复时 Authorization 作废、审批要求版本条件更新 | **有加强**：`TestLeaseAtomicity` 证明"恢复被拒后库一个字节没变"，降级路径更难被"半截恢复"打开 |
| **T5** | 恢复过程把库搞坏（半替换、并发写） | `.prerestore` 副本 + 排空 + 授权作废 | **有加强**：BFC-01 让恢复与写者的互斥变成内核级原子操作，Linux 上有实测，Windows 侧代码已改、未实跑（见第八节） |
| **T6** | 长期任务被看门狗误判失联 | 判定与更新同事务 + 条件更新 | **无新证据**，沿用 rc6 |
| **T7** | 服务暴露到公网，外部来客可读可写 | `check_bind_host` 挡非回环 + `BodyLimitMiddleware` 挡大包 | **有加强（本轮最大变化）**：BFC-03 增加连接级闸门，即使误绑 0.0.0.0 外部请求也只能拿 403。见第五节 |
| **T8** | 模型身份可被密码学证明 | 明确不实现，只做角色绑定 | **无变化**。本轮没有引入任何签名/证明机制 |

---

## 三、逐项回答 8 个问题

### 问题 1：能否绕过审批？

**结论：不能。** 四个入口全部被堵，且堵法是"服务端定值"，不依赖调用方自觉。

**(a) 审批人身份是硬编码的。**
`bigfish/core.py:672` 直接判 `actor not in (R.HUMAN_OWNER,)` 就抛"只有人类所有者可以执行审批"。执行器令牌无论怎么调都进不来——不是"执行器默认不许"，而是"只许人类所有者"，白名单语义。

**(b) 状态机不允许从草稿直接跳到已批准。**
`bigfish/core.py:680` 的 `allowed = {PENDING_APPROVAL, REVIEW_PASSED}`，任何不在这个集合里的状态都会被拒。`DRAFT` / `IN_REVIEW` / `RETURNED` 都必须先走完审查。

**(c) 版本条件更新挡并发。**
`bigfish/core.py:687` 比对 `expected_version`，`:694` 要求审查票收齐，最后的 `UPDATE ... WHERE id=? AND current_version=? AND status=?` 靠 `rowcount != 1` 拦截并发窗口——两个进程同时批准，只有一个能成功。

**(d) HTTP/MCP 面根本不暴露审批工具。**
`bigfish/server.py:546` 的 `TOOL_PERMISSIONS` 只有五个工具（list_tasks / claim_task / heartbeat / submit_result / task_status），没有 approve。`tests/test_v011_p1.py:311` 的 `test_no_approval_tool` 直接断言 MCP 工具清单里不存在审批入口。

**测试证据**：`tests/test_v011_rc3.py:129`（`test_cancel_before_approval_cannot_approve`）、`tests/test_v011_rc3.py:199`（`test_votes_before_submit`）、`tests/test_v011_p1.py:311`（`test_no_approval_tool`）、`tools/selfcheck.py` 的"审批只能在进程内"一项。

**残余**：审批是单点的人类动作，没有二次确认、没有冷却期。人手滑点错，系统不会拦。

---

### 问题 2：任务取消之后，还能被领取吗？

**结论：不能，且"取消后领取"与"领取后取消"两条路径分别有独立防线。**

**(a) 领取时读取消标记。**
`bigfish/core.py:781`：如果 `control.cancel_requested` 或 `cancel_requested_at` 有值，直接抛"任务已被请求取消，不能领取"。

**(b) 领取的 UPDATE 自带取消条件。**
`bigfish/core.py:803-804` 的 `UPDATE tasks SET status=DISPATCHED ... WHERE id=? AND status=APPROVED AND current_version=? AND cancel_requested_at IS NULL`。即使 (a) 的读与 (b) 的写之间被插入了取消动作，`rowcount` 也会是 0，抛"任务已被其他执行器领取"。

**(c) 取消与领取在同一事务里排队。**
`claim()` 从 `:769` 起整段包在 `with self.db.tx()` 里；(a) 的读和 (b) 的写同事务，不存在"读到未取消、写完才被取消"的窗口。

**(d) 已经派发出去的任务被取消时，要求执行器确认。**
`tests/test_v011_rc3.py:209`（`test_cancel_while_dispatched_needs_ack`）验证取消进入 `CANCELLING` 而不是直接 `CANCELLED`，必须等执行器 ack。

**测试证据**：`tests/test_v011_rc3.py:117`（`test_cancel_after_approved_cannot_claim`）、`tests/test_v011_rc3.py:140`（`test_cancel_and_claim_concurrent`，用 `threading.Barrier(2)` 双线程真抢，断言"只能有一个合法结果，且都不许取消后又签出票"）、`tests/test_v011_rc3.py:209`。

**残余**：`update_version` 会把 `cancel_requested_at` 清空（`bigfish/core.py:394-400`）。这是**故意**的——否则"取消→改 Prompt→重新审批"的任务永远领不到；但副作用是"取消"不是终态，改一版就复活成可领取。这是产品语义而非漏洞，写在这里以免被误读成回归。

---

### 问题 3：从备份恢复之后，旧执行票会复活吗？

**结论：不会。靠 epoch 单调递增，旧票在恢复后必然失效。**

**(a) 每张票带签发时的 epoch。**
`bigfish/core.py:799` 取 `restore_epoch()`，`:814-816` 的 `INSERT INTO tickets` 把 epoch 一起写进去。

**(b) 每次恢复都把 epoch 抬到 `max(现有, 快照内) + 1`。**
`bigfish/backup.py:409-418`：`new_epoch = max(captured_epoch, staged_epoch) + 1`。注意 `max` 的两个入参——只取快照里的 epoch 是不够的（从更老的快照恢复会倒退），必须同时和当前库的值取大。

**(c) 校验票时比对 epoch。**
`bigfish/core.py:907-909`：`int(t["epoch"]) != epoch` 就抛"执行票已失效（数据库已从备份恢复），请重新领取"。

**测试证据**：`tests/test_v011_rc3.py:451`（`test_old_ticket_invalid_after_restore`）、`tests/test_v011_rc5_restore_authority.py:199`（`test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot`，先把 epoch 抬到 7 再恢复 epoch=0 的快照，断言恢复后是 8 而不是 0）、`tests/test_v011_rc5_restore_authority.py:176`（`tickets_revoked` 计数非零，是真实吊销不是"看起来吊销"）、`tests/test_v011_rc5_restore_authority.py:223`（`replay_rejected_after_cancel`，取消后重放不再签发）。

**残余**：epoch 是库内的整数，没有签名。能写库的人就能改 epoch。威胁模型里"能写库"已经等同于全权（T1 的前置条件），这条不额外增加风险。

---

### 问题 4：Watchdog 会不会误杀长期任务？

**结论：设计上不会，并且"判断"和"更新"在同一个事务里，不存在读到旧时间戳就杀掉的窗口。**

**(a) 心跳续租，且续租有时间上限。**
`tests/test_v011_p1.py:139`（`heartbeat_extends_lease`）、`:151`（`lease_capped_by_max_ttl`）、`:158`（`ticket_never_exceeds_max_ttl_from_issue`，从签发时刻算绝对上限，防止反复续租无限延长）。

**(b) 按"当前活票"查心跳，不按 `task_id` 取最新 run。**
`bigfish/core.py:1213` 的注释写明：按 `task_id` 取最新 run 会读到**历史 run** 的心跳，把真正失联的执行器误判成活着——这是反方向的坑，同样致命。

**(c) 判断与更新同事务 + 条件更新。**
`bigfish/core.py:1200` 的 docstring："判断与更新在同一个事务里，并对状态做条件更新，所以心跳刚续租不会被事务外读到的旧时间戳误杀。" `:1240` 的 `UPDATE ... WHERE id=? AND status=?` 靠 `rowcount != 1` 跳过被别人改过的行，不覆盖。

**(d) 取消中的任务用 `max(last_beat, cancel_requested_at)` 计时。**
`bigfish/core.py:1233`：进入 `CANCELLING` 后，等待 ack 的计时起点取两者较大值，避免"刚取消就被判超时"。

**测试证据**：`tests/test_v011_p1.py:56`（`watchdog_blocks_without_gui`）、`:74`（`watchdog_singleton_per_db`）、`:80`（`watchdog_survives_sweep_error`，扫一次出错不能把看门狗带走）、`tests/test_v011_p3.py:221`（`watchdog_runs_in_headless`）、`tests/test_v011_rc3.py:314`（`watchdog_and_heartbeat_race`，心跳与扫描真并发）。

**残余**：`tests/test_v011_rc6.py:233` 记录了"停看门狗线程超时抛 TimeoutError"的路径，`:263` 记录"停服失败不置 paused 抛已中止"。这两条是**停服**路径的保守处理（宁可不进入维护模式也不假装成功），不是误杀，但会让"暂停"在极端情况下失败——这是有意的取舍。

---

### 问题 5：幂等键并发时会不会重复执行？

**结论：不会重复执行，副作用写入也只有一份。**

**(a) 幂等检查与业务写入在同一个事务。**
`bigfish/core.py:769` 起整段 `with self.db.tx()`；`:766` 的 `idem_scope_key = "claim:{ex}:{tid}:{idem or '-'}"` 把执行器、任务、幂等键三者编进作用域，不同执行器用同一个键不会互相顶掉。

**(b) 同键不同请求直接拒绝。**
`bigfish/core.py:1283`：同一个键带着不同 fingerprint 回来，抛"同一幂等键被用于不同请求，已拒绝"——防止"键不变、请求偷偷换内容"。

**(c) 不带键必须用随机 nonce。**
`bigfish/core.py:801-803` 的注释点破了根因："否则第一次领取→BLOCKED→重新批准→再领取会算出完全相同的票，旧票持有者可以继续用"。这是 rc4 遗留的真缺陷，现在没有幂等键就随机化。

**(d) 幂等记录里不许出现票与 Prompt。**
`_idem_put`（`bigfish/core.py:1300`）落库前过 `assert_no_secrets` + `sanitize`；`assert_no_secrets`（`:1368`）对 `prompt` 字段名和任何含凭据形态的字符串直接抛错。

**测试证据**：`tests/test_v011_rc4.py:78`（`idem_key_with_colon_replays_same_ticket`，键里带冒号不能被 split 出别的票）、`:86`（不签第二张票）、`:211`（`reclaim_after_block_issues_new_ticket`）、`:230`（`old_ticket_revoked_on_new_claim`）、`:252`（`no_idem_key_uses_random_nonce`）、`:265`（`derive_ticket_is_deterministic_per_nonce`）、`tests/test_v011_p0.py:160`（`claim_idempotency_has_no_ticket_or_prompt`）、`tests/test_v011_rc3.py:273/283/297`（领取/心跳/提交三类都不重复写副作用）。

**残余**：幂等记录只按 TTL 清理（`purge_idempotency`，`bigfish/core.py:1307`），过期之前表会缓慢长大。这是空间问题，不是安全问题。

---

### 问题 6：非回环监听还能绕过吗？（BFC-03，两层防线）

**结论：单靠"文档里说别这么绑"确实能被绕过；rc7 之后有两层，第二层不依赖调用方。**

**第一层：带闸门的工厂。**
`create_production_app()`（`bigfish/server.py:599`）返回 `(app, host)`，返回之前先过 `check_bind_host()`（`:607`）。`check_bind_host`（`:49`）只放行 IPv4/IPv6 回环，其他一律 `BindError`，文案明确指向"用 Tailscale Serve 转发，不要直接绑 0.0.0.0"。GUI 的 `ApiServer` 与 CLI 的 `serve` 都改走这个工厂（`bigfish/gui.py:98-106`、`bigfish/server.py:618`），不再各写一套判断。

**第二层：连接级中间件。**
`LoopbackGuardMiddleware`（`bigfish/server.py:121`）挂在每个 app 上（`:244`）。它对**每一根 HTTP 连接**判两个字段：

- `scope["client"][0]`：对端 IP，局域网/公网来客必然不是回环；
- `scope["server"][0]`：本端 IP，绑 0.0.0.0 时填的是该连接**实际落到的本地地址**，外部来客连上来是网卡地址。

两者任一非回环，`:166-171` 直接回 403，body 是"本服务只接受回环访问，当前连接来自 …"。关键在"这两个字段由传输层填充、客户端伪造不了"——写在 `:129-132` 的 docstring 里，这是整个设计成立的前提。

**唯一豁免是进程内常量。**
`TRUSTED_PEERS = frozenset({"testclient", "testserver"})`（`:140`），只给 TestClient 用，它不经过任何 socket，外部拿不到这个值。`:246` 的用例专门断言这个集合**恰好**是这两个常量，任何真实地址混进去就失败——防止后人"为了方便"把 127.0.0.1 加进白名单（那样中间件就废了）。

**为什么不是一层就够。** 第一层保护的是"我们自己启动服务时不绑错"，第二层保护的是"别人用我们的 app 启动服务时绑错"。后者是审计的真实攻击路径：`uvicorn.run(create_app(...), host="0.0.0.0")` 根本不经过 `check_bind_host`。

**测试证据**：`tests/test_v011_rc7.py:223`（`0.0.0.0`/`::`/`[::]`/`192.168.1.10`/`100.64.1.2` 全拒）、`:228`（回环全放行）、`:234`（`TestClient(app, client=("192.168.1.9", 54321))` 读 `/health` 得 403）、`:241`（回环得 200）、`:246`（受信名单封闭）。

**残余**：见 R5。中间件只覆盖 HTTP scope，`scope["type"] == "websocket"` 不经过判断（当前版本没有 WS 端点，将来加要补）。

---

### 问题 7：凭据是否明文持久化？（含 `credential_status` 三值语义）

**结论：数据库和备份包里都没有明文票、没有明文 token、没有密钥文件。这一结论在 rc7 换了一种**更诚实的表述方式**。**

**（a）库里存什么。**
执行票只在签发那一刻以明文返回给调用方，库里存的是 `token_hash`（SHA-256）。`tests/test_v011_p0.py:174`（`db_file_has_no_plaintext_ticket`）直接扫库文件字节，找不到票原文。

**（b）操作包里存什么。**
`bigfish/backup.py:141` 的 `credential_status` 是 `"hashes-only"`，`:142-144` 的 `credential_detail` 逐项写清："含 executors.token_hash 与 tickets.token_hash（SHA-256，不可逆）；不含明文 token、不含执行票原文、不含密钥文件。" `tests/test_v011_p0.py:209`（`bundle_has_no_ticket`）扫整包字节。

**（c）审计包里存什么。**
`bigfish/backup.py:221` 是 `"pattern-scrubbed-unverified"`，`:222-230` 的 `credential_scan` 把"怎么扫的、扫了哪些字段、覆盖不到什么"全写进 manifest：`mode: pattern-based`、`patterns` 列出实际规则、`fields_scanned` 明说含 `task_versions.prompt` 正文、`coverage` 承认"换行拆分、编码变形、私有格式的密钥无法保证命中"、`manual_review_required: True`。

**（d）三值枚举的语义（BFC-04 的核心改动）。**

| 值 | 含义 | 谁能用 |
|---|---|---|
| `none` | 明确不含任何凭据形态 | 目前没有包会产出这个值，预留给"确认过且结构上不可能含凭据"的包 |
| `hashes-only` | 只含不可逆哈希，无明文 | 操作包（`export_bundle`） |
| `pattern-scrubbed-unverified` | 按已知模式脱敏过，**未证明干净** | 审计包（`export_audit_bundle`） |

改动的理由写在 `bigfish/backup.py:135-140`：旧的布尔字段叫 `contains_plaintext_credentials`，写 `False` 等于对外宣称"我检查过了、保证没有"。而这个保证**不能**由"白名单里没有 token 列"推出来——白名单只管列名，管不了自由文本里贴了什么。改成枚举后，字段只陈述"实际观测到的事实"，不做承诺。

**测试证据**：`tests/test_v011_rc7.py:262`（操作包是 `hashes-only` 且布尔字段**不出现**，用例名就叫"布尔承诺又回来了"）、`:270`（审计包是 `pattern-scrubbed-unverified` 且 `manual_review_required` 为真）、`:278`（两种包的取值都落在允许集合内，枚举可被机器判定）；另有 `tests/test_v011_rc5_key_heal.py:82`（密钥轮换吊销全部活票）、`:92`（旧票不能鉴权）、`tests/test_v011_rc5_review_audit.py:246`（Prompt 正文里的票被脱敏）。

**残余**：见 R2、R3。

---

### 问题 8：跨版本恢复呢？

**结论：向下兼容（老库能开、能迁移），向上明确拒绝（新库不许被老程序打开）。**

**(a) 老 schema 的库能迁移到当前版本。**
`tests/test_v011_rc3.py:507`（`test_restore_older_schema_migrates`）构造一个 `schema_version=2` 的库——用 `ALTER TABLE ... DROP COLUMN` 真的把 `tickets.epoch`、`executors.model`、`tasks.cancel_requested_at` 三列删掉——然后打包恢复。断言：恢复后 `schema_version` 变成 4，且备份里那张 `APPROVED` 的任务被**冻结**成 `BLOCKED`，`blocked_reason` 含"冻结"，`frozen_approved >= 1`。

**(b) 为什么必须冻结而不是保留。** 一张老库里的 `APPROVED` 可能早已被撤销过（只是撤销记录在更新的库版本里）。恢复它等于把一个"可能已经作废的派工许可"重新发出去。冻结是唯一保守的选择，注释写在 `tests/test_v011_rc3.py:526-527`。

**(c) 未来 schema 明确拒绝，且拒绝时不动原库。**
`bigfish/backup.py:262-266` 的 `_schema_version_of` 比对：`ver > SCHEMA_VERSION` 时报"备份 schema 版本 N 高于当前程序支持的 M"。`tests/test_v011_rc3.py:533` 断言两件事：抛 `RestoreError`（文案含"高于"），并且**原库任务状态仍是 `APPROVED`**——被拒绝的恢复不能有任何副作用。

**(d) 坏包直接拒。**
`tests/test_v011_rc3.py:550`（`test_restore_corrupted_bundle_rejected`）；`bigfish/backup.py:308` 包装 zip 异常为"备份包损坏"，`:327` 校验 sha256 与 manifest 一致。

**(e) 打不开的库不能把租约攥在手里。**
`tests/test_v011_rc6.py:61` 这条用例很关键：即使 `Database` 构造因为版本过高抛 `DatabaseVersionError`，写者租约也必须已经放掉——否则"库打不开"会变成"库永远恢复不了"的死锁。这是 rc6 补的，rc7 的 BFC-01 重写没有破坏它。

**测试证据**：`tests/test_v011_rc3.py:507 / :533 / :550`、`tests/test_v011_rc6.py:61`、`tests/test_v011_rc5_restore_authority.py:199`。

**残余**：迁移逻辑只有 `v2 → v4` 一条实测路径。v3 库怎么迁移、中间版本缺哪些列，没有专门用例。

---

## 四、rc7 新增的 5 个安全面

这一节只写本轮**新增**的东西。上面第三节复用的是既有机制，这里的是新代码。

### 4.1 租约原子性（BFC-01）

**问题。** 旧实现分两步：先扫"有没有别的写者在用"，再锁**另一把**锁。两把不是同一个内核对象，扫描返回后新实例仍能在新文件上建锁，互斥失效。Windows 上尤其明显——审计把它定性为 restore 的 TOCTOU。

**处理。** `bigfish/locks.py` 整文件重写（265 行），只保留**一把访问锁**，写者共享、恢复排他：

- POSIX：`flock(LOCK_SH)` 写者 / `flock(LOCK_EX)` 恢复（`bigfish/locks.py:66`、`:128`、`:231`）；
- Windows：`SLOTS = 64`（`:37`），每个写者从 `os.getpid() % SLOTS` 起往后找一个空字节并锁住 1 字节（`:149-151`），恢复端一次锁整段 64 字节（`:229`）。全满则抛"数据库正在被恢复，或同时使用的实例过多"。

还有两个必须守住的细节：

1. **绝不删锁文件。** `bigfish/locks.py:187-190` 写明理由：`unlink` 之后新来的实例会在**新的 inode** 上加锁，老实例还锁着旧 inode，两边各用各的锁。rc7 一度这么写过，`tests/test_v011_rc6.py` 的"另开实例阻止恢复"当场失败。gate 文件同理（`:265`）。
2. **同进程引用计数。** Windows 上同一进程开两个 `Database`，第二个不能把第一个的槽位抢走（`:171-180` 的 `held[1] -= 1` 归零才解锁）。

**正确性来源写在 `exclusive_access` 的 docstring 里**（`:203-208`）：gate 锁只用来**区分错误**（"另一个恢复在跑" vs "有写者在用"），正确性在访问锁上。

**测试证据**：`tests/test_v011_rc7.py:93 / :118 / :134 / :144 / :153`（5 项，逐条见测试报告 4.1）。

**残余**：见 R4、R6。

### 4.2 MCP 字段上限（BFC-02）

**问题。** MCP 工具此前只校验"字段在不在、类型对不对"，自由文本没有长度上限。一个 `detail` 字段可以塞满内存——整体上限 `MAX_BODY_BYTES` 挡得住单次请求，挡不住"在允许的包体里塞一个巨型字段"。

**处理。** `MAX_TEXT_FIELD = 4 * 1024 * 1024`（`bigfish/server.py:41`，可用 `BFC_MAX_TEXT_FIELD` 覆盖）。五处自由文本全部声明 `maxLength`：心跳的 `detail`（`:183`、schema `:508`），提交结果的 `log`/`diff`/`tests`/`note`（`:191-194`、schema `:525-528`）。服务端 `_validate_args`（`:439-440`）按 schema 里的 `maxLength` 逐个校验，超了返回"参数 X 超过长度上限 N 字符（实际 M）"。

**为什么要同时改 schema 和校验。** 只改校验，模型侧看不到边界，会反复撞墙；只改 schema，客户端可以无视声明直接发。两边都要有。

**测试证据**：`tests/test_v011_rc7.py:178 / :186 / :196 / :207`（4 项，含"刚好等于上限必须放行"的边界与"被拒的提交不留半条 artifact"）。

**残余**：上限是固定值，没有按字段分级（`diff` 和 `note` 用同一个 4 MiB）。

### 4.3 连接级回环闸门（BFC-03）

见第三节问题 6，此处不重复。补充一点代码事实：`create_app()` 仍然返回裸 app（测试与进程内调用需要），闸门在中间件里；`create_production_app()`（`:599`）是给真实入口用的，返回 `(app, host)` —— rc7 顺手修了它的返回类型标注，原来写 `-> FastAPI` 但实际返回元组，这是 pyflakes 抓不到、但会让类型检查器误判的错。

**残余**：见 R5。

### 4.4 manifest 声明语义（BFC-04）

**问题。** `contains_plaintext_credentials: false` 是**布尔**，读起来像承诺；同一份 manifest 里又有 `manual_review_required: true`。两者放一起自相矛盾——"保证没有凭据"和"必须人工复核"不能同时为真。

**处理。** 改枚举（三值语义见第三节问题 7），并把审计包的 `credential_scan` 写全：扫的模式、扫的字段、覆盖不到的情形、命中数、是否必须人工复核。设计原则写在 `bigfish/backup.py:135-140`：**manifest 只陈述观测到的事实，不做保证**。

**测试证据**：`tests/test_v011_rc7.py:262 / :270 / :278`（3 项，其中 `:267` 和 `:276` 两条断言是 `assertNotIn("contains_plaintext_credentials", m)`——防回归，布尔承诺不许回来）。

**残余**：见 R2。

### 4.5 移动端角色边界（MOBILE）

**问题。** 移动端要能看任务、能留言，但不能领任务、不能导出库。这需要在角色层显式建模，而不是"给个执行器令牌凑合用"。

**处理。** `bigfish/roles.py` 新增 `MOBILE_VIEWER = "mobile_viewer"`（`:15`），权限集**只有三项**：`task.read` / `event.read` / `review.create`（`:46`）。注释写明取舍："刻意不含 `task.claim` / `run.*` / `approve` / `backup.*`：手机丢了也不至于能把任务领走或把库导出去。"

配套三处：

- `_DB_ROLE_MAP` 加 `"mobile": MOBILE_VIEWER`（`:54-55`）——**这是最容易被忽略的一处**。`role_for_db_row`（`:77`）对未知角色会回落到 `EXECUTOR_DSH`（`:94`），也就是说"库里写了个没登记的角色名"等于"拿到执行器权限"。新增角色必须同时改 `ALL_ROLES` / `PERMISSIONS` / `_DB_ROLE_MAP`，警告写在 `:51-53`。
- `register_executor` 的白名单从"查表默认放行"改成 `if role not in R.DB_ROLES: raise`（`bigfish/core.py:163`）——闭合而不是开放。
- 令牌前缀 `bfc_mb_`（`bigfish/core.py:174`），日志与审计里一眼可辨，也为将来按前缀做限流留了抓手。

**测试证据**：`tests/test_v011_rc7.py:348 / :354 / :361 / :366 / :371`（5 项，其中 `:366` 是"db role 绝不回落到执行器"，`:371` 是"未知角色注册必须抛"）。

**残余**：见 R7。角色有了，端点没有——移动端的 REST/SSE 还没实现（测试报告第 7 节 U6）。

---

## 五、残余风险 R1–R8

编号 R1–R8 是本轮视角的完整清单，每条标注 rc7 状态。与 rc6 的 S1–S13 有重叠的地方在备注里指出。

| # | 残余风险 | rc7 状态 | 说明 |
|---|---|---|---|
| **R1** | Windows 真·双进程租约未实跑 | **仍在**（rc6 S1） | BFC-01 的 Windows 分支（槽位锁）全部带 `# pragma: no cover - 仅 Windows`，Linux 上走的是 `flock` 分支。代码写了，测试没跑。这是本轮最大的证据缺口 |
| **R2** | 审计包脱敏仍是模式匹配，不是 DLP | **仍在**（rc6 S2/S10） | `credential_scan.mode` 就是 `pattern-based`，`coverage` 自己承认"换行拆分、编码变形、私有格式的密钥无法保证命中"。rc7 只是把这个事实写进了 manifest，没有让它变安全 |
| **R3** | 数据库文件未加密 | **仍在**（rc6 S11） | 保护靠文件系统权限（POSIX `0600` / Windows `icacls`）。能读到文件的人能读到全部任务正文 |
| **R4** | `exclusive_access` 的 timeout 是重试轮询，不是排队 | **仍在**（rc6 S6） | `bigfish/locks.py:227-234` 每 50 ms 试一次，到期抛错。没有公平性保证，高并发下可能一直抢不到 |
| **R5** | 连接级闸门只覆盖 HTTP scope | **部分修（新）** | WebSocket / 长连接不经过 `LoopbackGuardMiddleware`。当前版本没有 WS 端点，将来加必须同时补 |
| **R6** | 更早版本的实例不被租约看见 | **仍在**（rc6 S7） | 旧版程序用旧的锁文件命名，新版看不见它。升级过渡期两个版本同时跑是危险的 |
| **R7** | 移动端只有角色，没有端点 | **部分修（新）** | `MOBILE_VIEWER` 的权限边界成立，但 REST/SSE 端点未实现。当前没有任何"移动端能做什么"的实际路径 |
| **R8** | `create_app()` 仍可被裸用 | **已缓解**（rc6 S3 → 缓解） | 第二层中间件让"裸用"不再是致命问题（外部来客 403），但裸用本身没有被禁止。文档与命名上仍未强制走 `create_production_app()` |

**rc7 已修掉的**：rc6 的 S3（`create_app` 裸绑全网卡 → 现在有连接级闸门）、S9（MCP 无字段级上限 → 现在有 `MAX_TEXT_FIELD`）、S5 的一半（租约不再靠可删的标记文件判断，改成一个内核对象；但"锁文件被手工删掉"的场景仍未覆盖）。

**rc7 明确没动的**：S2/S10（脱敏深度）、S11（库加密）、S6（timeout 语义）、S7（跨版本可见性）、S12（`stop_watcher` 只 join 不查 `is_alive`）、S13（"回滚也失败→保留维护模式"仍无自动化覆盖）。

---

## 六、本轮未做的安全验证

以下项目本轮**没有做**，任何地方都不代表它们通过了。列出来是为了让下一位审阅者知道边界在哪。

1. **Windows 真机双进程抢租约。** 只有 Linux 上的线程级实测。Windows 的槽位锁（`SLOTS = 64`）与 `msvcrt.locking` 语义在真机上完全没有验证。这是 BFC-01 的核心，也是最大缺口（R1）。
2. **真实的公网暴露演练。** BFC-03 的验证用的是 `TestClient(app, client=("192.168.1.9", 54321))` 伪造 scope，不是真从另一台机器打过来。中间件的判据本身是传输层字段，伪造测试能覆盖逻辑，但覆盖不了"真实网卡绑 0.0.0.0 时 `scope["server"]` 到底填什么"——这一条需要真机。
3. **WebSocket / SSE 长连接的闸门行为。** 当前无此端点，未测（R5）。
4. **跨设备 MCP 闭环。** rc2 时期跑过 11/11，本轮对 `server.py` 改动较大（BFC-02 改校验、BFC-03 加中间件），闭环没重跑（测试报告 U4）。
5. **依赖供应链审计。** `requirements.lock` 锁了 40 条精确版本，但没做 `pip-audit` / `safety` 之类的漏洞扫描；CI 的 `security-note` 作业只扫 token 形态（`ghp_` / `bfc_*` / PEM），不扫依赖 CVE。
6. **密钥文件被替换的场景。** 有密钥轮换的用例（`tests/test_v011_rc5_key_heal.py`），但没有"攻击者把 key 文件换成自己的"这种场景的验证。
7. **审计包实际外发后的人工复核流程。** manifest 要求 `manual_review_required: True`，但"谁来复核、复核什么、复核记录存哪"没有落地机制。
8. **压力与拒绝服务。** 字段上限挡住了单字段撑爆，但没有并发连接数上限、没有速率限制。本地单用户场景下影响有限，一旦有人把它放到共享机器上就是问题。
9. **移动端端点。** 角色有了，端点没有，所以"移动端能不能越权"这个问题目前**无法被验证**，只能被设计约束（R7）。
10. **exe 分发包的签名与完整性。** 未打包，未签名（测试报告 U2）。

---

## 七、语义变更与兼容性

| 变更 | 影响面 | 兼容处理 |
|---|---|---|
| manifest 去掉 `contains_plaintext_credentials`，改 `credential_status` | 任何按旧字段解析 manifest 的外部脚本 | **破坏性**。旧字段直接不写，没有兼容别名——保留别名等于保留那个布尔承诺 |
| `create_production_app()` 返回 `(app, host)` | 内部调用方 | GUI 与 CLI 已同步；对外无影响（这函数此前不存在） |
| `LoopbackGuardMiddleware` 默认挂载 | 所有 HTTP 请求 | TestClient 走 `TRUSTED_PEERS` 放行；真实回环请求不受影响；非回环从"能访问"变成"403" |
| `register_executor` 对未知角色抛错 | 内部调用方与脚本 | **破坏性**。此前未知角色会被当成执行器注册成功，现在抛 `CouncilError`。这是刻意的提权面收敛 |
| 新增 `mobile_viewer` 角色 | 角色枚举、`_DB_ROLE_MAP` | 纯新增，不改变既有角色的权限 |
| `MAX_TEXT_FIELD` 默认 4 MiB | MCP / REST 自由文本 | 超限从"接受"变成"错误"。可用 `BFC_MAX_TEXT_FIELD` 调整 |
| 锁文件命名与语义 | 与旧版本共存 | `access_lock_path` 产出 `.access.lock`，与 rc6 之前的标记方案不兼容。跨版本共跑是危险的（R6） |

---

## 八、复核方式

审阅者可用以下命令自行核对本文的每一处断言：

```bash
cd bigfish-council

# 1) 全量测试（期望 Ran 294 / OK）
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 2) 只看本轮 27 项
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest tests.test_v011_rc7 -v

# 3) 代码位置逐条核对
grep -n "credential_status" bigfish/backup.py          # BFC-04
grep -n "MAX_TEXT_FIELD"    bigfish/server.py          # BFC-02
grep -n "LoopbackGuardMiddleware\|check_bind_host" bigfish/server.py   # BFC-03
grep -n "SLOTS\|exclusive_access\|绝不删除" bigfish/locks.py          # BFC-01
grep -n "MOBILE_VIEWER\|_DB_ROLE_MAP" bigfish/roles.py                # MOBILE
grep -n "_close_quietly\|_dispatch" bigfish/cli.py                    # BFC-05
```

读法：本文每条"代码位置"都可以用上面的 `grep` 打到行；每条"测试证据"都可以用第 2 步的命令单独跑。**如果某条 grep 打不到，说明代码版本变了，本文结论需要重新评估**——不要假定结论仍然成立。

---

*本文件只描述 rc7 分支 `c0c54bc` 加未提交工作树的状态。合并之后的行号会漂移，届时以 `git log` 定位提交、以 `grep` 重新取行号为准。*
