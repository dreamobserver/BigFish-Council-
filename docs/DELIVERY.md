# 交付清单（v0.1）

交付日期：2026-11-18　　版本：bigfish-council 0.1.0

## 1. 已交付并验证

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 只读环境体检（VPS / Tailscale / Windows 端口） | 已完成，结论见 docs/TESTLOG.md 第 4 节 |
| P1 | Windows 本地控制中心 GUI（PySide6，五页 + 内嵌 API + 托盘） | 已完成，离屏冒烟 9/9 通过 |
| P2 | 本地受限 API + MCP 服务端（只暴露 5 个执行器工具） | 已完成，单元测试 + 自检 14/14 通过 |
| P3 | DSH 经 127.0.0.1:3098 跳板取任务 → 回传结果 | 协议层跑通（本机回环模拟） |
| P4 | 源码 + Windows 构建脚本 + 文档 + 测试日志 + 回滚 | 已完成（28 个文件） |

## 2. 验证证据（全部为实际执行结果）

- 单元测试：VPS 21/21 通过；CLIENT-A 21/21 通过（4.043s）。
- 自检脚本 `tools/selfcheck.py`：14/14 通过。
- GUI 离屏冒烟 `tools/gui_smoke.py`：9/9 通过（五个页面构造与切换 + API 线程 + 退出）。
- 本机闭环：创建 → 送审 → 双模型投票 → 审批 → 领取 → 心跳 → 回传 → 待验收，全链路通过。
- MCP 闭环：initialize / tools/list / claim / heartbeat（幂等）/ submit / status 全部符合预期，
  且工具列表里没有审批类工具、列表接口不返回 Prompt。

## 3. 未完成（受外部条件限制）

| 项 | 阻塞原因 | 需要谁 |
|---|---|---|
| Windows 端到端联调 | CLIENT-A 上 windows-mcp 的动作类工具（PowerShell / FileSystem / Process / Wait）全部 12s 超时，只有 ControlStatus 正常 | 用户在本机重启 windows-mcp，见 docs/WINDOWS-MCP-RECOVERY.md |
| 把 src.zip 传到 D:\BigFishCouncil | 同上，唯一的文件通道就是那个 MCP | 用户重启后再传，或本机直接用 U 盘/共享盘 |
| 真实桌面渲染确认 | VPS 无显示器，只能离屏验证 | 用户在本机启动 GUI 肉眼确认 |

## 4. 需要用户明确授权的三件事

本程序默认不做这三件事，做了会改变本机/局域网的长期状态：

1. **注册登录自启计划任务**（`install-task.ps1`）：会在本机写入一个开机自启项，
   名字 `BigFishCouncil`，可随时用 `Unregister-ScheduledTask -TaskName BigFishCouncil` 删除。
2. **导入 bigfish-council 的 MCP 连接**（模板 `dsh-mcp-bigfish.json`）：会让云端 DSH
   多一个能连本机 3098 端口的连接。
3. **Tailscale Serve 暴露**：本次方案默认不开，也不需要开（DSH 容器与宿主共享 netns，
   可直接访问 100.x）。如果之后要跨设备访问，再单独确认。

## 5. 交付物清单

- 源码：`bigfish/`（8 个模块）
- 测试：`tests/test_core.py`（15 例）、`tests/test_api.py`（6 例）
- 工具：`tools/selfcheck.py`、`tools/gui_smoke.py`、`tools/bfc-proxy.mjs`
- 脚本：`setup.ps1`、`build.ps1`、`start-gui.ps1`、`install-task.ps1`
- 文档：`README.md`、`docs/USAGE.md`、`docs/SECURITY.md`、`docs/ROLLBACK.md`、
  `docs/TESTLOG.md`、`docs/WINDOWS-MCP-RECOVERY.md`
- 配置模板：`dsh-mcp-bigfish.json`
