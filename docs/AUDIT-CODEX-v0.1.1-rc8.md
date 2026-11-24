# BigFish Council v0.1.1-rc8 审计与“造轮子”检查

审计对象为仓库 `main` 的提交 `5f8cd5a7ba729c8163b69a2b326adc144d104f54`，以及 Release `v0.1.1-rc8`。

## 结论摘要

- 核心状态机、执行票、恢复互斥、未知角色失败关闭、请求体限制和 Windows 编码修复已经进入 rc8。
- GitHub Actions 的五个 job 均为成功，覆盖 Ubuntu/Windows 与 Python 3.11/3.13。这个结果不能替代公司 Windows 11/Python 3.14、真实 GUI、跨设备 MCP 和打包产物验收。
- 当前 Release 只有源码压缩包和校验文件，没有 exe 或 apk。
- rc8 仍有一个发布结构缺陷，一个接口实现上的重复造轮子，以及一处失败关闭不完整的角色映射风险。

## 一、是否存在重复造轮子

| 模块 | 判断 | 依据 | 处理方向 |
| --- | --- | --- | --- |
| MCP HTTP/JSON-RPC | **是，建议整改** | `requirements.txt:7-8` 声明官方 `mcp` SDK，但 `bigfish/server.py:319-404,452-623` 从 `initialize`、`ping`、`tools/list`、`tools/call` 到错误码都手写了一套协议层，源码没有导入 `mcp` | 保留 `dispatch_tool()`、权限表和业务校验，把传输层换成官方 SDK 适配器。若 SDK 无法直接满足 Bearer 与回环部署，再保留一个很薄的兼容适配层，并删除未使用的 SDK 依赖 |
| SSE/长连接 | **未造轮子，当前是未实现** | `requirements.lock:20,48` 有 `httpx-sse` 与 `sse-starlette`，但代码没有 SSE endpoint，`bigfish/config.py:37-40` 把 3100 标为“计划中，未实现” | 下一阶段实现时直接使用 `sse-starlette`，不要再手写事件帧、断线重连和 keep-alive |
| Windows/ POSIX 数据库租约 | **不是简单造轮子** | 64 字节槽位模型需要“多写者共享、恢复独占、同进程句柄复用”的组合语义，普通单锁库不能直接替代 | 保留当前协议。可单独评估 `portalocker` 等库，但必须保留跨进程恢复、同进程引用计数和 100 轮并发回归 |
| 回环与请求体中间件 | **不是造轮子** | 这是项目自己的安全边界，尤其要覆盖 chunked body、实际连接地址和本地部署误绑 | 保留，补充真实 uvicorn socket 测试 |
| ReceiptLog、状态机、审批、恢复清理 | **不是造轮子** | 这些是本项目的审计证据和业务语义 | 保留领域实现，继续用 SQLite 事务与现有审计表 |
| Node 回环跳板 | **可接受的小型专用实现** | 需要自包含部署和固定路径白名单，当前只监听回环 | 保留，增加上游连接/读取超时和最大转发体积 |

### MCP 重复实现的最小拆分方向

```python
# bigfish/mcp_adapter.py
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bigfish-council")

@mcp.tool()
def council_list_tasks(ctx):
    ex = ctx.request_context.lifespan_context.executor
    require_executor(ex, "task.claim")
    return council.available_tasks()

# 其他工具只负责把参数交给现有 dispatch_tool。
# 业务状态机、权限表、幂等和审计仍由 bigfish.core 保持唯一实现。
```

迁移时必须先用 DSH 真实连接完成 `initialize → tools/list → tools/call`，再删除手写 JSON-RPC 分支。不能只替换 import。

## 二、审计发现

### 【高】rc8 tag 仍落在无父提交上，发布溯源链断裂

GitHub API 显示 `v0.1.1-rc8` 的注释 tag 指向提交 `5f8cd5a7...`，该提交 `parents=[]`，文件全部以 `added` 形式出现。这样无法从 rc7 通过正常祖先链验证 rc8 的增量，回滚、审计 diff 和构建证明都只能依赖人工对照。

修复方向是从 rc7 的真实提交创建连续提交，再在该提交上创建签名或至少带注释的 tag。不要把完整导出目录重新生成成根提交。

```bash
git fetch --tags origin
git switch -c release/rc8-linear <rc7-real-commit>
git restore --source 5f8cd5a7 -- .
git add .
git commit -m "release: v0.1.1-rc8"
git tag -a v0.1.1-rc8-linear -m "BigFish Council v0.1.1-rc8"
git push origin release/rc8-linear v0.1.1-rc8-linear
```

### 【一般】未知 reviewer model 仍会失败开放到 Gemini

`bigfish/roles.py:96-104` 已经把未知 `db_role` 映射到 `DENIED_ROLE`，但 `db_role="reviewer"` 且 `model` 不是 `gpt/gemini` 时，会继续按名称猜测，名称没有 `gpt` 时默认返回 `REVIEWER_GEMINI`。脏数据因此仍能得到审查权限。

建议改为无法确认模型就拒绝。

```python
if db_role == "reviewer":
    m = (model or "").strip().lower()
    if m in ("gpt", "gemini"):
        return ARCHITECT_GPT if m == "gpt" else REVIEWER_GEMINI
    low = (name or "").lower()
    if "gpt" in low and "gemini" not in low:
        return ARCHITECT_GPT
    if "gemini" in low and "gpt" not in low:
        return REVIEWER_GEMINI
    return DENIED_ROLE
```

同时增加 `model="unknown"`、空名称和 `authenticate_any()` 的拒绝测试。

### 【一般】移动端只有角色和端口预留，没有可调用接口

`MOBILE_VIEWER` 在 `bigfish/roles.py:13-15,50-53` 已存在，权限也只读，但没有移动 REST 路由、SSE 路由、配对流程、分页接口或 Android 项目。3099、3100、3101 在 `bigfish/config.py:37-40` 都仍是 `PORT_RESERVED`，Release 也没有 apk。当前状态应写成“接口编号和权限模型预留”，不能写成“安卓端初步开发完成”。

推荐的小型架构如下。

```text
Windows exe
  ├─ Council / SQLite / GUI 审批
  ├─ 3096 本地 API + DSH MCP
  └─ MobileGateway
       ├─ 3099 REST：配对、任务列表、详情、评论、状态
       └─ 3100 SSE：事件推送，断线后按 event_id 补拉

Android APK
  ├─ 配对二维码或一次性代码
  ├─ 短期 access token + 可撤销 device_id
  ├─ REST 只调用 mobile_viewer 权限
  └─ SSE 只订阅脱敏事件
```

第一步应先落地接口契约和鉴权测试，再做 Android UI。不要让 APK 直接调用 `authenticate_any()` 后自行解释权限。

```python
@app.get("/mobile/v1/tasks")
def mobile_tasks(mobile=Depends(mobile_identity)):
    require_role(mobile, R.MOBILE_VIEWER, "task.read")
    return {"items": council.list_tasks(limit=50, offset=0)}

@app.get("/mobile/v1/events/stream")
async def mobile_events(mobile=Depends(mobile_identity)):
    require_role(mobile, R.MOBILE_VIEWER, "event.read")
    return EventSourceResponse(event_generator(mobile["id"]))
```

### 【一般】发布文档的测试数量互相矛盾

`docs/DELIVERY-v0.1.1-rc8.md:9` 写 318 项，`docs/TEST-REPORT-v0.1.1-rc8.md:9`、tag 说明和 `README.md:18-19` 写 322 项，`README.md:303,363` 后半段仍残留 295 项和“期望 Ran 295 tests”。这会影响发布签字和后续回归判断。

处理方式是把测试数量放入一个生成文件或 CI artifact，README 只引用该来源。至少应在 rc8 文档中统一为 322，并删除旧版 295 的执行指令。

### 【存疑】MCP 兼容性依赖手写协议，当前 CI 没覆盖真实客户端

CI 覆盖的是单测、selfcheck 和 GUI offscreen smoke，没有真实 DSH MCP Connector 的握手与长连接回归。手写服务固定 `MCP_PROTOCOL = "2025-03-26"`，只返回 JSON，未实现 SSE 会话、重连和协议版本协商。当前配置虽然声明 `Accept: application/json, text/event-stream`，不能据此证明真实客户端兼容。

发布前至少跑一次真实链路并保存原始握手结果。

```text
initialize
→ notifications/initialized
→ tools/list
→ council_list_tasks
→ council_claim_task
→ council_heartbeat
→ council_submit_result
```

## 三、已确认无问题的部分

- rc8 的 Windows 同进程句柄并发修复确实把查表、抢槽、登记放入同一进程锁，并在异常路径关闭句柄。
- 未登记角色不再回落到执行器，服务层缺少 `role_canonical` 时也按零权限处理。
- `run_capture()` 先取 bytes 再按多个代码页解码，避免 `icacls` 解码异常造成静默 ACL 失效。
- Release 的 GitHub Actions 五个 job 均成功。这个结论只覆盖 CI runner，不覆盖公司真机。

## 四、rc8 后开发路线

### 阶段 0：先收口发布工程

1. 修复 tag 祖先链并重新生成 Release 校验文件。
2. 统一 322 项测试口径。
3. 在 Windows 11/Python 3.14 真机跑全量、真实 GUI、ACL 和恢复回滚。
4. 用真实 DSH 连接完成一次跨设备 MCP 闭环。

### 阶段 1：去掉 MCP 传输层重复实现

1. 抽出 `mcp_adapter.py`。
2. 用官方 SDK 提供 initialize、tools/list、tools/call 和 HTTP 传输。
3. 保留现有 `dispatch_tool()` 作为唯一业务入口。
4. 增加 SDK 客户端和旧客户端各一套兼容回归。

### 阶段 2：Windows exe

1. 在 Windows 构建机执行 `build.ps1`。
2. 运行安装后首次启动、升级、恢复、退出和端口占用测试。
3. 发布 `BigFishCouncil.exe`、版本号、sha256 和回滚包。

### 阶段 3：移动只读 API

1. 实现配对与设备撤销。
2. 实现 `/mobile/v1/tasks`、`/mobile/v1/tasks/{id}`、评论和事件游标。
3. 使用 `sse-starlette` 实现 3100 SSE，并定义 `Last-Event-ID` 补偿。
4. 对所有移动接口做越权、分页、撤销和断线重连测试。

### 阶段 4：Android APK

1. Kotlin + Jetpack Compose，只做登录/配对、任务、详情、评论、事件流。
2. access token 存 Android Keystore，支持设备撤销。
3. 不在 APK 放审批、恢复、发票和执行器权限。
4. 通过真实 Windows exe 的 3099/3100 做联调，再发布签名 apk。

## 五、审计边界

本次依据 GitHub `main`、rc8 tag、Release 附件、源码和 CI 元数据完成静态审计。没有公司 Windows 11 真机、真实 DSH Connector、GUI 真窗口和 exe/apk 产物，因此这些项目不能标记为已验证。

