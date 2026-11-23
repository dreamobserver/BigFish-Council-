# 交付说明：bigfish-council v0.1.1-rc8

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc8` |
| 上一版 | `0.1.1-rc7` |
| 定位 | **发布候选**，不是生产版本。生产/现役仍是 rc2（公司机 3096）或 rc3（本机） |
| 库结构 | schema **4** / backup format **4**，与 rc7 相同，**无迁移** |
| 测试 | 318 项（Linux 全绿；Windows 由 GitHub Actions 跑） |
| 本轮性质 | 只修不重写：2 条 P1 提权/DoS + 1 条静默失效 + 3 条工程项 |

---

## 1. 本轮修了什么

| ID | 严重度 | 一行说明 |
| --- | --- | --- |
| R7-01 | P1 | Windows 同进程并发打开数据库时句柄与槽位双泄漏，64 次后永久打不开 |
| R7-02 | P1 | `executors` 表里出现未登记角色时，旧代码**回落成执行器**（提权） |
| R8-01 | P1 | `icacls` 输出解码失败会让 ACL 校验静默失效 |
| CI-01 | P2 | 三处子进程编码/超时写死，导致 Windows CI 红灯 |
| CI-02 | P3 | 编码断言依赖 locale |
| MINOR-01 | P3 | CLI `executor-add --role` 缺 `mobile` |

细节见 `docs/FIX-MATRIX-v0.1.1-rc8.md` 与 `docs/DIFF-SUMMARY-v0.1.1-rc8.md`。

---

## 2. 交付物清单

### 2.1 源码

| 路径 | 说明 |
| --- | --- |
| `/home/dsh/bigfish-council` | 工作副本，分支 `fix/v0.1.1-rc7-hardening`，HEAD `031ebcf` |
| `/home/dsh/bfc-src-v011rc8.zip` | 源码包，106 文件 / 827982 字节 |
| `/home/dsh/bigfish-council-public` | 脱敏副本（107 文件），已推到 GitHub `fa37e2e` |

### 2.2 文档（本轮新增）

- `docs/FIX-MATRIX-v0.1.1-rc8.md` —— 修复总表
- `docs/DIFF-SUMMARY-v0.1.1-rc8.md` —— 逐文件真实 diff
- `docs/TEST-REPORT-v0.1.1-rc8.md` —— 测试报告（含平台 B）
- `docs/SECURITY-REVIEW-v0.1.1-rc8.md` —— 安全复审
- `docs/DELIVERY-v0.1.1-rc8.md` —— 本文件
- `docs/ROLLBACK-v0.1.1-rc8.md` —— 回滚
- `docs/MIGRATION-v0.1.1-rc8.md` —— 迁移说明（本版为空操作）

---

## 3. 验证证据

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 平台 A 全量 | `.venv/bin/python -m unittest discover -s tests -t .` | `Ran 318 tests in 148.742s` / `OK (skipped=1)` |
| 静态检查 | `pyflakes bigfish tools tests` | 零输出 |
| 字节码 | `compileall -q bigfish tests tools` | 退出码 0 |
| 自检 | `tools/selfcheck.py` | 16 / 16 |
| GUI 冒烟 | `tools/gui_smoke.py`（offscreen） | 9 / 9 |
| 脱敏副本 | 同上全量 | `Ran 318 tests in 153.043s` / `OK (skipped=1)` |
| 平台 B | GitHub Actions `windows-latest` × py3.11/3.13 | 见 `TEST-REPORT` §5.3 |

---

## 4. 怎么用

与 rc7 相同，无新增步骤：

```bash
# Linux / VPS 自检
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \\
  .venv/bin/python -m unittest discover -s tests -t .

# 本地起服务（只回环）
.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096

# GUI
.venv/bin/python -m bigfish --data-dir ./data
```

Windows 上 `D:\\BigFishCouncil\\bigfish-council-rc8` 已就位（57+ 文件），依赖装好。

---

## 5. 上线前仍需人工做的（需授权）

| # | 事项 | 为什么不能自动做 |
| --- | --- | --- |
| 1 | 把 rc8 推上公司机 3096 现役服务 | 会中断正在使用的旧版；需要你决定时间窗 |
| 2 | 导入 DSH 的 `bigfish-council` MCP 连接 | 需要 human_owner 批准，且要填 Bearer token |
| 3 | 注册开机自启 | 会改变机器启动行为 |
| 4 | GUI 真窗口人工验收 | 需要显示器前的人点一遍 |
| 5 | 打 tag + 发布 | 建议在 §3 证据都齐、CI 绿之后 |

---

## 6. 明确未做（不粉饰）

| 项 | 状态 |
| --- | --- |
| 公司机（Windows 11 / Py 3.14.6）跑 318 项 | **未做**，机器离线 |
| GUI 真窗口验收 | **未做**，只有离屏 9/9 |
| 跨设备 MCP 闭环（VPS → 公司机） | **未做**，通道不可达；rc2 时曾 11/11 |
| exe 打包 | **未做**，需 Windows 构建机 |
| Windows 真机 ACL 实测 | **未做**（rc3 时做过一次：`acl: granted`） |
| Cloudflare 层 SSE 125s 掐断 | **未解**，与本产品无关 |

---

## 7. 一句话总结

rc8 把 rc7 剩下的 3 条 P1 关掉了，Linux 318 项全绿，脱敏副本同样全绿并已推到 GitHub 让 CI 跑 Windows。生产部署、真机验收、exe 打包都还没做，所以它是 **rc8 发布候选**，不是「可以交付给使用者」的版本。
