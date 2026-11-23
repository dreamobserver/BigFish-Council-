# BigFish Council / 大肥鱼议事厅 v0.1.1-rc7

Windows 本地控制中心 + Tailscale 私网 + VPS DSH 执行器。
人类在本机批准，云端只能领取**已批准**的任务，回传结果后仍需人类验收。

全程不开放任何公网端口，不使用 Tailscale Funnel。

> **rc7 是第五轮审计（Codex，在仓库里直接提的 8 条）后的收口版。**
> 八条 BFC-01～BFC-08 全部复核成立并已修，另加一个手机端只读角色：
>
> - **BFC-01** 跨进程租约「检查→上锁」之间有窗口 → `bigfish/locks.py` 重写成单一内核对象（约 300 行），租约文件句柄本身就是锁，不再有先查后锁；
> - **BFC-02** MCP 自由文本字段没有长度上限 → 5 个工具的自由文本字段全部声明 `maxLength`，超限返回 `isError: true` 且**不落库**；
> - **BFC-03** `create_app()` 不负责 bind，`TestClient` 走网络时绕过回环闸门 → 新增 `create_production_app()`（返回 `(app, host)`）+ ASGI 层 `LoopbackGuardMiddleware`，按**连接来源**判定，非回环一律 403；
> - **BFC-04** manifest 的 `contains_plaintext_credentials: false` 与 `manual_review_required: true` 自相矛盾 → 换成三值枚举 `credential_status`（`none` / `hashes-only` / `pattern-scrubbed-unverified`）；
> - **BFC-05** CLI 非 restore 分支不关库 → `main()` 用统一 `finally` 关库，进程退出后别人能立刻拿到排他租约；
> - **BFC-06** `allowed-ports` 不打印预留端口 → 输出拆成 `implemented`（3096/3097/3098）与 `reserved`（3099/3100/3101/8765/18765/18766）；
> - **BFC-07** 发布工程不闭环 → 新增 `requirements.lock`（精确 pin）与 `.github/workflows/ci.yml`（ubuntu + windows 双跑、含令牌泄漏扫描）。**exe 产物仍未做**（见 AUDIT.md §5 U2）；
> - **BFC-08** `v0.1.1-rc6` 的 tag 指向一个没有父提交的根提交，与 `main` 历史不连通 → 本版起 tag 打在真实提交上、带注释；
> - **MOBILE** 手机端只读角色 `MOBILE_VIEWER`：`register_executor(..., "mobile")`、token 前缀 `bfc_mb_`、有 `task.read` 没有 `task.claim`。REST/SSE 端点仍是 docs/ARCHITECTURE-FUTURE.md 的草案。
>
> **schema 本轮未变，仍为 v4**（`SCHEMA_VERSION=4` / `BACKUP_FORMAT_VERSION=4`），数据库不需要迁移。
> 测试从 265 项涨到 **294 项**，v0.1.0 的 21 项原样在位、无删除、无放宽断言。
>
> **rc7 仍是发布候选，不是生产版。** 平台 A（Debian 12 / Python 3.11.2）**294 项全绿**（`Ran 294 tests in 59.793s` + `OK`，退出码 0）；
> **平台 B（Windows 11 / Python 3.14.6）的 rc7 294 项尚未重跑**，上一次是 rc6 的 265 项全绿。
> 还欠 **GUI 真窗口验收**、**开机自启注册**、**跨设备 MCP 闭环重跑**。详见 AUDIT.md §5 与 CHANGELOG.md。
>
> 跨设备联调（VPS → 公司机 MCP 闭环 11/11）已在 rc2 打通，但那验的是 rc2 代码。详见 docs/NETWORK-TOPOLOGY.md §4.4。

---

## 0. 怎么拿到这个版本

唯一的正式分发渠道是本仓库的 **GitHub Release**，不额外挂任何分发站点：

<https://github.com/dreamobserver/BigFish-Council-/releases/tag/v0.1.1-rc7>

| 附件 | 用途 |
| --- | --- |
| `bfc-src-v011rc7.zip` | rc7 完整源码树。解压即用，不需要 git |
| `bfc-rc6-to-rc7.patch` | rc6 发布点 → rc7 的完整统一 diff，含文档 |
| `bfc-rc6-to-rc7-code.patch` | 同上但只含 `bigfish/ tests/ tools/ requirements*`，给只想读代码改动的人 |
| `SHA256SUMS.txt` | 上面每个附件的 sha256 |
| `gemini-review-bundle.txt` | 单文件纯文本审计快照，给读不到仓库正文的网页版模型直接喂 |

> 每个附件的**文件数与 sha256 以 Release 页与本包内 `SHA256SUMS.txt` 为准**——
> 写进 README 的固定值会在下一次重打包时失真，这里不再钉死。

只想要最新代码、不要发布包，直接 clone 即可：

```bash
git clone https://github.com/dreamobserver/BigFish-Council-.git
cd BigFish-Council-
```

## 1. 架构

```
┌──────────────────────── Windows（本机） ────────────────────────┐
│  BigFish Council GUI (PySide6)                                  │
│    ├─ 任务 / 议事 / 审批 / 监控 / 总览 五个页面                  │
│    ├─ 人类审批：只有 GUI 进程内能调用 Council.approve()          │
│    └─ 本地 HTTP + MCP 服务 127.0.0.1:3096（只绑回环，硬校验）    │
│         ▲                                                        │
└─────────┼────────────────────────────────────────────────────────┘
          │ Tailscale tailnet（100.64.0.0/10，无公网入口）
          │
┌─────────┴──────── VPS-HOST / DSH 容器 ────────────────────────┐
│  127.0.0.1:3098  tools/bfc-proxy.mjs  ← 回环跳板（必需）          │
│         │  转发 → CLIENT-B-TAILNET-IP:3096（公司机 CLIENT-B）      │
│  DSH MCP 客户端 → http://127.0.0.1:3098/mcp                      │
│    工具：council_list_tasks / council_claim_task /               │
│          council_heartbeat / council_submit_result /             │
│          council_task_status                                     │
└──────────────────────────────────────────────────────────────────┘
```

**为什么必须有 127.0.0.1:3098 跳板**：DSH 的 MCP 连接器只接受回环或 RFC1918 地址。
Tailnet 的 100.64/10 属于运营商级 NAT 段（CGNAT），不算私网，直连会被拒绝。
跳板只监听回环、只放行固定路径，不扩大暴露面。

**端口职责（唯一真源在 `bigfish/config.py:PORT_ROLES`）**：

| 端口 | 位置 | 用途 |
| --- | --- | --- |
| 3096 | Windows | 本地 API / MCP 监听（默认且只允许回环） |
| 3098 | VPS | `bfc-proxy.mjs` 回环跳板 |
| 3097 | 测试机 | 单测与 GUI 冒烟临时端口，不参与部署 |

绑定非回环地址会**直接拒绝启动**（`return 2`），不是提示。
VPS 侧 `tools/bfc-proxy.mjs` 同理：`isLoopback()` 在 `listen` 之前判定，
非回环直接 `exit(1)`（`BFC_PROXY_LISTEN_HOST` 填 `0.0.0.0`、`::`、`[::]` 一律拒绝）。

**端口预留 3099 / 3100 / 3101**（`config.PORT_RESERVED`，只登记不启用）：
3099 = 手机端 REST 网关、3100 = 手机端 SSE、3101 = 对外 MCP 桥接，三者均计划中、未实现，
**不并入 `PORT_ROLES`**。8765 / 18765 / 18766 属外部进程（windows-mcp 及其跳板），本项目只连不听。

## 2. 状态机

```
DRAFT ──送审──▶ IN_REVIEW ──GPT+Gemini 双 approve──▶ REVIEW_PASSED
                    │                                   │
                    └──有人 reject──▶ 退回修改           ▼
                                                  PENDING_APPROVAL
                                                       │ 人类批准
                                                       ▼
                                                   APPROVED ──领取──▶ DISPATCHED
                                                                        │
                                                       心跳/取消/暂停   ▼
                                                                     RUNNING
                                                                        │ 回传
                                                                        ▼
                                                    SUBMITTED（待验收）──▶ DONE
```

关键约束（有单测覆盖）：

- 单模型 approve 不推进状态；`kind=comment` 的审查意见不计票。
- 必须两个模型**对同一版本**都投 approve 才进 PENDING_APPROVAL。
- 改 Prompt 生成新版本，旧版本的票全部作废，重新回到 IN_REVIEW；**执行中（DISPATCHED/RUNNING）禁止改 Prompt**。
- 审批在一个 `BEGIN IMMEDIATE` 事务内完成，`UPDATE` 带版本条件，**跨版本漂移会被拒**。
- 未批准的 APPROVED 之外状态不能被领取；同一任务不能被领取两次。
- 执行器回传 completed 只到 SUBMITTED，**不会自动 DONE**，必须人类验收。
- 心跳超时自动转 BLOCKED（由**独立看门狗线程**执行，不依赖 GUI 是否开着）。
- 心跳会续租，但租约绝对上限 6 小时，无法靠刷心跳永久占坑。
- HTTP / MCP 侧**不存在**任何审批、改 Prompt、管理密钥的工具。

### 2.1 取消语义（rc3 新增）

取消分两条路，取决于任务有没有派出去：

```
DRAFT / IN_REVIEW / PENDING_APPROVAL / APPROVED ...
        │ 人类点取消
        ▼
    CANCELLED        ← 立即生效，所有执行票当场吊销

DISPATCHED / RUNNING
        │ 人类点取消
        ▼
    CANCELLING       ← 只是「请求停」，不假装远程已停
        │
        ├─ 执行器心跳时看到 cancel=True，ack 后调 confirm_cancel → CANCELLED
        └─ 执行器失联超时 → BLOCKED（等人工，不自动放行）
```

- `CANCELLING` 期间不许回传 `completed`，只能回传 `cancelled`。
- 取消过的任务**不可能**被再领取（claim 的 UPDATE 带 `cancel_requested_at IS NULL`）。
- 撤掉任一必要通过票会重新评估审批：未派工 → 回 IN_REVIEW；已派工 → 转 BLOCKED 并吊销执行票。
  审批记录**不删除**，但 `latest_approval()["effective"]` 会变 False，不能再当授权用。

## 3. 安装

### 3.1 Windows（控制中心）

一条命令搞定装依赖 + 跑单测 + 自检：

```powershell
cd D:\BigFishCouncil\bigfish-council
powershell -ExecutionPolicy Bypass -File setup.ps1
```

手动版：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe tools\selfcheck.py    # 16 项自检
```

打包成 exe（可选）：见 `build.ps1`（**本轮未在 Windows 上验证过产物**）。

### 3.2 启动

```powershell
# GUI（推荐）
.\start-gui.ps1
# 或
.\.venv\Scripts\pythonw.exe -m bigfish --data-dir "$env:LOCALAPPDATA\BigFishCouncil"

# 无界面（只跑 HTTP/MCP 服务，仍带看门狗）
.\.venv\Scripts\python.exe -m bigfish --headless --port 3096
```

数据目录默认 `%LOCALAPPDATA%\BigFishCouncil`，可用 `BIGFISH_DATA_DIR` 覆盖。

```
%LOCALAPPDATA%\BigFishCouncil\
├── council.db              SQLite（WAL 模式）
├── config.json             可安全公开的配置（不含任何凭据）
├── secrets\ticket.key      执行票派生密钥，0600，**不进备份包**
└── logs\
```

### 3.3 命令行（无 GUI 也能跑完整闭环）

```powershell
python -m bigfish.cli init
python -m bigfish.cli task-create --title "给 README 加一节" --goal "..." --risk low --prompt "..."
python -m bigfish.cli review-start <task-id>
python -m bigfish.cli review <task-id> --model gpt    --content "同意" --verdict approve --kind vote
python -m bigfish.cli review <task-id> --model gemini --content "同意" --verdict approve --kind vote
python -m bigfish.cli approve <task-id> --decision approve --note "人工批准"
python -m bigfish.cli executor-add dsh-cloud          # 只显示一次执行器 Token
python -m bigfish.cli reenable dsh-cloud              # 恢复后重新授权并轮换凭据（新 Token 只显示一次）
python -m bigfish.cli serve --port 3096
python -m bigfish.cli watchdog                        # 前台跑看门狗（服务里已自带）
python -m bigfish.cli overview
python -m bigfish.cli allowed-ports                   # 打印端口职责表
python -m bigfish.cli export backup.zip
python -m bigfish.cli inspect backup.zip
python -m bigfish.cli restore backup.zip             # 3096 有人在听会拒绝，需先停服
python -m bigfish.cli restore backup.zip --force       # 人工逃生门：确知风险时强行替换
```

## 4. 接入 DSH

1. Windows 侧先把服务开起来，确认回环健康检查通过：
   `Invoke-WebRequest http://127.0.0.1:3096/health`。
2. VPS 上启动跳板（只绑回环）：
   `BFC_PROXY_TARGET_HOST=CLIENT-A-TAILNET-IP BFC_PROXY_TARGET_PORT=3096 nohup node /绝对路径/tools/bfc-proxy.mjs > /tmp/bfc-proxy.log 2>&1 &`
   （**必须用绝对路径**，`cd` 在部分包装脚本里不生效）
3. 在 VPS 验证跳板：`curl -s http://127.0.0.1:3098/health`。
4. 把 `dsh-mcp-bigfish.json` 的 `mcpServers.bigfish-council` 段导入 DSH 的 MCP 连接
   （`mcp_connector_import_json`），URL 填 `http://127.0.0.1:3098/mcp`，
   Authorization 头填 `Bearer <执行器 Token>`。

执行器 Token 只在 `executor-add` 时明文显示一次，库里只存 SHA-256。

网络检查脚本（4 层，第 4 层跨机必须显式给 target，否则自己会说「不能算通过」）：

```bash
.venv/bin/python tools/net_check.py
.venv/bin/python tools/net_check.py --target-host CLIENT-B-TAILNET-IP --target-port 3096

# 跨机 MCP 闭环（真实链路，需先在 Windows 侧批准一张任务）
BFC_BASE_URL=http://CLIENT-B-TAILNET-IP:3096 BFC_TOKEN=<执行器 token> \
  .venv/bin/python tools/remote_check.py
```

## 5. 目录

| 路径 | 说明 |
| --- | --- |
| `bigfish/core.py` | 状态机与全部业务规则 + `Watchdog`（唯一可信源） |
| `bigfish/roles.py` | 四角色权限表（GPT / Gemini / 人类 / 执行器） |
| `bigfish/security.py` | 令牌哈希、执行票 HMAC 派生、密钥文件 |
| `bigfish/backup.py` | 备份导出 / 检查 / 原子恢复（含 WAL、`_block_inflight` 授权作废、维护模式排空、密钥自愈） |
| `bigfish/db.py` | schema 4、迁移、审计表、一致性快照、维护模式（`MaintenanceError`） |
| `bigfish/runtime.py` | `open_council()` 统一装配路径 |
| `bigfish/config.py` | 端口、路径、循环地址的唯一真源 |
| `bigfish/server.py` | 本地 HTTP API + 手写 MCP JSON-RPC |
| `bigfish/gui.py` | PySide6 五页界面 |
| `bigfish/executor_client.py` | 执行器侧纯标准库客户端（含 `ReceiptLog` 收据与 reconcile 对账） |
| `tools/bfc-proxy.mjs` | 127.0.0.1:3098 → Windows:3096 回环跳板（监听地址非回环即 `exit(1)`） |
| `tools/net_check.py` | 网络 4 层只读探测 |
| `tools/remote_check.py` | 跨机 MCP 闭环检查。**默认只读**，必须显式 `--task-id` 才闭环 |
| `tools/e2e_loopback.py` | 同机回环端到端（22 项断言） |
| `tools/selfcheck.py` | 16 项自检 |
| `tools/gui_smoke.py` | GUI 离屏冒烟（9 项） |
| `tools/dev_setup.sh` | Linux/VPS/WSL 独立开发环境安装脚本（不碰系统包） |
| `tools/start-headless.ps1` | **用计划任务后台起 headless（不要用 Start-Process，会堵死 windows-mcp）**。⚠️ 真机未验证通过，本轮实际用 WMI 派生 |
| `tools/stop-headless.ps1` | 停掉计划任务并释放端口（只杀确属 BigFish Council 的进程，不无差别清端口） |
| `build.ps1` | PyInstaller 打包脚本 |
| `docs/DELIVERY-v0.1.1.md` | **本版交付说明：逐阶段的改动/测试/迁移/回滚/未完成** |
| `docs/SECURITY-REVIEW-v0.1.1-rc6.md` | **rc6 安全复审：Astra 五问（F-05～F-09）逐条 + Q1–Q9 现状 + 残余风险** |
| `docs/DELIVERY-v0.1.1-rc6.md` | **rc6 交付说明：9 条修复、测试证据、迁移、回滚、未完成** |
| `docs/FIX-MATRIX-v0.1.1-rc6.md` | **rc6 修复总表：F-01～F-09 + Astra 发布门槛完成情况** |
| `docs/DIFF-SUMMARY-v0.1.1-rc6.md` | **rc6 代码 Diff 摘要（按文件给真实代码片段）** |
| `docs/TEST-REPORT-v0.1.1-rc6.md` | **rc6 测试报告：265 项逐文件统计、平台 A/B 双平台实测（B 首跑 2 错→修 2 个缺陷→全绿）** |
| `docs/TESTLOG-v0.1.1-rc6.md` | rc6 原始工作日志（含中途失败与修法） |
| `docs/MIGRATION-v0.1.1-rc6.md` | rc6 迁移说明（schema 未变 + 新增 .access.lock 文件 + manifest 结构变化） |
| `docs/ROLLBACK-v0.1.1-rc6.md` | rc6 回滚到 rc5 的步骤与验证 |
| `docs/FIX-MATRIX-v0.1.1-rc7.md` | **rc7 修复总表：BFC-01～BFC-08 + 手机只读角色，逐条给文件行号与回归测试** |
| `docs/DIFF-SUMMARY-v0.1.1-rc7.md` | **rc7 代码 Diff 摘要（按文件给真实代码片段）** |
| `docs/TEST-REPORT-v0.1.1-rc7.md` | **rc7 测试报告：294 项逐文件统计 + 27 项新增逐条说明** |
| `docs/SECURITY-REVIEW-v0.1.1-rc7.md` | **rc7 安全复审：八问逐条 + 五个新增安全面 + 残余风险** |
| `docs/DELIVERY-v0.1.1-rc7.md` | **rc7 交付说明：九条修复、验证证据、需授权项** |
| `docs/MIGRATION-v0.1.1-rc7.md` | rc6 → rc7 迁移（schema 未变，无需库迁移） |
| `docs/ROLLBACK-v0.1.1-rc7.md` | rc7 → rc6 回滚步骤与风险 |
| `docs/SECURITY-REVIEW-v0.1.1-rc5.md` | rc5 安全复核：R1–R9 逐条语义、威胁模型 T1–T8 现状、残余风险 |
| `docs/TEST-REPORT-v0.1.1-rc5.md` | **rc5 测试报告：244 项逐文件统计、平台 A 实测、平台 B 未跑** |
| `docs/TESTLOG-v0.1.1.md` | 实测日志与缺陷根因 |
| `docs/TESTLOG-v0.1.1-rc5.md` | rc5 原始工作日志（按时间顺序记命令与输出） |
| `docs/SECURITY-REVIEW-v0.1.1.md` | 威胁模型、凭据处理、残余风险 |
| `docs/NETWORK-TOPOLOGY.md` | 端口真源、数据流、4 层实测、跨机打通记录（§4.4） |
| `docs/MIGRATION-v0.1.1.md` | schema 1→2 迁移说明 |
| `docs/ROLLBACK-v0.1.1.md` | 回滚步骤与混跑危害 |
| `docs/WINDOWS-MANUAL-v0.1.1.md` | **Windows 本机人工操作说明（照着敲，每步都有预期输出）** |
| `docs/WINDOWS-MCP-RECOVERY.md` | windows-mcp 卡死的症状、根因与恢复步骤 |
| `docs/FIX-MATRIX-v0.1.1-rc3.md` | **rc3 修复总表：30+ 条，ID / 问题 / 文件 / 方案 / 测试 / 状态** |
| `docs/DIFF-SUMMARY-v0.1.1-rc3.md` | **rc3 代码 Diff 摘要（按模块给关键代码，不只概念）** |
| `docs/TEST-REPORT-v0.1.1-rc3.md` | **rc3 测试报告（双平台命令、退出码、未跑项与原因）** |
| `docs/SECURITY-REVIEW-v0.1.1-rc3.md` | **rc3 安全复审：9 问逐项回答 + 残余风险** |
| `CHANGELOG.md` | 版本变更记录 |

## 6. 测试

```powershell
cd D:\BigFishCouncil\bigfish-council
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

**294 个用例**（v0.1.0 原有 21 项全部保留 + 273 项新增，无 skip / 无 expectedFailure）：

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `test_core.py` | 15 | 状态机、审查门禁、审批、执行票、心跳超时 |
| `test_api.py` | 6 | HTTP 端点、MCP 往返、不暴露审批工具 |
| `test_v011_p0.py` | 16 | SQLite 备份/恢复、审批并发、无明文执行票 |
| `test_v011_p1.py` | 24 | Watchdog、执行票续期、心跳幂等、MCP 错误语义 |
| `test_v011_p2.py` | 26 | 四角色表、审查身份绑定、执行器边界、审计、手机端只读角色 2 |
| `test_v011_p3.py` | 20 | 端口拓扑、只允许回环、后台进程、GUI 冒烟 |
| `test_v011_rc3.py` | 26 | 状态机 7 + 并发 7（真线程 + Barrier）+ 恢复 7 + 安全边界 5 |
| `test_v011_rc4.py` | 33 | 幂等契约 6 + MCP 协议 7 + 多轮执行票 6 + 审查绑定 4 + 执行器客户端 4 + 辅助 6 |
| `test_v011_rc5_restore_authority.py` | 24 | 恢复禁用全部身份 3 + reenable 轮换凭据 3 + 冻结 APPROVED 2 + 恢复后重放拒绝 2 + 维护模式 6 + 维护期 HTTP 语义 4 |
| `test_v011_rc5_receipts.py` | 24 | ReceiptLog 读写与容错 6 + state_of 判定 6 + 落盘耐久性 2 + run_once 收据链 5 + 客户端接线 5 |
| `test_v011_rc5_review_audit.py` | 13 | 双通过事件落库 3 + 撤评论不动审批 3 + 事务内时间戳 2 + 审计包列白名单 5 |
| `test_v011_rc5_proxy.py` | 9 | 跳板监听硬闸门：0.0.0.0 / :: / [::] / 空值 / 内网 / 公网拒绝 6 + 回环放行 3 |
| `test_v011_rc5_key_heal.py` | 8 | 指纹首次补记不动票 2 + 轮换吊销活票 5 |
| `test_v011_rc5_cli_guard.py` | 4 | CLI 恢复探端口拒绝 1 + `--force` 绕过 1 + 端口探测助手 1 |
| `test_v011_rc5_dispatch_id.py` | 3 | claim 下发 dispatch_id 1 + 重放同号 1 + 重新派工换号 1 |
| `test_v011_rc6.py` | 16 | 跨进程租约 6 + 审计凭据声明诚实性 5 + 请求体上限 2 + GUI 停服确认 3 |
| `test_v011_rc7.py` | 27 | **rc7 新增**：租约原子性 5 + MCP 字段上限 4 + 回环闸门 5 + manifest 凭据枚举 3 + CLI 关库 2 + 发布工程 3 + 手机只读角色 5 |

自检 16 项：

```powershell
.\.venv\Scripts\python.exe tools\selfcheck.py
```

GUI 冒烟（无显示器时用离屏模式）：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe tools\gui_smoke.py
```

平台 A（Debian 12 / Python 3.11.2 / SQLite 3.40.1）**rc7 的 294 项全绿**（实测 `Ran 294 tests in 59.793s` + `OK`，退出码 0）。
平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）**rc7 的 294 项尚未重跑**；最近一次是 rc6 的 265 项全绿
（`Ran 265 tests in 59.975s` + `OK (skipped=1)`，退出码 0，当时修完两个 Windows 专属真缺陷：msvcrt 按句柄锁、CLI restore 不关库，首跑是 `FAILED (errors=2)`）。
这一项在 AUDIT.md §5 U5 里明确记为未完成，不当作已完成。
结果见 [AUDIT.md](AUDIT.md) §4 与 [CHANGELOG.md](CHANGELOG.md)。

## 7. 安全边界（必读）

- **DSH 不是安全沙箱**。它负责「按批准的内容执行」，不负责限制执行器能做什么。真正的边界是：
  任务必须人类批准、执行票有租约、结果必须人类验收、执行器工具集里没有审批。
- **模型身份没有密码学证明**。GPT / Gemini 的审查票是人工粘贴进 GUI 的，署名记 `human_owner`，
  不冒充模型自证。
- **备份包不加密**，里面有任务正文与 Prompt。靠文件系统权限保护，导出后请自行看管。
- 详见 [docs/SECURITY-REVIEW-v0.1.1.md](docs/SECURITY-REVIEW-v0.1.1.md)；
  rc6 这一批的安全语义（Astra 五问 F-05～F-09 逐条、Q1–Q8 现状、Windows 侧残余风险）见
  [docs/SECURITY-REVIEW-v0.1.1-rc6.md](docs/SECURITY-REVIEW-v0.1.1-rc6.md)。

## 8. 升级

### 8.1 从 v0.1.0 / rc1 / rc2 / rc3 / rc4 / rc5 / rc6 升到 rc7

1. 停掉所有进程（GUI、headless、任何打开着数据库的东西）；
2. **完整备份数据目录**（含 `secrets\ticket.key`）；
3. 换代码；
4. 首次启动自动迁移：schema 1 → 2 → 3 → 4，全部只 ADD COLUMN + 建表建索引，不重建表；
5. 跑 `python -m unittest discover -s tests -t .`，期望 `Ran 294 tests` + `OK`。

**rc6 → rc7 没有 schema 变化**（SCHEMA_VERSION 仍 4、BACKUP_FORMAT_VERSION 仍 4）：
rc6 只改行为，数据库不用迁移。新增的磁盘痕迹只有租约文件：
`council.db.access.lock`（POSIX 用 fcntl 共享/排他锁）与 Windows 侧的
`council.db.access.<pid>.lock`（msvcrt 没有共享锁，改成每个写者一个文件）。
它们不含任何业务数据，进程退出即失去意义；备份/恢复时会被自动跳过，
手工清理也只能在确认没有进程打开该库的情况下删。详见 [docs/MIGRATION-v0.1.1-rc6.md](docs/MIGRATION-v0.1.1-rc6.md)。

**rc3 → rc4 无 schema 变化**（SCHEMA_VERSION 仍是 3）：rc4 只改行为与工具脚本，
唯一需要人注意的是执行票派生加了随机 nonce，升级后旧票一律不能再用。

**rc4 → rc5 有 schema 变化**：SCHEMA_VERSION 3 → 4、BACKUP_FORMAT_VERSION 3 → 4。
用 v3 的库打开会自动执行 `MIGRATIONS[4]`：

```python
4: [
    "ALTER TABLE executors ADD COLUMN disabled_reason TEXT",
    "CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled)",
]
```

只有一列一索引，不重建表、不删列、不改既有值。**迁移后不可降级**：
库版本高于程序版本时 `Database.__init__` 直接抛 `DatabaseVersionError` 拒绝打开，
不会把文件改成 WAL 也不会静默退回 v3。回滚只能连库一起回滚（见 `docs/ROLLBACK-v0.1.1-rc4.md`）。

升级后有三处行为变化需要人知道：

- 恢复备份后**所有执行器身份都会被禁用**（写 `disabled_reason`），
  必须由人类跑 `python -m bigfish.cli reenable <exec-id>` 重新授权，**同时换发新凭据**，旧 token 立刻失效；
- 恢复备份后 `APPROVED` 任务会被冻结成 `BLOCKED`，需人工核对后重新走审批才能再派工；
- `bfc-proxy.mjs` 现在会拒绝非回环监听地址，VPS 上如果你之前设过 `BFC_PROXY_LISTEN_HOST`，
  必须是 `127.0.0.1` / `localhost` / `::1`，否则跳板起不来（`exit(1)`）。

累计迁移会新增：`executors.model`、`executors.disabled_reason`、`tickets.epoch`、
`tasks.cancel_requested_at`，以及四个索引（`idx_audit_created` / `idx_tickets_executor` /
`idx_idem_expires` / `idx_executors_enabled`）。**不会删除任何列、不改任何既有值。**

### 8.2 Windows 侧特别注意

- 升级前先确认没有第二个 GUI 实例开着数据库（`os.replace` 在 Windows 上会因文件占用失败）；
- 密钥文件权限走 `icacls`（不是 chmod），首次创建时会自动收紧；
- **Windows 侧 rc7 还没跑**：最近一次 Windows 全量是 rc6 的 265 项全绿（59.975s / 退出码 0）。
  rc4 的 162 项、rc5 的 244 项、rc6 的 265 项在 Windows 上都属于未验证，升级到新机器后先跑一遍自检再上业务；
- **Windows 的恢复锁分支（msvcrt）没在真机验证过**：POSIX 的 fcntl 分支有 6 项单测，
  Windows 走的是另一套 `<db>.access.<pid>.lock` 探活逻辑，只在代码层面推演过；
- 恢复备份前先确认 3096 上没人监听（GUI / headless 都要停）。CLI 会自己探端口并拒绝，
  GUI 会先停服再进维护模式；`--force` 是绕过 CLI 检查的人工逃生门，只在确知风险时用。

详见 [docs/MIGRATION-v0.1.1.md](docs/MIGRATION-v0.1.1.md) 与
[docs/ROLLBACK-v0.1.1.md](docs/ROLLBACK-v0.1.1.md)。

细节与失败处置见 [docs/MIGRATION-v0.1.1.md](docs/MIGRATION-v0.1.1.md)，回滚见 [docs/ROLLBACK-v0.1.1.md](docs/ROLLBACK-v0.1.1.md)。