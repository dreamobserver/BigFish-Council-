# 安全说明与威胁模型

## 信任边界

| 组件 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| Windows GUI 进程 | 建任务、改 Prompt、投审查、**人类审批**、验收 | 无（它是唯一的授权源头） |
| 本地 HTTP 3096 | 列可领取任务、领取、心跳、回传、查状态 | 审批、改 Prompt、管理执行器、读历史审批 |
| MCP `/mcp` | 同上五个工具 | 同上，且**不注册**任何 approve/admin 工具 |
| DSH 执行器（云端） | 拿到已批准任务的 Prompt、回传结果 | 看到未批准任务、看到 Prompt 列表、把任务置为完成 |

## 关键设计取舍

1. **审批只在进程内**。`Council.approve()` 没有任何 HTTP 路由能调到。
2. **列表不暴露 Prompt**。`/api/v1/tasks/available` 与 `council_list_tasks` 只返回
   id/title/risk/status/version，Prompt 正文必须领取后才返回。
3. **Token 只存哈希**。`executors` 表存 SHA-256，明文只在 `executor-add` 时打印一次。
4. **回传不等于完成**。执行器 `completed` → `SUBMITTED`，必须人类点「验收通过」才 `DONE`。
5. **单票不能过**。必须 GPT 与 Gemini **对同一版本**都投 approve；改 Prompt 生成新版本后旧票作废。
6. **心跳超时自动阻断**。默认 120 秒没心跳 → `BLOCKED`，防止死任务永久占位。
7. **回环跳板只放行固定路径**。`tools/bfc-proxy.mjs` 对 `/health`、`/mcp*`、`/api/*` 之外的路径一律 404。

## 已知风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 执行器 Token 泄露 | 可领取任务、伪造回传 | 只在内存/环境变量中使用；泄露后在 `executors` 里吊销并重发 |
| Tailnet 内有其他设备 | 可访问 3096 | 只监听 127.0.0.1；若需跨机访问改用 Tailscale Serve 并限定 ACL |
| SQLite 单文件 | 数据库损坏会丢历史 | `export` 定期导出 zip 备份 |
| 云端拿到 Prompt | Prompt 里若含密钥会外泄 | 提交审批前人工检查 Prompt，禁止把密钥写进任务 |
| MCP 客户端无 session | 中间人重放 | 服务端不信任 session，每个请求都校验 Bearer |

## 明确不做的事

- 不开公网端口，不用 Tailscale Funnel。
- 不改动现有 DSH 容器、不升级 DSH 内核、不动生产模型供应商。
- 不读 Codex OAuth 凭据。
- 默认禁用 Codex / Astra 升级通道（GUI 上的「申请 Codex 升级」按钮**只写事件**，不调用任何模型）。
