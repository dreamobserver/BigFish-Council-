# 测试报告 · v0.1.1-rc7

> 版本：`0.1.1-rc7`（`bigfish/__init__.py:6`）
> 分支：`fix/v0.1.1-rc7-hardening` · HEAD `c0c54bc`
> 口径：本文件只写**本机实测**与**代码事实**两样东西。凡未跑的都进第 7 节，不在正文里暗示"应该没问题"。

---

## 0. 结论

一句话：rc7 的 294 项单元测试在本机全绿（`Ran 294 tests in 59.793s` / `OK` / `RC=0`），27 项新增用例逐条咬住本轮 8 个审计发现（BFC-01～BFC-08）与移动端只读角色边界，静态检查与两套冒烟脚本零失败；没跑的只有平台 B 复跑、真窗口验收、跨设备闭环与打包，清单见第 7 节。

---

## 1. 环境

### 1.1 平台 A（本轮唯一实跑平台）

| 项 | 值 | 取法 |
|---|---|---|
| 发行版 | Debian 12 (bookworm) | `/etc/os-release` |
| 内核 | Linux x86_64 容器 | `uname -r` |
| Python | 3.11.2（`.venv`） | `.venv/bin/python -V` |
| SQLite | 3.40.1 | `sqlite3.sqlite_version` |
| GUI 栈 | PySide6 + Qt offscreen | `QT_QPA_PLATFORM=offscreen` |
| 内存上限 | cgroup 3328 MB | `/sys/fs/cgroup/memory.max` |

### 1.2 命令前缀

本机 Qt 依赖装在非标准路径，所有涉及 GUI 的测试与脚本都必须带两个环境变量，否则 `import PySide6` 直接失败：

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen
```

单元测试里有 GUI 相关的用例（`tools/gui_smoke.py` 走真实 QApplication），所以全量跑也带这两个变量。纯逻辑用例不受影响，但统一前缀更省事，也避免"换个 shell 就挂"的假故障。

### 1.3 平台 B（Windows 11 / Python 3.14.6）

rc6 时平台 B 跑过 265 项。rc7 新增的 27 项（尤其租约原子性与 CLI 关库）在平台 B 上**一项都没跑**，见第 7 节 U5。第 6 节列出的平台差异是从代码与 rc6 实跑结论里读出来的，不是本轮实测。

---

## 2. 全量结果

### 2.1 命令

```bash
cd /home/dsh/bigfish-council
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
```

### 2.2 输出

```text
/home/dsh/bigfish-council/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
............................................................................................Fontconfig error: Cannot load default config file: No such file: (null)
This plugin does not support propagateSizeHints()
..........................................................................拒绝恢复：127.0.0.1:3096 上还有服务在监听，请先停掉它（tools/stop-headless.ps1 或关闭 GUI），确认没有其他实例后再重试；确实要强行恢复请加 --force。
...拒绝恢复：127.0.0.1:55283 上还有服务在监听，请先停掉它（tools/stop-headless.ps1 或关闭 GUI），确认没有其他实例后再重试；确实要强行恢复请加 --force。
.............................................................................................................................
----------------------------------------------------------------------
Ran 294 tests in 61.458s

OK
RC=0
```

发布口径用 `59.793s`（facts 里登记的那次冷跑），上面这次复跑是 `61.458s`，两次都是 `OK` / `RC=0`，差异来自本机负载，不是用例行为变化。

### 2.3 噪音逐行说明

| 行 | 来源 | 是否需要处理 |
|---|---|---|
| `StarletteDeprecationWarning: Using httpx with starlette.testclient` | 第三方库依赖告警 | 否。库内告警，不影响断言；升级 httpx2 会在依赖锁里单独处理 |
| `Fontconfig error: Cannot load default config file` | offscreen 下 Qt 找字体 | 否。无头环境固有噪音 |
| `This plugin does not support propagateSizeHints()` | Qt offscreen 插件 | 否。同上 |
| `拒绝恢复：127.0.0.1:3096 ...` | `test_v011_p3.py` 的端口占用反转测试 | 否。这是断言里的**预期路径**：测试先占端口，再验证 CLI 拒绝恢复并给出可执行指令 |
| 第二行 `拒绝了恢复：127.0.0.1:55283` | 同上的第二个用例（随机端口） | 否。同上 |

关键结论：**没有任何一条是测试失败或错误**，`OK` 与 `RC=0` 是干净的。

---

## 3. 分组表

`tests/` 下共 18 个文件：17 个 `test_*.py` 模块 + `__init__.py`（空包标记，1 行）。逐模块用例数如下，数字取自 `grep -cE '^\s+def test_'`，与 unittest 的 294 完全对得上。

| # | 文件 | 用例数 | 覆盖焦点 |
|---|---|---:|---|
| 1 | `tests/test_api.py` | 6 | HTTP 面骨架：鉴权、参数校验、状态码 |
| 2 | `tests/test_core.py` | 15 | 核心域模型：任务、版本、审查、审批 |
| 3 | `tests/test_v011_p0.py` | 16 | P0 阻断项：票不落明文、幂等不存票 |
| 4 | `tests/test_v011_p1.py` | 24 | 看门狗、心跳租约、幂等 replay、MCP 错误面 |
| 5 | `tests/test_v011_p2.py` | 26 | 权限边界（P2-2）、执行器角色、越权 403 |
| 6 | `tests/test_v011_p3.py` | 20 | 端口占用、优雅停机、headless 生命周期 |
| 7 | `tests/test_v011_rc3.py` | 26 | 取消/审批并发、恢复后旧票、schema 迁移 |
| 8 | `tests/test_v011_rc4.py` | 33 | 幂等键派生、票重签、随机 nonce |
| 9 | `tests/test_v011_rc5_cli_guard.py` | 4 | CLI 危险命令的二次确认与拒绝 |
| 10 | `tests/test_v011_rc5_dispatch_id.py` | 3 | 派工编号唯一性 |
| 11 | `tests/test_v011_rc5_key_heal.py` | 8 | 密钥轮换即吊销全部活票 |
| 12 | `tests/test_v011_rc5_proxy.py` | 9 | 本机代理的转发与回环约束 |
| 13 | `tests/test_v011_rc5_receipts.py` | 24 | 结果回执收口（完成/失败/阻断/取消） |
| 14 | `tests/test_v011_rc5_restore_authority.py` | 24 | 恢复授权、epoch 单调、事务原子性 |
| 15 | `tests/test_v011_rc5_review_audit.py` | 13 | 审查语料脱敏与审计事件 |
| 16 | `tests/test_v011_rc6.py` | 16 | 全量覆盖补齐：manifest、限流、停服、锁 |
| 17 | `tests/test_v011_rc7.py` | 27 | 本轮 8 个审计发现 + 移动端角色 |
| | **合计** | **294** | |

与 rc6 对比：rc6 是 16 个模块 265 项，rc7 是 17 个模块 294 项，净增 29 项。拆开看——新增模块 `tests/test_v011_rc7.py` 贡献 27 项；`tests/test_v011_p2.py` 贡献 2 项（`test_mobile_viewer_cannot_execute`、`test_mobile_db_role_maps_to_viewer_not_executor`），同时把硬编码 `len(R.ROLES) == 4` 的旧用例改名并放开为 `test_canonical_roles`。

另外两个文件只补强断言不增用例：`tests/test_v011_rc4.py` +11 行、`tests/test_v011_rc5_review_audit.py` +8 行。所以 265 + 27 + 2 = 294，账是平的。

---

## 4. rc7 新增 27 项逐项说明

七个类，每个类对应一条审计发现的收敛点。每项只说一句话：它验的是哪条不变式。

### 4.1 `TestLeaseAtomicity`（5 项，`tests/test_v011_rc7.py:85`）

对应 BFC-01：旧实现"先扫有没有别的写者，再锁另一把锁"，两把不是同一个内核对象，扫描返回后新实例仍能在新文件上建锁，互斥失效。新实现只有一把访问锁（写者共享、恢复排他）。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_writer_cannot_open_while_exclusive` | :93 | 恢复持排他锁期间，新 `Database` 必须抛 `LeaseError` 而不是安静打开 |
| `test_exclusive_refused_while_writer_alive` | :118 | 有写者活着时恢复必须失败，且**被拒之后库一个字节都没变**（任务状态前后相等） |
| `test_same_process_second_database_does_not_deadlock` | :134 | 同进程多实例共享租约不互锁：老实例还能读写、新实例也能过完整性检查 |
| `test_lease_released_after_close_allows_exclusive` | :144 | `close()` 之后排他锁必须能立刻拿到，否则恢复永远等不到窗口 |
| `test_lock_files_are_never_deleted` | :153 | 锁文件关闭后仍然存在——`unlink` 会让新实例在新 inode 上加锁，互斥当场失效 |

### 4.2 `TestMcpFieldLimit`（4 项，`:167`）

对应 BFC-02：MCP 工具此前只校验"字段存在/类型对"，自由文本没有长度上限，一个 `detail` 能塞满内存。现在 `MAX_TEXT_FIELD`（4 MiB，`bigfish/server.py:41`）同时约束 schema 声明与服务端校验。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_free_text_fields_declare_max_length` | :178 | 5 个自由文本字段（心跳 `detail` + 提交的 `log/diff/tests/note`）在 `tool_specs()` 里**都**声明了 `maxLength`，模型侧能看见边界 |
| `test_oversized_heartbeat_detail_is_error` | :186 | `MAX_TEXT_FIELD + 1` 的心跳被当成错误返回，文案含"长度上限"，不是静默截断 |
| `test_at_limit_field_is_accepted` | :196 | 正好等于上限必须放行——边界不能写成 off-by-one |
| `test_oversized_result_log_is_error_and_nothing_written` | :207 | 被拒的提交**不留半条 artifact**：`get_task(tid)["runs"] == []` |

### 4.3 `TestBindGate`（5 项，`:222`）

对应 BFC-03：`create_app()` 只返回 app，第三方仍可 `uvicorn.run(create_app(...), host="0.0.0.0")` 绕过 `check_bind_host()`。rc7 改成两道：带闸门的工厂 + 连接级中间件。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_production_factory_rejects_public_host` | :223 | `0.0.0.0` / `::` / `[::]` / `192.168.1.10` / `100.64.1.2` 全部 `BindError`，一个都不放行 |
| `test_production_factory_accepts_loopback` | :228 | `127.0.0.1` / `localhost` / `::1` / `None` 放行，且返回的 host 落在回环集合内 |
| `test_guard_middleware_blocks_non_loopback_client` | :234 | 就算有人裸用 `create_app()` 绑全网卡，局域网来客 `GET /health` 也只能拿 403 |
| `test_guard_middleware_allows_loopback_client` | :241 | 回环来客同样路径拿 200——闸门不能把正常路也堵死 |
| `test_trusted_peers_are_only_the_in_process_constants` | :246 | 受信名单**恰好**是 `{"testclient","testserver"}`，任何真实地址都不得混进去，否则中间件形同虚设 |

### 4.4 `TestManifestCredentialStatus`（3 项，`:257`）

对应 BFC-04：`contains_plaintext_credentials: false` 与 `manual_review_required: true` 自相矛盾——布尔字段天然读成"我检查过了、保证没有"。改成枚举后只陈述观测到的事实。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_operation_bundle_says_hashes_only` | :262 | 操作包 `credential_status == "hashes-only"`，且布尔字段不再出现（"布尔承诺又回来了"直接失败） |
| `test_audit_bundle_does_not_promise_clean` | :270 | 审计包 `== "pattern-scrubbed-unverified"` 且 `manual_review_required` 为真——不许声称干净 |
| `test_status_value_is_from_known_set` | :278 | 两种包产出的值都落在 `{none, hashes-only, pattern-scrubbed-unverified}`，枚举可被机器判定 |

### 4.5 `TestCliClosesDatabase`（2 项，`:290`）

对应 BFC-05：`cli.py` 原来只有 restore 分支关库，其他分支返回后连接还挂着（Windows 上表现为文件占用、WAL 残留）。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_cli_returns_and_releases_lease` | :291 | CLI 跑完 `init` 之后，进程外立刻能拿到 `exclusive_access`——库真的关了 |
| `test_allowed_ports_lists_reserved` | :301 | `allowed-ports` 同时打印 implemented 与 reserved，3099/3100/3101 三个预留端口不再隐身 |

### 4.6 `TestReleaseEngineering`（3 项，`:317`）

对应 BFC-07：发布工程不闭环——没有依赖锁、没有 CI，`requirements.txt` 全是浮动版本。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_requirements_lock_pins_versions` | :318 | `requirements.lock` 存在且精确 pin 超 10 条，fastapi/uvicorn/pydantic/PySide6/mcp 五个关键包**都**被锁定 |
| `test_ci_workflow_covers_both_platforms` | :328 | CI 覆盖 `ubuntu-latest` + `windows-latest`，跑 `unittest`，并有 `security-note` 作业 |
| `test_ci_secret_scan_actually_matches_tokens` | :337 | CI 里的 `ghp_[A-Za-z0-9]` 正则**真能命中**——写个扫不出东西的规则等于没扫 |

### 4.7 `TestMobileViewerRole`（5 项，`:347`）

对应 MOBILE：手机端要能看、能留言，但丢了也不能领任务或导出库。

| 用例 | 行号 | 验的不变式 |
|---|---:|---|
| `test_mobile_registers_with_own_prefix` | :348 | 移动端有专用 id 前缀 `mobile-` 与令牌前缀 `bfc_mb_`，日志里一眼可辨 |
| `test_mobile_authenticates_as_viewer` | :354 | 鉴权后 `role_canonical == mobile_viewer`：`task.read` 为真、`task.claim` 为假 |
| `test_mobile_token_cannot_use_executor_channel` | :361 | 移动端令牌调 `authenticate(token, "executor")` 必须抛——通道不能串 |
| `test_mobile_db_role_never_falls_back_to_executor` | :366 | `role_for_db_row("mobile")` 落到 `MOBILE_VIEWER`，**绝不能**落到 `EXECUTOR_DSH`（未知角色回落即提权） |
| `test_executor_registration_rejects_unknown_role` | :371 | `register_executor("x", "god")` 必须抛——角色白名单是闭合的 |

---

## 5. 静态检查与冒烟

### 5.1 静态检查

```bash
.venv/bin/python -m pyflakes bigfish tests tools      # PF_RC=0，零输出
.venv/bin/python -m compileall -q bigfish tests tools # CC_RC=0
```

pyflakes 本轮抓到过一个真问题：`bigfish/gui.py` 顶部有 `from .server import create_app`，函数内又 import 了一次，顶部那条从未使用。顺手删掉的同时把 GUI 的 `ApiServer` 改成调 `create_production_app()`（`bigfish/gui.py:98-106`），让 GUI 与 CLI 共用同一道闸门，而不是各写一套判断。

### 5.2 自检脚本

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/selfcheck.py
```

```text
  [ok]   坏 Token 被拒
  [ok]   HTTP 坏 Token 被拒
  [ok]   审批只能在进程内

通过 16 项，失败 0 项
自检全部通过，可以启动 GUI。
```

### 5.3 GUI 冒烟

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py
```

```text
  [ok]   页面可切换：council
  [ok]   页面可切换：approval
  [ok]   页面可切换：monitor
  [ok]   状态栏文案  API：未启动
  [ok]   待审批任务被识别

通过 9 项，失败 0 项
GUI 冒烟通过。
```

注意：`gui_smoke` 是 offscreen 冒烟，验的是"窗口能建、页面能切、控件能读到数据"，**不验**真窗口下的像素、DPI、多显示器行为。真窗口验收见第 7 节 U1。

### 5.4 汇总

| 检查 | 结果 |
|---|---|
| 单元测试 | 294 项 / 0 失败 / RC=0 |
| pyflakes | RC=0，零告警 |
| compileall | RC=0 |
| selfcheck | 16/16 |
| gui_smoke | 9/9 |

---

## 6. 平台差异

本轮所有结论都在 Linux 上取得。两处在 Windows 上语义不同，写代码与读报告时都必须记住。

### 6.1 `chmod` 在 Windows 上不收紧权限

`bigfish/security.py` 的 `secure_file()` 分平台实现：

- POSIX：`os.chmod(path, 0o600)`（`bigfish/security.py:167`）；
- Windows：`os.chmod` 只改只读位，**不改 ACL**，所以必须 `icacls <path> /inheritance:r`（`:177`）去掉继承，再 `icacls <path> /grant:r {user}:F`（`:179`）只授权当前用户，最后还要回读校验（`:201`）。

同一套逻辑用于 `data_dir` 与密钥父目录（`bigfish/config.py:120-121`）。含义很直接：**在 Windows 上"文件权限已收紧"这件事不能靠 chmod 断言**，rc7 的测试也没有跨平台断言 ACL，只断言函数不抛异常。

### 6.2 `subprocess.terminate()` 的退出码是 1，不是 -15

`tests/test_v011_p3.py:158` 的 `test_clean_shutdown` 明确分叉：

```python
if os.name == "nt":
    # Windows 没有可被捕获的 SIGTERM：subprocess.terminate() 走 TerminateProcess，
    # 进程被系统强制结束，退出码为 1 而不是 POSIX 的 -15
    self.assertIn(rc, (0, 1))
else:
    self.assertIn(rc, (0, -15))
```

同一个用例在 `:176-190` 改用"能否重新绑定端口"判断资源是否回收，而不是看 `connect` 结果——Windows 上连接被拒的原因可能是防火墙、可能是 TIME_WAIT，不能当作进程已退出的证据。

另外 `test_graceful_shutdown_on_sigint`（`:191`）整个用例标了 `@unittest.skipIf(os.name == "nt", ...)`：Windows 无 SIGINT 语义，这条路径在平台 B 上不存在，跳过是诚实的做法，不是掩盖。全仓 `skipIf(os.name == "nt")` 只有三处（`p3:165` 的分支、`p3:191`、`rc3:660`）。

---

## 7. 未跑清单

以下项目**本轮没有跑**，报告里任何地方都不代表它们通过了。

| 编号 | 项目 | 现状 | 影响 |
|---|---|---|---|
| U1 | Windows 真机 GUI 窗口验收 | 未做。只有 offscreen 冒烟 | 像素级布局、DPI 缩放、多显示器行为无证据 |
| U2 | exe 打包（PyInstaller） | 未做 | 分发包仍要求用户自带 Python 3.11 |
| U3 | 开机自启注册 | 未做 | 需要用户手工建快捷方式 |
| U4 | 跨设备 MCP 闭环 | rc7 未重跑（rc2 时期 11/11） | 本轮对 `server.py` 改动较大（BFC-02/03），闭环需在真机上复验 |
| U5 | 平台 B 复跑 rc7 的 294 项 | 未做 | Windows 上的租约实现（槽位锁）完全没有实跑证据 |
| U6 | 移动端 REST/SSE 端点 | 未实现，只有角色与草案 | 第 4.7 节验的是角色边界，不是端点行为 |
| U7 | apk 打包 | 未做 | 移动端暂无分发形态 |
| U8 | `core.py` / `server.py` 分层重构 | 未做 | `core.py` 已 1379 行，维护成本在涨 |

其中 U5 最要紧：BFC-01 的 Windows 实现（`SLOTS = 64`，每写者独占 1 字节，恢复端一次锁整段）在 Linux 上走的是另一条分支（`flock`），`_acquire_windows` / `_win_try_lock` 全部标了 `# pragma: no cover - 仅 Windows`。也就是说：**本轮关于"租约原子性已修"的证据，在 Linux 上成立；在 Windows 上成立与否，代码写了、测试没跑。**

---

## 8. 复核方式

想在另一台机器上复现本报告：

```bash
cd bigfish-council
.venv/bin/python -V                                  # 期望 3.11.x
.venv/bin/python -c "import sqlite3;print(sqlite3.sqlite_version)"
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .   # 期望 Ran 294 / OK
.venv/bin/python -m pyflakes bigfish tests tools      # 期望零输出
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/selfcheck.py                 # 期望 16/16
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py                 # 期望 9/9
```

用例数对不上时先跑 `for f in tests/test_*.py; do echo "$f $(grep -cE '^\s+def test_' $f)"; done`，再与第 3 节表格逐行比。数字对不上说明检出的代码版本变了，不是测试坏了。
