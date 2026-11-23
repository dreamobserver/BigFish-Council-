"""任务状态机.

沿用 Taskboard v2.1 安全设计：
- 审查评论（comment）与正式投票（vote）分离；
- 必须 GPT + Gemini 对同一版本都投 approve 才进入 REVIEW_PASSED；
- 人类批准（approve）之后才允许派工（issue ticket）；
- 未通过人工验收不能进入 DONE；
- 执行失联且副作用不明确 → BLOCKED；
- 任务板离线期间不生成新执行授权（claim 时严格校验状态 + 票有效期）。

v0.1.1-rc3 增加 CANCELLING：
- 取消请求在「尚未派工」的状态下立即生效（→ CANCELLED，同时吊销执行票）；
- 已派工（DISPATCHED/RUNNING）不能只标 CANCELLED 就当远程停了：先进 CANCELLING，
  等执行器下一次心跳读到 cancel=True 并用 status=cancelled 回传确认，才真正 CANCELLED；
- 执行器失联则由看门狗把 CANCELLING 置为 BLOCKED（副作用不明确，交人工确认）。
"""
from __future__ import annotations

DRAFT = "DRAFT"
IN_REVIEW = "IN_REVIEW"
REVIEW_PASSED = "REVIEW_PASSED"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
DISPATCHED = "DISPATCHED"
RUNNING = "RUNNING"
CANCELLING = "CANCELLING"
SUBMITTED = "SUBMITTED"
DONE = "DONE"
BLOCKED = "BLOCKED"
RETURNED = "RETURNED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"

ALL = [
    DRAFT, IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL, APPROVED,
    DISPATCHED, RUNNING, CANCELLING, SUBMITTED, DONE, BLOCKED, RETURNED,
    REJECTED, CANCELLED,
]

TERMINAL = {DONE, REJECTED, CANCELLED}

# 只有这些状态允许生成执行票
CLAIMABLE = {APPROVED}

# 允许执行器心跳/回传的状态
EXECUTOR_ACTIVE = {DISPATCHED, RUNNING, CANCELLING}

# 取消请求可以「立即完成」的状态：还没有任何执行授权在外
CANCEL_IMMEDIATE = {DRAFT, IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL, APPROVED,
                    RETURNED, SUBMITTED, BLOCKED}

# 取消请求需要执行器确认的状态：执行授权已经发出去，不能单方面宣布停止
CANCEL_NEEDS_ACK = {DISPATCHED, RUNNING}

# 派工后仍在外部执行的状态（看门狗需要照看）
RUNNING_STATES = {DISPATCHED, RUNNING, CANCELLING}

# 审批有效性依赖审查票的状态：撤票必须重新评估
REVIEW_GATED = {IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL, APPROVED}

LABELS = {
    DRAFT: "草稿",
    IN_REVIEW: "议事中",
    REVIEW_PASSED: "双审通过",
    PENDING_APPROVAL: "待人类审批",
    APPROVED: "已批准/待派工",
    DISPATCHED: "已派工",
    RUNNING: "执行中",
    CANCELLING: "取消中（待执行器确认）",
    SUBMITTED: "待验收",
    DONE: "已完成",
    BLOCKED: "已阻断",
    RETURNED: "退回修改",
    REJECTED: "已拒绝",
    CANCELLED: "已取消",
}


class StateError(RuntimeError):
    """非法状态迁移."""


def label(status: str) -> str:
    return LABELS.get(status, status)
