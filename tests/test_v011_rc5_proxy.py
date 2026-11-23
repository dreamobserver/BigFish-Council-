"""v0.1.1-rc5 回归：bfc-proxy 跳板的监听地址闸门（审计项 R7，发布阻断）。

为什么单独测：这个 Node 跳板把 VPS 回环端口转发到 Windows 的 MCP 端口，
自身不做任何鉴权，安全性完全依赖「只绑回环 + 上层 Tailscale/SSH 隧道」。
原先 LISTEN_HOST 直接取环境变量，只要设成 0.0.0.0 就能把未鉴权的 MCP 挂到公网，
所以闸门必须在 listen 之前生效，且不合法时以非 0 退出码结束 —— 这里就从
子进程外部观察这两点，不依赖脚本内部实现细节。

断言只覆盖「监听地址闸门」：不连它转发的目标端口，也不验证代理转发语义。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "bfc-proxy.mjs"
NODE = shutil.which("node") or shutil.which("node.exe")

# 启动闸门判定的超时。
# 正常拒绝是毫秒级的，但「进程还没起来」与「闸门失效」在外部看起来一样，
# 所以这个值买到的是**判定窗口**，不是性能指标：
#   - 太小 → Windows 上 node 冷启动 + Defender 扫描就要 3 秒以上，
#     GitHub windows-latest 实测 TimeoutExpired（rc7 CI 就是这么红的）；
#   - 太大 → 闸门真失效时白等。
# 取 25 秒：真被放行时进程会一直活着，25 秒足以区分；正常拒绝仍在 1 秒内返回。
STARTUP_TIMEOUT = 25.0


def _free_port() -> int:
    """挑一个当前空闲的高位端口，避免与本机 3098 之类的真实监听撞车。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn(host: str) -> subprocess.Popen[str]:
    """用干净的环境起跳板：清掉其他 BFC_PROXY_* 变量，只放大监听地址。

    目标端口指向一个必然连不上的本地端口，确保「进程活着」只可能来自
    监听成功，而不是碰巧连上了上游。
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("BFC_PROXY_")}
    env["BFC_PROXY_LISTEN_HOST"] = host
    env["BFC_PROXY_PORT"] = str(_free_port())
    env["BFC_PROXY_TARGET_HOST"] = "127.0.0.1"
    env["BFC_PROXY_TARGET_PORT"] = "1"
    return subprocess.Popen(
        [NODE, str(SCRIPT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _reap(proc: subprocess.Popen[str]) -> None:
    """无论断言成败都要收尸，避免端口与子进程泄漏到下一个用例。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()


@unittest.skipUnless(NODE, "未找到 node，跳过 bfc-proxy 监听地址闸门测试")
class ProxyListenGateTest(unittest.TestCase):
    """非回环地址必须 3 秒内以非 0 退出；回环地址必须能起来。"""

    def _expect_reject(self, host: str, label: str) -> None:
        proc = _spawn(host)
        try:
            try:
                out, err = proc.communicate(timeout=STARTUP_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                # 还没退出就等于闸门没拦住，进程可能已经在 listen 上了。
                self.fail(
                    "%s（%r）没有在 %.0f 秒内退出，闸门可能是被跳过了；"
                    "stdout=%r stderr=%r" % (label, host, STARTUP_TIMEOUT,
                                             exc.output, exc.stderr))
            combined = (err or "") + (out or "")
            self.assertNotEqual(proc.returncode, 0,
                                "%s（%r）以 0 退出，说明闸门被通用错误处理吞掉了；输出=%r"
                                % (label, host, combined))
            self.assertTrue(
                "拒绝启动" in combined or "只允许监听回环地址" in combined,
                "%s（%r）的 stderr 没有拒绝字样，实际=%r" % (label, host, combined))
            # 回显实际拿到的值，运维才看得出是哪条配置写错了。
            self.assertIn(host, combined,
                          "%s（%r）的错误信息没有回显实际值，实际=%r" % (label, host, combined))
            self.assertNotIn("listening on", combined,
                             "%s（%r）在被拒绝后仍然进入了 listen" % (label, host))
        finally:
            _reap(proc)

    def test_any_address_is_rejected(self) -> None:
        """0.0.0.0 是最危险的写法：等于把未鉴权的 MCP 直接暴露到公网。"""
        self._expect_reject("0.0.0.0", "通配地址")

    def test_ipv6_any_is_rejected(self) -> None:
        """'::' 是 IPv6 的通配地址，语义上等同于 0.0.0.0。"""
        self._expect_reject("::", "IPv6 通配地址")

    def test_ipv6_any_bracketed_is_rejected(self) -> None:
        """带方括号的 [::] 只剥括号、不做特判，同样必须被拒。"""
        self._expect_reject("[::]", "IPv6 通配地址（方括号写法）")

    def test_empty_value_is_rejected(self) -> None:
        """显式设成空串是配置错误，退回默认值等于悄悄放宽，必须拒绝。"""
        self._expect_reject("", "空字符串")

    def test_lan_address_is_rejected(self) -> None:
        """RFC1918 也不是回环：绑上去局域网内任何人都能直连。"""
        self._expect_reject("192.168.1.5", "局域网地址")

    def test_public_address_is_rejected(self) -> None:
        """公网地址更不必说，绑上就是全互联网可达。"""
        self._expect_reject("8.8.8.8", "公网地址")

    def _expect_alive(self, host: str, label: str) -> None:
        proc = _spawn(host)
        try:
            try:
                proc.wait(timeout=STARTUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.assertIsNone(proc.poll(),
                                  "%s（%r）刚超时就退出了，属于竞态" % (label, host))
                return
            out, err = proc.communicate()
            self.fail("%s（%r）被拒绝了，但它应当被放行：exit=%r stderr=%r"
                      % (label, host, proc.returncode, (err or "") + (out or "")))
        finally:
            _reap(proc)

    def test_default_loopback_is_allowed(self) -> None:
        """127.0.0.1 是默认值，必须能正常进入 listen（活着就说明起来了）。"""
        self._expect_alive("127.0.0.1", "回环地址")

    def test_loopback_range_and_hostname_are_allowed(self) -> None:
        """127.0.0.0/8 整段与 localhost 都属于回环，不能误杀。"""
        self._expect_alive("127.0.1.2", "回环段地址")
        self._expect_alive("localhost", "主机名 localhost")

    def test_bracketed_ipv6_is_not_rejected_by_gate(self) -> None:
        """'[::1]' 必须过闸门。

        这里不断言它一定绑成功：Node 的 listen 只接受裸地址，脚本已做去括号归一，
        但某些环境本身没有 IPv6 回环。所以判定标准是「要么起来了，要么死在 listen
        而不是死在闸门」—— 只要不出现拒绝字样，就说明闸门放行了。
        """
        proc = _spawn("[::1]")
        try:
            try:
                out, err = proc.communicate(timeout=STARTUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                return  # 活着 = 已进入 listen，正是期望结果
            combined = (err or "") + (out or "")
            self.assertNotIn("拒绝启动", combined,
                             "'[::1]' 是回环地址，却被闸门拒绝了；输出=%r" % combined)
            self.assertIn("listen error", combined,
                          "'[::1]' 既没被拒绝也没进入 listen，状态不明；输出=%r" % combined)
        finally:
            _reap(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
