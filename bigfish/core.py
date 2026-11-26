"""Council 核心业务：任务、审查、审批、执行票、心跳、回传、验收.

v0.1.1-rc3 修复要点（对应任务书 P1/P2）：
- P1 取消：APPROVED 取消后不可领取；已派工先进 CANCELLING 等执行器确认，
  失联由看门狗置 BLOCKED；全程审计（request_cancel / confirm_cancel）。
- P1 审批有效性：撤票后重算，APPROVED 也不能保留失效审批；已派工则撤销
  执行授权并安全阻断，历史审批与撤销记录都保留。
- P1 submit_for_review 也走 _recompute_review（先投票后送审不再卡住）。
- P1 鉴权竞态：claim/heartbeat/submit 在业务事务内复验执行器 enabled、
  票未撤销未过期、票世代与恢复世代一致、任务仍归该执行器所有。
- P1/P2 幂等：检查与写入在同一个事务里；同键异体拒绝；重放不产生新事件。
- P2 脱敏：events / artifacts / idem 响应统一 scrub 后再落库。
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from . import roles as R
from . import state as S
from .backup import export_audit_bundle as _export_audit_bundle
from .backup import export_bundle as _export_bundle
from .db import Database, dumps, loads, now
from .security import (derive_ticket, hash_token, new_token, request_fingerprint,
                       sanitize, scrub, ticket_id_for)

RISK_LEVELS = ("low", "medium", "high")
MODELS = ("gpt", "gemini")
MODEL_LABELS = {"gpt": "GPT", "gemini": "Gemini"}

# C：审批「仍然生效」的状态白名单（正向列举）。
# 原来的黑名单写法漏排了 PENDING_APPROVAL / REVIEW_PASSED / BLOCKED，
# 导致撤票退回议事后的历史批准仍被 GUI 当成有效授权。
EFFECTIVE_APPROVAL_STATES = {S.APPROVED, S.DISPATCHED, S.RUNNING, S.CANCELLING,
                              S.SUBMITTED, S.DONE}
VERDICTS = ("approve", "reject", "comment")
SUBMIT_STATUSES = ("completed", "failed", "blocked", "cancelled")

MAX_PARALLEL_PER_EXECUTOR = 4


class CouncilError(RuntimeError):
    """业务错误：message 直接回给调用方，不含敏感信息。"""


def _task_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    d["control"] = loads(d.get("control"), {}) or {}
    d["status_label"] = S.label(d["status"])
    return d


class Watchdog(threading.Thread):
    """独立后台看门狗：定时扫描心跳超时，把失联任务置为 BLOCKED。

    - 单例：同一数据库路径只允许一个实例运行；
    - 与 GUI 无关：GUI 关闭/最小化不影响；
    - 退出时 stop() 会等线程收敛。
    """

    _instances: dict[str, "Watchdog"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, council: "Council", interval: float = 10.0):
        super().__init__(name="bfc-watchdog", daemon=True)
        self.council = council
        self.interval = max(1.0, float(interval))
        # 注意：不能叫 self._stop —— 那是 threading.Thread 的方法，覆盖后 join() 会炸
        self._stop_evt = threading.Event()
        self.sweeps = 0
        self.errors = 0
        self.last_blocked: list[dict] = []

    @staticmethod
    def key_for(db_path) -> str:
        """单例键必须用「解析后」的绝对路径：相对路径与绝对路径不能算两个实例。"""
        try:
            return str(Path(db_path).expanduser().resolve())
        except Exception:
            return str(db_path)

    @classmethod
    def start_for(cls, council: "Council", interval: float = 10.0) -> "Watchdog":
        key = cls.key_for(council.db.path)
        with cls._instances_lock:
            existing = cls._instances.get(key)
            if existing and existing.is_alive():
                return existing
            wd = cls(council, interval)
            cls._instances[key] = wd
            wd.start()
            return wd

    @classmethod
    def stop_for(cls, db_path) -> "Watchdog | None":
        """停掉并移除该数据库上的看门狗；返回被停掉的那个实例（没有则 None）。"""
        key = cls.key_for(db_path)
        with cls._instances_lock:
            wd = cls._instances.pop(key, None)
        if wd:
            wd.stop()
        return wd

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                blocked = self.council.sweep_timeouts()
                self.sweeps += 1
                if blocked:
                    self.last_blocked = blocked
            except Exception:      # 看门狗不能因单次异常退出
                self.errors += 1
                continue

    def stop(self) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=5)


class Council:
    def __init__(self, db: Database, key: bytes | None = None,
                 heartbeat_ttl: int = 120, ticket_ttl: int = 900,
                 ticket_max_ttl: int = 21600, heartbeat_lease: int = 180,
                 key_path=None):
        self.db = db
        self.key = key or b""
        self.key_path = key_path
        self.heartbeat_ttl = heartbeat_ttl
        self.ticket_ttl = ticket_ttl
        self.ticket_max_ttl = ticket_max_ttl
        self.heartbeat_lease = heartbeat_lease
        self._watchdog: Watchdog | None = None

    # ------------------------------------------------------------------ 事件
    def log_event(self, task_id: str | None, kind: str, detail: str = "") -> None:
        """事件落库前统一脱敏：executor 回传内容里出现凭据也不会写进数据库。"""
        self.db.execute(
            "INSERT INTO events(task_id, kind, detail, created_at) VALUES(?,?,?,?)",
            (task_id, scrub(kind)[:80], scrub(detail)[:4000], now()),
        )

    def events(self, task_id: str, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 执行器
    def register_executor(self, name: str, role: str = "executor",
                          model: str | None = None) -> dict:
        """创建执行器/审查身份。Token 明文只在这里返回一次。

        审查身份在**创建时**把模型绑定写进 executors.model 列（不可由改名改变），
        不再靠 name 字符串里的 gpt/gemini 猜（P2-3）。model 省略时按旧规则从
        name 推断，仅用于兼容既有部署。
        """
        # 白名单与 roles.DB_ROLES 同源：放行 mobile 是为了让手机端身份能被创建，
        # role_for_db_row() 会把它映射成 MOBILE_VIEWER（只读），不会变成执行器。
        if role not in R.DB_ROLES:
            raise CouncilError("role 只能是 " + " / ".join(sorted(R.DB_ROLES)))
        if role == "reviewer":
            if model is None:
                # rc9 / AUDIT-RC8-02：注册入口与 role_for_db_row 用同一套判定，
                # 不能一个失败关闭、一个默认 Gemini —— 否则「名字里没有 gpt」
                # 就会静默建出一个 Gemini 审查身份，谁都不知道它代表谁。
                low = (name or "").lower()
                has_gpt = "gpt" in low
                has_gem = "gemini" in low
                if has_gpt and not has_gem:
                    model = "gpt"
                elif has_gem and not has_gpt:
                    model = "gemini"
                else:
                    raise CouncilError(
                        "审查身份必须显式指定 model=gpt 或 model=gemini"
                        "（名称无法唯一判断，拒绝猜测）")
            if model not in MODELS:
                raise CouncilError("审查身份的 model 只能是 gpt 或 gemini")
        else:
            model = None
        prefix = {"executor": "bfc_ex_", "reviewer": "bfc_rv_", "admin": "bfc_ad_",
                  "mobile": "bfc_mb_"}[role]
        ex_id = role + "-" + uuid.uuid4().hex[:8]
        token = new_token(prefix)
        self.db.execute(
            "INSERT INTO executors(id,name,role,model,token_hash,enabled,created_at)"
            " VALUES(?,?,?,?,?,1,?)",
            (ex_id, name, role, model, hash_token(token), now()),
        )
        self.db.audit(R.HUMAN_OWNER, "executor.registered", ex_id,
                      f"{role}:{name}" + (f":{model}" if model else ""))
        self.log_event(None, "executor.registered",
                       f"{role}:{name}" + (f":{model}" if model else ""))
        out = {"id": ex_id, "name": name, "role": role, "token": token}
        if model:
            out["model"] = model
        return out

    def authenticate(self, token: str, role: str = "executor") -> dict:
        """按数据库 role 列鉴权；返回带规范角色 role_canonical 的字典。"""
        if not token:
            raise CouncilError("缺少执行器令牌")
        h = hash_token(token)
        row = self.db.one("SELECT * FROM executors WHERE token_hash=? AND enabled=1", (h,))
        if row is None:
            # R1：恢复后被禁用的身份必须给出可执行的原因，不能只说「令牌无效」——
            # 运维要知道该重新授权还是该换凭据。
            gone = self.db.one("SELECT * FROM executors WHERE token_hash=?", (h,))
            if gone is not None and not gone["enabled"]:
                raise CouncilError(self._disabled_message(gone))
            raise CouncilError("执行器令牌无效")
        got = row["role"]
        if got != role and not (role == "reviewer" and got in ("executor", "reviewer")):
            raise CouncilError("执行器令牌无效")
        self.db.execute("UPDATE executors SET last_seen=? WHERE id=?", (now(), row["id"]))
        d = dict(row)
        d.pop("token_hash", None)
        d["role_canonical"] = self._canonical_role_or_reject(d, role)
        return d

    def authenticate_any(self, token: str) -> dict:
        if not token:
            raise CouncilError("缺少令牌")
        h = hash_token(token)
        row = self.db.one("SELECT * FROM executors WHERE token_hash=? AND enabled=1", (h,))
        if row is None:
            gone = self.db.one("SELECT * FROM executors WHERE token_hash=?", (h,))
            if gone is not None and not gone["enabled"]:
                raise CouncilError(self._disabled_message(gone))
            raise CouncilError("令牌无效")
        self.db.execute("UPDATE executors SET last_seen=? WHERE id=?", (now(), row["id"]))
        d = dict(row)
        d.pop("token_hash", None)
        d["role_canonical"] = self._canonical_role_or_reject(d, None)
        return d

    def _canonical_role_or_reject(self, row: dict,
                                  channel_role: str | None) -> str:
        """把库里的 role 映射成业务角色；认不出的身份直接拒绝（R7-02）。

        旧实现让未登记的 db_role 兜底成 EXECUTOR_DSH —— 一次手工 SQL 或旧版本
        残留行就能拿到执行器权限。现在失败关闭：映射结果是 DENIED_ROLE 就抛错，
        调用方连 role_canonical 都拿不到。

        这里必须是普通方法而不是 staticmethod：拒绝时要写一条审计事件
        （auth.denied_unknown_role），走的是 self.log_event。
        """
        canonical = R.role_for_db_row(row.get("role"), row.get("name"),
                                      row.get("model"))
        if canonical == R.DENIED_ROLE:
            detail = f"身份 {row.get('id')} 的库内角色 {row.get('role')!r} 未登记，拒绝鉴权"
            # 只记身份 id，不记令牌
            self.log_event(None, "auth.denied_unknown_role", str(row.get("id")))
            raise CouncilError(detail)
        return canonical

    def executors(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id,name,role,model,enabled,created_at,last_seen FROM executors"
            " ORDER BY created_at")
        return [dict(r) for r in rows]

    def revoke_executor(self, exec_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE executors SET enabled=0 WHERE id=?", (exec_id,))
            c.execute("UPDATE tickets SET revoked_at=? WHERE executor_id=? AND revoked_at IS NULL",
                      (now(), exec_id))
        self.db.audit(R.HUMAN_OWNER, "executor.revoked", exec_id)
        self.log_event(None, "executor.revoked", exec_id)

    @staticmethod
    def _disabled_message(row) -> str:
        reason = (row["disabled_reason"] if "disabled_reason" in row.keys()
                  else None) or "被管理员禁用"
        return f"该身份已被禁用：{reason}；必须由人类所有者重新授权并更换凭据"

    def reenable_executor(self, exec_id: str, actor: str = R.HUMAN_OWNER) -> dict:
        """人类重新授权一个被禁用的身份（R1）。

        重新启用**同时轮换凭据**：恢复前的旧 token 可能已经泄露到外部，
        只翻 enabled=1 等于把旧凭据一起复活。所以这里返回一个新 token，
        旧 token 的哈希在库里被直接替换掉，物理上无法再用。
        """
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM executors WHERE id=?", (exec_id,)).fetchone()
            if row is None:
                raise CouncilError("身份不存在")
            role = row["role"]
            prefix = {"executor": "bfc_ex_", "reviewer": "bfc_rv_",
                      "admin": "bfc_ad_"}.get(role, "bfc_ex_")
            token = new_token(prefix)
            c.execute(
                "UPDATE executors SET enabled=1, disabled_reason=NULL, token_hash=?,"
                " last_seen=NULL WHERE id=?", (hash_token(token), exec_id))
        self.db.audit(actor, "executor.reenabled", exec_id,
                      f"{role}:{row['name']}（凭据已轮换）")
        self.log_event(None, "executor.reenabled", f"{role}:{row['name']}")
        return {"id": exec_id, "name": row["name"], "role": role, "token": token,
                "model": row["model"] if "model" in row.keys() else None}

    def _assert_executor_active(self, c, executor: dict) -> None:
        """事务内复验执行器仍然启用：吊销与业务操作不可能同时生效（P1-3.2）。"""
        ex_id = (executor or {}).get("id")
        if not ex_id:
            raise CouncilError("缺少执行器身份")
        row = c.execute(
            "SELECT enabled, disabled_reason FROM executors WHERE id=?", (ex_id,)).fetchone()
        if row is None:
            raise CouncilError("执行器不存在")
        if not row["enabled"]:
            raise CouncilError(self._disabled_message(row))

    # ------------------------------------------------------------------ 任务
    def create_task(self, title: str, goal: str = "", scope: str = "", risk: str = "low",
                    prompt: str = "", control: dict | None = None) -> dict:
        if not title.strip():
            raise CouncilError("任务标题不能为空")
        if risk not in RISK_LEVELS:
            raise CouncilError("risk 只能是 low / medium / high")
        tid = "task-" + uuid.uuid4().hex[:10]
        t = now()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO tasks(id,title,goal,scope,risk,status,current_version,prompt,control,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
                (tid, title.strip(), goal, scope, risk, S.DRAFT, prompt,
                 dumps(control or {}), t, t))
            c.execute(
                "INSERT INTO task_versions(task_id,version,prompt,goal,scope,risk,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (tid, 1, prompt, goal, scope, risk, "初始版本", t))
        self.log_event(tid, "task.created", title.strip())
        return self.get_task(tid)

    def get_task(self, tid: str) -> dict:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if row is None:
            raise CouncilError("任务不存在")
        d = _task_dict(row)
        d["votes"] = self.vote_summary(tid, d["current_version"])
        d["reviews"] = self.reviews(tid, d["current_version"])
        d["runs"] = self.runs(tid)
        d["latest_approval"] = self.latest_approval(tid)
        return d

    def list_tasks(self, status: str | None = None, keyword: str = "",
                   limit: int = 200, statuses: set[str] | list[str] | tuple | None = None,
                   offset: int = 0) -> list[dict]:
        """列任务。status 单值、statuses 集合二者可并用（取交集语义）。

        排序固定 updated_at DESC, id DESC —— 只按 updated_at 排序时，同一秒内
        创建的多条任务在不同 SQLite 版本下顺序不稳，翻页会重复或漏项。
        offset 供 GUI / 未来的移动端分页使用。"""
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if statuses:
            vals = list(statuses)
            sql += " AND status IN (" + ",".join("?" * len(vals)) + ")"
            params.extend(vals)
        if keyword:
            sql += " AND (title LIKE ? OR goal LIKE ?)"
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(0, int(limit)), max(0, int(offset))])
        return [_task_dict(r) for r in self.db.query(sql, params)]

    def get_task_status_row(self, tid: str) -> dict | None:
        """轻量状态查询：只取状态机相关列，不装配 reviews/runs/approval。

        给移动端轮询、MCP 状态查询这类高频路径用；get_task() 一次要跑 5 条查询，
        轮询时不合适。找不到返回 None，不抛异常。"""
        row = self.db.one(
            "SELECT id,title,status,current_version,executor_id,blocked_reason,"
            " cancel_requested_at,control,updated_at FROM tasks WHERE id=?", (tid,))
        if row is None:
            return None
        d = dict(row)
        d["control"] = loads(d.get("control")) or {}
        d["status_label"] = S.label(d["status"])
        return d

    def _require_status(self, task: dict, allowed: set[str], action: str) -> None:
        if task["status"] not in allowed:
            raise CouncilError(f"当前状态「{S.label(task['status'])}」不允许{action}")

    def update_prompt(self, tid: str, prompt: str, note: str = "",
                      goal: str | None = None, scope: str | None = None,
                      risk: str | None = None, expected_version: int | None = None) -> dict:
        """修改 Prompt 生成新版本并把任务打回议事（旧投票与旧票失效）。

        expected_version 非空时做乐观并发控制：版本不符直接拒绝（P1-2.4）。
        """
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            if task["status"] not in (S.DRAFT, S.RETURNED, S.IN_REVIEW, S.REVIEW_PASSED,
                                      S.PENDING_APPROVAL, S.BLOCKED):
                raise CouncilError(
                    f"当前状态「{S.label(task['status'])}」不允许修改 Prompt")
            if expected_version is not None and task["current_version"] != expected_version:
                raise CouncilError(
                    f"版本已变（当前 v{task['current_version']}，请求基于 v{expected_version}），"
                    "已拒绝覆盖")
            version = task["current_version"] + 1
            g = task["goal"] if goal is None else goal
            sc = task["scope"] if scope is None else scope
            rk = task["risk"] if risk is None else risk
            if rk not in RISK_LEVELS:
                raise CouncilError("risk 只能是 low / medium / high")
            t = now()
            c.execute(
                "INSERT INTO task_versions(task_id,version,prompt,goal,scope,risk,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (tid, version, prompt, g, sc, rk, note, t))
            # D：新版本必须把取消/阻断/执行器残留一起清掉。
            # 否则「已取消的任务改一版 Prompt 再重新审批」会永远领不到
            # （claim 的 WHERE cancel_requested_at IS NULL 会一直挡住）。
            ctl = dict(task["control"] or {})
            ctl.pop("cancel_requested", None)
            ctl.pop("pause_requested", None)
            cur = c.execute(
                "UPDATE tasks SET current_version=?, prompt=?, goal=?, scope=?, risk=?,"
                " status=?, updated_at=?, cancel_requested_at=NULL,"
                " blocked_reason=NULL, executor_id=NULL, control=?"
                " WHERE id=? AND current_version=?",
                (version, prompt, g, sc, rk, S.IN_REVIEW, t,
                 dumps(ctl), tid, task["current_version"]))
            if cur.rowcount != 1:
                raise CouncilError("任务已被其他进程修改，请刷新后重试")
            # 新版本自动作废旧版本的执行票与旧审批
            c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
                      (t, tid))
        self.log_event(tid, "task.version", f"v{version} {note}".strip())
        return self.get_task(tid)

    def set_control(self, tid: str, control: dict) -> dict:
        self.get_task(tid)
        self.db.execute("UPDATE tasks SET control=?, updated_at=? WHERE id=?",
                        (dumps(control), now(), tid))
        return self.get_task(tid)

    def versions(self, tid: str) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM task_versions WHERE task_id=? ORDER BY version DESC", (tid,))]

    def diff_versions(self, tid: str, v1: int, v2: int) -> str:
        import difflib
        a = self.db.one("SELECT prompt FROM task_versions WHERE task_id=? AND version=?", (tid, v1))
        b = self.db.one("SELECT prompt FROM task_versions WHERE task_id=? AND version=?", (tid, v2))
        if a is None or b is None:
            raise CouncilError("版本不存在")
        return "".join(difflib.unified_diff(
            (a["prompt"] or "").splitlines(keepends=True),
            (b["prompt"] or "").splitlines(keepends=True),
            fromfile=f"v{v1}", tofile=f"v{v2}", n=3))

    def submit_for_review(self, tid: str) -> dict:
        """送审：状态推进后立即按当前版本的既有票重算（P1-2.3）。"""
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            if task["status"] not in (S.DRAFT, S.RETURNED, S.IN_REVIEW):
                raise CouncilError(
                    f"当前状态「{S.label(task['status'])}」不允许送审")
            cur = c.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND current_version=?"
                " AND status=?", (S.IN_REVIEW, now(), tid, task["current_version"],
                                  task["status"]))
            if cur.rowcount != 1:
                raise CouncilError("任务状态已被修改，请刷新后重试")
            version = task["current_version"]
        self.log_event(tid, "task.review_started", f"v{version}")
        # 已经有双票的任务送审后应直接进入待审批，而不是卡在 IN_REVIEW
        self._recompute_review(tid)
        return self.get_task(tid)

    # ------------------------------------------------------------------ 审查
    def add_review(self, tid: str, model: str, content: str, verdict: str | None = None,
                   version: int | None = None, source: str = "manual",
                   kind: str = "comment", actor: str | None = None) -> dict:
        """kind=comment 只记录意见；kind=vote 才计票。

        actor 由调用方（服务端鉴权上下文）传入；省略时视为 human_owner 转述。
        若 actor 是审查角色，则 model 必须与该角色绑定的模型一致。
        """
        if model not in MODELS:
            raise CouncilError("model 只能是 gpt 或 gemini")
        if kind not in ("comment", "vote"):
            raise CouncilError("kind 只能是 comment 或 vote")
        if verdict is not None and verdict not in VERDICTS:
            raise CouncilError("verdict 只能是 approve / reject / comment")
        if kind == "vote" and verdict not in ("approve", "reject"):
            raise CouncilError("投票必须给出 approve 或 reject")
        actor = actor or R.HUMAN_OWNER
        bound = R.model_for_role(actor)
        if bound is not None and bound != model:
            raise CouncilError(
                f"角色「{R.ROLE_LABELS[actor]}」只能以 {bound} 身份投票")
        t = now()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            if task["status"] not in (S.IN_REVIEW, S.REVIEW_PASSED, S.PENDING_APPROVAL,
                                      S.RETURNED, S.DRAFT):
                raise CouncilError("当前状态不接受审查输入")
            v = version if version is not None else task["current_version"]
            exists = c.execute(
                "SELECT 1 FROM task_versions WHERE task_id=? AND version=?", (tid, v)).fetchone()
            if exists is None:
                raise CouncilError("版本不存在")
            c.execute(
                "INSERT INTO reviews(task_id,version,model,kind,verdict,content,source,actor,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, v, model, kind, verdict, scrub(content)[:20000], source, actor, t))
        self.log_event(tid, f"review.{kind}", f"{model} v{v} {verdict or ''}".strip())
        self._recompute_review(tid)
        return self.get_task(tid)

    def revoke_review(self, review_id: int, actor: str = R.HUMAN_OWNER) -> dict:
        """撤销一张审查票（人工干预必须有审计）。

        v0.1.1-rc3：撤票后必须重新评估审批有效性——未派工的不能保留失效审批，
        已派工的撤销执行授权并安全阻断。历史审批/撤销记录都保留，不删证据。
        """
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise CouncilError("审查记录不存在")
            if row["revoked_at"]:
                raise CouncilError("该审查记录已撤销")
            c.execute("UPDATE reviews SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                      (now(), review_id))
            tid = row["task_id"]
            version = row["version"]
            kind = row["kind"] if "kind" in row.keys() else "vote"
            task_row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            t = now()
            invalidated = False
            was_status = task_row["status"] if task_row else None
            # 只有「票」才影响审批有效性。撤一条普通评论只是把那段文字下架，
            # 它本来就不计入 vote_summary；历史缺陷是这里不判 kind，撤评论也能
            # 把运行中的任务打成 BLOCKED（审计项 R3）。
            if kind != "vote":
                task_row = None
            if task_row is not None and version == task_row["current_version"]:
                if task_row["status"] in (S.PENDING_APPROVAL, S.APPROVED):
                    c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?"
                              " AND current_version=? AND status=?",
                              (S.IN_REVIEW, t, tid, version, task_row["status"]))
                    c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?"
                              " AND revoked_at IS NULL", (t, tid))
                    invalidated = True
                elif task_row["status"] in (S.DISPATCHED, S.RUNNING, S.CANCELLING):
                    c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?"
                              " AND revoked_at IS NULL", (t, tid))
                    c.execute("UPDATE tasks SET status=?, blocked_reason=?, updated_at=?"
                              " WHERE id=? AND status=?",
                              (S.BLOCKED, "审查票被撤销，执行授权已作废，需人工确认副作用",
                               t, tid, task_row["status"]))
                    invalidated = True
        self.db.audit(actor, "review.revoked", f"review#{review_id}", f"{tid} v{version}")
        self.log_event(tid, "review.revoked", f"review#{review_id} v{version}")
        if invalidated:
            self.db.audit(actor, "approval.invalidated", tid,
                          f"v{version} 因撤票失效（原状态 {S.label(was_status)}）")
            self.log_event(tid, "approval.invalidated",
                           f"撤票导致 v{version} 审批失效（原状态 {S.label(was_status)}）")
        self._recompute_review(tid)
        return self.get_task(tid)

    def reviews(self, tid: str, version: int | None = None) -> list[dict]:
        if version is None:
            rows = self.db.query(
                "SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC", (tid,))
        else:
            rows = self.db.query(
                "SELECT * FROM reviews WHERE task_id=? AND version=? ORDER BY id DESC",
                (tid, version))
        out = []
        for r in rows:
            d = dict(r)
            d["model_label"] = MODEL_LABELS.get(d["model"], d["model"])
            d["revoked"] = bool(d.get("revoked_at"))
            out.append(d)
        return out

    def vote_summary(self, tid: str, version: int) -> dict:
        """同一版本、每个模型只取最新一张**未撤销**的票。"""
        result: dict[str, dict] = {}
        for r in self.db.query(
            "SELECT model, verdict, created_at, actor, revoked_at FROM reviews"
            " WHERE task_id=? AND version=? AND kind='vote'"
            " ORDER BY id DESC", (tid, version)):
            if r["model"] not in result:
                # C：只看该模型最新一张票（含已撤销）。最新票被撤 = 立场撤退，
                # 不能再回落到更早那张赞成票上凑出「双通过」。
                gone = bool(r["revoked_at"])
                result[r["model"]] = {"verdict": None if gone else r["verdict"],
                                      "at": r["created_at"], "actor": r["actor"],
                                      "revoked": gone}
        for m in MODELS:
            result.setdefault(m, {"verdict": None, "at": None, "actor": None,
                               "revoked": False})
        approved = all(result[m]["verdict"] == "approve" for m in MODELS)
        rejected = any(result[m]["verdict"] == "reject" for m in MODELS)
        result["all_approved"] = approved
        result["any_rejected"] = rejected
        result["complete"] = all(result[m]["verdict"] is not None for m in MODELS)
        return result

    def votes_text(self, tid: str, version: int) -> str:
        v = self.vote_summary(tid, version)
        parts = [f"{MODEL_LABELS[m]}={v[m]['verdict'] or '未投'}" for m in MODELS]
        if all(v[m]["verdict"] is None for m in MODELS):
            return "尚未投票"
        return "　".join(parts) + ("　（双通过）" if v["all_approved"] else
                                   ("　（有 reject）" if v["any_rejected"] else ""))

    def _recompute_review(self, tid: str) -> None:
        """按当前版本的票重算状态；APPROVED/PENDING_APPROVAL 也会被降级（P1-2.2）。"""
        events: list[tuple[str, str]] = []
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                return
            task = dict(row)
            if task["status"] not in S.REVIEW_GATED:
                return
            version = task["current_version"]
            votes = self._vote_summary_locked(c, tid, version)
            t = now()
            if votes["all_approved"]:
                if task["status"] in (S.IN_REVIEW, S.REVIEW_PASSED):
                    cur = c.execute(
                        "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status=?"
                        " AND current_version=?",
                        (S.PENDING_APPROVAL, t, tid, task["status"], version))
                    if cur.rowcount == 1:
                        events.append(("task.review_passed", f"v{version} 双模型通过"))
                        events.append(("task.pending_approval", "等待人类审批"))
                # 注意：这里**不能 return**。历史缺陷：提前 return 跳过了函数末尾
                # 的 log_event 循环，双模型通过的两条事件永远写不进库（审计只看
                # 事件表会以为任务从没通过审查）。
            # 未双通过：从待审批/已批准退回议事，并作废可能已存在的授权
            elif task["status"] in (S.PENDING_APPROVAL, S.APPROVED):
                cur = c.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status=?"
                    " AND current_version=?",
                    (S.IN_REVIEW, t, tid, task["status"], version))
                if cur.rowcount == 1:
                    c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?"
                              " AND revoked_at IS NULL", (t, tid))
                    events.append(("task.review_reopened",
                                   f"v{version} 票不再双通过，退回议事"))
            elif task["status"] == S.REVIEW_PASSED:
                c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?"
                          " AND current_version=? AND status=?",
                          (S.IN_REVIEW, t, tid, version, S.REVIEW_PASSED))
            if not votes["all_approved"] and votes["any_rejected"]:
                c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (t, tid))
        for kind, detail in events:
            self.log_event(tid, kind, detail)

    def _vote_summary_locked(self, c, tid: str, version: int) -> dict:
        result: dict[str, dict] = {}
        for r in c.execute(
                "SELECT model, verdict, created_at, actor, revoked_at FROM reviews"
                " WHERE task_id=? AND version=? AND kind='vote'"
                " ORDER BY id DESC", (tid, version)):
            if r["model"] not in result:
                gone = bool(r["revoked_at"])
                result[r["model"]] = {"verdict": None if gone else r["verdict"],
                                      "at": r["created_at"], "actor": r["actor"],
                                      "revoked": gone}
        for m in MODELS:
            result.setdefault(m, {"verdict": None, "at": None, "actor": None,
                                 "revoked": False})
        result["all_approved"] = all(result[m]["verdict"] == "approve" for m in MODELS)
        result["any_rejected"] = any(result[m]["verdict"] == "reject" for m in MODELS)
        result["complete"] = all(result[m]["verdict"] is not None for m in MODELS)
        return result

    # ------------------------------------------------------------------ 人类审批
    def approve(self, tid: str, decision: str, note: str = "",
                actor: str = R.HUMAN_OWNER, expected_version: int | None = None) -> dict:
        """人类审批：整段收进一个 BEGIN IMMEDIATE 事务，版本条件更新（P0-2）。"""
        if decision not in ("approve", "reject", "return"):
            raise CouncilError("decision 只能是 approve / reject / return")
        if actor not in (R.HUMAN_OWNER,):
            raise CouncilError("只有人类所有者可以执行审批")
        target = {"approve": S.APPROVED, "reject": S.REJECTED, "return": S.RETURNED}[decision]
        t = now()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            version = task["current_version"]
            allowed = {S.PENDING_APPROVAL, S.REVIEW_PASSED}
            if decision == "return":
                allowed |= {S.IN_REVIEW}
            if task["status"] not in allowed:
                raise CouncilError(
                    f"当前状态「{S.label(task['status'])}」不允许该审批操作")
            if expected_version is not None and version != expected_version:
                raise CouncilError(
                    f"任务版本已变（当前 v{version}，请求基于 v{expected_version}）")
            if decision == "approve":
                votes = self._vote_summary_locked(c, tid, version)
                if not votes["complete"]:
                    raise CouncilError("当前版本尚未集齐两个模型的审查票")
                if not votes["all_approved"]:
                    raise CouncilError("双模型尚未对当前版本同时投 approve，不能批准")
                for m in MODELS:
                    if votes[m]["actor"] == R.EXECUTOR_DSH:
                        raise CouncilError("执行器不能参与审查投票")
            c.execute(
                "INSERT INTO approvals(task_id,version,decision,note,actor,evidence,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (tid, version, decision, scrub(note)[:2000], actor,
                 f"votes:v{version}", t))
            cur = c.execute(
                "UPDATE tasks SET status=?, updated_at=?, blocked_reason=NULL"
                " WHERE id=? AND current_version=? AND status=?",
                (target, t, tid, version, task["status"]))
            if cur.rowcount != 1:
                raise CouncilError("任务在审批过程中被修改，已拒绝本次审批")
            if decision != "approve":
                c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
                          (t, tid))
        self.db.audit(actor, f"approval.{decision}", tid, f"v{version} {note}".strip())
        self.log_event(tid, f"approval.{decision}", note)
        return self.get_task(tid)

    def latest_approval(self, tid: str, with_effective: bool = True) -> dict | None:
        """最近一条审批记录。

        历史记录永不删除（撤票后依然保留原始证据），所以这里额外派生一个
        `effective` 标记：只有「审批版本 == 当前版本」且任务状态仍处在审批之后
        的合法位置时才算生效。撤票退回议事中的任务会看到 effective=False。
        """
        row = self.db.one(
            "SELECT * FROM approvals WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,))
        if row is None:
            return None
        d = dict(row)
        if with_effective:
            task = self.db.one("SELECT status, current_version FROM tasks WHERE id=?", (tid,))
            d["effective"] = bool(
                task is not None
                and d["version"] == task["current_version"]
                and d["decision"] == "approve"
                and task["status"] in EFFECTIVE_APPROVAL_STATES)
        return d

    def approvals(self, tid: str) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM approvals WHERE task_id=? ORDER BY id DESC", (tid,))]

    # ------------------------------------------------------------------ 执行票
    def available_tasks(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id,title,risk,status,current_version,updated_at FROM tasks"
            " WHERE status=? AND cancel_requested_at IS NULL"
            " ORDER BY updated_at ASC LIMIT 20", (S.APPROVED,))
        return [dict(r) for r in rows]

    def _derive_ticket(self, ex_id: str, tid: str, version: int,
                       idem_key: str | None) -> str:
        if not self.key:
            raise CouncilError("服务端缺少执行票密钥，无法签发")
        return derive_ticket(self.key, ex_id, tid, version, idem_key)

    def claim(self, tid: str, executor: dict, idem_key: str | None = None) -> dict:
        """领取任务：发放执行票，返回不可变 Prompt。

        v0.1.1-rc3：
        - 幂等检查与业务写入在同一个事务（并发同键不会双发）；
        - 取消过的任务（cancel_requested 非空）一律不可领取；
        - 事务内复验执行器仍启用。
        """
        ex_id = (executor or {}).get("id")
        if not ex_id:
            raise CouncilError("缺少执行器身份")
        idem_scope_key = f"claim:{ex_id}:{tid}:{idem_key or '-'}"
        fp = request_fingerprint({"op": "claim", "task": tid, "ex": ex_id,
                                  "idem": idem_key or ""})
        t = now()
        with self.db.tx() as c:
            self._assert_executor_active(c, executor)
            if idem_key:
                cached = self._idem_get_locked(c, idem_scope_key, fp, for_update=True)
                if cached is not None:
                    return self._replay_claim(cached, tid, ex_id)
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            control = task.get("control") or {}
            if control.get("cancel_requested") or task.get("cancel_requested_at"):
                raise CouncilError("任务已被请求取消，不能领取")
            if task["status"] != S.APPROVED:
                raise CouncilError(
                    f"任务当前状态「{S.label(task['status'])}」不可领取")
            running = c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status IN (?,?,?) AND executor_id=?",
                (S.DISPATCHED, S.RUNNING, S.CANCELLING, ex_id)).fetchone()["n"]
            if running >= MAX_PARALLEL_PER_EXECUTOR:
                raise CouncilError(
                    f"同一执行器并行任务已达上限（{MAX_PARALLEL_PER_EXECUTOR}）")
            version = task["current_version"]
            # D：没有幂等键时必须引入随机 nonce 派生票。
            # 否则第一次领取 → 任务 BLOCKED → 重新批准 → 再次领取会算出
            # **完全相同的票**，旧票持有者可以拿历史票继续用（多轮执行漏洞）。
            ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)
            ticket = self._derive_ticket(ex_id, tid, version, ticket_nonce)
            ticket_id = ticket_id_for(ticket)
            epoch = self.db.restore_epoch()
            # 先做条件更新，抢不到就直接失败
            cur = c.execute(
                "UPDATE tasks SET status=?, executor_id=?, updated_at=?"
                " WHERE id=? AND status=? AND current_version=?"
                " AND cancel_requested_at IS NULL",
                (S.DISPATCHED, ex_id, t, tid, S.APPROVED, version))
            if cur.rowcount != 1:
                raise CouncilError("任务已被其他执行器领取")
            # 先吊销同任务所有仍有效的旧票，再插新票；不用 INSERT OR REPLACE
            # （REPLACE 会静默覆盖同 id 的历史票，丢掉吊销证据）。
            c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?"
                      " AND revoked_at IS NULL", (t, tid))
            c.execute(
                "INSERT INTO tickets(id,task_id,version,executor_id,token_hash,"
                "epoch,issued_at,expires_at,max_expires_at,renewed_at,revoked_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (ticket_id, tid, version, ex_id, hash_token(ticket), epoch, t,
                 min(t + self.ticket_ttl, t + self.ticket_max_ttl),
                 t + self.ticket_max_ttl, t))
            ver_row = c.execute(
                "SELECT prompt FROM task_versions WHERE task_id=? AND version=?",
                (tid, version)).fetchone()
            prompt = ver_row["prompt"] if ver_row else task["prompt"]
            if idem_key:
                self._idem_put(c, idem_scope_key, "claim", fp,
                               {"task_id": tid, "version": version,
                                "ticket_id": ticket_id, "nonce": ticket_nonce})
        self.log_event(tid, "ticket.issued", f"{ticket_id} -> {ex_id}")
        return {
            "task_id": tid,
            "version": version,
            "title": task["title"],
            "goal": task["goal"],
            "scope": task["scope"],
            "risk": task["risk"],
            "prompt": prompt,
            "control": task.get("control") or {},
            "ticket": ticket,
            "ticket_id": ticket_id,
            # R5：执行端用这个编号落「开工/完成收据」。重新派工会换新票，
            # 所以拿票号当派工号天然满足「旧收据不会被新收据覆盖」。
            "dispatch_id": ticket_id,
            "expires_at": min(t + self.ticket_ttl, t + self.ticket_max_ttl),
            "max_expires_at": t + self.ticket_max_ttl,
        }

    def _replay_claim(self, cached: dict, tid: str, ex_id: str) -> dict:
        """幂等重放：重新派生同一张票，内容按记录的版本取。

        R1 修复：重放**不能**无条件把票再交出去。审计发现旧实现只 get_task(tid)
        取当前值，既不查取消、也不查票是否已被吊销/恢复作废，导致「票早就被撤销
        了，客户端重试同一个幂等键还能拿到一张能用的票」。这里把票重新过一遍
        有效性校验，并且要求任务此刻仍归属该执行器且仍在执行中。
        """
        version = cached["version"]
        task = self.get_task(tid)
        if (task.get("cancel_requested_at")
                or (task.get("control") or {}).get("cancel_requested")):
            raise CouncilError("任务已被请求取消，本次重放不再签发执行票")
        if task["executor_id"] != ex_id:
            raise CouncilError("任务不属于该执行器，拒绝重放执行票")
        if task["status"] not in (S.DISPATCHED, S.RUNNING, S.CANCELLING):
            raise CouncilError(
                f"任务当前状态「{S.label(task['status'])}」，重放不再签发执行票")
        if int(task["current_version"]) != int(version):
            raise CouncilError("任务版本已变更，本次执行已作废，不能重放")
        trow = self.db.one("SELECT * FROM tickets WHERE id=?", (cached["ticket_id"],))
        if trow is None:
            raise CouncilError("执行票记录不存在，拒绝重放")
        self._check_ticket_row(dict(trow))
        ver_row = self.db.one(
            "SELECT prompt FROM task_versions WHERE task_id=? AND version=?", (tid, version))
        prompt = ver_row["prompt"] if ver_row else task["prompt"]
        # D：优先用记录里的 nonce（无幂等键时是随机值），旧记录回退到 _idem
        nonce = cached.get("nonce") or cached.get("_idem") or None
        ticket = self._derive_ticket(ex_id, tid, version, nonce or None)
        return {
            "task_id": tid,
            "version": version,
            "title": task["title"],
            "goal": task["goal"],
            "scope": task["scope"],
            "risk": task["risk"],
            "prompt": prompt,
            "control": task.get("control") or {},
            "ticket": ticket,
            "ticket_id": cached["ticket_id"],
            "dispatch_id": cached["ticket_id"],
            "expires_at": trow["expires_at"],
            "max_expires_at": trow["max_expires_at"],
            "replayed": True,
        }

    def _ticket_row_locked(self, c, token: str, executor: dict) -> dict:
        if not token:
            raise CouncilError("缺少执行票")
        row = c.execute(
            "SELECT * FROM tickets WHERE token_hash=? AND executor_id=?",
            (hash_token(token), (executor or {}).get("id"))).fetchone()
        if row is None:
            raise CouncilError("执行票无效")
        return dict(row)

    def _check_ticket_row(self, t: dict) -> dict:
        """票的有效性校验：撤销 / 过期 / 绝对上限 / 恢复世代。"""
        if t["revoked_at"]:
            raise CouncilError("执行票已被撤销")
        epoch = self.db.restore_epoch()
        if int(t.get("epoch") or 0) != epoch:
            raise CouncilError("执行票已失效（数据库已从备份恢复），请重新领取")
        t_now = now()
        if t["expires_at"] < t_now:
            raise CouncilError("执行票已过期，请重新领取")
        if t.get("max_expires_at") and t["max_expires_at"] < t_now:
            raise CouncilError("执行租约已达绝对上限，需重新领取")
        return t

    def _ticket_lookup(self, token: str, executor: dict) -> dict:
        return self._check_ticket_row(self._ticket_row_locked(self.db.conn, token, executor))

    def _require_executable_task(self, c, tid: str, version: int,
                                 executor: dict | None = None) -> dict:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise CouncilError("任务不存在")
        task = _task_dict(row)
        if task["status"] in (S.BLOCKED, S.CANCELLED, S.DONE, S.REJECTED):
            raise CouncilError(
                f"任务当前状态「{S.label(task['status'])}」不接受执行操作")
        if executor is not None and task.get("executor_id") != executor.get("id"):
            raise CouncilError("任务不属于该执行器")
        if version is not None and task["current_version"] != version:
            raise CouncilError("任务版本已变更，本次执行作废")
        return task

    def heartbeat(self, token: str, executor: dict, status: str = "running",
                  detail: str = "", idem_key: str | None = None) -> dict:
        """心跳：续租 + 幂等（同一幂等键重复请求返回完全一致的结果）。"""
        if status not in ("running", "paused"):
            raise CouncilError("status 只能是 running 或 paused")
        h = hash_token(token or "")
        with self.db.tx() as c:
            # R4：时间戳必须在**拿到写锁之后**取。放在 tx 之外的话，等锁期间
            # （最多 busy_timeout=5s，并发高时更久）算出的 t 已经过期，续租
            # 会写出一个「比现在还早」的 expires_at，等于刚续完就超时。
            t = now()
            self._assert_executor_active(c, executor)
            row = c.execute(
                "SELECT * FROM tickets WHERE token_hash=? AND executor_id=?",
                (h, (executor or {}).get("id"))).fetchone()
            if row is None:
                raise CouncilError("执行票无效")
            ticket = self._check_ticket_row(dict(row))
            fp = request_fingerprint({"op": "heartbeat", "status": status,
                                      "detail": detail or "", "ticket": ticket["id"]})
            idem_scope_key = f"hb:{ticket['id']}:{idem_key or '-'}"
            if idem_key:
                cached = self._idem_get_locked(c, idem_scope_key, fp, for_update=True)
                if cached is not None:
                    return dict(cached, replayed=True)
            tid = ticket["task_id"]
            self._require_executable_task(c, tid, ticket["version"], executor)
            run = c.execute(
                "SELECT * FROM runs WHERE ticket_id=? ORDER BY started_at DESC LIMIT 1",
                (ticket["id"],)).fetchone()
            if run is None:
                run_id = "run-" + uuid.uuid4().hex[:10]
                c.execute(
                    "INSERT INTO runs(id,task_id,ticket_id,version,executor_id,status,"
                    "started_at,last_beat_at) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, tid, ticket["id"], ticket["version"], executor["id"], status, t, t))
            else:
                run_id = run["id"]
                c.execute("UPDATE runs SET last_beat_at=?, status=?, finished_at=NULL WHERE id=?",
                          (t, status, run_id))
            # 续租：只延长到 min(now+lease, max_expires_at)
            new_exp = min(t + self.heartbeat_lease,
                          ticket.get("max_expires_at") or (t + self.ticket_max_ttl))
            c.execute("UPDATE tickets SET expires_at=?, renewed_at=? WHERE id=?",
                      (new_exp, t, ticket["id"]))
            c.execute(
                "UPDATE tasks SET status=CASE WHEN status=? THEN ? ELSE status END,"
                " updated_at=? WHERE id=?", (S.DISPATCHED, S.RUNNING, t, tid))
            if detail:
                c.execute("INSERT INTO events(task_id,kind,detail,created_at) VALUES(?,?,?,?)",
                          (tid, "run.heartbeat", f"{status} {scrub(detail)}"[:4000], t))
            task2 = _task_dict(c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            result = {
                "ok": True, "task_id": tid, "run_id": run_id, "server_time": t,
                "task_status": task2["status"],
                "heartbeat_ttl": self.heartbeat_ttl,
                "lease_expires_at": new_exp,
                "max_expires_at": ticket.get("max_expires_at"),
                "cancel": bool(task2["control"].get("cancel_requested")),
                "pause": bool(task2["control"].get("pause_requested")),
                "cancel_ack_required": task2["status"] == S.CANCELLING,
            }
            if idem_key:
                self._idem_put(c, idem_scope_key, "heartbeat", fp, result)
        return result

    def submit_result(self, token: str, executor: dict, status: str, log: str = "",
                      diff: str = "", tests: str = "", note: str = "",
                      idem_key: str | None = None) -> dict:
        """执行器回传结果。status: completed / failed / blocked / cancelled。

        cancelled 是「接受取消指令后确认已停止」的唯一合法路径（P1-2.1）。
        """
        if status not in SUBMIT_STATUSES:
            raise CouncilError("status 只能是 completed / failed / blocked / cancelled")
        h = hash_token(token or "")
        with self.db.tx() as c:
            t = now()          # R4：时间戳必须在持锁之后取，理由同 heartbeat
            self._assert_executor_active(c, executor)
            tk_row = c.execute(
                "SELECT * FROM tickets WHERE token_hash=? AND executor_id=?",
                (h, (executor or {}).get("id"))).fetchone()
            if tk_row is None:
                raise CouncilError("执行票无效")
            ticket = dict(tk_row)
            fp = request_fingerprint({"op": "submit", "status": status, "note": note or "",
                                      "ticket": ticket["id"],
                                      "log": hash_token(log or "")[:16],
                                      "diff": hash_token(diff or "")[:16],
                                      "tests": hash_token(tests or "")[:16]})
            idem_scope_key = f"submit:{ticket['id']}:{idem_key or '-'}"
            if idem_key:
                # 幂等重放必须先于票有效性校验：首次提交会吊销票据（P1-3）
                cached = self._idem_get_locked(c, idem_scope_key, fp, for_update=True)
                if cached is not None:
                    return dict(cached)
            ticket = self._check_ticket_row(ticket)
            tid = ticket["task_id"]
            task = self._require_executable_task(c, tid, ticket["version"], executor)
            if status == "cancelled":
                if task["status"] != S.CANCELLING:
                    raise CouncilError(
                        f"当前状态「{S.label(task['status'])}」不是取消中，不能提交取消确认")
            elif task["status"] not in (S.DISPATCHED, S.RUNNING, S.BLOCKED):
                raise CouncilError(
                    f"任务当前状态「{S.label(task['status'])}」不接受结果回传")
            run = c.execute(
                "SELECT id FROM runs WHERE ticket_id=? ORDER BY started_at DESC LIMIT 1",
                (ticket["id"],)).fetchone()
            if run is None:
                run_id = "run-" + uuid.uuid4().hex[:10]
                c.execute(
                    "INSERT INTO runs(id,task_id,ticket_id,version,executor_id,status,"
                    "started_at,last_beat_at) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, tid, ticket["id"], ticket["version"], executor["id"], status, t, t))
            else:
                run_id = run["id"]
            c.execute("UPDATE runs SET status=?, finished_at=?, last_beat_at=?, exit_note=?"
                      " WHERE id=?", (status, t, t, scrub(note)[:500], run_id))
            for kind, content in (("log", log), ("diff", diff), ("tests", tests), ("note", note)):
                if content:
                    c.execute(
                        "INSERT INTO artifacts(run_id,kind,content,created_at) VALUES(?,?,?,?)",
                        (run_id, kind, scrub(content)[:200000], t))
            if status == "cancelled":
                new_status = S.CANCELLED
                reason = scrub(note)[:500] or "执行器已确认停止"
            elif status == "completed":
                new_status = S.SUBMITTED
                reason = None
            else:
                new_status = S.BLOCKED
                reason = scrub(note)[:500] or f"执行器回报 {status}"
            c.execute(
                "UPDATE tasks SET status=?, updated_at=?, blocked_reason=? WHERE id=?"
                " AND current_version=?", (new_status, t, reason, tid, ticket["version"]))
            c.execute("UPDATE tickets SET revoked_at=? WHERE id=?", (t, ticket["id"]))
            result = {"ok": True, "task_id": tid, "run_id": run_id, "task_status": new_status}
            if idem_key:
                self._idem_put(c, idem_scope_key, "submit", fp, result)
        self.log_event(tid, "run.submitted", f"{status} {note}".strip())
        return result

    def runs(self, tid: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC", (tid,))
        out = []
        for r in rows:
            d = dict(r)
            d["artifacts"] = self.artifacts(d["id"])
            out.append(d)
        return out

    def artifacts(self, run_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self.db.query(
                "SELECT kind, content FROM artifacts WHERE run_id=? ORDER BY id", (run_id,)):
            out[r["kind"]] = (out.get(r["kind"], "") + "\n" + r["content"]).strip()
        return out

    # ------------------------------------------------------------------ 本地控制
    def request_pause(self, tid: str, pause: bool = True) -> dict:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            ctl = _task_dict(row)["control"]
            ctl["pause_requested"] = pause
            c.execute("UPDATE tasks SET control=?, updated_at=? WHERE id=?",
                      (dumps(ctl), now(), tid))
        self.log_event(tid, "control.pause", str(pause))
        return self.get_task(tid)

    def request_cancel(self, tid: str, actor: str = R.HUMAN_OWNER) -> dict:
        """请求取消（P1-2.1）。

        - 尚未派工（DRAFT/待审/已批准等）：立即 CANCELLED，并吊销所有执行票；
        - 已派工（DISPATCHED/RUNNING）：进 CANCELLING，票保留给执行器读到取消指令
          并回传确认；执行器失联则由看门狗置 BLOCKED；
        - 重复请求幂等，不产生重复审计。
        """
        already = False
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            status = task["status"]
            if status in S.TERMINAL:
                raise CouncilError("任务已结束")
            if status == S.CANCELLING:
                already = True
                target = S.CANCELLING
            elif status in S.CANCEL_NEEDS_ACK:
                target = S.CANCELLING
            else:
                target = S.CANCELLED
            ctl = task["control"]
            ctl["cancel_requested"] = True
            t = now()
            cur = c.execute(
                "UPDATE tasks SET control=?, status=?, cancel_requested_at=?, updated_at=?"
                " WHERE id=? AND status=?",
                (dumps(ctl), target, t, t, tid, status))
            if cur.rowcount != 1:
                raise CouncilError("任务状态已被其他操作修改，请刷新后重试")
            if target == S.CANCELLED:
                c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
                          (t, tid))
        if not already:
            self.db.audit(actor, "task.cancelled" if target == S.CANCELLED
                          else "task.cancelling", tid,
                          f"原状态 {S.label(status)}")
            self.log_event(tid, "control.cancel",
                           f"人类请求取消（{S.label(status)} → {S.label(target)}）")
        return self.get_task(tid)

    def confirm_cancel(self, token: str, executor: dict, note: str = "",
                       idem_key: str | None = None) -> dict:
        """执行器确认已按取消指令停止（P1-2.1 的确认环节）。"""
        return self.submit_result(token, executor, "cancelled", note=note,
                                  idem_key=idem_key)

    def verify(self, tid: str, accept: bool, note: str = "") -> dict:
        target = S.DONE if accept else S.RETURNED
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            # D：验收通过只允许 SUBMITTED（执行器真的跑完了）。
            # BLOCKED 是「副作用不明」，只能退回重做或人工作废，
            # 不能靠点一下「通过」把它变成 DONE 掩盖掉。
            allowed = (S.SUBMITTED,) if accept else (S.SUBMITTED, S.BLOCKED)
            if task["status"] not in allowed:
                raise CouncilError(
                    f"当前状态「{S.label(task['status'])}」不允许"
                    + ("验收通过" if accept else "验收退回"))
            cur = c.execute(
                "UPDATE tasks SET status=?, updated_at=?, blocked_reason=? WHERE id=?"
                " AND current_version=? AND status=?",
                (target, now(), None if accept else (note or "验收未通过"),
                 tid, task["current_version"], task["status"]))
            if cur.rowcount != 1:
                raise CouncilError("任务状态已被修改，请刷新后重试")
        self.db.audit(R.HUMAN_OWNER, "task.verified", tid,
                      f"{'accept' if accept else 'reject'} {note}".strip())
        self.log_event(tid, "task.verify", f"{'通过' if accept else '不通过'} {note}".strip())
        return self.get_task(tid)

    def block(self, tid: str, reason: str) -> dict:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CouncilError("任务不存在")
            task = _task_dict(row)
            if task["status"] in S.TERMINAL:
                raise CouncilError("任务已结束")
            c.execute("UPDATE tasks SET status=?, blocked_reason=?, updated_at=? WHERE id=?",
                      (S.BLOCKED, scrub(reason)[:500], now(), tid))
            c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
                      (now(), tid))
        self.log_event(tid, "task.blocked", reason)
        return self.get_task(tid)

    def sweep_timeouts(self) -> list[dict]:
        """看门狗：执行失联 → BLOCKED。

        v0.1.1-rc3：判断与更新在**同一个事务**里，并对状态做条件更新，
        所以「心跳刚续租」不会被事务外读到的旧时间戳误杀（P1-5.1）。
        """
        blocked: list[dict] = []
        cutoff = now() - self.heartbeat_ttl
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT * FROM tasks WHERE status IN (?,?,?) ORDER BY updated_at",
                (S.DISPATCHED, S.RUNNING, S.CANCELLING)).fetchall()
            for r in rows:
                task = dict(r)
                # D：心跳必须按「当前这张活票」查，不能按 task_id 取最新一条 run。
                # 多轮执行后同一任务会有多条 run，按 task_id 取会读到历史 run 的
                # 心跳时间，把真正失联的当前执行误判成还活着。
                tk = c.execute(
                    "SELECT id FROM tickets WHERE task_id=? AND revoked_at IS NULL"
                    " ORDER BY issued_at DESC LIMIT 1", (task["id"],)).fetchone()
                if tk is not None:
                    run = c.execute(
                        "SELECT last_beat_at FROM runs WHERE ticket_id=?"
                        " ORDER BY started_at DESC LIMIT 1", (tk["id"],)).fetchone()
                else:
                    run = None
                last = (run["last_beat_at"] if run and run["last_beat_at"]
                        else task["updated_at"])
                if task["status"] == S.CANCELLING:
                    last = max(last, task.get("cancel_requested_at") or 0)
                    if last >= cutoff:
                        continue
                    reason = "取消请求未被执行器确认（疑似失联），副作用不明确，需人工确认"
                else:
                    if last >= cutoff:
                        continue
                    reason = "执行器心跳超时，副作用不明确，需人工确认"
                t = now()
                cur = c.execute(
                    "UPDATE tasks SET status=?, blocked_reason=?, updated_at=?"
                    " WHERE id=? AND status=?", (S.BLOCKED, reason, t, task["id"],
                                                 task["status"]))
                if cur.rowcount != 1:
                    continue          # 期间被心跳/回传改过，本次不处理
                c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?"
                          " AND revoked_at IS NULL", (t, task["id"]))
                blocked.append({"task_id": task["id"], "title": task["title"],
                                "reason": reason})
        for item in blocked:
            self.log_event(item["task_id"], "task.blocked", item["reason"])
        return blocked

    def start_watchdog(self, interval: float | None = None) -> Watchdog:
        self._watchdog = Watchdog.start_for(
            self, interval if interval is not None else 10.0)
        return self._watchdog

    def stop_watchdog(self) -> None:
        """只停「本实例启动的那一个」，绝不误杀别人的看门狗（A.4）。

        历史缺陷：这里无条件 Watchdog.stop_for(db_path)，导致 create_app(
        watchdog_interval=None) 的 lifespan 收尾时会停掉外部（GUI/CLI）启动的
        看门狗——测试里看不出问题，真机上表现为「服务一关，看门狗也没了」。
        """
        mine = self._watchdog
        self._watchdog = None
        if mine is None:
            return
        with Watchdog._instances_lock:
            key = Watchdog.key_for(self.db.path)
            if Watchdog._instances.get(key) is mine:
                Watchdog._instances.pop(key, None)
        mine.stop()

    # ------------------------------------------------------------------ 幂等
    def _idem_get_locked(self, c, key: str, fingerprint: str | None = None,
                         for_update: bool = False) -> dict | None:
        """事务内读取幂等记录：检查与写入同一事务，杜绝并发双发（P1-5.2）。"""
        sql = ("SELECT response, request_hash, expires_at FROM idempotency WHERE key=?"
               + (" LIMIT 1" if not for_update else ""))
        row = c.execute(sql, (key,)).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < now():
            c.execute("DELETE FROM idempotency WHERE key=?", (key,))
            return None
        if fingerprint is not None and row["request_hash"] and row["request_hash"] != fingerprint:
            raise CouncilError("同一幂等键被用于不同请求，已拒绝")
        data = loads(row["response"]) or {}
        # 键形如 claim:{ex}:{task}:{idem}，而 idem 本身可能含冒号（客户端自定义前缀），
        # 所以只能 maxsplit=3，不能全量 split 后取 parts[3]（会截断，重放派生出别的票）。
        parts = key.split(":", 3)
        if len(parts) == 4 and parts[0] == "claim":
            data.setdefault("_idem", "" if parts[3] == "-" else parts[3])
        return data

    def _idem_get(self, key: str, fingerprint: str | None = None) -> dict | None:
        return self._idem_get_locked(self.db.conn, key, fingerprint)

    def _idem_put(self, conn, key: str, scope: str, fingerprint: str,
                  response: dict, ttl: int = 86400) -> None:
        assert_no_secrets(response)
        conn.execute(
            "INSERT OR REPLACE INTO idempotency(key,scope,request_hash,response,created_at,"
            "expires_at) VALUES(?,?,?,?,?,?)",
            (key, scope, fingerprint, dumps(sanitize(response)), now(), now() + ttl))

    def purge_idempotency(self, limit: int = 500) -> int:
        """清理过期幂等记录（只清过期，不动仍需重试的有效记录，P2-7.2）。"""
        t = now()
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT key FROM idempotency WHERE expires_at IS NOT NULL AND expires_at < ?"
                " ORDER BY expires_at LIMIT ?", (t, limit)).fetchall()
            for r in rows:
                c.execute("DELETE FROM idempotency WHERE key=?", (r["key"],))
        n = len(rows)
        if n:
            self.db.audit(R.HUMAN_OWNER, "idempotency.purged", "idempotency", f"删除 {n} 条")
        return n

    # ------------------------------------------------------------------ 导出/统计
    def overview(self) -> dict:
        counts = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
        active = self.db.query(
            "SELECT * FROM tasks WHERE status IN (?,?,?) ORDER BY updated_at DESC LIMIT 5",
            (S.DISPATCHED, S.RUNNING, S.CANCELLING))
        waiting = self.db.query(
            "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT 5",
            (S.PENDING_APPROVAL,))
        blocked = self.db.query(
            "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT 5",
            (S.BLOCKED,))
        return {
            "counts": counts,
            "active": [_task_dict(r) for r in active],
            "waiting_approval": [_task_dict(r) for r in waiting],
            "blocked": [_task_dict(r) for r in blocked],
            "total": sum(counts.values()),
        }

    def export_bundle(self, path) -> str:
        """一致性备份（走 sqlite3 backup API，见 backup.export_bundle）。"""
        return str(_export_bundle(self.db, path))

    def export_audit_bundle(self, path) -> str:
        """可安全外发的脱敏审计包（不含 token 哈希/执行票/幂等响应）。"""
        return str(_export_audit_bundle(self.db, path))

    def restore_from_bundle(self, path, before_replace=None) -> dict:
        """从备份恢复，并把轮换后的新密钥装载回本实例（P1-4.1 / P1-4.2）。

        before_replace 在关连接之前调用：调用方用它暂停 API 与看门狗，
        避免「库正在被替换但还有线程在写」。
        """
        from .backup import restore_bundle as _restore
        from .security import load_or_create_key
        result = _restore(self.db, path, key_path=self.key_path,
                          before_replace=before_replace)
        if self.key_path is not None and result.get("key_rotated"):
            self.key = load_or_create_key(self.key_path)
        return result


TICKET_PREFIX = "bfc_tk_"


def assert_no_secrets(obj, path: str = "$") -> None:
    """守卫：幂等记录/普通存储绝不允许出现凭据明文或 Prompt 正文。"""
    from .security import contains_secret
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_no_secrets(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_no_secrets(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        if contains_secret(obj) or path.rsplit(".", 1)[-1] == "prompt":
            raise CouncilError(f"拒绝持久化敏感字段：{path}")
