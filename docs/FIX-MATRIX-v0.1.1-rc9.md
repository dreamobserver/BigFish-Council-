# 修复总表：bigfish-council v0.1.1-rc9

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc9`（`bigfish/__init__.py:6`） |
| 日期 | 2026-11-26 |
| 相对基线 | 远端 main `aeab598b3e`（rc8 + Codex 审计文档入库）；本地工作副本上一个提交 `e28f590` |
| 本轮性质 | **只修不重写**：把 MCP 协议层从手写换成官方 SDK，连带修 SDK 暴露的 4 处真缺陷；未知 reviewer model 改失败关闭；文档口径统一 |
| 差异规模 | **11 files changed, 1118 insertions(+), 293 deletions(-)**。核心新增 `bigfish/mcp_adapter.py` 533 行；`bigfish/server.py` 从 630 行降到 393 行（51+/282−）；新增测试 `tests/test_v011_rc9.py` 181 行 15 例、`tests/test_v011_rc9_socket.py` 306 行 8 例 |
| 库结构 | `SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`，**rc9 未改**，不需要迁移、不需要回滚库 |
| 全量测试 | 平台 A：**`Ran 345 tests in 175.583s / OK (skipped=1)`**（rc8 的 322 + rc9 新增 23） |
| 静态检查 | `pyflakes bigfish tools tests` 零输出 `PF_RC=0`；`compileall -q` `RC=0` |
| 平台 | 平台 A（Debian 12 / Python 3.11.2 / SQLite 3.40.1）345 项全绿；平台 B（Windows）由 GitHub Actions 承担，run 45812528363 两档都是 345 全绿（py3.11 178.800s / py3.13 198.766s，OK skipped=1），详见 `TEST-REPORT-v0.1.1-rc9.md` §5 |

状态口径（沿用 rc8，不自行改级）：

- `已修复`：代码已改，且有能失败的回归测试，本批次实测通过。
- `已修复（未覆盖）`：代码已改，但自动化证据只覆盖一部分，缺什么在正文点名。
- `已修复（文档级）`：改的是契约、声明或发布流程，本身没有可失败的自动化断言。
- `未修`：本轮明确不做，写清原因。

严重度沿用 Codex 审计给的档：P1 = 直接让一条安全保证落空或让协议层不可用；P2 = 不直接破保证，但会让声明与事实不一致。

---

## 一、rc9 修复总表

本轮来源是 Codex 对 rc8 的审计（`docs/AUDIT-CODEX-v0.1.1-rc8.md`，185 行）六条结论。我逐条回源码核对后：三条属实、一条只对一半、一条是口径提醒、一条是存疑但成立。编号沿用审计里的 AUDIT-RC8-xx；换 SDK 过程中新暴露的缺陷另编号 R9-xx。

| 编号 | 级别 | 原始问题（一句话） | 修改文件（行号 / 函数） | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| AUDIT-RC8-01 | P1 | `requirements.txt:7-8` 声明了官方 `mcp` SDK，但 `bigfish/server.py:319-404,452-623` 一直手写 initialize / ping / tools/list / tools/call，全仓 `grep -rn "import mcp"` 零命中——协议演进跟不上、错误码靠自己维护 | 新增 `bigfish/mcp_adapter.py`（533 行）；`bigfish/server.py` 删除整段手写 JSON-RPC，改为 `Route("/mcp", endpoint=McpAsgi(council))`；`__all__` 保留 15 个历史导出名 | 协议交给官方 SDK 的 `lowlevel.Server` + `StreamableHTTPSessionManager`；业务只留 `dispatch_tool()` 一个入口，REST 与 MCP 共用；工具清单仍由 `tool_specs()` 决定（审批类工具不许出现）；参数校验仍走 `_validate_args()`（字段级 `maxLength` 是 BFC-02 的交付物，换实现不能丢） | `tests/test_v011_rc9_socket.py` 8 项（真 uvicorn + 官方 SDK 客户端）；`tests/test_api.py` 6 项（原有，未改断言）；`tools/selfcheck.py`、`tools/e2e_loopback.py` 实跑 | 已修复 |
| AUDIT-RC8-02 | P1 | 未知 reviewer model 失败开放：`role_for_db_row("reviewer", "weird", "unknown")` 返回 `reviewer_gemini`。等于「手工写一行 SQL 或旧版本残留行 = 白拿审查票权」 | `bigfish/roles.py:97-112` `role_for_db_row`；`bigfish/core.py:164-182` `register_executor` | model 命中 gpt/gemini 直接用；否则只在名称里**唯一可辨**时推断；两可或都不含 → `DENIED_ROLE`（零权限哨兵）。注册入口改成同一套判定，原来那里默认 Gemini、现在拒绝猜测 | `tests/test_v011_rc9.py` 15 项：脏行拒绝、空名空 model 拒绝、名称含 gpt+gemini 双关键词拒绝、显式 model 仍工作、唯一名称推断仍工作（旧数据兼容）、`authenticate()`/`authenticate_any()` 拒绝、脏 reviewer 拿不到票权、拒绝写审计、审计不含明文 token | 已修复 |
| AUDIT-RC8-06 | P1 | MCP 兼容性「自己测自己」：CI 只用 FastAPI `TestClient` 跑手写协议，从来没有真实客户端走过 initialize → notifications/initialized → tools/list → tools/call | 新增 `tests/test_v011_rc9_socket.py`（306 行） | 测试手法换成：进程内起**真 uvicorn**（真 socket、真端口，`_free_port()` 取随机 loopback 端口）+ 客户端用**官方 mcp SDK**（`streamablehttp_client` + `ClientSession`）。协议层由第三方实现验收，不是本仓库代码 | 该文件即证据。覆盖协议版本协商、serverInfo、工具集恰为五件（且无 approve/restore/add_executor）、每个工具 inputSchema 是 object、完整闭环（服务端状态真变 DISPATCHED→RUNNING→SUBMITTED）、同幂等键两次 claim 拿同一张票、业务失败 isError=true、坏 token 被拒、缺 token 被拒、执行器读别人任务被拒 | 已修复 |
| R9-01 | P1 | 换 SDK 后暴露：`StreamableHTTPSessionManager.run()` 每个实例只能进一次，第二次抛 `RuntimeError: ... can only be called once per instance`（`mcp/server/streamable_http_manager.py:155`）。如果照抄「建一个 manager 反复用」的官方示例，服务在第二次请求就全挂 | `bigfish/mcp_adapter.py:377-405` `McpAsgi.__init__` / `_new_manager` | 每请求新建一个 manager（`stateless=True` 本来不存会话，不损失能力）。附带好处：不需要把 app lifespan 拉起来，`TestClient` 不进 `with` 也能调 | `tests/test_v011_rc9_socket.py::test_full_roundtrip_over_real_socket`（同一个 socket 上连续五次调用）、`tests/test_api.py` 全部（TestClient 反复请求） | 已修复 |
| R9-02 | P1 | 鉴权失败返回 `HTTP 200 + JSON-RPC error(-32001)`。官方 SDK 客户端收到这种响应时 `JSONRPCMessage.model_validate_json` 报 9 条 pydantic 校验错（`id` 不能为 null），然后**挂住不退出**——实测挂满 3 分钟被外部超时砍掉。裸 curl 测不出来，只有真客户端能暴露 | `bigfish/mcp_adapter.py:415-430` `_auth` / `_jsonrpc_error`；`tools/selfcheck.py:137` 断言 200→401；`tools/remote_check.py:133` 注释 | 按 MCP 规范，凭据无效回 **HTTP 401**，body 里仍带 `-32001` 便于脚本解析。维护期同理回 **503 + Retry-After: 15**（见 R9-04） | `test_bad_token_rejected_over_real_socket`、`test_missing_token_rejected_over_real_socket` 用官方 SDK 断言「拿到的是异常而不是挂死」；`tools/selfcheck.py` 实跑 16/16 | 已修复 |
| R9-03 | P2 | 用 `Mount("/mcp", ...)` 挂子应用时，对精确路径 `/mcp` 只给 PARTIAL 匹配，Starlette 的 `redirect_slashes` 会先回 **307**，客户端跟着跳一次才拿到响应；而且 `Mount` 会把前缀吞掉，`scope["path"]` 变形 | `bigfish/server.py:340-352` | 改用 `app.router.routes.insert(0, Route("/mcp", endpoint=_mcp_app, methods=["POST","GET","DELETE"]))`，同时插一条 `/mcp/` 兜尾斜杠。Route 的 endpoint 传**类实例**时按 ASGI 直接调用（传函数会被包成 request-response，签名不对） | `tests/test_v011_rc9_socket.py` 全部（客户端不接受 307 后会跟跳，但会额外花一轮）；`test_api.py` 里对 `/mcp` 精确路径的直接 POST | 已修复 |
| R9-04 | P2 | 维护期（恢复备份中）必须让调用方知道「这次没执行」。旧实现在 SDK 内抛错只能变成 200 + 错误体，执行器可能误以为票据到手 | `bigfish/mcp_adapter.py:460-475`；`bigfish/db.py:358-361` 新增 `maintenance_reason` 属性 | 在进 SDK 之前判 `council.db.in_maintenance()`，直接回 503 + `{"maintenance": true}` + `Retry-After: 15`，文案里带维护原因 | 由 rc5 的 `TestRestoreAuthority` 一组间接覆盖（恢复期拒绝）；`mcp_adapter` 侧无独立断言，属 `已修复（未覆盖）` | 已修复（未覆盖） |
| AUDIT-RC8-05 | P2 | 发布文档测试数量三处矛盾：`DELIVERY-v0.1.1-rc8.md:9` 写 318、`README.md:303` 写 295、`README.md:18` 写 322 | `README.md`、`CHANGELOG.md`、`docs/DELIVERY-v0.1.1-rc9.md`、本文件 | 全部统一到 345，并把测试表补齐 rc8/rc9 三行（rc8 那行当时漏登记） | 无自动化；用 `grep -rn "318\|322\|295" README.md docs/*rc9*` 人工复核 | 已修复（文档级） |
| AUDIT-RC8-04 | P3 | 移动端只有端口与角色预留，没有 apk、没有 mobile 路由。审计说「不要宣称初步开发完成」 | `README.md`、`CHANGELOG.md` | 事实核对：`docs/ARCHITECTURE-FUTURE.md:889` 已写「apk — 未实现」，README/CHANGELOG 也从来没有「安卓初步开发完成」的说法。所以这不是虚假声明，是**口径不够显眼**。在 README 的「明确未做」里点名「移动端 = 只有接口编号与只读角色，无 APK」 | 无自动化 | 已修复（文档级） |
| AUDIT-RC8-03 | P2 | rc8 tag 落在**无父提交**上（`5f8cd5a7ba` parents=[]，全部文件都是 added），无法用祖先链验增量。根因是历史发布方式：每版在公开副本里 `rm -rf .git` + `git init` + 单提交 `push -f` | 发布流程（`/tmp/push_rc9_final.sh`） | rc9 改为**在已有远端历史上追加提交**：clone 远端 main → 覆盖工作树 → 一次正常 commit（带 parent）→ push（不用 -f）→ tag 打在这个提交上。这样 rc9 有祖先，`git diff v0.1.1-rc8 v0.1.1-rc9` 直接可用 | 发布后 `git log --graph` 与 `git rev-list --count HEAD` 人工核对；`git diff --stat rc8 rc9` 能出结果 | 已修复（发布流程级） |

> **历史提交无法补救**：`v0.1.1-rc6` / `v0.1.1-rc7` / `v0.1.1-rc8` 三个 tag 所在的提交本来就是三条互不相连的根提交，这一点改不了（改写历史会让已发布的附件哈希与 tag 指向全部失效）。rc9 起改为线性追加，并在此记录分界点。

---

## 二、每条修复的「为什么不是打补丁」

### 2.1 AUDIT-RC8-01：为什么必须换 SDK，而不是补全手写协议

手写版能跑，问题在**边界**：

1. **协议版本**。手写版把 `2025-03-26` 写成返回值。客户端请求 `2025-11-25` 时它还是回 `2025-03-26`，而规范要求「服务端支持则回显客户端请求的版本，不支持则回自己支持的最高版本」。SDK 的 `ServerSession._received_request` 做的是前者。
2. **错误码**。手写版对参数错误回 `-32600`，SDK 回 `-32602`。同一个输入两个码，客户端没法写稳定分支。
3. **方法演进**。SDK 会加方法（`tasks/*` 就是后来加的）。手写版对未知方法一律 `-32601`，等于把「客户端升级」变成「我们坏掉」。

这三条都不是「改几行」能解决的——它们是「协议语义该由谁来维护」的问题。所以整层换掉，但**边界刻意保留**：工具清单、参数校验、鉴权顺序都还在我们手里（见 `mcp_adapter.py` 模块 docstring 的「刻意保留的三件事」）。

换完之后业务侧**一行没改**：`dispatch_tool()` 从 `server.py` 原样搬过去，REST 与 MCP 还是共用它。这是「换实现不换契约」。

### 2.2 AUDIT-RC8-02：为什么是「拒绝」而不是「默认 Gemini」

旧代码的意图是兼容：rc2 之前的审查身份没有 `model` 列，只能靠名字猜。但「猜不到就默认 Gemini」把兼容写成了**提权**：

```
role_for_db_row("reviewer", "weird", "unknown")  ->  reviewer_gemini   # 有票权
role_for_db_row("reviewer", "",       None)      ->  reviewer_gemini   # 有票权
```

审查票权是这个产品的**核心信任根**：双模型都投 approve 才能进 PENDING_APPROVAL。凭空多一个 Gemini 身份，等于审批可以一个人刷两票。

所以改成三态：显式 model → 直接用；名称唯一可辨 → 推断（旧数据仍然能用）；其余 → `DENIED_ROLE`（`PERMISSIONS` 是空集，`authenticate()` 直接抛错并写 `auth.denied_unknown_role` 审计）。

注册入口（`register_executor`）同步改成同一套判定。原来那里是 `model = "gpt" if "gpt" in low and "gemini" not in low else "gemini"` —— 一个入口失败关闭、另一个默认 Gemini，就是「同一件事两套规矩」，迟早对不上。现在两可时直接报错要求显式传参。

### 2.3 R9-01：为什么每请求新建 manager 是正解，而不是「缓存一个全局的」

官方示例是「模块级建一个 manager，在 lifespan 里 `async with manager.run()`，整个进程共用」。照抄到我们这里会撞两条：

1. `run()` 每个实例只能进一次（`streamable_http_manager.py:155` 直接 `raise RuntimeError`）。第二次请求就炸。
2. 那要求 app 必须有 lifespan。我们的 `create_app()` 已经有 lifespan（跑 Watchdog），但有测试与非 FastAPI 调用方直接 `app(scope, receive, send)`，不经过 lifespan。

`stateless=True` 下 manager 本来不保存会话状态——它只是一段「把 ASGI 请求翻译成 ServerSession 调用」的胶水。所以每请求新建既正确又不损失能力，顺带把 lifespan 依赖去掉。代价是每次请求构造一个对象（微秒级），可以忽略。

### 2.4 R9-02：为什么 401 不是「更严格」，而是「必须」

MCP 规范对 Streamable HTTP 的要求是：凭据无效回 401，客户端据此走重新授权。回 `200 + error` 在协议上是**语法合法的 JSON-RPC 响应**，客户端会尝试解析它——而错误响应的 `id` 是 `null`，官方的 `JSONRPCMessage` 模型不接受 `id: null`，于是 9 条 pydantic 校验错，`streamablehttp_client` 在清理阶段卡住不返回。

实测：把 rc8 的服务端对着官方 SDK 客户端跑，客户端挂满 3 分钟被外部超时砍掉，日志里只有 pydantic 报错，**没有任何一句说「令牌错了」**。这不是严格性问题，是「报错报不出来」的问题。

`tools/selfcheck.py` 的断言因此从 `st == 200` 改成 `st == 401`。这不是放宽，是改对了：rc8 那条断言描述的是缺陷本身。

---

## 三、与 rc8 的行为差异

| 维度 | rc8 | rc9 |
| --- | --- | --- |
| MCP 协议实现 | 手写 JSON-RPC（`server.py` 内 300 行） | 官方 SDK `lowlevel.Server` + `StreamableHTTPSessionManager` |
| 协议版本 | 固定回 `2025-03-26` | 协商（支持 `2024-11-05` / `2025-03-26` / `2025-06-18` / `2025-11-25`） |
| 鉴权失败 | `200 + -32001` | **`401` + `-32001`** |
| 维护期 | `200 + 错误文案` | **`503 + Retry-After: 15`** |
| 参数错误码 | `-32600` | `-32602`（SDK 语义） |
| 未知 reviewer model | 默认 Gemini（有票权） | 拒绝 + 审计（零权限） |
| `register_executor` 名称两可 | 默认 Gemini | 抛错要求显式传参 |
| 工具集 | 5 个执行器工具 | 不变（测试逐名断言） |
| 库结构 | schema 4 | 不变 |
| REST 接口 | `/health`、`/api/v1/*` | 不变 |

---

## 四、本轮未做（不粉饰）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| rc6/rc7/rc8 三个 tag 的祖先链 | **改不了** | 改写历史会让已发布附件哈希与 tag 指向全部失效。rc9 起改为线性追加并记录分界点 |
| 平台 B（Windows 真机）复跑 345 项 | **未做** | 公司机与家里机 Tailscale 均离线；由 GitHub Actions 的 `windows-latest` 承担 |
| GUI 真窗口人工验收 | **未做** | 只有离屏 9/9，需要显示器前的人点一遍 |
| 跨设备 MCP 闭环重跑 | **未做** | 两台 Windows 都不在线 |
| exe 打包 | **未做** | 需要 Windows 构建机 |
| 维护期 503 的独立断言 | **未覆盖** | 间接由恢复期测试覆盖，但没有直接断言 MCP 那条分支 |

---

## 五、一句话总结

rc9 修的是「协议层重复造轮子」这件根因，附带把 SDK 暴露的四处真缺陷（单次 run、401、307、503）一起关掉，再把未知审查身份从失败开放改成失败关闭。库结构没动，全部 345 项测试在 Linux 上真跑通过，MCP 这一层现在由第三方 SDK 的官方客户端验收，不是自己测自己。
