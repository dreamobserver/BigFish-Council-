# 回滚说明

所有改动都是**新增**，不修改任何既有系统。回滚按下面顺序做，每一步都可以单独停。

## 1. 停掉本地服务（Windows）

```powershell
# 结束 GUI / headless
Get-Process pythonw,python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*BigFishCouncil*" } | Stop-Process -Force
# 确认 3096 已释放
Test-NetConnection 127.0.0.1 -Port 3096
```

## 2. 停掉 VPS 跳板

```bash
pkill -f 'bfc-proxy.mjs'
ss -ltnp 2>/dev/null | grep 3098 || echo '3098 已释放'
```

## 3. 移除 DSH 的 MCP 连接

```
mcp_connector_status                      # 找到 bigfish-council 的 key
mcp_connector_disconnect({ key: "<key>" })
```

这只会删掉这一条连接，其余（bilibili / stickers / nekowhale / windows / home-pc）不受影响。

## 4. 删除数据

```powershell
# 数据目录（含 SQLite）
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\BigFishCouncil"
# 源码
Remove-Item -Recurse -Force "D:\BigFishCouncil"
```

**删之前先备份**：GUI → 设置 → 备份导出，或 `python -m bigfish.cli export D:\bfc-backup.zip`。

## 5. 完全无残留检查

```bash
# VPS
ps aux | grep -E 'bfc-proxy|bigfish' | grep -v grep   # 应为空
```

```powershell
# Windows
Get-NetTCPConnection -LocalPort 3096,3098 -ErrorAction SilentlyContinue
```

没有监听、没有进程、没有新增的系统服务 —— 回滚完成。

## 不影响的东西（回滚时不要动）

- DSH 容器本身（未重启、未重建、未升级）
- 现有 tailscaled（未安装新组件）
- 现有的 wmcp-proxy / windows-mcp 通道
- 模型供应商配置
