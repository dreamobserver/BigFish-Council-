# 测试报告：bigfish-council v0.1.1-rc4

## 0. 一句话

平台 A（Debian 12 / Python 3.11.2）本轮实测 **Ran 162 tests / OK / 退出码 0**，耗时 19.5–25.8s。
平台 B（Windows 11 / Python 3.14.6）本轮**未执行**，rc4 的 162 项在 Windows 上没跑过，见第 8 节。
新增 33 项全在 `tests/test_v011_rc4.py`，六个分组逐项在第 3 节。

---

## 1. 环境

### 1.1 平台 A（本轮实测）

    OS        Debian GNU/Linux 12 (bookworm)，kernel 5.15.0-187-generic
    平台串    Linux-5.15.0-187-generic-x86_64-with-glibc2.36
    主机名    VPS-HOST
    Python    3.11.2
    SQLite    3.40.1
    Node      v22.23.2
    版本      bigfish.__version__ = "0.1.1-rc4"
    分支      fix/v0.1.1-rc4-hardening
    HEAD      2ea5d1b（2026-11-19 16:38:17 +0000）
    工作区    19 个已跟踪文件修改 / 2 个新增文件已暂存 / 4 个未跟踪文档，见第 2.3 节
    GUI 依赖  LD_LIBRARY_PATH=/home/dsh/.local/libbfc（119 个文件）、QT_QPA_PLATFORM=offscreen

关键第三方（pip list 实测）：PySide6 6.12.0、fastapi 0.163.2、mcp 1.30.0、httpx 0.28.1、
cryptography 50.0.2、pyflakes 4.1.0。规模：`git ls-files` 57 个文件；`bigfish`+`tools`+`tests`
28 个 .py（bigfish 14 个）8569 行。

### 1.2 平台 B（本轮未执行）

| 项 | rc3 状态 | rc4 状态 |
| --- | --- | --- |
| Windows 全量单元测试 | 已执行，129/129 | **未执行** |
| Windows 自检 16 项 | 已执行 | **未执行** |
| Windows GUI 冒烟 9 项 | 已执行 | **未执行** |

rc3 的 129 项在 Windows 11 / Python 3.14.6 上跑过（25.263s / OK），rc4 的 162 项整体**没有**。
第 8 节给建议命令。

---

## 2. 全量单元测试

### 2.1 命令与结果

    $ cd /home/dsh/bigfish-council
    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t .
    ----------------------------------------------------------------------
    Ran 162 tests in 25.800s

    OK
    $ echo $?
    0

带 `-v` 的同一套（逐项留存）：`Ran 162 tests in 19.457s / OK`，退出码 0；日志 179 行 /
19448 字节，`... ok` 161 行 + `ok` 单独成行 1 行 = 162 个通过标记，`FAIL:`/`ERROR:` 各 0 行。
连跑四次全部 OK：19.457 / 23.776 / 24.222 / 25.800s，差值来自系统缓存，不涉及断言。

### 2.2 分组单跑（每个 test_*.py 单独执行）

| 文件 | 用例数 | 耗时 | 结果 | 覆盖 |
| --- | --- | --- | --- | --- |
| tests/test_core.py | 15 | 3.534s | OK | 状态机、审查门禁、审批、执行票、心跳超时 |
| tests/test_api.py | 6 | 1.008s | OK | HTTP 端点、MCP 往返、不暴露审批工具 |
| tests/test_v011_p0.py | 15 | 0.412s | OK | SQLite 备份/恢复、审批并发、无明文执行票 |
| tests/test_v011_p1.py | 24 | 3.986s | OK | Watchdog、执行票续期、心跳幂等、MCP 错误语义 |
| tests/test_v011_p2.py | 24 | 0.414s | OK | 四角色表、审查身份绑定、执行器边界、审计 |
| tests/test_v011_p3.py | 19 | 6.127s | OK | 端口拓扑、只允许回环、后台进程、GUI 冒烟 |
| tests/test_v011_rc3.py | 26 | 3.875s | OK | 状态机 7 / 并发 7 / 恢复 7 / 安全边界 5 |
| tests/test_v011_rc4.py | 33 | 1.487s | OK | **本轮新增**，见第 3 节 |
| **合计** | **162** | | | 8 个文件，单跑数之和与全量一致 |

单跑耗时之和约 20.8s，与全量的 19.5–25.8s 量级一致（全量多 import 与进程启动开销）；
单跑数之和等于全量 Ran 数，不存在「全量少跑了用例」。

### 2.3 既有测试有没有被改

    $ git diff --stat tests/
     tests/test_v011_rc4.py | 473 ++++
     1 file changed, 473 insertions(+)

既有 7 个测试文件**工作区改动为零**（`tests/test_v011_rc4.py` 是新增，状态码 ` A`）：rc3 的
129 项在 rc4 里一个断言都没动。`git diff bigfish/db.py` 同样为空，`SCHEMA_VERSION` 仍是 3
（bigfish/db.py:21），rc3 → rc4 不需要迁库。

---

## 3. 新增 33 项：逐组、逐项

    $ .venv/bin/python -m unittest tests.test_v011_rc4 -v
    Ran 33 tests in 1.487s
    OK

| 类 | 用例数 | 耗时 | 结果 |
| --- | --- | --- | --- |
| TestIdempotencyContract（A 幂等契约） | 6 | 0.121s | OK |
| TestMcpProtocol（B MCP 协议语义） | 7 | 0.580s | OK |
| TestMultiRoundTickets（C 多轮执行票） | 6 | 0.217s | OK |
| TestReviewBinding（D 审查绑定） | 4 | 0.099s | OK |
| TestExecutorClient（E 执行器客户端） | 4 | 0.186s | OK |
| TestAuxiliary（F 辅助） | 6 | 0.162s | OK |

先记一处文档与代码不符：`tests/test_v011_rc4.py` 的模块 docstring（第 5–9 行）写的是
「B 6 项 / C 5 项 / F 5 项」，实际收集到 B 7 / C 6 / F 6，总数 33 与 CHANGELOG 一致。
docstring 的分解数字偏小，测试没有少跑；本轮只写文档，未改该文件。

### 3.1 A 组：幂等契约（6 项）

对应缺陷：`_idem_get_locked()` 以前对幂等键做全量 `split(":")` 后取 `parts[3]`，
客户端自定义的含冒号前缀会被截断，重放时算出另一张票。现在用 `key.split(":", 3)`
（bigfish/core.py:1215）。

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_idem_key_with_colon_replays_same_ticket | 幂等键 claim:tenant:retry-1（自带两个冒号）连续 claim 两次 | 两次 ticket 相等；第二次 replayed 为真 |
| test_idem_key_with_colon_does_not_issue_second_ticket | 键 a:b:c claim 两次后数票 | tickets 表中该任务行数 == 1 |
| test_same_key_same_body_returns_identical_response | 同键同体心跳两次 | 两次 server_time 相同；第二次 replayed 为真 |
| test_same_key_different_body_rejected | 同键、detail 由 A 改 B | 第二次抛 CouncilError，消息含「不同请求」 |
| test_replay_does_not_duplicate_events | 同键心跳两次后数事件 | events 中 kind=run.heartbeat 计数 == 1 |
| test_concurrent_same_key_heartbeat_runs_once | 4 线程 + threading.Barrier(4) 同时同键心跳 | 无异常；server_time 去重后只剩 1 个；心跳事件 1 条 |

并发那项断言数据库最终状态（事件条数、响应时间戳集合）。同键不同体的拒绝落在
`_idem_get_locked` 的 fingerprint 比对（bigfish/core.py:1210）。

### 3.2 B 组：MCP 协议语义（7 项）

对应缺陷：`/mcp` 以前把「缺 method 的畸形请求」当通知回 202，客户端会一直等；`ping` 带 id
也回 202；`tools/call` 的参数错误穿到 core 里变成「服务端内部错误」。现在 handler 先判
`jsonrpc == "2.0"`、再判 method 类型，只有**无 id** 才算通知（bigfish/server.py:191–201、
220–223），参数先过 `_validate_args()`（bigfish/server.py:270）。

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_ping_with_id_returns_result | ping 带 id=7 POST /mcp | HTTP 200（不是 202）；回包 id == 7；含 result |
| test_ping_as_notification_gets_202 | ping 不带 id | HTTP 202 |
| test_missing_method_is_invalid_request | 只有 jsonrpc 和 id，没有 method | error.code == -32600 |
| test_wrong_jsonrpc_version_rejected | jsonrpc 传 1.0 调 tools/list | error.code == -32600 |
| test_tool_arg_type_error_is_isError | council_claim_task 的 task_id 传数字 123 | result.isError 为真；文本含「字符串」 |
| test_tool_enum_error_is_isError | council_heartbeat 的 status 传 nonsense | result.isError 为真；文本含 running（提示合法值） |
| test_unknown_argument_rejected | council_list_tasks 传 bogus=1 | result.isError 为真；文本含 bogus |

三条参数校验走同一路径：`_validate_args` 返回错误文案 → 包成 CouncilError → `isError: true`。
协议失败与业务失败因此分层：鉴权 -32001、协议非法 -32600、业务与参数走 result.isError。

### 3.3 C 组：多轮执行票（6 项）

对应缺陷：`claim()` 无幂等键时票从 (ex_id, task_id, version, 幂等键) 派生，同一任务
「BLOCKED → 退回 → 重新批准 → 再领取」会算出**完全相同的票**，旧票持有者能接着用。现在
无幂等键时用随机 nonce：`ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)`
（bigfish/core.py:746）；领取时先吊销同任务所有活票再插新票，保留吊销证据。

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_reclaim_after_block_issues_new_ticket | 领第一张票 → block → verify(False) 退回 → 改 Prompt → 重审重批 → 再领 | 第二张票 ≠ 第一张；旧票心跳抛 CouncilError |
| test_old_ticket_revoked_on_new_claim | 同上，另查票据行与错误文案 | 派工中改 Prompt 先抛错；旧票 revoked_at 非空；旧票心跳报错含「撤销」；新票心跳可用 |
| test_no_idem_key_uses_random_nonce | 无幂等键两轮领取 | 两张票不相等（随机 nonce 生效） |
| test_derive_ticket_is_deterministic_per_nonce | 直接调 derive_ticket | 同 nonce 结果相等；不同 nonce 不等；前缀 bfc_tk_ |
| test_verify_accepts_only_submitted | 回传 completed → 人类验收 | SUBMITTED → verify(True) → DONE |
| test_verify_cannot_accept_blocked | 回传 blocked 后想直接验收 | 状态 BLOCKED；verify(True) 抛错；仍是 BLOCKED |

`test_verify_cannot_accept_blocked` 守的是「副作用不明不能点一下变 DONE」：BLOCKED 的唯一
出口是 `verify(accept=False)` 退回，这个语义在 C 组被复用了三次。

### 3.4 D 组：审查绑定（4 项）

对应缺陷：投票汇总以前取该模型最新一张**未撤销**的票，撤掉最新赞成票会回落到更早那张，
凑出「双通过」；`latest_approval()` 用黑名单排状态，漏排 PENDING_APPROVAL / REVIEW_PASSED /
BLOCKED，撤票退回议事后的历史批准仍显示 effective=True。现在 `EFFECTIVE_APPROVAL_STATES`
是正向白名单（bigfish/core.py:36、684）。

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_revoke_latest_approve_does_not_fall_back | gpt 连投两张 approve 后撤最新一张 | vote_summary 里 gpt.verdict 为 None；gpt.revoked 为真；all_approved 为假 |
| test_latest_approval_not_effective_after_revoke | 先批准（effective 应真），再撤 gpt 票 | 撤票后 latest_approval().effective 为假 |
| test_votes_before_submit_go_straight_to_pending | DRAFT 期先投两票，再送审 | 投票期间仍是 DRAFT；送审后直接 PENDING_APPROVAL |
| test_revoke_after_dispatch_blocks_task | RUNNING 状态撤票 | 状态落到 BLOCKED 或 IN_REVIEW；旧票心跳抛错 |

第一项刻意在批准**之前**造出两张赞成票：APPROVED 之后不再接受新票，不这么摆测不到
「回落」这条路径（tests/test_v011_rc4.py:299 注释写明）。

### 3.5 E 组：执行器客户端（4 项）

对应缺陷：`safe_submit` 旧写法用 `kwargs.get("task_id")` 取任务号，而 `submit()` 没有该形参，
取到的恒为 None，等于「响应丢失时永远直接抛」；`RECONCILED_OK` 若含 BLOCKED，被看门狗打成
BLOCKED 的任务会被当成提交成功（高危误判）。现在是
`RECONCILED_OK = ("SUBMITTED", "DONE", "CANCELLED")`（bigfish/executor_client.py:142），
`safe_submit(ticket, task_id=None, **kwargs)` 把 task_id 提成形参（同文件 144–162）。

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_safe_submit_requires_task_id | submit 抛 _UncertainNetworkError，调用方不给 task_id | 抛 RuntimeError，消息含 task_id |
| test_blocked_is_not_treated_as_success | 同上，但 task_status 返回 BLOCKED | 抛 RuntimeError，消息含「不能证明」 |
| test_submitted_is_treated_as_success | 同上，但 task_status 返回 SUBMITTED | ok 为真、reconciled 为真 |
| test_safe_claim_reuses_same_key | 第一次 claim 抛不确定错误，retries=1 重试 | 两次调用的 idem 参数相等；返回票 bfc_tk_z |

第四项守「重试不能换幂等键」：换了键服务端会签第二张票，等于双发。`safe_claim` 与
`safe_heartbeat` 都先算好 key 再递归重试（同文件 164–185）。

### 3.6 F 组：辅助（6 项）

| 用例 | 测什么 | 断言什么 |
| --- | --- | --- |
| test_list_tasks_statuses_filter | 两条任务、其中一条送审后按状态集合过滤 | 结果含 DRAFT 那条、不含已送审那条 |
| test_list_tasks_offset_paging_is_stable | 5 条任务，limit=2 取 offset 0 与 2 | 合并 4 条无重复 |
| test_list_tasks_order_is_deterministic | 同秒创建 4 条，连续查两次 | 两次顺序相同；且等于 (updated_at, id) 双降序 |
| test_get_task_status_row_is_light | 轻查询一条任务与一个不存在的 id | 有 status/status_label；没有 reviews 键；不存在返回 None |
| test_reserved_ports_do_not_collide | 比对 PORT_RESERVED 与 PORT_ROLES | 两集合不相交；在用端口恰为 3096、3098、3097 |
| test_roles_db_whitelist | 查 roles.DB_ROLES | 含 executor、含 admin、不含 mobile |

第三项的排序契约来自 `list_tasks` 的 `ORDER BY updated_at DESC, id DESC`（bigfish/core.py:296）：
只按 updated_at 排时同秒多条顺序不稳，翻页会重复或漏项。第四项是移动端轮询用的轻接口，
只查 tasks 单表，不像 `get_task()` 要装配 reviews/runs/approval。

---

## 4. 轻量验证脚本

### 4.1 自检

    $ .venv/bin/python tools/selfcheck.py
    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。        退出码 0，耗时 4.2s

16 项：服务在线、health 不含任务信息、看门狗随服务启动、initialize 成功、五个执行器工具、
没有审批类工具、能列出已批准任务、列表不含 Prompt、能领取并拿到 Prompt、重复领取被拒、
心跳上报、回传只到待验收、状态可查、坏 Token 被拒、HTTP 坏 Token 被拒、审批只能在进程内。

自检默认端口是 **3094**（`BFC_SELFCHECK_PORT` 可覆盖，tools/selfcheck.py:22）。rc4 从 3099
挪到 3094，因为 3099 留给移动端网关、3095 被本容器的 quota-hub 占用；同时新增「对面是不是
本服务」的校验，避免端口冲突时拿着别人的 /health 误判通过。

### 4.2 GUI 冒烟

    $ .venv/bin/python tools/gui_smoke.py
    通过 9 项，失败 0 项
    GUI 冒烟通过。                      退出码 0，耗时 3.1s

9 项：主窗口构造、五个页面都在（count=5）、五个页面逐个切换、状态栏文案「API：未启动」、
待审批任务被识别。

---

## 5. 回环端到端（额外跑的）

    $ .venv/bin/python -m bigfish.cli --data-dir /tmp/e2e-rc4 serve --port 3093 &
    $ .venv/bin/python tools/e2e_loopback.py \
          --url http://127.0.0.1:3093/mcp --data-dir /tmp/e2e-rc4
    通过 22 项，失败 0 项                  退出码 0

脚本末尾自己打印：以上全部在**同一台机器**的回环上完成，证明协议链路、权限边界与状态机
正确，跨设备联调需另按 NETWORK-TOPOLOGY.md 执行。

选 3093 而非默认端口，是为了不和平台上可能残留的 3096 撞车；跑完已 pkill，
`ps aux | grep -c "[b]igfish.cli"` 为 0，无残留进程。

---

## 6. 静态检查

    $ .venv/bin/python -m pyflakes bigfish tools tests; echo RC=$?
    RC=0                       （无输出，耗时 1.1s）
    $ .venv/bin/python -m compileall -q bigfish tools tests; echo RC=$?
    RC=0                       （无输出，耗时 0.1s）

分目录跑 pyflakes 也是 0 行输出：bigfish RC=0 / tools RC=0 / tests RC=0。
rc3 清了 14 处未使用导入/变量，rc4 没有再引入新的。

---

## 7. 已知噪音（不影响结论）

全量跑一次稳定出现三条，各 1 次：

| 文本 | 出现位置 | 来源 | 为什么无害 |
| --- | --- | --- | --- |
| Fontconfig error: Cannot load default config file: No such file: (null) | 首次构造 QApplication 时（test_v011_p3 的 GUI 用例、gui_smoke 第 1 行） | Qt 字体子系统 | 本容器没装 fontconfig 配置：/etc/fonts/fonts.conf 不存在、fc-list 返回 0 条。Qt 拿不到默认配置就打这行，改用内置回退，离屏渲染照常完成，用例随后仍是 ok |
| StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead. | 每个 import TestClient 的测试模块首行（test_api / p1 / p2 / rc3 / rc4） | 第三方 fastapi/testclient.py:1 | 弃用提示，不是错误。httpx 装在 0.28.1，TestClient 当前版本仍可用，162 项全跑在它上面 |
| This plugin does not support propagateSizeHints() | test_close_without_tray_shuts_down_cleanly 那一行 | Qt offscreen 平台插件 | offscreen 后端不实现窗口尺寸提示接口。该用例断言的正是「无托盘环境关窗要真退出」，最终 ok |

三条都在 stderr 或测试描述行里，不进 `Ran N tests` 计数，也不改变退出码。179 行日志里
噪音占 3 行（约 1.7%），`FAIL:` 与 `ERROR:` 均为 0 行。

---

## 8. 平台 B（Windows 11 / Python 3.14.6）：本轮未执行

### 8.1 状态

**未执行。** 这一轮没有在 Windows 上跑任何东西：162 项全量、33 项新增、16 项自检、9 项 GUI
冒烟，一项都没有。rc3 的 129 项结果代指不了 rc4——新增 33 项里 B 组 7 项改的正是 `/mcp` 的
HTTP 语义，而 Windows 侧执行器就靠这条链路工作。

### 8.2 建议命令

在 Windows 侧用独立验证目录，不覆盖正在用的目录：

    $root = 'D:\BigFishCouncil\bigfish-council-rc4'
    $py   = 'D:\BigFishCouncil\bigfish-council\.venv\Scripts\python.exe'
    Set-Location $root
    $env:PYTHONIOENCODING = 'utf-8'   # 先设，避免日志被控制台代码页转成 U+FFFD
    $env:QT_QPA_PLATFORM  = 'offscreen'
    & $py -m unittest discover -s tests -t . -v 1> D:\temp\rc4-win.log 2>&1
    "EXIT=$LASTEXITCODE"
    & $py tools\selfcheck.py 1> D:\temp\rc4-win-selfcheck.log 2>&1
    & $py tools\gui_smoke.py 1> D:\temp\rc4-win-gui.log 2>&1
    & $py -m pyflakes bigfish tools tests; "PYFLAKES_RC=$LASTEXITCODE"

两条 rc3 踩过的坑照着做：

1. **先设 `PYTHONIOENCODING=utf-8`，重定向到文件。** rc3 的 Windows 日志里 261 个 U+FFFD 落在
   中文 docstring 上，是 PowerShell 按 GBK 写盘再按 UTF-8 读回，不可逆。
2. **不要直连控制台**，先用文件接住再读。rc3 里 `start-headless.ps1` 就因为不重定向，把
   windows-mcp 的单队列用管道堵死了。

### 8.3 预期要注意的差异（推测，未验证）

- 密钥权限：Windows 上 `os.chmod` 对 ACL 无效，走 `security.secure_file()` 的 icacls 分支。
  rc3 实测返回 `(True, "acl limited")`，rc4 未重测，标**未验证**。
- 进程退出码：`subprocess.terminate()` 在 Windows 上是 1 而非 POSIX 的 -15；
  test_v011_p3 已有 `os.name == "nt"` 分支（该文件 164 行），同样标**未验证**。
- 路径分隔符：新增测试没有硬编码 POSIX 路径，理论上不受影响，没跑过就是没跑过。

---

## 9. 本轮未做的检查

| 检查 | 状态 | 原因 |
| --- | --- | --- |
| 类型检查 mypy | **未做** | .venv 未安装：`python -m mypy --version` 报 No module named mypy |
| 类型检查 pyright | **未做** | `npx --no-install pyright` 因缺包被 npx 取消（missing packages and no YES option: pyright@1.1.414），项目未引入 |
| 24h soak / 长稳 | **未做** | 无 24 小时窗口；只做了 4 次全量连跑与 1 次回环 e2e |
| 跨机 MCP 闭环（Tailscale） | **未做** | rc4 未重跑。跨机闭环要先把代码部署到对端并重启 headless，属于变更生产服务 |
| Windows 全平台测试 | **未执行** | 见第 8 节 |
| 真人 GUI 验收 | **未做** | 只做了 offscreen 冒烟，真显示器下的 DPI 缩放、托盘图标、关窗行为要人点 |
| pytest 跑同一套件 | **未做** | No module named pytest；本项目用 unittest |

项目从来没有引入过 mypy / pyright，本轮也没有新增依赖。`compileall` 只证明语法可编译，
`pyflakes` 只证明没有未定义名和未使用名，两者都替代不了类型检查；本文说「静态检查通过」
仅指这两项。

---

## 10. 日志留存

本轮输出全部在 /home/dsh/bfc-rc4-logs/，16 个文件 / 492 行 / 44445 字节：

| 文件 | 行 / 字节 | 文件 | 行 / 字节 |
| --- | --- | --- | --- |
| unittest-linux.txt（162 项 -v） | 179 / 19448 | test_v011_p3.txt（19 项） | 28 / 2335 |
| unittest-linux-run1.txt（第 2 次） | 9 / 651 | test_v011_rc3.txt（26 项） | 35 / 3712 |
| unittest-linux-run2.txt（第 3 次） | 9 / 651 | test_v011_rc4.txt（33 项） | 40 / 4482 |
| test_core.txt（15 项） | 21 / 1593 | selfcheck.txt（16 项） | 23 / 700 |
| test_api.txt（6 项） | 14 / 866 | gui_smoke.txt（9 项） | 13 / 447 |
| test_v011_p0.txt（15 项） | 20 / 1966 | e2e-loopback.txt（22 项） | 35 / 1336 |
| test_v011_p1.txt（24 项） | 33 / 3200 | pyflakes.txt | 1 / 5 |
| test_v011_p2.txt（24 项） | 31 / 3048 | compileall.txt | 1 / 5 |

目录后来多出 6 个文件（5 个 `*-delivery.txt` 副本 + `unittest-linux-nogl.txt`），
是重复落盘，不计入合计。当前目录实测 22 个文件 / 878 行 / 83805 字节。

这些是磁盘原始字节，没有转录。rc3 的 Windows 日志经 PowerShell 转码；本轮全部在本机直接
重定向，不存在编码残损。

---

## 11. 与 rc3 的对照

| 项 | rc3 | rc4 | 变化 |
| --- | --- | --- | --- |
| 测试总数 | 129 | **162** | +33 |
| 测试文件数 | 7 | 8 | 新增 test_v011_rc4.py（473 行） |
| 全量耗时（平台 A） | 15.984s | 19.5–25.8s | 变长，主要是新增的 MCP 与并发用例 |
| 自检 | 16/16 | 16/16 | 默认端口 3099 → 3094，新增「对面是不是本服务」校验 |
| GUI 冒烟 | 9/9 | 9/9 | 无变化 |
| pyflakes | 无输出 | 无输出 | 无变化 |
| compileall | 通过 | 通过 | 无变化 |
| SCHEMA_VERSION | 3 | 3 | 不变，rc3 → rc4 免迁库 |
| 既有 129 项 | — | 断言零改动 | 只新增文件，`git diff tests/` 只有新文件 |
| 平台 B | 129/129 已跑 | **未执行** | 状态倒退，见第 8 节 |

新增的 33 项每组对着一个具体缺陷：A 票据截断、B 的 202/-32600/isError 三层语义、C 旧票复活、
D 撤票回落、E 的 BLOCKED 误判成功。带编号的注释只有 `E1`（tests/test_v011_rc4.py:76）、
`E2–E4`（同文件 151）、`E5`（同文件 352）；C 组类注释标的是 `D`、D 组标的是 `C`（同文件
209、297），字母与本文分组不一致，属原文件编号习惯。

---

## 12. 复现清单（照抄可跑）

    cd /home/dsh/bigfish-council
    export LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen
    .venv/bin/python -m unittest discover -s tests -t .   # 162 项；分组换成 tests.<文件名>
    .venv/bin/python tools/selfcheck.py                   # 16 项
    .venv/bin/python tools/gui_smoke.py                   # 9 项
    .venv/bin/python -m pyflakes bigfish tools tests; echo "RC=$?"
    .venv/bin/python -m compileall -q bigfish tools tests; echo "RC=$?"

本轮实测结论：以上全部退出码 0（另加 e2e_loopback 22 项），162/162、16/16、9/9、22/22 全绿。
平台 B 未执行，类型检查与 soak 未做。
