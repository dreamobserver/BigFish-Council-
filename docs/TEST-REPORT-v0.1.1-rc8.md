# 测试报告：bigfish-council v0.1.1-rc8

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc8` |
| 平台 A | Debian 12 / x86_64 / Python 3.11.2 / SQLite 3.40.1 / node v22.23.2 |
| 平台 A 命令 | `LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t .` |
| 平台 A 结果 | **`Ran 322 tests in 148.492s` / `OK (skipped=1)` / `UT_RC=0`** |
| 平台 B | GitHub Actions `windows-latest`（Windows Server 2022）× Python 3.11 / 3.13，见 §5 |
| 平台 C | 公司机 Windows 11 / Python 3.14.6 —— **本轮未跑**，两台 Windows 均不在线 |
| 静态检查 | `pyflakes bigfish tools tests` 零输出；`compileall -q bigfish tests tools` 通过 |
| 自检 | `tools/selfcheck.py` 16 / 16（**含 `PYTHONIOENCODING=cp1252` 复跑**） |
| GUI 冒烟 | `tools/gui_smoke.py` 9 / 9（offscreen） |
| 脱敏副本 | 与本地同一份代码，推 GitHub 前单独复跑，见 §5.4 |

---

## 1. 测试总数构成

| 文件 | 项数 | 主题 |
| --- | --- | --- |
| `tests/test_v011_rc4.py` | 33 | rc4 边界与发布工程 |
| `tests/test_v011_rc7.py` | 28 | 租约原子性、字段上限、连接闸门、凭据状态、CI 规则 |
| `tests/test_v011_rc8.py` | **27** | 本轮新增（见 §3） |
| `tests/test_v011_rc3.py` | 26 | 状态机、取消确认、恢复、幂等、审计包 |
| `tests/test_v011_p2.py` | 26 | 四角色权限 |
| `tests/test_v011_rc5_restore_authority.py` | 24 | 恢复后权威重建 |
| `tests/test_v011_rc5_receipts.py` | 24 | 执行回执与结果未知判定 |
| `tests/test_v011_p1.py` | 24 | 取消/审批/执行票 |
| `tests/test_v011_p3.py` | 20 | 网络与监听边界 |
| `tests/test_v011_rc6.py` | 16 | 租约、审计诚实性、体积上限、GUI 停服 |
| `tests/test_v011_p0.py` | 16 | WAL 备份、审批版本漂移、明文票 |
| `tests/test_core.py` | 15 | v0.1.0 原始核心域 |
| `tests/test_v011_rc5_review_audit.py` | 13 | 审查与投票分离 |
| `tests/test_v011_rc5_proxy.py` | 9 | 代理监听闸门 |
| `tests/test_v011_rc5_key_heal.py` | 8 | 密钥自愈 |
| `tests/test_api.py` | 6 | HTTP / MCP 接口 |
| `tests/test_v011_rc5_cli_guard.py` | 4 | CLI 恢复守卫 |
| `tests/test_v011_rc5_dispatch_id.py` | 3 | 派工 id 语义 |
| **合计** | **322** | |

v0.1.0 的 21 项（`test_core` 15 + `test_api` 6）从第一轮起就在，本轮没有删除、没有改断言、没有跳过。

---

## 2. skipped 的那 1 项是什么

`tests/test_v011_rc8.py` `test_local_table_refcount_shared`：

```python
@unittest.skipUnless(os.name == "nt", "槽位模型只在 Windows 上生效")
```

验证 Windows 槽位式字节锁在**同进程内共享句柄**的语义（refcount=2，同一个 `fh`）。Linux 走 `flock`，没有槽位概念，所以跳过。它在 CI 的 Windows runner 上真跑——这正是「平台差异用明确条件测试」而不是「静默跳过安全测试」的证据。

另有两条条件测试：`tests/test_v011_p3.py` 的 `skipIf(os.name == "nt")`（Windows 无 SIGINT 语义，`TerminateProcess` 退出码 1 而非 -15）、`tests/test_v011_rc5_proxy.py` 的 `skipUnless(NODE)`（本机装了 node，实际未跳过）。

---

## 3. 本轮新增 27 项

### 3.1 `TestConcurrentLocalHandles`（2 项）

| 用例 | 断言什么 |
| --- | --- |
| `test_parallel_open_close_100_times` | `ThreadPoolExecutor(max_workers=8)` + `threading.Barrier` 让 8 个线程同时起跑，每线程 100 轮「打开 → 持有一小会儿 → 关闭」同一数据库；结束后断言每轮都成功、`locks._LOCAL` 为空 |
| `test_local_table_refcount_shared` | 同进程两次 acquire 同一路径 → 同一个 `fh`、refcount=2、第一次 release 后仍为 1、第二次后条目消失（仅 Windows） |

必须用真线程 + 屏障的原因：这个 bug 的触发条件就是「两个线程在同一瞬间都看到空表」。顺序调用复现不出来；rc7 时代这条路径在 Linux 上一直是绿的，因为 Linux 走 `flock` 分支，根本没有 `_LOCAL` 表。

### 3.2 `TestUnknownRoleFailsClosed`（7 项）

| 用例 | 断言什么 |
| --- | --- |
| `test_mapping_returns_denied_role` | `role_for_db_row("superuser") == DENIED_ROLE`，不返回任何有权限的角色 |
| `test_denied_role_has_no_permissions` | `PERMISSIONS[DENIED_ROLE] == set()`，对 `task.claim` / `run.submit` / `approve` 全部 `can() is False` |
| `test_authenticate_rejects_unknown_role` | 手工把 `executors.role` 改成 `superuser`，拿有效 token 调 `authenticate()` → `CouncilError` |
| `test_authenticate_any_rejects_unknown_role` | 同上，走 `authenticate_any()` |
| `test_unknown_role_cannot_claim_via_service` | 走服务层 claim 被拒，且回查任务仍是 APPROVED 未被领取 |
| `test_dispatch_tool_denies_missing_canonical_role` | 传没有 `role_canonical` 的执行器字典给 `dispatch_tool`，claim 被拒（旧实现会兜底成执行器放行） |
| `test_known_executor_still_works` | 正常注册的执行器仍能 claim / heartbeat / submit——防「修太狠把主路径也堵死」 |

### 3.3 `TestSubprocessEncodingIndependence`（2 项）

| 用例 | 断言什么 |
| --- | --- |
| `test_chinese_survives_under_ascii_env` | `PYTHONIOENCODING=ascii` 起子进程跑 `python -m bigfish --headless --host 0.0.0.0`，父进程按 UTF-8 解码后必须读到中文「只允许监听回环地址」，退出码 2 |
| `test_proxy_gate_constant_is_a_window_not_a_latency` | `tools/bfc-proxy.mjs` 的启动窗口常量不小于 10 秒，且没有被当成性能断言 |

### 3.4 `TestWindowsAclMatching`（6 项）

`DOMAIN\\user:(F)` 能匹配当前用户 / 裸用户名能匹配 / 只有 SID 时按 SID 候选匹配 / 只有别人的条目必须判否 / `Everyone`、`BUILTIN\\Users`、`Authenticated Users` 被标为宽泛主体 / `verify_file_restricted` 仍必须拿到当前用户候选（不能因为「有 ACL 就算通过」）。

### 3.5 `TestConsoleDecoding`（6 项）

GBK 字节解出中文且无 U+FFFD / UTF-8 正常 / 乱码仍返回 `str` 不抛 / 空 bytes / `run_capture` 跑输出 `b"\xd2\xf3"` 的子进程必须拿到非空文本 / 缺失二进制 `rc≠0` 且 err 含「调用失败」。

### 3.6 `TestConsoleEncodingFallback`（4 项，CI 暴露后补）

这一组是**看了 CI 的真实失败日志之后**才写的（§5.2），不是预防性设计。

| 用例 | 断言什么 |
| --- | --- |
| `test_cp1252_console_survives` | `PYTHONIOENCODING=cp1252` 子进程里 `import bigfish` 后 `print("健康检查")`，退出码必须为 0，stdout 按 UTF-8 解码能读到中文，`ENC=utf-8` |
| `test_gbk_console_is_left_alone` | `PYTHONIOENCODING=cp936` 时**不许**被改成 UTF-8：断言 stdout 原始字节里含 `"健康检查".encode("gbk")` 且含 `b"ENC=gbk"` |
| `test_utf8_console_untouched` | UTF-8 环境不动 |
| `test_env_switch_actually_disables` | `BFC_NO_STDIO_FIX=1` 时必须回到崩溃（`UnicodeEncodeError`），否则前三条可能只是「子进程恰好没输出中文」的假绿 |

这些测试全部断言**原始字节**而不是父进程解码后的字符串。原因写在测试注释里：子进程按哪种编码写，本身就是被测行为，父进程替它解码等于把结论提前写进测试——第一版就是这么写的，`test_gbk_console_is_left_alone` 直接假红。

---

## 4. 测试质检（任务书第九节要求逐条查）

rc8 只新增，不改旧断言（除 `test_v011_rc5_restore_authority.py` 的语义修正，见 `DIFF-SUMMARY` §7.4），以及 `test_heartbeat_lease_is_in_the_future` 的时钟粒度修正（见 §5.2）。

| 坏味道 | 本轮检查结果 |
| --- | --- |
| 恒真断言 | 无。新测试每条都断言具体值、具体异常文案或具体字节 |
| 只查字段不查业务结果 | `test_unknown_role_cannot_claim_via_service` 断言了「任务仍 APPROVED 未被领取」，不只看返回值 |
| 只模拟异常没真并发 | `TestConcurrentLocalHandles` 用 `ThreadPoolExecutor` + `Barrier`，没有 `sleep` 占位 |
| 没验证 DB 最终状态 | `test_denied_role_has_no_permissions` 直接读 `PERMISSIONS` 常量；claim 测试回查任务状态 |
| 失败回滚未验证 | 本轮不涉及恢复流程 |
| 条件跳过掩盖失败 | Windows 专属项在 CI runner 上真跑 |
| 测试用「解码后的文本」断言编码行为 | §3.6 已全部改为断言原始字节 |

---

## 5. 平台 B：GitHub Actions

### 5.1 为什么用 CI 而不是公司机

rc8 收尾时探测：`100.64.0.11:8765`/`:3096` TIMEOUT（CLIENT-B 离线）；`100.64.0.12:8765`/`:3096` ECONNREFUSED（CLIENT-A 在线但 windows-mcp 没起）。两台都连不上。CI 的 `windows-latest` 是同族平台的替代证据，**不等于公司机真机通过**。

### 5.2 第一次运行：318 项跑完，两处失败（真实日志）

运行 id `45170828326`，head `fa37e2e`。

| job | 结果 | 失败点 |
| --- | --- | --- |
| `ubuntu-latest / py3.11` | success | — |
| `ubuntu-latest / py3.13` | success | — |
| `边界自检` | success | — |
| `windows-latest / py3.11` | **failure** | `Unit tests` |
| `windows-latest / py3.13` | **failure** | `Self check` |

**关键事实：Windows 上 318 项是跑完了的**（`Ran 318 tests in 181.158s`），只挂了 1 项；py3.13 那个 job 的单测甚至是 `OK (skipped=1)`，挂在后面的自检步骤。

失败 1（py3.11 单测）：

```
FAIL: test_heartbeat_lease_is_in_the_future
  (tests.test_v011_rc5_review_audit.TestTimestampInsideTransaction)
  File "D:\a\BigFish-Council-\BigFish-Council-\tests\test_v011_rc5_review_audit.py", line 152
    self.assertGreater(after["renewed_at"], before["renewed_at"], "续租时间没有前进")
AssertionError: 1795435663.622005 not greater than 1795435663.622005 : 续租时间没有前进
Ran 318 tests in 181.158s
FAILED (failures=1, skipped=1)
```

两次取值到小数点后 6 位完全相同。根因是 Windows 的 `time.time()` 分辨率约 15.6ms，claim 与 heartbeat 落在同一个 tick 内。**这是测试对时钟粒度的假设错了，不是续租失效**——续租本身在同一次调用里完成，`expires_at` 也照常被续。

失败 2（py3.13 自检）：

```
File "D:\a\BigFish-Council-\BigFish-Council-\tools\selfcheck.py", line 79, in main
    print("健康检查")
UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-3
```

runner 的 `sys.stdout` 是 cp1252。**这是产品缺陷，不是测试问题**：任何人拿英文版 Windows（或把输出重定向到 cp1252 控制台）跑 `tools/selfcheck.py`，都会在印第一行中文时整进程 traceback 退出，看不到任何检查结果。py3.11 那个 job 只是因为单测先挂了，没走到自检这步才显得「没这个问题」。

### 5.3 修完之后

| 修复 | 内容 |
| --- | --- |
| R8-02 | 新增 `bigfish/console.py`：`install_utf8_stdio()` 只在「当前编码表示不了中文」时把 stdout/stderr 切到 UTF-8 + `errors="replace"`；cp936/UTF-8 环境不动。`bigfish/__init__.py` 导入时自动执行（`__main__` / `cli` / `gui` / 所有 tools 都 import 它，一处生效） |
| R8-03 | `tests/test_v011_rc5_review_audit.py` 在两次心跳之间显式 `time.sleep(0.05)`，让断言测「续租有没有发生」而不是「系统时钟够不够细」 |

本地验证（复现路径与 CI 一致）：

```
PYTHONIOENCODING=cp1252 python -c "print('健康检查')"
  → UnicodeEncodeError: 'charmap' codec can't encode characters（复现 CI 现象）

PYTHONIOENCODING=cp1252 python -c "import bigfish; print('健康检查')"
  → 健康检查 / STDIO_STATUS {'stdout': 'ok', 'stderr': 'ok'} / ENCODING utf-8 replace

PYTHONIOENCODING=cp936 python -c "import bigfish; print('健康检查')"
  → 不动，ENCODING 仍是 gbk

PYTHONIOENCODING=cp1252 python tools/selfcheck.py
  → 通过 16 项，失败 0 项
```

修复后在平台 A 全量：**`Ran 322 tests in 148.492s / OK (skipped=1)`**（318 + 4 项新测试）。

### 5.4 第二次运行：五个 job 全绿，322 项两个平台都跑完

修完 `9941965` 与文档回填后重推（head `fdd5c12`），run **`45174755219`**：

| job | 结论 | 关键日志 |
| --- | --- | --- |
| `ubuntu-latest / py3.11` | success | 平台 A 全量 |
| `ubuntu-latest / py3.13` | success | 平台 A 全量 |
| `边界自检` | success | 端口/回环闸门等静态检查 |
| `windows-latest / py3.11` | success | `Ran 322 tests in 183.951s` / `OK (skipped=1)` / selfcheck 16/16 / gui_smoke 9/9 |
| `windows-latest / py3.13` | success | `Ran 322 tests in 197.687s` / `OK (skipped=1)` / selfcheck 16/16 / gui_smoke 9/9 |

从第一次的 `Ran 318 / FAILED (failures=1)` 到第二次的 `Ran 322 / OK (skipped=1)`，两处失败都消失，且**测试数从 318 涨到 322 而不是减少**——新增的 4 项控制台编码测试没有靠删测试换绿灯。

这一轮最值钱的地方不是「CI 绿了」，而是它把一个从 rc6 背到 rc7 的错误结论推翻了：

> rc7 的文档里写着「Windows 跑不了全量（3 项编码/超时红灯）」。

真实情况是 **318 项全跑完了**，只是挂着 1 项时钟粒度 + 另有个 job 的自检崩在 cp1252 上。那台机器一直连不上，才让这个结论多活了一版。


---

## 6. 与 rc7 的差异

| 项 | rc7 | rc8 |
| --- | --- | --- |
| 测试数 | 295（Linux 全绿；Windows 294） | 322（Linux 与 Windows 都全绿） |
| Windows 能否跑全量 | 不能（3 项编码/超时红灯） | **能**：CI 上 322 项全跑完（两次运行，见 §5） |
| 未登记角色 | 兜底成执行器 | 拒绝 + 审计 |
| Windows 同进程并发打开 | 句柄/槽位双泄漏 | 整段串行 + 异常关句柄 |
| Windows ACL 校验 | 可能因解码失败静默失效 | 强类型解码兜底 |
| 英文版 Windows 跑自检 | cp1252 下 traceback 退出 | 自动切 UTF-8，正常输出 |

---

## 7. 未验证清单（不粉饰）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 公司机（Windows 11 / Py3.14.6）跑 322 项 | **未做** | 机器离线；CI runner 是 Windows Server 2022，不是同一台 |
| GUI 真窗口人工验收 | **未做** | 无显示器；离屏 9/9 |
| 跨设备 MCP 闭环 | **未做** | Windows 侧通道不可达 |
| exe 打包 | **未做** | 需 Windows 构建机 |
| Windows 真机 `icacls` 实测 | **未做（本轮）** | rc3 时做过一次（`acl: granted`） |
| cp1252 之外的代码页（如 cp437、日文 cp932） | **未逐个验证** | 兜底逻辑对所有「编不了中文」的编码一视同仁，但只实测了 cp1252 / cp936 / utf-8 三种 |
