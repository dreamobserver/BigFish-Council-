# BigFish Council / 大肥鱼议事厅 v0.1.1-rc5

Windows 本地控制中心 + Tailscale 私网 + VPS-HOST DSH 执行器。
人类在本机批准，云端只能领取**已批准**的任务，回传结果后仍需人类验收。

全程不开放任何公网端口，不使用 Tailscale Funnel。

> **rc5 是第三轮审计（R1–R9）后的收口版。** 相对 rc4 修了 9 项审计问题：
>
> - **R1** 恢复后授权未作废（APPROVED 仍可被领取、executors 仍 enabled、`_replay_claim` 无条件重发票）
>   → `backup._block_inflight` 五步作废 + `core.reenable_executor` 强制轮换凭据 + `_replay_claim` 全量校验 + CLI `reenable`；
> - **R2** `_recompute_review` 提前 return 丢掉「双模型通过 / 待审批」两条事件 → 删掉 return；
> - **R3** 撤一条普通评论能把运行中任务打成 BLOCKED → 只有 `kind=vote` 才动审批；
> - **R4** heartbeat / submit 的 `now()` 取在事务外，租约可以落在过去 → 挪进事务内；
> - **R5** 执行端无收据、崩了不知道做到哪 → `ReceiptLog` + reconcile + 服务端下发 `dispatch_id`；
> - **R6** 审计包用列黑名单（加一列就漏一次）→ 改列白名单 `AUDIT_COLUMNS` + `manifest.policy=allowlist`；
> - **R7** `bfc-proxy.mjs` 监听地址无校验 → `isLoopback` 硬闸门，`listen` 之前 `exit(1)`；
> - **R8** 恢复期没有排空 → db 维护模式（写锁作屏障 + `MaintenanceError` → 503 + Retry-After）+ GUI 停服/接回 + CLI 探端口拒绝（`--force` 逃生）；
> - **R9** `ensure_key_consistency` 自愈后旧票仍能认证 → `_heal_key` 轮换密钥时同时吊销全部活票 / 清幂等 / 写审计，仅补记指纹时不动票。
>
> schema 3 → 4：`MIGRATIONS[4]` 只加 `executors.disabled_reason` 一列 + 一个 `idx_executors_enabled` 索引。
> 测试从 162 项涨到 **244 项**，v0.1.0 的 21 项原样在位。
>
> **v0.1.1-rc5 是发布候选，不是生产版。** 平台 A（Debian 12 / Python 3.11.2）
> **244 项全绿**；平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）**本批次未跑**。
> 上一次 Windows 实测是 **rc3 的 129 项全绿**（25.263s，退出码 0，129 个 ok、0 个失败）；
> **rc4 的 162 项与 rc5 的 244 项都还没在 Windows 上重跑**。
> Windows 上已实测到的差异（rc3 时期结论，未随 rc5 复验）：密钥文件权限真正生效的是 NTFS ACL（`icacls`），
> `os.chmod` 的 mode 仍是 0o666 但不起作用，`verify_file_restricted()` 走 ACL 分支返回 `(True, "acl limited")`；
> `subprocess.terminate()` 的退出码是 1 而不是 POSIX 的 -15。详见
> [docs/TEST-REPORT-v0.1.1-rc4.md](docs/TEST-REPORT-v0.1.1-rc4.md)。
>
> 跨设备联调（VPS → 公司机 `CLIENT-B-TAILNET-IP:3096`，MCP 闭环 11/11）已在 rc2 打通，
> 但那条链路验的是 rc2 代码；「常驻」（WMI 起的进程重启即消失）、GUI 真窗口、
> MCP 连接导入仍未做。详见 [docs/NETWORK-TOPOLOGY.md](docs/NETWORK-TOPOLOGY.md) §4.4。

---

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
| `docs/SECURITY-REVIEW-v0.1.1-rc5.md` | **rc5 安全复核：R1–R9 逐条语义、威胁模型 T1–T8 现状、残余风险** |
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

**244 个用例**（v0.1.0 原有 21 项全部保留 + 222 项新增，无 skip / 无 expectedFailure）：

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `test_core.py` | 15 | 状态机、审查门禁、审批、执行票、心跳超时 |
| `test_api.py` | 6 | HTTP 端点、MCP 往返、不暴露审批工具 |
| `test_v011_p0.py` | 16 | SQLite 备份/恢复、审批并发、无明文执行票 |
| `test_v011_p1.py` | 24 | Watchdog、执行票续期、心跳幂等、MCP 错误语义 |
| `test_v011_p2.py` | 24 | 四角色表、审查身份绑定、执行器边界、审计 |
| `test_v011_p3.py` | 20 | 端口拓扑、只允许回环、后台进程、GUI 冒烟 |
| `test_v011_rc3.py` | 26 | 状态机 7 + 并发 7（真线程 + Barrier）+ 恢复 7 + 安全边界 5 |
| `test_v011_rc4.py` | 33 | 幂等契约 6 + MCP 协议 7 + 多轮执行票 6 + 审查绑定 4 + 执行器客户端 4 + 辅助 6 |
| `test_v011_rc5_restore_authority.py` | 20 | 恢复禁用全部身份 3 + reenable 轮换凭据 3 + 冻结 APPROVED 2 + 恢复后重放拒绝 2 + 维护模式 6 + 维护期 HTTP 语义 4 |
| `test_v011_rc5_receipts.py` | 24 | ReceiptLog 读写与容错 6 + state_of 判定 6 + 落盘耐久性 2 + run_once 收据链 5 + 客户端接线 5 |
| `test_v011_rc5_review_audit.py` | 13 | 双通过事件落库 3 + 撤评论不动审批 3 + 事务内时间戳 2 + 审计包列白名单 5 |
| `test_v011_rc5_proxy.py` | 9 | 跳板监听硬闸门：0.0.0.0 / :: / [::] / 空值 / 内网 / 公网拒绝 6 + 回环放行 3 |
| `test_v011_rc5_key_heal.py` | 7 | 指纹首次补记不动票 2 + 轮换吊销活票 5 |
| `test_v011_rc5_cli_guard.py` | 3 | CLI 恢复探端口拒绝 1 + `--force` 绕过 1 + 端口探测助手 1 |
| `test_v011_rc5_dispatch_id.py` | 3 | claim 下发 dispatch_id 1 + 重放同号 1 + 重新派工换号 1 |

自检 16 项：

```powershell
.\.venv\Scripts\python.exe tools\selfcheck.py
```

GUI 冒烟（无显示器时用离屏模式）：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe tools\gui_smoke.py
```

平台 A（Debian 12 / Python 3.11.2）**244 项全绿**；平台 B（Windows 11 / Python 3.14.6）
**本批次未跑**，上一次 Windows 全量是 rc3 的 129 项全绿。
结果见 [docs/TEST-REPORT-v0.1.1-rc5.md](docs/TEST-REPORT-v0.1.1-rc5.md)
与 [docs/TESTLOG-v0.1.1-rc5.md](docs/TESTLOG-v0.1.1-rc5.md)。

## 7. 安全边界（必读）

- **DSH 不是安全沙箱**。它负责「按批准的内容执行」，不负责限制执行器能做什么。真正的边界是：
  任务必须人类批准、执行票有租约、结果必须人类验收、执行器工具集里没有审批。
- **模型身份没有密码学证明**。GPT / Gemini 的审查票是人工粘贴进 GUI 的，署名记 `human_owner`，
  不冒充模型自证。
- **备份包不加密**，里面有任务正文与 Prompt。靠文件系统权限保护，导出后请自行看管。
- 详见 [docs/SECURITY-REVIEW-v0.1.1.md](docs/SECURITY-REVIEW-v0.1.1.md)；
  rc5 这一批的安全语义（R1–R9 逐条、威胁模型 T1–T8 现状）见
  [docs/SECURITY-REVIEW-v0.1.1-rc5.md](docs/SECURITY-REVIEW-v0.1.1-rc5.md)。

## 8. 升级

### 8.1 从 v0.1.0 / rc1 / rc2 / rc3 / rc4 升到 rc5

1. 停掉所有进程（GUI、headless、任何打开着数据库的东西）；
2. **完整备份数据目录**（含 `secrets\ticket.key`）；
3. 换代码；
4. 首次启动自动迁移：schema 1 → 2 → 3 → 4，全部只 ADD COLUMN + 建表建索引，不重建表；
5. 跑 `python -m unittest discover -s tests -t .`，期望 `Ran 244 tests` + `OK`。

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
- **Windows 侧本批次没有跑 rc5**：上一次 Windows 全量是 rc3 的 129 项（25.263s / 退出码 0）；
  rc5 的 244 项在 Windows 上属于未验证，升级到新机器后先跑一遍自检再上业务；
- 恢复备份前先确认 3096 上没人监听（GUI / headless 都要停）。CLI 会自己探端口并拒绝，
  GUI 会先停服再进维护模式；`--force` 是绕过 CLI 检查的人工逃生门，只在确知风险时用。

详见 [docs/MIGRATION-v0.1.1.md](docs/MIGRATION-v0.1.1.md) 与
[docs/ROLLBACK-v0.1.1.md](docs/ROLLBACK-v0.1.1.md)。

细节与失败处置见 [docs/MIGRATION-v0.1.1.md](docs/MIGRATION-v0.1.1.md)，回滚见 [docs/ROLLBACK-v0.1.1.md](docs/ROLLBACK-v0.1.1.md)。