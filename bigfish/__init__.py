"""BigFish Council — 大肥鱼议事厅.

Windows 本地控制中心 + Tailscale 私网 + DSH 执行器。
"""

__version__ = "0.1.1-rc9"
APP_NAME = "BigFish Council"
APP_NAME_CN = "大肥鱼议事厅"

# 控制台编码兜底（R8-02）：必须在任何 print 之前执行。
# Windows 英文版 / CI runner 的 stdout 是 cp1252，中文输出会抛 UnicodeEncodeError，
# 自检脚本会在第一行输出就 traceback 退出。详见 bigfish/console.py 的模块注释。
from .console import install_utf8_stdio as _install_utf8_stdio  # noqa: E402

STDIO_STATUS = _install_utf8_stdio()
