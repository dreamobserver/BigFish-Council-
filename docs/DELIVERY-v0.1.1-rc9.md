# 交付说明：bigfish-council v0.1.1-rc9

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc9` |
| 上一版 | `0.1.1-rc8` |
| 定位 | **发布候选**，不是生产版本。生产/现役仍是 rc2（公司机 3096）或 rc3（本机） |
| 库结构 | schema **4** / backup format **4**，与 rc8 相同，**无迁移** |
| 测试 | **345 项**（Linux 全绿 175.583s；Windows 由 GitHub Actions 跑） |
| 本轮性质 | 只修不重写：MCP 协议层换成官方 SDK + 未知审查身份失败关闭 |
| 起点 | Codex 对 rc8 的第三方审计（`docs/AUDIT-CODEX-v0.1.1-rc8.md`，185 行） |

---

## 1. 本轮修了什么

| ID | 严重度 | 一行说明 |
| --- | --- | --- |
| AUDIT-RC8-01 | P1 | MCP 层一直是手写 JSON-RPC，`requirements.txt` 里声明的官方 SDK 从未被 import |
| AUDIT-RC8-02 | P1 | 未知 reviewer model **回落成 Gemini**，等于写错一列就白拿审查票权 |
| AUDIT-RC8-06 | P1 | MCP 只有 FastAPI `TestClient` 覆盖，从无真实客户端握手 |
| R9-01 | P1 | 换 SDK 后暴露：session manager 每实例只能 `run()` 一次，第二次请求就炸 |
| R9-02 | P1 | 鉴权失败回 `200 + error`，官方 SDK 客户端解析失败后**挂死不返回** |
| R9-03 | P2 | 用 `Mount` 挂子应用会让 `/mcp` 精确路径先回 307 |
| R9-04 | P2 | 维护期回 `200 + 错误文案`，执行器可能误判「票到手了」 |
| AUDIT-RC8-03 | P2 | rc8 tag 落在无父提交上（发布流程问题，本轮改正并记录分界点） |
| AUDIT-RC8-05 | P2 | 发布文档里 318 / 322 / 295 三个测试数互相矛盾 → 统一 345 |
| AUDIT-RC8-04 | P3 | 移动端口径：只有端口与只读角色预留，**无 APK** |

细节见 `docs/FIX-MATRIX-v0.1.1-rc9.md` 与 `docs/DIFF-SUMMARY-v0.1.1-rc9.md`。

---

## 2. 交付物清单

### 2.1 源码

| 路径 | 说明 |
| --- | --- |
| `/home/dsh/bigfish-council` | 工作副本（本地历史） |
| `bfc-src-v011rc9.zip` | 源码包（GitHub Release 附件） |
| `SHA256SUMS.txt` | 逐文件校验和 + 附件哈希 |

### 2.2 本轮新增文档

- `docs/FIX-MATRIX-v0.1.1-rc9.md` —— 修复总表
- `docs/DIFF-SUMMARY-v0.1.1-rc9.md` —— 逐文件真实 diff
- `docs/TEST-REPORT-v0.1.1-rc9.md` —— 测试报告
- `docs/SECURITY-REVIEW-v0.1.1-rc9.md` —— 安全复审
- `docs/DELIVERY-v0.1.1-rc9.md` —— 本文件
- `docs/MIGRATION-v0.1.1-rc9.md` —— 迁移（本版为空操作）
- `docs/ROLLBACK-v0.1.1-rc9.md` —— 回滚

---

## 3. 验证证据

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 平台 A 全量 | `.venv/bin/python -m unittest discover -s tests -t .` | `Ran 345 tests in 175.583s` / `OK (skipped=1)` |
| 静态检查 | `pyflakes bigfish tools tests` | 零输出 |
| 字节码 | `compileall -q bigfish tools tests` | 退出码 0 |
| 自检 | `tools/selfcheck.py` | **16 / 16**（含坏 Token 现在断言 401） |
| GUI 冒烟 | `tools/gui_smoke.py`（offscreen） | **9 / 9** |
| 端到端 | `tools/e2e_loopback.py` | **22 / 22**（同机回环） |
| MCP 真客户端 | `tests/test_v011_rc9_socket.py` | **8 / 8**（真 uvicorn + 官方 SDK） |
| 平台 B | GitHub Actions `windows-latest` × py3.11/3.13 | 见 `TEST-REPORT` §5 |

---

## 4. 怎么用

```bash
# Linux / VPS 自检
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \\
  .venv/bin/python -m unittest discover -s tests -t .

# 本地起服务（只回环）
.venv/bin/python -m bigfish --headless --data-dir ./data --port 3096

# GUI
.venv/bin/python -m bigfish --data-dir ./data
```

MCP 客户端连接方式**没变**（`http://127.0.0.1:3098/mcp` 或直连回环 3096），只是现在由官方 SDK 承载协议，`initialize` 请按规范带 `protocolVersion` / `capabilities` / `clientInfo`。

---

## 5. 上线前仍需人工做的（需授权）

| # | 事项 | 为什么不能自动做 |
| --- | --- | --- |
| 1 | 把 rc9 推上公司机 3096 现役服务 | 会中断正在使用的旧版；需要你决定时间窗 |
| 2 | 导入/更新 DSH 的 `bigfish-council` MCP 连接 | 需要 human_owner 批准，且要填 Bearer token |
| 3 | 注册开机自启 | 会改变机器启动行为 |
| 4 | GUI 真窗口人工验收 | 需要显示器前的人点一遍 |

---

## 6. 明确未做（不粉饰）

| 项 | 状态 |
| --- | --- |
| 公司机（Windows 11 / Py 3.14.6）跑 345 项 | **未做**，两台 Windows 的 Tailscale 都离线 |
| GUI 真窗口验收 | **未做**，只有离屏 9/9 |
| 跨设备 MCP 闭环（VPS → 公司机） | **未做**，通道不可达；rc2 时曾 11/11 |
| exe 打包 | **未做**，需 Windows 构建机 |
| 维护期 503 的独立断言 | **未覆盖**，间接由恢复期测试覆盖 |
| rc6/rc7/rc8 三个 tag 的祖先链 | **改不了**，改写历史会让已发布附件哈希失效 |

---

## 7. 一句话总结

rc9 把「MCP 层自己造轮子」这个根因换掉了，顺手关掉 SDK 暴露的四处真缺陷（单次 run、401、307、503），并把「未知审查身份默认 Gemini」这条提权口子堵死。库结构没动，345 项在 Linux 上真跑全绿，MCP 由官方 SDK 的客户端验收而不是自己测自己。生产部署、真机复跑、exe 打包都还没做，所以它是 **rc9 发布候选**，不是「可以交给使用者」的版本。
