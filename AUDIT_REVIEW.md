# BigFish Council v0.1.1-rc5 — 独立代码审计与修复记录

- 审计基线：main @ c6d663d
- 审计分支：review/rc5-hardening-20261120
- 版本基线：0.1.1-rc5
- 基线测试口径：244 项（Linux 结果由项目维护者提供；本次审计环境未执行测试）
- 本分支新增回归测试：5 项；预期总数 249 项，必须实际运行后才能确认
- 状态：Draft review；不构成 rc5 发布签署，不创建 tag

## 结论

rc5 的状态机、审批事务、执行票吊销、幂等重放校验和审计列白名单整体有明确设计，代码注释与测试覆盖也比普通个人项目完整。但恢复路径的两个关键安全保证在实现层面没有完全成立：

1. 维护检查与锁获取存在 TOCTOU 竞态；
2. 恢复世代在维护模式中读取失败后被静默降为 0。

另有密钥自愈失败仍继续启动、CLI 端口预检晚于数据库初始化等问题。本分支对上述四项做了小范围修复并补充五个回归测试。本分支代码尚未执行测试，不能把这些修复视为已验证。

## 本分支已修改

### F-01 — P1：维护模式 guard 与锁获取不是原子操作

位置：bigfish/db.py 的 query / one / execute / _Tx.__enter__。

原实现先调用 _guard()，再获取 _lock。线程 A 可以先通过 guard 并暂停；线程 B 随后取得锁、开启维护并释放锁；A 再取得锁继续访问数据库。这样维护屏障可能放过一个已经排队的读写操作。

修复：所有普通访问改为先获取同一把锁，再在锁内调用 _guard()；事务入口在 guard、BEGIN 或 SAVEPOINT 失败时统一释放锁。

新增测试：
- test_query_guard_is_atomic_with_lock
- test_transaction_guard_is_atomic_with_lock

### F-02 — P1：restore_epoch 读取异常被吞掉，单调递增保证失效

位置：bigfish/backup.py 的 restore_bundle()。

原实现先 enter_maintenance()，再调用 db.restore_epoch()。后者经过普通数据库访问 guard，因此在维护模式中抛出 MaintenanceError；外层 except Exception 把它吞掉并设为 0。随后 _prepare_staging() 计算 max(captured_epoch, staged_epoch)+1，但 captured_epoch 已经不是活库真实世代。

修复：在取得跨恢复锁并进入 _allow_during_maintenance() 后读取当前世代；不再吞掉异常或用 0 兜底。

新增测试：先导出 epoch=0 的快照，再把活库 epoch 提升到 7，恢复后必须得到 epoch=8。

### F-03 — P1：密钥一致性自愈失败时启动流程 fail-open

位置：bigfish/runtime.py:17-20。

原实现捕获 ensure_key_consistency() 的所有异常后直接继续加载密钥并启动 Council。若磁盘密钥与数据库指纹不一致，而修复在换钥前失败，应用可能继续使用未核验的旧密钥；这与该检查负责阻止旧执行票复活的安全职责冲突。原注释还声称失败会留在 audit/log，但这个 except 分支本身没有写日志。

修复：校验失败时关闭已打开的数据库连接并拒绝启动，抛出带原始异常链的明确错误。

新增测试：模拟密钥修复抛错，要求 open_council() 失败，且不得继续调用 load_or_create_key()。

### F-04 — P2：CLI restore 的端口预检发生在打开数据库之后

位置：bigfish/cli.py 的 main()。

原实现先执行 build()，而 build() 会调用 open_council()，其中包括打开数据库、初始化/迁移 schema、检查密钥一致性；之后才检查 API 端口是否被占用。端口保护来得太晚。

修复：restore 命令先加载 Settings 并做端口预检，通过后才调用 open_council()。

新增测试：模拟端口占用，断言在返回拒绝码之前没有打开数据库。

## 仍未解决、发布前必须处理

### F-05 — P1/P2：GUI 停服失败可能被当成成功

位置：bigfish/gui.py 的 MainWindow.pause_services() 与 ApiServer.stop()。

ApiServer.stop() 在 join 超时后没有检查线程是否仍存活；pause_services() 又吞掉 stop/watchdog 的异常，并在操作开始前就设置 _services_paused=True。恢复流程可能在 API 线程未确认退出时继续替换数据库；恢复结束时 ApiServer.start() 若看到旧线程仍活着会直接返回，服务可能无法重新启动。

建议：
- stop 超时后检查 thread.is_alive()，未退出就抛出明确错误；
- pause 只有在 API 与 watchdog 都确认停止后才置为 paused；
- 部分停服失败时要么可靠回滚已停止的服务，要么中止恢复并明确报告；
- 增加真实线程生命周期测试，不能只用 TestClient 或离屏窗口替代。

### F-06 — P1：跨进程恢复互斥仍然只是尽力而为

位置：bigfish/backup.py::_restore_lock、bigfish/cli.py 的端口检查。

恢复锁只互斥多个 restore 调用，不会阻止另一个普通 Database 实例继续写入。端口探测存在 TOCTOU；其他实例可以在探测后启动，或使用不同端口/直接打开同一数据目录。POSIX 下替换文件后，旧连接可能继续写旧 inode，造成数据分叉；Windows 通常表现为替换失败。CLI 的 --force 更是显式绕过端口预检。

本分支把预检前移，但没有声称解决跨进程互斥。建议在所有数据库写者与恢复者之间建立同一套 OS 级进程锁/维护租约；恢复持独占锁，普通进程启动与写入前也必须遵守。仅增加第二次端口探测不能消除竞态。

### F-07 — P1/P2：审计包的“无明文凭据”声明过强

位置：bigfish/backup.py:148-202、bigfish/security.py:34-93。

审计包白名单比列黑名单可靠，这是正确方向。但白名单仍包含 task_versions.prompt 等自由文本，脱敏仅覆盖 BFC token、Bearer、部分 sk-/GitHub/AWS key 和 PEM 私钥。Google API key、Slack token、Stripe sk_live_、通用 password=...、数据库连接串、换行/拆分/编码后的凭据等可能漏过。

因此 manifest 直接写 contains_plaintext_credentials=false，并标注“可外发”，无法由当前正则证明。建议改为明确的 pattern-based 检测声明、加入 manual_review_required=true，并考虑默认不导出 prompt 正文或提供显式 opt-in。不能把“未命中已知正则”表述成“保证无凭据”。

### F-08 — P2：create_app 本身不限制监听地址

serve()、GUI 与 CLI 启动入口有回环校验；但 create_app() 只返回 ASGI app，第三方代码仍可直接用 uvicorn.run(create_app(...), host="0.0.0.0")。这不是当前官方入口的直接漏洞，但应在 API 文档中明确 create_app 不负责 socket bind，部署必须经过 serve()，或提供封装后的唯一启动接口。

### F-09 — P2：自由文本请求没有统一 body size 限制

MCP/HTTP 的 log、diff、tests、detail 等自由文本进入 scrub 和截断前，整个请求体已经被解析进内存。当前部署假设是本机回环/Tailscale 私网，风险有所降低，但错误配置或被控制的执行器仍可能提交超大请求造成内存压力。建议在 ASGI 层设置请求体上限，并在字段 schema 增加长度限制。

## 对 AUDIT.md 中 Q1–Q8 的复核

- Q1 approve：目前看是安全的。核心更新处于 BEGIN IMMEDIATE 事务内，第二个并发审批会读到已变更状态；条件 UPDATE 是额外防线。可选 expected_version 是调用方陈旧版本保护，不是唯一并发锁。
- Q2 secret：仍是模式脱敏，不是拒写或完整 DLP；尤其不能把审计包的 false 声明当作保证。
- Q3 bind host：官方 serve/GUI/CLI 入口做了校验；直接用 ASGI app 自行绑定仍可绕过。
- Q4 restore epoch：基线实现存在 F-02；本分支修复并补充旧快照回滚测试。
- Q5 watchdog/restore：数据库维护竞态由 F-01 修复；GUI 停服确认仍有 F-05。
- Q6 heartbeat：时间戳已在事务锁内获取，当前实现方向正确。
- Q7 audit allowlist：白名单列控制有效；但 allowlist 不能证明自由文本没有凭据，且 dropped_tables 是静态清单。
- Q8 review revoke：撤票会对当前版本重算；对 DISPATCHED/RUNNING/CANCELLING 有显式阻断与吊销逻辑。已提交/已完成任务不自动回滚，属于需要在产品语义中明确的不可逆边界。

## 发布门槛

不要在这些项目完成前打 v0.1.1-rc5 tag：

1. 在 Linux 与 Windows 分别运行完整测试；本分支预期 249 项，实际数量和结果以运行输出为准。
2. 验证 F-01 两个并发回归测试、F-02 epoch=7 恢复旧快照得到 8、F-03 密钥自愈失败时拒绝启动、F-04 端口占用时数据库尚未打开。
3. 完成 Windows 真实 GUI 窗口验收，并验证 API/watchdog 停服超时路径。
4. 完成至少一次跨设备 MCP 闭环回归；注意此前 rc2 的 11/11 不能替代 rc5 验收。
5. 明确跨进程 restore lock 的处理方案，或把它作为有明确限制的发布阻断项。

本分支仅包含可追溯的窄范围修复和审计记录；没有修改 main，没有打 tag，也没有宣称测试已通过。