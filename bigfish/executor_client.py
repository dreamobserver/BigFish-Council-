"""DSH 执行器侧的轻量客户端（云端可直接用，也可给 DSH MCP 工具复用）.

只用标准库。Token 从环境变量读取，绝不写进命令行或日志。
  BFC_BASE_URL=http://<windows-tailnet-ip>:3096
  BFC_TOKEN=bfc_ex_xxx
  BFC_RECEIPTS=<可选：本地收据 JSONL 路径，默认不落盘>

v0.1.1-rc3（P1-5.3）：
- 每个**逻辑请求**生成一个幂等键，网络重试复用同一个键；
- 服务端返回历史成功（replayed）视为成功，不报错、不重复执行；
- 网络错误不确定是否送达时，用只读接口查询最终状态，而不是盲目重发。

v0.1.1-rc5（R1 决定二）：
- 执行端按稳定 dispatch_id 本地落「开工/完成收据」（ReceiptLog）；
- 收据**每次写完立刻 fsync**，断电前已落盘——数据库从旧备份恢复后，
  服务端不知道某次派工到底有没有真正开工，只能靠执行端本地收据对账；
- 派工重新派发时 dispatch_id 会变，旧收据因此不会被新收据覆盖；
- 旧服务端没有 dispatch_id 字段时回退用 ticket_id，属于降级而非报错。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_TIMEOUT = 15


def new_idem_key(prefix: str = "idem") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class _UncertainNetworkError(RuntimeError):
    """请求可能已到达服务端（写操作超时/连接中断），结果未知。"""


class ReceiptLog:
    """执行端本地「收据」日志：追加式 JSONL，一行一个事件。

    为什么单独落盘：真正能证明「这次派工开工了没有」的证据在执行端，不在服务端。
    服务端数据库一旦从旧备份恢复，就丢失了「是否已派发/是否已开工」的事实，
    这时候只能拿执行端本地收据去对账；因此每条收据写完必须 flush + fsync，
    保证断电之前它已经在磁盘上（这是整个类存在的意义，不要为了性能省掉）。

    path=None 时是「纯内存 no-op」：不创建任何文件，收据只留在进程内存里，
    进程结束即消失（给「不要收据」的场景用一条明确、不会偷偷落盘的路径）。
    """

    #: 允许的事件名。closed 类事件表示执行端已经明确交代过结果。
    EVENTS = ("claimed", "started", "finished", "failed", "blocked",
              "cancelled", "uncertain")
    #: 收尾类事件：出现即说明执行端明确交代过，不再处于「结果未知」。
    CLOSED_EVENTS = ("finished", "failed", "blocked", "cancelled")

    def __init__(self, path: str | Path | None = None):
        # 默认值取环境变量：执行器一般由守护脚本拉起，用环境变量配路径最省事。
        raw = path if path is not None else os.environ.get("BFC_RECEIPTS")
        self.path: Path | None = Path(raw).expanduser() if raw else None
        self._mem: list[dict] = []
        if self.path is not None and str(self.path.parent) not in ("", "."):
            # 目录不存在就建：收据写不进去等于没写，宁可提前失败也不要静默丢。
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, dispatch_id: str, event: str, task_id: str | None = None,
               ticket_id: str | None = None, note: str = "") -> dict:
        """追加一条收据并**落盘**（flush + fsync），返回写入的那行。

        事件名不在 EVENTS 里直接抛 ValueError：写错事件名等于把对账依据写歪，
        静默接受会让 state_of 给出错误的判定。
        """
        if event not in self.EVENTS:
            raise ValueError(
                f"未知收据事件 {event!r}，只能是 {', '.join(self.EVENTS)}")
        row = {
            "ts": time.time(),
            "dispatch_id": dispatch_id,
            "event": event,
            "task_id": task_id,
            "ticket_id": ticket_id,
            "note": note,
        }
        if self.path is None:
            self._mem.append(row)
            return row
        line = json.dumps(row, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            # 顺序不能反：先 flush 到内核，再 fsync 到磁盘。
            # 断电时「已经返回给调用方」的收据必须真的在盘上，否则对账会漏掉开工记录。
            f.flush()
            os.fsync(f.fileno())
        return row

    def read_all(self) -> list[dict]:
        """读回全部收据（按写入顺序）。

        坏行直接跳过：断电可能把最后一行写了一半，为了让对账还能继续，
        不能因为一行残缺就整体炸掉——能读出多少算多少。
        """
        if self.path is None:
            return list(self._mem)
        rows: list[dict] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return rows
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def state_of(self, dispatch_id: str) -> dict:
        """单次派工的对账结论。

        判定表（顺序判定，先命中先返回）：
          1. 最后一条是 finished/failed/blocked/cancelled → "已完成"
             这四件事都说明执行端明确交代过结果，不需要人来判断；
          2. 有 started，且 started 之后没有收尾类事件 → "结果未知"
             开工了没收尾（掉电/被杀/网络断），正是要交给人判断的那种；
          3. 只有 claimed（没有任何 started） → "未开工"
             服务端说派了，执行端只确认收到，没真正动手；
          4. 完全没有记录 → "未知"。
        """
        rows = [r for r in self.read_all() if r.get("dispatch_id") == dispatch_id]
        if not rows:
            return {"dispatch_id": dispatch_id, "last_event": None,
                    "started_at": None, "finished_at": None, "verdict": "未知"}
        last = rows[-1].get("event")
        started = [r for r in rows if r.get("event") == "started"]
        closed = [r for r in rows if r.get("event") in self.CLOSED_EVENTS]
        if last in self.CLOSED_EVENTS:
            verdict = "已完成"
        elif started:
            verdict = "结果未知"
        elif len(rows) and all(r.get("event") == "claimed" for r in rows):
            verdict = "未开工"
        else:
            verdict = "未知"
        return {
            "dispatch_id": dispatch_id,
            "last_event": last,
            "started_at": started[0].get("ts") if started else None,
            # finished_at 取第一个收尾事件的时间：重试后的第二次收尾不改变「何时交代过」
            "finished_at": closed[0].get("ts") if closed else None,
            "verdict": verdict,
        }

    def summary(self) -> dict:
        """全局口径：按 dispatch_id 去重后统计各判定结果，给对账面板用。"""
        seen: list[str] = []
        for row in self.read_all():
            did = row.get("dispatch_id")
            if did and did not in seen:
                seen.append(did)
        out = {"total": len(seen), "完成": 0, "结果未知": 0, "未开工": 0, "未知": 0}
        # 判定词是"已完成"，统计口径用的是"完成"——这两个词不能靠字符串硬拼，必须显式映射。
        key_of = {"已完成": "完成", "结果未知": "结果未知",
                  "未开工": "未开工", "未知": "未知"}
        for did in seen:
            out[key_of[self.state_of(did)["verdict"]]] += 1
        return out


# 「没传 receipts」与「显式传 None」必须能区分：前者读环境变量，后者表示不要收据。
_UNSET = object()


class ExecutorClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT, receipts: object = _UNSET):
        """receipts 三种写法：

        - 不传（默认哨兵 _UNSET）：用 ReceiptLog(os.environ.get("BFC_RECEIPTS"))，
          环境变量没配就是内存 no-op；
        - 传 ReceiptLog 或路径 str/Path：用它落收据；
        - 显式传 None 或 False：明确表示「不要收据」（内存 no-op，绝不建文件）。
          之所以用哨兵而不是把默认值写成 None，就是因为 None 已被占用为「关闭」。
        """
        self.base_url = (base_url or os.environ.get("BFC_BASE_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("BFC_TOKEN", "")
        self.timeout = timeout
        self.receipts = self._as_receipts(receipts)
        if not self.base_url or not self.token:
            raise RuntimeError("需要 BFC_BASE_URL 与 BFC_TOKEN")

    @staticmethod
    def _as_receipts(receipts: object) -> ReceiptLog:
        if receipts is _UNSET:
            return ReceiptLog(os.environ.get("BFC_RECEIPTS"))
        if receipts is None or receipts is False:
            return ReceiptLog(None)
        if isinstance(receipts, ReceiptLog):
            return receipts
        return ReceiptLog(receipts)      # type: ignore[arg-type]  # str | Path

    @staticmethod
    def dispatch_id_of(resp: dict) -> str:
        """取稳定派工号；旧服务端没有 dispatch_id 时降级用 ticket_id。"""
        return resp.get("dispatch_id") or resp.get("ticket_id") or ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None,
              retries: int = 1) -> dict:
        """发一次逻辑请求。

        写操作（有 body 且带 idempotency_key）重试时**复用同一 body**，
        服务端凭幂等键返回历史结果，不会重复执行。
        """
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in self._headers().items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in (401, 403, 409, 404, 422):
                    # 这些是确定性拒绝，重试没有意义
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from None
                last_exc = RuntimeError(f"HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_exc = e
                # 写操作遇到网络不确定：不盲目重发，交给调用方查询最终状态
                if body is not None:
                    raise _UncertainNetworkError(
                        f"网络错误（结果未知）: {getattr(e, 'reason', e)}") from None
            if attempt < retries:
                time.sleep(0.6)
        raise RuntimeError(f"网络错误: {getattr(last_exc, 'reason', last_exc)}")

    # ---- 只读 ----
    def health(self) -> dict:
        req = urllib.request.Request(self.base_url + "/health")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def list_tasks(self) -> list[dict]:
        return self._call("GET", "/api/v1/tasks/available").get("tasks", [])

    def task_status(self, task_id: str) -> dict:
        return self._call("GET", f"/api/v1/tasks/{task_id}/status")

    # ---- 执行 ----
    def claim(self, task_id: str, idem: str | None = None) -> dict:
        """领取任务；成功后立刻落一条 claimed 收据。

        dispatch_id 是「这一次派工」的稳定编号，重新派工会变；旧服务端没有这个字段时
        降级用 ticket_id。收据落盘在**返回之前**完成，调用方拿到票就代表证据已在盘上。
        """
        key = idem or new_idem_key("claim")
        resp = self._call("POST", "/api/v1/tasks/claim",
                          {"task_id": task_id, "idempotency_key": key})
        self.receipts.record(self.dispatch_id_of(resp), "claimed", task_id=task_id,
                             ticket_id=resp.get("ticket_id") or resp.get("ticket"))
        return resp

    def heartbeat(self, ticket: str, status: str = "running", detail: str = "",
                  idem: str | None = None) -> dict:
        key = idem or new_idem_key("hb")
        return self._call("POST", "/api/v1/runs/heartbeat",
                          {"ticket": ticket, "status": status, "detail": detail,
                           "idempotency_key": key})

    def submit(self, ticket: str, status: str = "completed", log: str = "",
               diff: str = "", tests: str = "", note: str = "",
               idem: str | None = None) -> dict:
        key = idem or new_idem_key("submit")
        return self._call("POST", "/api/v1/runs/result", {
            "ticket": ticket, "status": status, "log": log,
            "diff": diff, "tests": tests, "note": note,
            "idempotency_key": key,
        })

    def confirm_cancel(self, ticket: str, note: str = "已按取消指令停止",
                       idem: str | None = None) -> dict:
        """收到取消指令后，明确回传「已停止」以完成取消（P1-2.1）。"""
        return self.submit(ticket, "cancelled", note=note, idem=idem)

    # ---- 组合：模拟一次完整执行 ----
    def run_once(self, task_id: str, log: str = "mock run", diff: str = "",
                 tests: str = "ok") -> dict:
        """一次完整执行：领取 → 心跳 → 回传，并沿途落 started/finished 收据。

        每一步都有自己的幂等键；心跳若收到 cancel 指令，自动走取消确认路径。

        收据顺序在这里定死：claimed（claim 内部写）→ started（心跳发出前，代表真的动手了）
        → finished / cancelled（服务端确认后）。submit 遇到网络不确定时先写 uncertain
        再原样抛出：结果未知必须留在盘上，让 state_of 判成「结果未知」交人核对；
        吞掉异常会让收据看起来像正常结束，比不写更糟。
        """
        claimed = self.claim(task_id)
        ticket = claimed["ticket"]
        did = self.dispatch_id_of(claimed)
        ticket_id = claimed.get("ticket_id") or ticket

        # started 放在心跳之前：一旦进程在心跳途中挂掉，收据已经能证明「开工过」。
        self.receipts.record(did, "started", task_id=task_id, ticket_id=ticket_id,
                             note="开始执行")
        hb = self.heartbeat(ticket, "running", "开始执行")
        if hb.get("cancel") or hb.get("cancel_ack_required"):
            res = self.confirm_cancel(ticket, "收到取消指令，已停止并确认")
            self.receipts.record(did, "cancelled", task_id=task_id, ticket_id=ticket_id,
                                 note="收到取消指令并已确认")
            return {"claimed": {k: claimed[k] for k in ("task_id", "version", "ticket_id")},
                    "result": res, "cancelled": True}
        try:
            res = self.submit(ticket, "completed", log, diff, tests, "模拟执行完成")
        except _UncertainNetworkError as exc:
            # 不吞：先落 uncertain 说明「这次提交结果不明」，再原样抛给调用方处理。
            self.receipts.record(did, "uncertain", task_id=task_id, ticket_id=ticket_id,
                                 note=f"结果未知: {exc}")
            raise
        self.receipts.record(did, "finished", task_id=task_id, ticket_id=ticket_id,
                             note="结果已回传")
        return {"claimed": {k: claimed[k] for k in ("task_id", "version", "ticket_id")},
                "result": res}

    def reconcile(self, receipts: "ReceiptLog | str | Path | None" = None) -> dict:
        """人工对账入口：汇总收据，并列出「结果未知」的 dispatch_id。

        服务端数据库从旧备份恢复后，只能拿执行端本地收据来判定某次派工到底有没有
        真正开工；结果未知（开工了没收尾）不能自动定性，必须连编号一起交给人核对。

        receipts 为 None 时用 self.receipts；否则接受 ReceiptLog 或路径。
        """
        log = self.receipts if receipts is None else self._as_receipts(receipts)
        summary = log.summary()
        if not summary.get("total"):
            # 一条收据都没有时给一个干净的形状，免得调用方还要判空才敢读 key。
            return {"total": 0, "unknown_inflight": []}
        seen: list[str] = []
        for row in log.read_all():
            did = row.get("dispatch_id")
            if did and did not in seen:
                seen.append(did)
        out = dict(summary)
        out["unknown_inflight"] = [d for d in seen
                                   if log.state_of(d)["verdict"] == "结果未知"]
        return out

    # 这几个状态才**能证明「本次提交确实落库」**：
    #   SUBMITTED / DONE —— 已进入待验收或验收通过
    #   CANCELLED      —— 取消确认已生效
    # BLOCKED 不算：心跳超时也会把任务打成 BLOCKED，跟我们这次提交无关，
    # 拿它当「提交成功」会让执行器把没送达的结果当成送达（高危误判）。
    RECONCILED_OK = ("SUBMITTED", "DONE", "CANCELLED")

    def safe_submit(self, ticket: str, task_id: str | None = None, **kwargs) -> dict:
        """提交结果；响应丢失时先查最终状态再决定，绝不盲目重发（P1-5.3）。

        task_id 必须是**显式参数**，不能靠 kwargs 碰运气——submit() 没有 task_id 形参，
        旧写法 kwargs.get("task_id") 恒为 None，等于「不确定时永远直接抛」。
        """
        try:
            return self.submit(ticket, **kwargs)
        except _UncertainNetworkError as exc:
            if not task_id:
                raise RuntimeError(
                    f"提交结果未知，且调用方没给 task_id，无法核对：{exc}") from None
            st = self.task_status(task_id)
            status = st.get("status")
            if status in self.RECONCILED_OK:
                return {"ok": True, "task_status": status, "reconciled": True}
            raise RuntimeError(
                f"提交结果未知：任务当前状态 {status}，不能证明本次结果已落库，"
                "请人工核对后再决定是否重提") from None

    def safe_claim(self, task_id: str, idem: str | None = None,
                   retries: int = 1) -> dict:
        """领取；网络不确定时**用同一个幂等键**重试（服务端返回历史票，不会双发）。"""
        key = idem or new_idem_key("claim")
        try:
            return self.claim(task_id, idem=key)
        except _UncertainNetworkError:
            if retries <= 0:
                raise
            return self.safe_claim(task_id, idem=key, retries=retries - 1)

    def safe_heartbeat(self, ticket: str, status: str = "running", detail: str = "",
                       idem: str | None = None, retries: int = 1) -> dict:
        """心跳；同样复用幂等键重试——重复心跳不会产生重复事件。"""
        key = idem or new_idem_key("hb")
        try:
            return self.heartbeat(ticket, status, detail, idem=key)
        except _UncertainNetworkError:
            if retries <= 0:
                raise
            return self.safe_heartbeat(ticket, status, detail, idem=key,
                                       retries=retries - 1)
