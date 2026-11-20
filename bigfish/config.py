"""运行时配置：端口与网络拓扑的唯一真源.

端口职责（全部可在 config.json / 环境变量覆盖）：
- 3096  Windows 本地 API 监听端口（默认只绑 127.0.0.1）
- 3098  VPS 侧 bfc-proxy 回环跳板端口（不是 Windows 端口）
- 3097  测试专用端口（单测/冒烟使用，不写进任何默认值）
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_DATA_DIR = "BIGFISH_DATA_DIR"
ENV_CONFIG = "BIGFISH_CONFIG"
ENV_API_HOST = "BIGFISH_API_HOST"
ENV_API_PORT = "BIGFISH_API_PORT"

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 3096
DEFAULT_PROXY_PORT = 3098
TEST_API_PORT = 3097

# 端口职责表（文档与自检共用同一份数据）
PORT_ROLES = {
    "windows_api": (DEFAULT_API_PORT, "Windows 本地控制中心 API 监听端口（默认仅回环）"),
    "vps_proxy": (DEFAULT_PROXY_PORT, "VPS 上 bfc-proxy.mjs 回环跳板端口"),
    "test_only": (TEST_API_PORT, "单测与 GUI 冒烟使用的临时端口，不参与部署"),
}

# 预留端口表（**平行字典**，不并入 PORT_ROLES）。
# 为什么不合并：tests/test_v011_p3.py 断言 PORT_ROLES 恰好是当前三行职责，
# 把未实现的端口塞进去等于对测试撒谎。预留即「先占号，不实现」。
# 见 docs/ARCHITECTURE-FUTURE.md 第 2 节。
PORT_RESERVED = {
    3099: ("mobile_rest", "手机端 REST 网关（计划中，未实现）"),
    3100: ("mobile_sse", "手机端 SSE/长连接推送（计划中，未实现）"),
    3101: ("mcp_bridge", "对外 MCP 桥接（计划中，未实现）"),
    8765: ("windows_mcp", "Windows 侧 windows-mcp 的 HTTP 端口（外部进程，非本项目）"),
    18765: ("wmcp_proxy_a", "VPS 跳板：客户端 A 的 windows-mcp 回环转发"),
    18766: ("wmcp_proxy_b", "VPS 跳板：客户端 B 的 windows-mcp 回环转发"),
}

# 端口铁律（写在这里，代码与文档引同一份）：
# 1) 本项目自己监听的端口只能绑回环；跨机一律走 Tailscale Serve。
# 2) 预留端口在真正实现前不得出现在 PORT_ROLES 或任何默认值里。
# 3) 8765 / 18765 / 18766 属于外部进程，本项目只连不听。
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str | None) -> bool:
    """回环判定：字符串白名单 + 真实 IP 解析（127.0.0.0/8、::1）。

    不用「以 127. 开头」这种字符串前缀判断，避免 ::1 之类被漏掉。
    """
    h = (host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        return False
    if h.lower() in LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def default_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BigFishCouncil"
    return Path.home() / ".bigfish-council"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    heartbeat_ttl_sec: int = 120
    ticket_ttl_sec: int = 900
    ticket_max_ttl_sec: int = 21600   # 单次执行租约的绝对上限（6 小时）
    heartbeat_lease_sec: int = 180    # 每次合法心跳把租约续到 now+该值
    watchdog_interval_sec: int = 10
    export_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "council.db"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def key_path(self) -> Path:
        """执行票派生密钥；与数据库分开存放，不写入导出包。"""
        return self.data_dir / "secrets" / "ticket.key"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def is_loopback(self) -> bool:
        return is_loopback_host(self.api_host)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.key_path.parent, 0o700)
        except OSError:
            pass

    def public_dict(self) -> dict:
        """可安全打印/写日志的配置视图（不含任何凭据）。"""
        return {
            "data_dir": str(self.data_dir),
            "api_host": self.api_host,
            "api_port": self.api_port,
            "heartbeat_ttl_sec": self.heartbeat_ttl_sec,
            "ticket_ttl_sec": self.ticket_ttl_sec,
            "ticket_max_ttl_sec": self.ticket_max_ttl_sec,
            "heartbeat_lease_sec": self.heartbeat_lease_sec,
            "watchdog_interval_sec": self.watchdog_interval_sec,
        }

    def save(self) -> None:
        self.ensure_dirs()
        payload = dict(self.public_dict())
        payload["export_dir"] = str(self.export_dir) if self.export_dir else None
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Settings":
        s = cls()
        if data_dir is not None:
            s.data_dir = Path(data_dir).expanduser().resolve()
        cfg_path = Path(os.environ.get(ENV_CONFIG) or (s.data_dir / "config.json"))
        if cfg_path.exists():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            # 显式传入的 data_dir 优先级最高
            if data_dir is None and raw.get("data_dir"):
                s.data_dir = Path(raw["data_dir"]).expanduser().resolve()
            s.api_host = raw.get("api_host", s.api_host)
            s.api_port = int(raw.get("api_port", s.api_port))
            s.heartbeat_ttl_sec = int(raw.get("heartbeat_ttl_sec", s.heartbeat_ttl_sec))
            s.ticket_ttl_sec = int(raw.get("ticket_ttl_sec", s.ticket_ttl_sec))
            s.ticket_max_ttl_sec = int(raw.get("ticket_max_ttl_sec", s.ticket_max_ttl_sec))
            s.heartbeat_lease_sec = int(raw.get("heartbeat_lease_sec", s.heartbeat_lease_sec))
            s.watchdog_interval_sec = int(raw.get("watchdog_interval_sec", s.watchdog_interval_sec))
            if raw.get("export_dir"):
                s.export_dir = Path(raw["export_dir"]).expanduser().resolve()
        # 环境变量优先级最高（联调/测试用）
        if os.environ.get(ENV_API_HOST):
            s.api_host = os.environ[ENV_API_HOST].strip()
        if os.environ.get(ENV_API_PORT):
            try:
                s.api_port = int(os.environ[ENV_API_PORT])
            except ValueError:
                pass
        s.ensure_dirs()
        return s
