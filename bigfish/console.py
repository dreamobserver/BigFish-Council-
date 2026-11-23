"""控制台编码兜底.

Windows 上 `sys.stdout` 默认用 locale 编码：英文版是 cp1252，中文版是 cp936/GBK。
本项目所有脚本都会打印中文（检查项名称、错误说明、状态标签），
在 cp1252 控制台上 `print("健康检查")` 直接抛 `UnicodeEncodeError`。

这不是「显示不好看」的问题：自检脚本会在第一次输出时整进程 traceback 退出，
用户看到的是一行报错而不是检查结果。GitHub Actions 的 windows-latest
（PYTHONIOENCODING 未设、Python 3.13）就是活生生的复现。

处理原则：

- 当前编码**能**表示中文（cp936/UTF-8）→ 什么都不做，免得把中文终端搞成乱码；
- 当前编码**表示不了**中文（cp1252/ascii）→ 把 stdout/stderr 切到 UTF-8 + `errors="replace"`；
- 任何异常都吞掉：编码兜底绝不能成为新的崩溃点；
- `BFC_NO_STDIO_FIX=1` 可关闭，方便测试「未修复时的行为」。
"""
from __future__ import annotations

import os
import sys

__all__ = ["install_utf8_stdio", "stdio_report"]

# 用中文做探针，而不是只看编码名：cp1252 与 utf-8 谁都认识，
# 真正要问的是「这个编码能不能把我的输出编码出去」。
_CANARY = "健康检查"


def _needs_upgrade(stream: object) -> bool:
    enc = getattr(stream, "encoding", None)
    if not enc:
        return False
    try:
        _CANARY.encode(str(enc))
    except (UnicodeEncodeError, LookupError):
        return True
    except Exception:  # noqa: BLE001 - 编码名千奇百怪，一律当「不行」
        return True
    return False


def _report(stream: object) -> str:
    enc = getattr(stream, "encoding", None)
    return str(enc) if enc else ""


def install_utf8_stdio() -> dict:
    """把不能表示中文的 stdout/stderr 切到 UTF-8.

    返回 `{"stdout": 状态, "stderr": 状态}`，状态取值：

    - `"ok"`               本来就够用，或已成功切换
    - `"no-stream"`        该 stream 不存在（pythonw 会是 None）
    - `"not-text"`         不是可重配的文本流（StringIO / 重定向包装）
    - `"skipped-env"`      被 `BFC_NO_STDIO_FIX` 关掉
    - `"failed:<err>"`     重配失败，已吞掉异常
    """
    if os.environ.get("BFC_NO_STDIO_FIX"):
        return {"stdout": "skipped-env", "stderr": "skipped-env"}

    result = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            result[name] = "no-stream"
            continue
        if not _needs_upgrade(stream):
            result[name] = "ok"
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            result[name] = "not-text"
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
            result[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - 兜底本身不许抛
            result[name] = "failed:" + type(exc).__name__
    return result


def stdio_report() -> dict:
    """只读探测：当前两个流的编码与是否需要切。"""
    out = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        out[name] = {
            "encoding": _report(stream),
            "needs_upgrade": bool(stream is not None and _needs_upgrade(stream)),
        }
    return out
