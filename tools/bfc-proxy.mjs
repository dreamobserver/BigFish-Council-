#!/usr/bin/env node
// BigFish Council 回环跳板：127.0.0.1:3098 -> Windows Tailnet CLIENT-A-TAILNET-IP:3096
//
// 为什么需要它：dsh-mcp-connector 只允许回环或 RFC1918 的 URL，而 Tailnet 是
// 100.64/10（CGNAT），既不是回环也不是私网段，所以用本地回环端口做跳板。
// 与既有的 wmcp-proxy-home.mjs 完全同构，已在本机验证可用。
//
// 安全边界（审计 R7，发布阻断项）：
// 这个跳板自身不做任何鉴权，/mcp* 与 /api/ 是原样透传到 Windows 侧 MCP 端口的，
// 所以它整套安全性完全建立在「只绑回环 + 上层 Tailscale / SSH 隧道」之上。
// 一旦监听地址被环境变量带去 0.0.0.0，等于把未鉴权的 MCP 直接挂到公网。
// 因此监听地址必须是硬闸门：与 bigfish/server.py 的 check_bind_host 同一口径，
// 判定放在 listen 之前，不合法立即 exit(1)，不允许被通用错误处理吞成正常退出。
//
// 用法： node bfc-proxy.mjs
// 环境变量：BFC_PROXY_PORT / BFC_PROXY_TARGET_HOST / BFC_PROXY_TARGET_PORT
import fs from 'node:fs';
import http from 'node:http';

/**
 * 去掉 IPv6 的方括号写法：'[::1]' 与 '::1' 是同一个地址。
 *
 * 为什么必须归一：Node 的 listen 不接受带方括号的形式（会 ENOTFOUND），
 * 所以「接受 [::1]」不能只在校验时放行，绑定前也必须换成裸地址。
 */
function stripBrackets(host) {
  let h = String(host == null ? '' : host).trim();
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
  return h;
}

/**
 * 回环判定：只认 127.0.0.1 / localhost / ::1，以及 127.0.0.0/8 的 IPv4 字面量。
 *
 * 为什么不用「以 127. 开头」这种字符串前缀判断：会放过 127.0.0.1.evil.com 之类
 * 的主机名，也会漏掉 [::1] 的带方括号写法；这里逐段解析后只比较数值，
 * 空串、'0.0.0.0'、'::'、'[::]' 以及任何非回环地址一律拒绝。
 */
function isLoopback(host) {
  if (typeof host !== 'string') return false;
  let h = stripBrackets(host);
  if (!h) return false;
  const lower = h.toLowerCase();
  if (lower === '127.0.0.1' || lower === 'localhost' || lower === '::1') return true;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (!m) return false;
  const octets = m.slice(1).map(Number);
  if (octets.some((n) => n > 255)) return false;
  return octets[0] === 127;   // 127.0.0.0/8 整段都是回环
}

// 未设置才用默认值；显式设成空串属于配置错误，按不合法处理（下面会拒绝）。
// 绑定用去括号后的形式，报错回显用原始值，方便对上写错的那条配置。
const LISTEN_HOST_RAW = process.env.BFC_PROXY_LISTEN_HOST;
const LISTEN_HOST = LISTEN_HOST_RAW === undefined ? '127.0.0.1' : stripBrackets(LISTEN_HOST_RAW);
const LISTEN_PORT = Number(process.env.BFC_PROXY_PORT || 3098);
const TARGET_HOST = process.env.BFC_PROXY_TARGET_HOST || 'CLIENT-A-TAILNET-IP';
const TARGET_PORT = Number(process.env.BFC_PROXY_TARGET_PORT || 3096);
const TARGET_HOSTPORT = TARGET_HOST + ':' + TARGET_PORT;

const log = (...a) => console.log(new Date().toISOString(), ...a);

// 启动闸门：放行前先判定，不合法直接退 1，绝不进入 listen。
// 用 fs.writeSync(2) 而不是 console.error：管道下 console 是异步写，
// process.exit 可能把最后一行错误吞掉，测试与运维都会看到空 stderr。
if (!isLoopback(LISTEN_HOST)) {
  const msg = [
    '拒绝启动：bfc-proxy 跳板只允许监听回环地址（127.0.0.1 / localhost / ::1 / 127.0.0.0/8）。',
    '实际拿到的 BFC_PROXY_LISTEN_HOST = ' + JSON.stringify(LISTEN_HOST_RAW),
    '原因：本代理不做鉴权，安全性完全依赖「只绑回环 + 上层 Tailscale/SSH 隧道」；',
    '如需跨机访问请走隧道，不要把它绑到 0.0.0.0。',
  ].join('\n');
  try { fs.writeSync(2, msg + '\n'); } catch { /* stderr 不可用时仍要退出 */ }
  process.exit(1);
}
const agent = new http.Agent({ keepAlive: true, maxSockets: 32 });

const server = http.createServer((req, res) => {
  // 只放行 MCP 与健康检查路径，其余一律 404，减少暴露面
  if (!(req.url === '/health' || req.url.startsWith('/mcp') || req.url.startsWith('/api/'))) {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
    return;
  }
  const headers = Object.assign({}, req.headers, { host: TARGET_HOSTPORT });
  const up = http.request(
    { host: TARGET_HOST, port: TARGET_PORT, method: req.method, path: req.url, headers, agent },
    (ur) => { res.writeHead(ur.statusCode || 502, ur.headers); ur.pipe(res); }
  );
  up.on('error', (e) => {
    log('upstream error:', e.code || e.message);
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
    res.end('upstream error: ' + (e.code || e.message));
  });
  req.on('error', (e) => log('client error:', e.code || e.message));
  req.pipe(up);
});

server.on('error', (e) => { log('listen error:', e.message); process.exit(1); });
server.listen(LISTEN_PORT, LISTEN_HOST, () =>
  log('bfc-proxy listening on ' + LISTEN_HOST + ':' + LISTEN_PORT + ' -> ' + TARGET_HOSTPORT));

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => { server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 2000); });
}
