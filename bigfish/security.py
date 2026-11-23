"""令牌、密钥、脱敏与文件权限工具.

设计：
- 执行器 Token 只存 sha256 哈希；明文只在注册时返回一次。
- 执行票 Token 由「本地密钥文件 + 执行器 + 任务 + 版本 + 幂等键」HMAC 派生，
  数据库里只有哈希，没有明文；同一幂等键重放能稳定复算出同一个票（可安全重试）。
- 密钥文件独立于数据库、且不进导出包：只拿到数据库泄露无法反推执行票。
- 恢复备份会轮换本密钥（rotate_key），恢复前签发的票因此无法再被派生。

v0.1.1-rc3：
- scrub()/sanitize() 统一脱敏入口，覆盖 bfc_* 令牌、Bearer、常见 API Key 与私钥；
- secure_file() 在 POSIX 上 chmod 0600，在 Windows 上用 icacls 收紧 NTFS ACL，
  失败时返回明确告警（不再静默 pass）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TOKEN_PREFIX = "bfc_"
KEY_BYTES = 32

# 所有对外发布的令牌前缀（执行器 / 审查 / 管理员 / 执行票）
SECRET_PREFIXES = ("bfc_tk_", "bfc_ex_", "bfc_rv_", "bfc_ad_")

# 每条是 (正则, 替换文本, 种类名)。种类名会写进审计包的 credential_scan，
# 让读包的人知道「我们到底查了哪些形态」，而不是一句「保证没有凭据」。
_PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"bfc_(?:tk|ex|rv|ad)_[A-Za-z0-9_\-]{8,}"), "bfc_…[REDACTED]", "bfc-token"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I), "Bearer [REDACTED]", "bearer"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-[REDACTED]", "openai-key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh-[REDACTED]", "github-token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED PRIVATE KEY]", "private-key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]", "aws-key"),
    # rc6 F-07：审计点名补上的几类。不追求穷尽，只求「漏了也不会被说成保证」。
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED GOOGLE KEY]", "google-key"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "[REDACTED SLACK TOKEN]", "slack-token"),
    (re.compile(r"\b[sr]k_live_[A-Za-z0-9]{10,}"), "[REDACTED STRIPE KEY]", "stripe-key"),
    (re.compile(r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*"
                r"['\"]?[^\s'\"]{6,}", re.I), "credential=[REDACTED]", "key-value"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s:/@]{3,}@", re.I),
     "://[REDACTED]@", "uri-userinfo"),
)

SCRUB_KINDS = tuple(k for _, _, k in _PATTERNS)


def new_token(prefix: str = "") -> str:
    return (prefix or TOKEN_PREFIX) + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def mask(token: str) -> str:
    if not token:
        return ""
    return token[:8] + "…" + token[-4:]


# ---------------------------------------------------------------- 脱敏
def scrub(text: str) -> str:
    """把字符串里的凭据替换成占位符；非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat, repl, _kind in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scan_secrets(text: str) -> list[str]:
    """返回这段文本命中的凭据**种类**（去重、排序），用于如实报告扫过什么。"""
    if not isinstance(text, str) or not text:
        return []
    hits = {kind for pat, _repl, kind in _PATTERNS if pat.search(text)}
    hits.update(k for k in SECRET_PREFIXES if k in text)
    return sorted(hits)


def sanitize(obj: Any) -> Any:
    """递归脱敏（dict/list/str），用于任何要落库的结构化内容。"""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def contains_secret(text: str) -> bool:
    """是否含有任何形态的凭据：完整令牌、被截断的令牌前缀、Bearer、私钥块。"""
    if not isinstance(text, str) or not text:
        return False
    if any(p in text for p in SECRET_PREFIXES):
        return True          # 连被截断的 bfc_tk_abc 也算，宁可拒写
    return any(pat.search(text) for pat, _repl, _kind in _PATTERNS)


# ---------------------------------------------------------------- 本地密钥
def load_or_create_key(path: Path) -> bytes:
    """读取或创建本地派生密钥（POSIX 0600 / Windows 收紧 ACL）。"""
    path = Path(path)
    if path.exists():
        data = path.read_bytes()
        if len(data) >= KEY_BYTES:
            secure_file(path)
            return data
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    write_key_atomic(path, key)
    return key


def write_key_atomic(path: Path, key: bytes) -> Path:
    """把密钥原子写到 path（先写 tmp 再 replace，尽量缩短权限窗口）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    secure_file(tmp)
    os.replace(tmp, path)
    secure_file(path)
    return path


def rotate_key(path: Path) -> bytes:
    """恢复备份时轮换派生密钥：恢复前签发的执行票随之全部失效。"""
    return write_key_atomic(Path(path), secrets.token_bytes(KEY_BYTES)) and \
        Path(path).read_bytes()


def secure_file(path: Path) -> dict:
    """把文件权限收紧到「仅当前用户可读写」。

    POSIX：chmod 0600。
    Windows：chmod 只改只读位，必须用 icacls 去掉继承并只授权当前用户。
    返回 {"ok": bool, "mode": str, "detail": str}，失败时不吞掉原因。
    """
    path = Path(path)
    info = {"ok": True, "mode": "", "detail": ""}
    try:
        os.chmod(path, 0o600)
        info["mode"] = oct(path.stat().st_mode & 0o777)
    except OSError as exc:                       # pragma: no cover - 权限极特殊场景
        info.update(ok=False, detail=f"chmod 失败: {exc}")
    if sys.platform != "win32":
        return info
    # Windows：NTFS ACL 才是真正的访问控制
    user = os.environ.get("USERNAME") or ""
    try:                                          # pragma: no cover - 仅在 Windows 走
        if user:
            run_capture(["icacls", str(path), "/inheritance:r"])
            rc, stdout, stderr = run_capture(
                ["icacls", str(path), "/grant:r", f"{user}:F"])
            info["acl"] = "granted" if rc == 0 else "failed"
            if rc != 0:
                info.update(ok=False, detail=(stderr or stdout or "").strip()[:300])
        else:                                     # pragma: no cover
            info.update(ok=False, detail="USERNAME 环境变量缺失，无法收紧 ACL")
    except (OSError, subprocess.SubprocessError) as exc:   # pragma: no cover
        info.update(ok=False, detail=f"icacls 调用失败: {exc}")
    return info


# icacls / whoami 输出的是**控制台代码页**（中文 Windows 上是 CP936），
# 而 Python 的 text=True 按 locale.getpreferredencoding() 解码。开了 PYTHONUTF8=1
# 或 PYTHONIOENCODING=utf-8 时 locale 报 utf-8，GBK 字节就解码失败 —— 异常发生在
# subprocess 的 reader 线程里，主线程只看到 returncode=0 + stdout=""。
# 这就是 rc8 全量测试里「icacls 无输出（rc=0）」的真身：不是 ACL 坏了，是解码炸了。
# 所以这里一律先拿 bytes，再按 UTF-8 → GBK → UTF-16LE → replace 的顺序兜底。
_WIN_TEXT_FALLBACKS = ("utf-8", "gbk", "cp936", "mbcs", "utf-16-le")


def _decode_console(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in _WIN_TEXT_FALLBACKS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run_capture(argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """跑一个外部命令并按控制台代码页兜底解码，永远不因编码抛异常。"""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"调用失败: {exc}"
    return (r.returncode if r.returncode is not None else 1,
            _decode_console(r.stdout or b""), _decode_console(r.stderr or b""))


# ---------------------------------------------------------------- Windows 身份
def _current_user_candidates() -> list[str]:
    """返回「当前用户」在 icacls 输出里可能出现的几种写法（小写）。

    rc8 教训：只认 `USERNAME` 一个环境变量太脆。Windows CI / 全量跑时见过
    icacls 输出里账户显示成 SID 或 `DOMAIN\\user` 形式，环境变量却是短的
    `USERNAME`，于是一条本来正常的 ACL 被判成「未包含当前用户」。这里把
    环境变量、getpass、`whoami` 三种来源合起来，只用于**匹配**，不用于授权。
    """
    out: list[str] = []

    def add(v: str | None) -> None:
        v = (v or "").strip()
        if v and v.lower() not in out:
            out.append(v.lower())

    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    add(user)
    if user and domain:
        add(f"{domain}\\{user}")
    try:
        import getpass
        add(getpass.getuser())
    except Exception:                                   # pragma: no cover
        pass
    rc, stdout, _err = run_capture(["whoami"])          # pragma: no cover - Windows
    text = (stdout or "").strip().splitlines()
    if rc == 0 and text:
        add(text[0])
    return out


def _current_user_sids() -> list[str]:                  # pragma: no cover - Windows
    """当前用户的 SID（形如 S-1-5-21-...）。账户名解析不出来时 icacls 会打 SID。"""
    sids: list[str] = []
    rc, stdout, _err = run_capture(["whoami", "/user", "/fo", "csv", "/nh"])
    if rc == 0:
        m = re.search(r"(S-1-5-21-[0-9\-]+|S-1-5-[0-9\-]+)", stdout or "")
        if m:
            sids.append(m.group(1).lower())
    return sids


def _icacls_dump(path: Path) -> tuple[int, str, str]:   # pragma: no cover - Windows
    return run_capture(["icacls", str(path)])


# 宽泛主体：出现任意一条就说明 ACL 没真正收紧。
# 用正则而不是裸子串，是因为 icacls 里同一个主体可能写成 `BUILTIN\\Users`、
# 也可能只写 `Users`（域前缀被省略）；而路径里 `C:\\Users\\...` 后面跟的是
# 反斜杠不是冒号，用 `users:` 就能把两者区分开。
_BROAD_ACL_PATTERNS = (
    (re.compile(r"\beveryone\b"), "Everyone"),
    (re.compile(r"\bauthenticated users\b"), "Authenticated Users"),
    (re.compile(r"\binteractive\b"), "INTERACTIVE"),
    (re.compile(r"\busers:"), "Users"),
)


def acl_broad_subject(low_output: str) -> str | None:
    """在 icacls 输出（已 lower）里找宽泛主体，命中返回该主体名。"""
    low = (low_output or "").lower()
    for pat, label in _BROAD_ACL_PATTERNS:
        if pat.search(low):
            return label
    return None


def acl_mentions_user(low_output: str, names: list[str],
                      sids: list[str] | None = None) -> bool:
    """ACL 是否确实授权给了当前用户（名字任一写法命中，或命中 SID）。

    抽成纯函数是为了能跨平台单测：Windows 的 icacls 输出是「文件路径 ACL」
    加若干「DOMAIN\\account:(权限)」行，这里只做子串匹配。
    """
    low = (low_output or "").lower()
    if any(n and n.lower() in low for n in names):
        return True
    return any(s and s.lower() in low for s in (sids or []))


def verify_file_restricted(path: Path) -> tuple[bool, str]:
    """只读校验：确认密钥文件确实只对当前用户开放。"""
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    if sys.platform != "win32":
        mode = path.stat().st_mode & 0o777
        return mode == 0o600, f"mode={oct(mode)}"
    # rc8：icacls 偶发失败（杀软扫描刚写的文件、句柄争用）会输出空串，
    # 重试两次再判死，失败时把 returncode / stderr 一起带出来，便于定位。
    rc, out, err = 0, "", ""
    for attempt in range(3):                            # pragma: no cover - Windows
        try:
            rc, out, err = _icacls_dump(path)
        except (OSError, subprocess.SubprocessError) as exc:   # pragma: no cover
            return False, f"icacls 调用失败: {exc}"
        if rc == 0 and out.strip():
            break
        time.sleep(0.2 * (attempt + 1))
    low = out.lower()
    if not low.strip():
        return False, (f"icacls 无输出（rc={rc}，stderr={(err or '').strip()[:200]}）")
    broad = acl_broad_subject(low)
    if broad:
        return False, f"ACL 仍对宽泛主体开放（{broad}）"
    # 必须确实授权给了当前用户，否则说明 ACL 写坏了（连本人都读不到）。
    # 认三种写法：USERNAME / DOMAIN\\USER / whoami 输出的账户名，外加固化的 SID。
    names = _current_user_candidates()
    sids = _current_user_sids()
    if acl_mentions_user(low, names, sids):
        return True, ("acl limited" if any(n in low for n in names)
                      else "acl limited (sid)")
    return False, (f"ACL 未包含当前用户（候选={names or ['未知用户']}，"
                   f"rc={rc}，stderr={(err or '').strip()[:200]}）")
def derive_ticket(key: bytes, executor_id: str, task_id: str, version: int,
                  idem_key: str | None) -> str:
    """确定性执行票：同一 (执行器, 任务, 版本, 幂等键) 必然得到同一张票。"""
    material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key or ''}".encode("utf-8")
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()
    return "bfc_tk_" + digest[:43]


def ticket_id_for(token: str) -> str:
    """执行票的稳定行标识（由票哈希派生，重放不会产生重复行）。"""
    return "tk-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def request_fingerprint(payload: dict) -> str:
    """请求体指纹：用于识别「同一幂等键不同请求体」。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
