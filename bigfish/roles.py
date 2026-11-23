"""四角色权限模型（P2-2）。

角色绑定到数据库 executors.role / executors.model 列，不依赖执行器名称字符串，
所以改名不能提权（P2-3 / P2-6.3）。
"""
from __future__ import annotations

ARCHITECT_GPT = "architect_gpt"
REVIEWER_GEMINI = "reviewer_gemini"
HUMAN_OWNER = "human_owner"
EXECUTOR_DSH = "executor_dsh"

# BFC-RC7: mobile end (remote chat roaming / read-only watching). Lower than executor:
# cannot claim, heartbeat, or submit. Naming deliberately avoids executor.
MOBILE_VIEWER = "mobile_viewer"

ALL_ROLES = (ARCHITECT_GPT, REVIEWER_GEMINI, HUMAN_OWNER, EXECUTOR_DSH,
             MOBILE_VIEWER)
# 兼容旧名字（v0.1.0 起的测试与文档用的是 ROLES）
ROLES = ALL_ROLES

ROLE_LABELS = {
    ARCHITECT_GPT: "架构（GPT）",
    REVIEWER_GEMINI: "审查（Gemini）",
    HUMAN_OWNER: "人类所有者",
    EXECUTOR_DSH: "执行器（VPS DSH）",
    MOBILE_VIEWER: "手机端（只读）",
}

# 角色 -> 绑定的模型；None 表示不涉及模型身份
REVIEWER_MODEL = {ARCHITECT_GPT: "gpt", REVIEWER_GEMINI: "gemini"}

# 权限名沿用 v0.1.0 起的点分命名；服务端 _need() 与本表共用同一套字符串
PERMISSIONS = {
    ARCHITECT_GPT: {"review.cast", "task.read", "audit.read"},
    REVIEWER_GEMINI: {"review.cast", "task.read", "audit.read"},
    HUMAN_OWNER: {
        "task.create", "task.edit", "task.review_start", "task.read",
        "approve", "task.cancel", "task.verify", "executor.manage",
        "backup.export", "backup.restore", "audit.read", "review.cast",
        "idempotency.purge", "ticket.issue",
    },
    EXECUTOR_DSH: {"task.claim", "run.heartbeat", "run.submit", "task.read.own"},
    # 只读 + 可留言。刻意不含 task.claim / run.* / approve / backup.*：
    # 手机丢了也不至于能把任务领走或把库导出去。
    MOBILE_VIEWER: {"task.read", "event.read", "review.create"},
}

# 数据库 executors.role -> 业务角色。
# rc7：mobile 已显式登记为 MOBILE_VIEWER。仍然保留这条警告——未知 db_role
# 会落到 EXECUTOR_DSH，即「没登记 = 拿到执行器权限」。新增角色时必须同时改
# ALL_ROLES / PERMISSIONS / _DB_ROLE_MAP 三处，缺一处就是提权。
# register_executor() 的白名单是第二道，只接受 DB_ROLES 里的值。
_DB_ROLE_MAP = {"executor": EXECUTOR_DSH, "reviewer": None, "admin": HUMAN_OWNER,
                "mobile": MOBILE_VIEWER}

# 允许出现在 executors.role 列里的取值（白名单，注册时校验）
DB_ROLES = frozenset(_DB_ROLE_MAP)


def can(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, set())


def require(role: str, permission: str, action: str = "") -> None:
    """越权时抛 PermissionError。action 用于把错误文案说人话。"""
    if not can(role, permission):
        what = action or permission
        raise PermissionError(f"角色「{ROLE_LABELS.get(role, role)}」无权{what}")


def model_for_role(role: str) -> str | None:
    """该角色被绑定到的模型；不是审查角色时返回 None。"""
    return REVIEWER_MODEL.get(role)


def role_for_db_row(db_role: str, name: str = "", model: str | None = None) -> str:
    """把数据库 executors.role(+model) 映射到业务角色。

    审查身份以 executors.model 列为准（注册时写入，不可由改名改变）；
    model 为空时按旧的名称规则推断，仅用于兼容 v0.1.1-rc2 之前的数据。
    """
    base = _DB_ROLE_MAP.get(db_role)
    # base 可能是 None（reviewer 要看 model），所以不能只判 base
    if db_role in _DB_ROLE_MAP and base is not None:
        return base
    if db_role == "reviewer":
        m = (model or "").strip().lower()
        if m not in ("gpt", "gemini"):
            low = (name or "").lower()
            m = "gpt" if ("gpt" in low and "gemini" not in low) else "gemini"
        return ARCHITECT_GPT if m == "gpt" else REVIEWER_GEMINI
    # 未知角色一律按最低权限处理
    return EXECUTOR_DSH
