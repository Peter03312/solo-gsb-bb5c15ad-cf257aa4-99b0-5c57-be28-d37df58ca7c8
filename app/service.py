"""Business logic: license registry, quota pools, revocation and idempotent verification."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator, Optional

from .db import connect, init_schema
from .timeutil import now_iso, parse_iso, to_iso

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

REASON_NO_LICENSE = "NO_LICENSE"             # 该影片+场馆没有任何授权
REASON_TIME_OUT_OF_WINDOW = "TIME_OUT_OF_WINDOW"  # 有授权但开始时间不在任何有效时段内
REASON_LICENSE_REVOKED = "LICENSE_REVOKED"   # 覆盖该时段的授权均已撤销
REASON_LICENSE_SUSPENDED = "LICENSE_SUSPENDED"  # 覆盖该时段且未撤销的授权全部处于暂停中
REASON_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"   # 授权有效但场次额度已用完
REASON_POOL_EXHAUSTED = "POOL_EXHAUSTED"     # 授权自身有余额，但所绑影片级共享池已耗尽

# 开场码领取 / 核销的业务错误码（422，未产生码/回执）
CODE_REASON_NOT_APPROVED = "VERIFICATION_NOT_APPROVED"  # 流水已拒绝，无开场码
CODE_REASON_ALREADY_STARTED = "SESSION_ALREADY_STARTED"  # 场次已过计划开场时刻，不可再领
CODE_REASON_CANCELLED = "SESSION_CANCELLED"  # 场次已取消，码不可领/不可核销，已领码立即失效
REDEEM_REASON_VENUE_MISMATCH = "VENUE_MISMATCH"  # 核销场馆与核准场馆不一致
REDEEM_REASON_TOO_EARLY = "REDEMPTION_TOO_EARLY"  # 早于计划开场时刻
REDEEM_REASON_WINDOW_CLOSED = "REDEMPTION_WINDOW_CLOSED"  # 超过开场后 30 分钟

# 核销窗口：自计划开场时刻起 30 分钟，左闭右开 [start, start+30min)
REDEMPTION_WINDOW = timedelta(minutes=30)

# 改签（reschedule）门槛错误码（422，不产生改签流水）
RESCHEDULE_REASON_NOT_APPROVED = "VERIFICATION_NOT_APPROVED"  # 原核验为拒绝，无核准场次
RESCHEDULE_REASON_CANCELLED = "SESSION_CANCELLED"            # 场次已取消，不可改签
RESCHEDULE_REASON_STARTED = "SESSION_ALREADY_STARTED"        # 当前开场时刻已到，不可改签
RESCHEDULE_REASON_CODE_ISSUED = "OPENING_CODE_ALREADY_ISSUED"  # 已领开场码，不可改签
RESCHEDULE_REASON_PAST_TARGET = "NEW_START_NOT_IN_FUTURE"    # 新开场时刻不晚于当前时刻
RESCHEDULE_REASON_SAME_TARGET = "NEW_START_UNCHANGED"        # 新开场时刻与当前排片相同

# 实体拷贝：改签特有的拒绝原因（落 decision='rejected' 改签流水，排片/分配/额度不变）
REASON_COPY_CONFLICT = "COPY_CONFLICT"  # 新时段与同拷贝既有占用冲突（重叠/转运不足）

# 实体拷贝登记 / 分配的业务错误码
COPY_REASON_HAS_APPROVED = "FILM_HAS_APPROVED_VERIFICATIONS"  # 影片已有核准流水，不可再登记拷贝
COPY_REASON_FILM_MISMATCH = "COPY_FILM_MISMATCH"      # 拷贝与场次不同片
COPY_REASON_ASSIGNMENT_REQUIRED = "COPY_ASSIGNMENT_REQUIRED"  # 已配置拷贝的影片领码前须先分配
COPY_CODE_CONFLICT = "COPY_CONFLICT"                  # 分配与既有占用冲突（409，不落账）
COPY_CODE_NOT_FOUND = "COPY_NOT_FOUND"                # 拷贝编号未知（404）

# 已取消的核准流水不再占用授权/池额度：取消登记后实时已用次数回退，
# 释放出的额度可被后续核验使用；原核验行及其快照保持原样。
_ACTIVE_APPROVED = """
    v.decision = 'approved'
    AND NOT EXISTS (
        SELECT 1 FROM cancellations c WHERE c.verification_id = v.id
    )
"""

# 当前生效归属由"最近一条成功改签"派生（无成功改签则回退到原核验行）。
# 改签在口径上原子等价于"原授权/池各释放一次、新授权/池各占用一次"，
# 因此实时已用次数统一按当前生效授权/池统计，原核验行保持原样可追溯。
# 以下片段引用外层核验行别名 v，且须能访问 reschedules 表。
_EFFECTIVE_LICENSE_EXPR = """
    COALESCE((
        SELECT r.to_license_id FROM reschedules r
        WHERE r.verification_id = v.id AND r.decision = 'approved'
        ORDER BY r.id DESC LIMIT 1
    ), v.license_id)
"""
_EFFECTIVE_POOL_EXPR = """
    COALESCE((
        SELECT r.to_pool_id FROM reschedules r
        WHERE r.verification_id = v.id AND r.decision = 'approved'
        ORDER BY r.id DESC LIMIT 1
    ), v.pool_id)
"""
# 当前排片的开场时刻：取最近一次成功改签的新时刻，否则为原核验时刻
_EFFECTIVE_START_EXPR = """
    COALESCE((
        SELECT r.to_start_time FROM reschedules r
        WHERE r.verification_id = v.id AND r.decision = 'approved'
        ORDER BY r.id DESC LIMIT 1
    ), v.start_time)
"""

_LICENSE_SELECT = """
SELECT
    l.id,
    l.film_id,
    l.venue_id,
    l.valid_from,
    l.valid_to,
    l.max_sessions,
    l.pool_id,
    l.note,
    l.created_at,
    p.film_id AS pool_film_id,
    p.max_sessions AS pool_max_sessions,
    p.note AS pool_note,
    p.created_at AS pool_created_at,
    CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS revoked,
    r.reason     AS revoke_reason,
    r.revoked_at AS revoked_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE {eff_lic} = l.id AND {active}
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE {eff_pool} = l.pool_id AND {active}
    ) AS pool_used_sessions
FROM licenses l
LEFT JOIN revocations r ON r.license_id = l.id
LEFT JOIN quota_pools p ON p.id = l.pool_id
""".format(eff_lic=_EFFECTIVE_LICENSE_EXPR, eff_pool=_EFFECTIVE_POOL_EXPR,
           active=_ACTIVE_APPROVED)

_POOL_SELECT = f"""
SELECT
    p.id,
    p.film_id,
    p.max_sessions,
    p.note,
    p.created_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE {_EFFECTIVE_POOL_EXPR} = p.id AND {_ACTIVE_APPROVED}
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM licenses l
        WHERE l.pool_id = p.id
    ) AS license_count
FROM quota_pools p
"""

# 核验查询统一左联取消记录：取消状态（cancelled/cancellation）随流水返回，
# 原核验决定与扣减时余额快照字段保持原样。
_VERIFICATION_SELECT = """
SELECT
    v.*,
    c.id           AS cancel_id,
    c.reason       AS cancel_reason,
    c.cancelled_at AS cancelled_at
FROM verifications v
LEFT JOIN cancellations c ON c.verification_id = v.id
"""

# 当前排片视图：在原核验/取消字段之外，再左联"最近一次成功改签"（lr）以及
# 改签计数（rc）。GET /verifications 用它返回当前生效的开场时刻、授权/池占用
# 与新占用后的剩余快照；原核验行字段（v.* 中的 start_time/license_id/...）
# 仍是首次核验的不可变决定与快照，另以 original_* 字段暴露。
_CURRENT_VERIFICATION_SELECT = """
SELECT
    v.*,
    c.id           AS cancel_id,
    c.reason       AS cancel_reason,
    c.cancelled_at AS cancelled_at,
    lr.id            AS last_reschedule_id,
    lr.operation_key AS last_operation_key,
    lr.from_start_time AS last_from_start_time,
    lr.to_start_time AS last_to_start_time,
    lr.from_license_id AS last_from_license_id,
    lr.to_license_id AS last_to_license_id,
    lr.from_pool_id  AS last_from_pool_id,
    lr.to_pool_id    AS last_to_pool_id,
    lr.new_remaining_quota AS last_new_remaining_quota,
    lr.new_pool_remaining_quota AS last_new_pool_remaining_quota,
    lr.rescheduled_at AS last_rescheduled_at,
    (
        SELECT COUNT(*) FROM reschedules r
        WHERE r.verification_id = v.id AND r.decision = 'approved'
    ) AS reschedule_count
FROM verifications v
LEFT JOIN cancellations c ON c.verification_id = v.id
LEFT JOIN reschedules lr ON lr.id = (
    SELECT r.id FROM reschedules r
    WHERE r.verification_id = v.id AND r.decision = 'approved'
    ORDER BY r.id DESC LIMIT 1
)
"""


class ConflictError(Exception):
    """409 — e.g. idempotency key reused with a different payload."""

    def __init__(self, message: str, existing: Optional[dict] = None,
                 index: Optional[int] = None, code: str = "CONFLICT",
                 extra: Optional[dict] = None):
        super().__init__(message)
        self.existing = existing
        # 批量核验时指出发生冲突的输入位置（从 0 开始）；单场核验为 None
        self.index = index
        # 细分错误码：幂等键/操作键冲突沿用 CONFLICT，拷贝占用冲突为 COPY_CONFLICT
        self.code = code
        # 附加到 409 响应体的扩展字段（如拷贝冲突时的 conflicts 列表）
        self.extra = extra or {}


class NotFoundError(Exception):
    """404 — referenced resource does not exist."""

    def __init__(self, message: str, code: str = "NOT_FOUND"):
        super().__init__(message)
        self.code = code


class UnprocessableError(Exception):
    """422 — semantically invalid request that passes schema validation,
    e.g. binding a license to a missing pool or to a pool of another film."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _hash_request(film_id: str, venue_id: str, start_iso: str) -> str:
    payload = json.dumps(
        {"film_id": film_id, "venue_id": venue_id, "start_time": start_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_reschedule(idempotency_key: str, new_start_iso: str) -> str:
    """Idempotency fingerprint of a reschedule op (original key + new start)."""
    payload = json.dumps(
        {"idempotency_key": idempotency_key, "new_start_time": new_start_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pool_brief(row: sqlite3.Row, used: Optional[int] = None) -> Optional[dict]:
    """Pool snapshot attached to a license row; ``None`` when unbound."""
    if row["pool_id"] is None:
        return None
    if used is None:
        used = row["pool_used_sessions"]
    max_sessions = row["pool_max_sessions"]
    return {
        "id": row["pool_id"],
        "film_id": row["pool_film_id"],
        "max_sessions": max_sessions,
        "used_sessions": used,
        "remaining_sessions": max_sessions - used,
    }


def _license_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    revoked = bool(row["revoked"])
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "pool_id": row["pool_id"],
        "pool": _pool_brief(row),
        "revoked": revoked,
        "revocation": (
            {"reason": row["revoke_reason"], "revoked_at": row["revoked_at"]}
            if revoked
            else None
        ),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _pool_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "bound_licenses": row["license_count"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _suspension_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "license_id": row["license_id"],
        "suspend_from": row["suspend_from"],
        "suspend_to": row["suspend_to"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _verification_out(row: sqlite3.Row, replay: bool) -> dict:
    cancelled = row["cancel_id"] is not None
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "start_time": row["start_time"],
        "decision": row["decision"],
        "license_id": row["license_id"],
        "pool_id": row["pool_id"],
        "reject_reason": row["reject_reason"],
        "remaining_quota": row["remaining_quota"],
        "pool_remaining_quota": row["pool_remaining_quota"],
        "verified_at": row["created_at"],
        "replay": replay,
        "cancelled": cancelled,
        "cancellation": (
            {"reason": row["cancel_reason"], "cancelled_at": row["cancelled_at"]}
            if cancelled
            else None
        ),
    }


def _cancel_out(cancel_row: sqlite3.Row, verification_row: sqlite3.Row,
                replay: bool) -> dict:
    """Cancellation record joined with the verification it released."""
    return {
        "id": cancel_row["id"],
        "idempotency_key": cancel_row["idempotency_key"],
        "verification_id": cancel_row["verification_id"],
        # 取消释放的是"当前生效"的授权/池占用（可能已改签）
        "license_id": _effective_license_id(verification_row),
        "pool_id": _effective_pool_id(verification_row),
        "reason": cancel_row["reason"],
        "cancelled_at": cancel_row["cancelled_at"],
        "replay": replay,
    }


def _effective_start(row: sqlite3.Row) -> str:
    """当前生效开场时刻（最近成功改签后的新时刻，否则原核验时刻）。"""
    if "last_to_start_time" in row.keys() and row["last_to_start_time"] is not None:
        return row["last_to_start_time"]
    return row["start_time"]


def _effective_license_id(row: sqlite3.Row):
    """当前生效授权 id。已改签过的行用 last_to_license_id，否则原列。"""
    if "last_to_license_id" in row.keys() and row["last_to_license_id"] is not None:
        return row["last_to_license_id"]
    return row["license_id"]


def _effective_pool_id(row: sqlite3.Row):
    """当前生效共享池 id（可由绑池改签到未绑池授权，因此不能简单判空）。"""
    if "last_reschedule_id" in row.keys() and row["last_reschedule_id"] is not None:
        return row["last_to_pool_id"]
    return row["pool_id"]


def _current_verification_out(row: sqlite3.Row, replay: bool) -> dict:
    """GET /verifications 视图：反映**当前排片**（可被改签更新）。

    顶层 start_time/license_id/pool_id/remaining_quota/pool_remaining_quota
    表示当前生效值；original_* 暴露原核验决定时的不可变快照；
    rescheduled/reschedule_count/last_reschedule 描述改签状态。
    原核验决定（decision）与取消状态始终不变。
    """
    rescheduled = row["last_reschedule_id"] is not None
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        # 当前排片（改签后）
        "start_time": _effective_start(row),
        "license_id": _effective_license_id(row),
        "pool_id": _effective_pool_id(row),
        "remaining_quota": (
            row["last_new_remaining_quota"] if rescheduled
            else row["remaining_quota"]
        ),
        "pool_remaining_quota": (
            row["last_new_pool_remaining_quota"] if rescheduled
            else row["pool_remaining_quota"]
        ),
        # 以下字段语义不变
        "decision": row["decision"],
        "reject_reason": row["reject_reason"],
        "verified_at": row["created_at"],
        "replay": replay,
        "cancelled": row["cancel_id"] is not None,
        "cancellation": (
            {"reason": row["cancel_reason"], "cancelled_at": row["cancelled_at"]}
            if row["cancel_id"] is not None
            else None
        ),
        # 改签信息
        "rescheduled": rescheduled,
        "reschedule_count": row["reschedule_count"],
        "original_start_time": row["start_time"],
        "original_license_id": row["license_id"],
        "original_pool_id": row["pool_id"],
        "original_remaining_quota": row["remaining_quota"],
        "original_pool_remaining_quota": row["pool_remaining_quota"],
        "last_reschedule": (
            {
                "id": row["last_reschedule_id"],
                "operation_key": row["last_operation_key"],
                "from_start_time": row["last_from_start_time"],
                "to_start_time": row["last_to_start_time"],
                "from_license_id": row["last_from_license_id"],
                "to_license_id": row["last_to_license_id"],
                "from_pool_id": row["last_from_pool_id"],
                "to_pool_id": row["last_to_pool_id"],
                "rescheduled_at": row["last_rescheduled_at"],
            }
            if rescheduled
            else None
        ),
    }


def _reschedule_out(row: sqlite3.Row, replay: bool) -> dict:
    """一条改签记录（核准或拒绝均返回；拒绝时排片与占用不变）。"""
    approved = row["decision"] == DECISION_APPROVED
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "verification_id": row["verification_id"],
        "operation_key": row["operation_key"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "from_start_time": row["from_start_time"],
        "to_start_time": row["to_start_time"],
        "decision": row["decision"],
        "from_license_id": row["from_license_id"],
        "to_license_id": row["to_license_id"],
        "from_pool_id": row["from_pool_id"],
        "to_pool_id": row["to_pool_id"],
        "reject_reason": row["reject_reason"],
        "remaining_quota": row["new_remaining_quota"] if approved else None,
        "pool_remaining_quota": row["new_pool_remaining_quota"] if approved else None,
        "reason": row["reason"],
        "rescheduled_at": row["rescheduled_at"],
        "replay": replay,
    }


def _copy_out(copy_row: sqlite3.Row, profile_row: sqlite3.Row, *,
              replay: bool) -> dict:
    """Copy record joined with its film-level profile."""
    return {
        "id": copy_row["id"],
        "film_id": copy_row["film_id"],
        "copy_code": copy_row["copy_code"],
        "duration_minutes": profile_row["duration_minutes"],
        "transfer_minutes": profile_row["transfer_minutes"],
        "created_at": copy_row["created_at"],
        "replay": replay,
    }


def _assignment_out_window(verification_row: sqlite3.Row,
                       profile_row: sqlite3.Row) -> tuple:
    """(start_iso, end_iso) occupancy window derived from the current schedule."""
    start_iso = verification_row["eff_start_time"]
    end_iso = to_iso(
        parse_iso(start_iso) + timedelta(minutes=profile_row["duration_minutes"])
    )
    return start_iso, end_iso


def _assignment_out(*, assignment_id: int, verification: sqlite3.Row,
                    copy: sqlite3.Row, profile: sqlite3.Row,
                    assigned_at: str, replay: bool,
                    previous: Optional[dict] = None) -> dict:
    """Copy-assignment payload; window follows the current schedule."""
    start_iso, end_iso = _assignment_out_window(verification, profile)
    return {
        "id": assignment_id,
        "idempotency_key": verification["idempotency_key"],
        "verification_id": verification["id"],
        "film_id": verification["film_id"],
        "venue_id": verification["venue_id"],
        "copy_id": copy["id"],
        "copy_code": copy["copy_code"],
        "start_time": start_iso,
        "end_time": end_iso,
        "previous_copy_id": previous["copy_id"] if previous else None,
        "previous_copy_code": previous["copy_code"] if previous else None,
        "assigned_at": assigned_at,
        "replay": replay,
    }


# 开场码状态查询：左联核销回执与取消记录，状态由三者共同决定。
# code 故意不 SELECT：除首次领取外任何查询/重领路径都不接触原码。
# effective_start_time 取最近成功改签后的新时刻（开场码按当前排片生效）。
_OPENING_CODE_SELECT = f"""
SELECT
    oc.id,
    oc.verification_id,
    oc.idempotency_key,
    oc.issued_at,
    {_EFFECTIVE_START_EXPR} AS start_time,
    rd.id          AS redemption_id,
    rd.venue_id    AS redemption_venue_id,
    rd.redeemed_at AS redeemed_at,
    c.cancelled_at AS cancelled_at
FROM opening_codes oc
JOIN verifications v ON v.id = oc.verification_id
LEFT JOIN redemptions rd ON rd.opening_code_id = oc.id
LEFT JOIN cancellations c ON c.verification_id = oc.verification_id
"""

# 核销路径取码：需要 code（请求按码匹配）与场次计划信息。
_OPENING_CODE_LOOKUP_SELECT = f"""
SELECT
    oc.id           AS opening_code_id,
    oc.code         AS code,
    oc.issued_at    AS issued_at,
    v.id            AS verification_id,
    v.idempotency_key AS idempotency_key,
    v.film_id       AS film_id,
    v.venue_id      AS venue_id,
    {_EFFECTIVE_START_EXPR} AS start_time,
    v.start_time    AS original_start_time,
    v.decision      AS decision,
    {_EFFECTIVE_LICENSE_EXPR} AS license_id,
    {_EFFECTIVE_POOL_EXPR} AS pool_id,
    rd.id           AS redemption_id,
    rd.venue_id     AS redemption_venue_id,
    rd.redeemed_at  AS redeemed_at,
    c.id            AS cancel_id
FROM opening_codes oc
JOIN verifications v ON v.id = oc.verification_id
LEFT JOIN redemptions rd ON rd.opening_code_id = oc.id
LEFT JOIN cancellations c ON c.verification_id = oc.verification_id
"""


def _redemption_out(row: sqlite3.Row, replay: bool, *,
                    verification_row: Optional[sqlite3.Row] = None) -> dict:
    """Redemption receipt.

    ``row`` may come from either the status join (``_OPENING_CODE_SELECT``)
    or the redeem lookup (``_OPENING_CODE_LOOKUP_SELECT``); the screening
    fields (film/venue/start/license/pool/key) are taken from
    ``verification_row`` when the status join does not carry them.
    """
    def pick(col: str):
        if col in row.keys():
            return row[col]
        assert verification_row is not None
        return verification_row[col]

    return {
        "id": row["redemption_id"],
        "idempotency_key": pick("idempotency_key"),
        "film_id": pick("film_id"),
        "venue_id": row["redemption_venue_id"],
        "start_time": pick("start_time"),
        "license_id": pick("license_id"),
        "pool_id": pick("pool_id"),
        "redeemed_at": row["redeemed_at"],
        "replay": replay,
    }


def _opening_status_out(row: Optional[sqlite3.Row], key: str,
                        verification_row: Optional[sqlite3.Row]) -> dict:
    """Build the status-only payload shared by GET and repeat-claim."""
    if row is None:
        return {
            "idempotency_key": key,
            "status": "not_issued",
            "issued_at": None,
            "cancelled_at": None,
            "redemption": None,
        }
    if row["cancelled_at"] is not None:
        status = "voided"
    elif row["redemption_id"] is not None:
        status = "redeemed"
    else:
        status = "issued"
    return {
        "idempotency_key": key,
        "status": status,
        "issued_at": row["issued_at"],
        "cancelled_at": row["cancelled_at"],
        "redemption": (
            _redemption_out(row, replay=False, verification_row=verification_row)
            if status == "redeemed"
            else None
        ),
    }


def _decide(candidates: list, start_iso: str, suspended_ids: Optional[set] = None):
    """Pick the license to consume, or the reason to reject.

    ``candidates`` is ordered by ``valid_to ASC`` so the first usable entry
    is the earliest-expiring license.  A license is usable only when it
    covers the slot, is neither revoked nor suspended at the slot, **and**
    still has balance of both its own quota and its bound pool.  Exactly
    one unit of the chosen license and one unit of its pool are consumed
    per approval.

    Suspended licenses are skipped just like revoked ones, but they get
    their own reason: ``LICENSE_SUSPENDED`` is reported when at least one
    unrevoked license covers the slot yet every such license is suspended
    at the slot (overlapping suspension intervals count as their union).
    Otherwise the decision falls back to the legacy precedence with
    suspended licenses removed from consideration, so the original
    reasons keep their original order.

    ``POOL_EXHAUSTED`` is reported when in-window, non-revoked,
    non-suspended licenses still have their own balance but every such
    license is blocked only because its bound pool is exhausted.
    """
    suspended_ids = suspended_ids or set()
    if not candidates:
        return DECISION_REJECTED, None, None, REASON_NO_LICENSE, None, None
    in_window = [c for c in candidates if c["valid_from"] <= start_iso <= c["valid_to"]]
    if not in_window:
        return DECISION_REJECTED, None, None, REASON_TIME_OUT_OF_WINDOW, None, None
    active = [c for c in in_window if not c["revoked"]]
    if not active:
        return DECISION_REJECTED, None, None, REASON_LICENSE_REVOKED, None, None
    if all(c["id"] in suspended_ids for c in active):
        return DECISION_REJECTED, None, None, REASON_LICENSE_SUSPENDED, None, None

    # skip suspended licenses; every remaining reason keeps the old order
    effective = [c for c in active if c["id"] not in suspended_ids]
    own_balance = [c for c in effective if c["used_sessions"] < c["max_sessions"]]
    if not own_balance:
        return DECISION_REJECTED, None, None, REASON_QUOTA_EXHAUSTED, None, None

    # earliest-expiring license with own balance; pool balance re-checked
    # against the pool actually bound to it (pool usage is film-global).
    for cand in own_balance:
        if cand["pool_id"] is None:
            return (
                DECISION_APPROVED, cand["id"], None, None,
                cand["max_sessions"] - cand["used_sessions"] - 1, None,
            )
        pool_max = cand["pool_max_sessions"]
        pool_used = cand["pool_used_sessions"]
        if pool_used < pool_max:
            return (
                DECISION_APPROVED, cand["id"], cand["pool_id"], None,
                cand["max_sessions"] - cand["used_sessions"] - 1,
                pool_max - pool_used - 1,
            )

    # every license that still has its own balance is blocked solely
    # because the pool it is bound to has run dry
    return DECISION_REJECTED, None, None, REASON_POOL_EXHAUSTED, None, None


class Store:
    """Thread-safe facade over the SQLite database.

    A fresh connection is opened per operation; the schema is created
    lazily on first use so importing the module has no side effects.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        """Create the schema if it does not exist yet (idempotent)."""
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            conn = connect(self.db_path)
            try:
                init_schema(conn)
            finally:
                conn.close()
            self._ready = True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure()
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # quota pools (film-level shared session cap across venues)
    # ------------------------------------------------------------------
    def create_pool(
        self,
        *,
        film_id: str,
        max_sessions: int,
        note: Optional[str] = None,
    ) -> dict:
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO quota_pools (film_id, max_sessions, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (film_id, max_sessions, note, now_iso()),
            )
            pool_id = cur.lastrowid
        return self.get_pool(pool_id)

    def get_pool(self, pool_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _POOL_SELECT + " WHERE p.id = ?", (pool_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"quota pool {pool_id} not found")
        return _pool_out(row)

    def list_pools(self, *, film_id: Optional[str] = None) -> list:
        sql, params = _POOL_SELECT, []
        if film_id is not None:
            sql += " WHERE p.film_id = ?"
            params.append(film_id)
        sql += " ORDER BY p.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_pool_out(r) for r in rows]

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    def create_license(
        self,
        *,
        film_id: str,
        venue_id: str,
        valid_from: datetime,
        valid_to: datetime,
        max_sessions: int,
        note: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> dict:
        valid_from_iso, valid_to_iso = to_iso(valid_from), to_iso(valid_to)
        if valid_to_iso <= valid_from_iso:
            raise ValueError("valid_to must be later than valid_from")
        with self._connection() as conn:
            if pool_id is not None:
                pool = conn.execute(
                    "SELECT id, film_id FROM quota_pools WHERE id = ?",
                    (pool_id,),
                ).fetchone()
                if pool is None:
                    raise UnprocessableError(
                        f"quota pool {pool_id} does not exist",
                        code="POOL_NOT_FOUND",
                    )
                if pool["film_id"] != film_id:
                    raise UnprocessableError(
                        f"quota pool {pool_id} belongs to film "
                        f"'{pool['film_id']}', not '{film_id}'",
                        code="POOL_FILM_MISMATCH",
                    )
            cur = conn.execute(
                """
                INSERT INTO licenses
                    (film_id, venue_id, valid_from, valid_to, max_sessions,
                     pool_id, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (film_id, venue_id, valid_from_iso, valid_to_iso,
                 max_sessions, pool_id, note, now_iso()),
            )
            license_id = cur.lastrowid
        return self.get_license(license_id)

    def get_license(self, license_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _LICENSE_SELECT + " WHERE l.id = ?", (license_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"license {license_id} not found")
        return _license_out(row)

    def list_licenses(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> list:
        sql, params = _LICENSE_SELECT, []
        clauses = []
        if film_id is not None:
            clauses.append("l.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("l.venue_id = ?")
            params.append(venue_id)
        if pool_id is not None:
            clauses.append("l.pool_id IS ?")
            params.append(pool_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY l.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_license_out(r) for r in rows]

    def count_licenses(self) -> int:
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]

    def revoke_license(self, license_id: int, reason: str) -> dict:
        """Revoke a license.  Only future verifications are blocked;
        previously approved sessions (and their pool consumption) keep
        their records untouched and are not refunded."""
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                lic = conn.execute(
                    "SELECT id FROM licenses WHERE id = ?", (license_id,)
                ).fetchone()
                if lic is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(f"license {license_id} not found")
                existing = conn.execute(
                    "SELECT * FROM revocations WHERE license_id = ?", (license_id,)
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"license {license_id} is already revoked",
                        existing=dict(existing),
                    )
                revoked_at = now_iso()
                cur = conn.execute(
                    "INSERT INTO revocations (license_id, reason, revoked_at) VALUES (?, ?, ?)",
                    (license_id, reason, revoked_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "license_id": license_id,
                    "reason": reason,
                    "revoked_at": revoked_at,
                }
            except (NotFoundError, ConflictError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ------------------------------------------------------------------
    # time-limited suspensions
    # ------------------------------------------------------------------
    def create_suspension(
        self,
        license_id: int,
        *,
        suspend_from: datetime,
        suspend_to: datetime,
        reason: str,
    ) -> dict:
        """Register an immutable, half-open ``[from, to)`` suspension.

        Overlapping intervals are allowed and take effect as their union;
        unknown licenses and inverted windows are rejected without leaving
        a record.  Runs under ``BEGIN IMMEDIATE`` so a suspension insert
        serialises against concurrent verifications: each verification
        decision then corresponds either to the complete pre-registration
        state or to the complete post-registration state.
        """
        from_iso, end_iso = to_iso(suspend_from), to_iso(suspend_to)
        if end_iso <= from_iso:  # defensive; the request schema validates this too
            raise ValueError("suspend_to must be later than suspend_from")
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                lic = conn.execute(
                    "SELECT id FROM licenses WHERE id = ?", (license_id,)
                ).fetchone()
                if lic is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(f"license {license_id} not found")
                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO suspensions
                        (license_id, suspend_from, suspend_to, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (license_id, from_iso, end_iso, reason, created_at),
                )
                suspension_id = cur.lastrowid
                conn.execute("COMMIT")
            except NotFoundError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return self.get_suspension(suspension_id)

    def get_suspension(self, suspension_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM suspensions WHERE id = ?", (suspension_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"suspension {suspension_id} not found")
        return _suspension_out(row)

    def list_suspensions(self, *, license_id: Optional[int] = None) -> list:
        sql = "SELECT * FROM suspensions"
        params: list = []
        if license_id is not None:
            sql += " WHERE license_id = ?"
            params.append(license_id)
        sql += " ORDER BY id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_suspension_out(r) for r in rows]

    # ------------------------------------------------------------------
    # verifications
    # ------------------------------------------------------------------
    def verify(
        self,
        *,
        film_id: str,
        venue_id: str,
        start_time: datetime,
        idempotency_key: str,
    ) -> dict:
        """Verify a screening request idempotently.

        The whole read-check-write sequence runs inside a single
        ``BEGIN IMMEDIATE`` transaction, so concurrent requests are
        serialised by SQLite and neither license quotas nor pool caps
        can ever be exceeded.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = self._verify_one(
                    conn,
                    film_id=film_id,
                    venue_id=venue_id,
                    start_time=start_time,
                    idempotency_key=idempotency_key,
                )
                conn.execute("COMMIT")
                return result
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def verify_batch(self, items: list[dict]) -> list[dict]:
        """Verify 1–100 screening requests atomically, in input order.

        Each item reuses the exact single-screening rules
        (:meth:`_verify_one`): authorisation selection, shared pools,
        suspensions, revocation and idempotency. Items are processed
        strictly in order, so an approval (including a business rejection
        row, which is also a persisted trace) of an earlier item changes
        the quota and pool usage seen by every later item; rejected
        requests still leave their traceable flow row and processing
        continues.

        Every item runs inside **one** ``BEGIN IMMEDIATE`` transaction:
        the whole batch either commits or rolls back. A key reused with
        the same payload (whether inside this batch or from history)
        replays the stored row (``replay=true``) and never consumes quota
        again; a key reused with a different payload aborts the batch
        with :class:`ConflictError` (carrying the offending ``index``),
        as does any processing exception — no new flow rows and no quota
        or pool usage survive. The single write lock additionally makes
        the batch indivisible relative to concurrent single-screening
        verifications and cancellations: they can observe only the
        complete before-batch or complete after-batch state and can
        never interleave into an overshoot.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                results: list[dict] = []
                for index, item in enumerate(items):
                    try:
                        result = self._verify_one(
                            conn,
                            film_id=item["film_id"],
                            venue_id=item["venue_id"],
                            start_time=item["start_time"],
                            idempotency_key=item["idempotency_key"],
                        )
                    except ConflictError as exc:
                        # 标出冲突项的输入位置；整批随之外层回滚
                        exc.index = index
                        raise
                    result["index"] = index
                    results.append(result)
                conn.execute("COMMIT")
                return results
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _verify_one(
        self,
        conn: sqlite3.Connection,
        *,
        film_id: str,
        venue_id: str,
        start_time: datetime,
        idempotency_key: str,
    ) -> dict:
        """Idempotent verification for one item, on an open IMMEDIATE txn.

        Used by both the single-screening endpoint (one item per
        transaction) and the batch endpoint (many items in one
        transaction). Never commits or rolls back — the caller owns the
        transaction boundary; raises :class:`ConflictError` when the
        idempotency key was previously stored with another payload.
        """
        start_iso = to_iso(start_time)
        request_hash = _hash_request(film_id, venue_id, start_iso)

        existing = conn.execute(
            _VERIFICATION_SELECT + " WHERE v.idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ConflictError(
                    "idempotency key "
                    f"'{idempotency_key}' was already used with a "
                    "different request payload",
                    existing=_verification_out(existing, replay=True),
                )
            return _verification_out(existing, replay=True)

        candidates = conn.execute(
            _LICENSE_SELECT + """
            WHERE l.film_id = ? AND l.venue_id = ?
            ORDER BY l.valid_to ASC, l.id ASC
            """,
            (film_id, venue_id),
        ).fetchall()

        # Suspensions are read inside the same IMMEDIATE transaction
        # as the decision, so an insert concurrent with this verify
        # either commits before the transaction starts (fully visible)
        # or waits until it ends (fully absent).  Half-open union:
        # the slot is covered when suspend_from <= t < suspend_to.
        suspended_ids: set = set()
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            hit_rows = conn.execute(
                f"""
                SELECT DISTINCT license_id FROM suspensions
                WHERE license_id IN ({placeholders})
                  AND suspend_from <= ? AND ? < suspend_to
                """,
                [c["id"] for c in candidates] + [start_iso, start_iso],
            ).fetchall()
            suspended_ids = {row["license_id"] for row in hit_rows}

        (
            decision, license_id, pool_id, reason,
            remaining, pool_remaining,
        ) = _decide(candidates, start_iso, suspended_ids)

        created_at = now_iso()
        cur = conn.execute(
            """
            INSERT INTO verifications
                (idempotency_key, request_hash, film_id, venue_id, start_time,
                 decision, license_id, pool_id, reject_reason,
                 remaining_quota, pool_remaining_quota, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (idempotency_key, request_hash, film_id, venue_id, start_iso,
             decision, license_id, pool_id, reason,
             remaining, pool_remaining, created_at),
        )
        return {
            "id": cur.lastrowid,
            "idempotency_key": idempotency_key,
            "film_id": film_id,
            "venue_id": venue_id,
            "start_time": start_iso,
            "decision": decision,
            "license_id": license_id,
            "pool_id": pool_id,
            "reject_reason": reason,
            "remaining_quota": remaining,
            "pool_remaining_quota": pool_remaining,
            "verified_at": created_at,
            "replay": False,
            "cancelled": False,
            "cancellation": None,
        }

    def get_verification(self, idempotency_key: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _CURRENT_VERIFICATION_SELECT + " WHERE v.idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"verification '{idempotency_key}' not found")
        return _current_verification_out(row, replay=False)

    def list_verifications(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        decision: Optional[str] = None,
        license_id: Optional[int] = None,
        pool_id: Optional[int] = None,
        limit: int = 200,
    ) -> list:
        sql = _CURRENT_VERIFICATION_SELECT
        clauses, params = [], []
        if film_id is not None:
            clauses.append("v.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("v.venue_id = ?")
            params.append(venue_id)
        if decision is not None:
            clauses.append("v.decision = ?")
            params.append(decision)
        # 按"当前生效"授权/池过滤（改签后归属可能改变）
        if license_id is not None:
            clauses.append(f"{_EFFECTIVE_LICENSE_EXPR} = ?")
            params.append(license_id)
        if pool_id is not None:
            clauses.append(f"{_EFFECTIVE_POOL_EXPR} = ?")
            params.append(pool_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY v.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_current_verification_out(r, replay=False) for r in rows]

    # ------------------------------------------------------------------
    # cancellations (approved sessions called off before they start)
    # ------------------------------------------------------------------
    def cancel_verification(self, idempotency_key: str, *, reason: str) -> dict:
        """Cancel an approved, not-yet-started screening by its idempotency key.

        The whole lookup-check-insert runs inside one ``BEGIN IMMEDIATE``
        transaction, serialised against concurrent verifications and
        cancellations by the SQLite write lock; the ``UNIQUE`` constraint on
        ``cancellations.verification_id`` is the final guard, so the license
        quota and the bound pool are each released **at most once** per
        verification and a concurrent verify can never oversell the freed
        unit.  The verification row itself (decision, balance snapshots) is
        never touched — only the live used counters derived from it change.

        Re-submitting the same cancellation returns the first record
        (``replay=True``) without releasing quota again; a different reason
        for the same key conflicts (409).
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                verification = conn.execute(
                    f"""
                    SELECT
                        v.*,
                        {_EFFECTIVE_START_EXPR}   AS eff_start_time,
                        {_EFFECTIVE_LICENSE_EXPR} AS eff_license_id,
                        {_EFFECTIVE_POOL_EXPR}    AS eff_pool_id
                    FROM verifications v
                    WHERE v.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if verification is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"verification '{idempotency_key}' not found"
                    )

                existing = conn.execute(
                    "SELECT * FROM cancellations WHERE verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    if existing["reason"] != reason:
                        raise ConflictError(
                            "verification "
                            f"'{idempotency_key}' was already cancelled with "
                            "a different reason",
                            existing=_cancel_out(existing, verification,
                                                 replay=True),
                        )
                    return _cancel_out(existing, verification, replay=True)

                if verification["decision"] != DECISION_APPROVED:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"verification '{idempotency_key}' was rejected and "
                        "cannot be cancelled",
                        code="VERIFICATION_NOT_APPROVED",
                    )
                # 仅可在当前排片开始前取消；当前开场时间已过（含恰在此刻）拒绝
                if verification["eff_start_time"] <= now_iso():
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already started "
                        "and cannot be cancelled",
                        code="SESSION_ALREADY_STARTED",
                    )

                cancelled_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO cancellations
                        (verification_id, idempotency_key, reason, cancelled_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (verification["id"], idempotency_key, reason, cancelled_at),
                )
                # 取消即释放实体拷贝分配：同事务删除分配行，占用区间随之消失，
                # 释放出的拷贝时段立即可被其他场次分配
                conn.execute(
                    "DELETE FROM copy_assignments WHERE verification_id = ?",
                    (verification["id"],),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "idempotency_key": idempotency_key,
                    "verification_id": verification["id"],
                    "license_id": verification["eff_license_id"],
                    "pool_id": verification["eff_pool_id"],
                    "reason": reason,
                    "cancelled_at": cancelled_at,
                    "replay": False,
                }
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ------------------------------------------------------------------
    # schedule changes (rescheduling an approved screening before it starts)
    # ------------------------------------------------------------------
    _RESCHEDULE_SELECT = """
    SELECT rs.*, v.film_id AS film_id, v.venue_id AS venue_id
    FROM reschedules rs
    JOIN verifications v ON v.id = rs.verification_id
    """

    def reschedule_verification(
        self,
        idempotency_key: str,
        *,
        new_start_time: datetime,
        operation_key: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Move an approved, not-started, not-cancelled, code-less screening.

        The target is a new start time for the **same** film and venue; it
        must be later than now and differ from the current schedule.  The
        whole release-then-reauthorise sequence runs in one
        ``BEGIN IMMEDIATE`` transaction: authorisation, suspension,
        revocation and bound-pool quota are all re-judged at the new time,
        with this screening's *current* occupancy counted as already freed
        (the move is atomically a release of the old authorisation/pool plus
        a consumption under the newly chosen authorisation/pool).

        - If the slot cannot be authorised, the standard reject reason is
          returned on a persisted ``decision='rejected'`` trace; the schedule
          and every occupancy stay unchanged.
        - Replaying the same ``operation_key`` with the same content (new
          start) returns the first record (``replay=True``) without migrating
          again; same key, different content conflicts (409); a key reused
          for another screening also conflicts.
        """
        new_start_iso = to_iso(new_start_time)
        request_hash = _hash_reschedule(idempotency_key, new_start_iso)
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                # operation key is globally idempotent: same key, same content
                # replays; same key, different content/verification conflicts
                prior_op = conn.execute(
                    self._RESCHEDULE_SELECT + " WHERE rs.operation_key = ?",
                    (operation_key,),
                ).fetchone()
                if prior_op is not None:
                    conn.execute("COMMIT")
                    if (
                        prior_op["idempotency_key"] != idempotency_key
                        or prior_op["request_hash"] != request_hash
                    ):
                        raise ConflictError(
                            f"reschedule operation key '{operation_key}' was "
                            "already used with a different request payload",
                            existing=_reschedule_out(prior_op, replay=True),
                        )
                    return _reschedule_out(prior_op, replay=True)

                verification = conn.execute(
                    f"""
                    SELECT
                        v.*,
                        {_EFFECTIVE_START_EXPR}   AS eff_start_time,
                        {_EFFECTIVE_LICENSE_EXPR} AS eff_license_id,
                        {_EFFECTIVE_POOL_EXPR}    AS eff_pool_id
                    FROM verifications v
                    WHERE v.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if verification is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"verification '{idempotency_key}' not found"
                    )

                # eligibility gates (no trace left on these hard failures)
                if verification["decision"] != DECISION_APPROVED:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"verification '{idempotency_key}' was rejected and "
                        "cannot be rescheduled",
                        code=RESCHEDULE_REASON_NOT_APPROVED,
                    )
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellations WHERE verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if cancelled is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' is cancelled and "
                        "cannot be rescheduled",
                        code=RESCHEDULE_REASON_CANCELLED,
                    )
                code_issued = conn.execute(
                    "SELECT 1 FROM opening_codes WHERE verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if code_issued is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already claimed an "
                        "opening code and cannot be rescheduled",
                        code=RESCHEDULE_REASON_CODE_ISSUED,
                    )
                current_start = verification["eff_start_time"]
                now = now_iso()
                if new_start_iso <= now:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        "new start time must be later than the current time",
                        code=RESCHEDULE_REASON_PAST_TARGET,
                    )
                if new_start_iso == current_start:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        "new start time must differ from the current schedule",
                        code=RESCHEDULE_REASON_SAME_TARGET,
                    )
                if current_start <= now:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already started "
                        "and cannot be rescheduled",
                        code=RESCHEDULE_REASON_STARTED,
                    )

                # re-judge at the new time for the SAME film and venue
                candidates = conn.execute(
                    _LICENSE_SELECT + """
                    WHERE l.film_id = ? AND l.venue_id = ?
                    ORDER BY l.valid_to ASC, l.id ASC
                    """,
                    (verification["film_id"], verification["venue_id"]),
                ).fetchall()

                suspended_ids: set = set()
                if candidates:
                    placeholders = ",".join("?" for _ in candidates)
                    hit_rows = conn.execute(
                        f"""
                        SELECT DISTINCT license_id FROM suspensions
                        WHERE license_id IN ({placeholders})
                          AND suspend_from <= ? AND ? < suspend_to
                        """,
                        [c["id"] for c in candidates] + [new_start_iso, new_start_iso],
                    ).fetchall()
                    suspended_ids = {row["license_id"] for row in hit_rows}

                # This screening's current occupancy is released first, so it
                # is available for the re-judgement: subtract one from the
                # current authorisation's own usage and the current pool's
                # usage (the move frees those exact units).
                cur_license_id = verification["eff_license_id"]
                cur_pool_id = verification["eff_pool_id"]
                adjusted = [dict(c) for c in candidates]
                for cand in adjusted:
                    if cand["id"] == cur_license_id:
                        cand["used_sessions"] = max(0, cand["used_sessions"] - 1)
                    if cur_pool_id is not None and cand["pool_id"] == cur_pool_id:
                        cand["pool_used_sessions"] = max(
                            0, cand["pool_used_sessions"] - 1
                        )

                (
                    decision, license_id, pool_id, reason_code,
                    remaining, pool_remaining,
                ) = _decide(adjusted, new_start_iso, suspended_ids)

                # 已分配实体拷贝的场次：新时段必须与该拷贝的既有占用兼容
                # （同场馆不重叠、跨场馆留足转运分钟，边界恰好满足可用）。
                # 冲突则改签被拒（落 rejected 流水），排片、分配与额度均不变。
                if decision == DECISION_APPROVED:
                    assignment = conn.execute(
                        """
                        SELECT a.copy_id AS copy_id
                        FROM copy_assignments a
                        WHERE a.verification_id = ?
                        """,
                        (verification["id"],),
                    ).fetchone()
                    if assignment is not None:
                        copy_profile = conn.execute(
                            "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                            (verification["film_id"],),
                        ).fetchone()
                        copy_conflicts = self._copy_conflicts(
                            conn,
                            copy_id=assignment["copy_id"],
                            venue_id=verification["venue_id"],
                            start_iso=new_start_iso,
                            duration_minutes=copy_profile["duration_minutes"],
                            transfer_minutes=copy_profile["transfer_minutes"],
                            exclude_verification_id=verification["id"],
                        )
                        if copy_conflicts:
                            decision = DECISION_REJECTED
                            license_id = None
                            pool_id = None
                            reason_code = REASON_COPY_CONFLICT
                            remaining = None
                            pool_remaining = None

                from_license_id = cur_license_id
                from_pool_id = cur_pool_id
                rescheduled_at = now
                cur = conn.execute(
                    """
                    INSERT INTO reschedules
                        (verification_id, idempotency_key, operation_key,
                         request_hash, from_start_time, to_start_time,
                         from_license_id, to_license_id,
                         from_pool_id, to_pool_id,
                         new_remaining_quota, new_pool_remaining_quota,
                         decision, reject_reason, reason, rescheduled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (verification["id"], idempotency_key, operation_key,
                     request_hash, current_start, new_start_iso,
                     from_license_id, license_id,
                     from_pool_id, pool_id,
                     remaining, pool_remaining,
                     decision, reason_code, reason, rescheduled_at),
                )
                row = conn.execute(
                    self._RESCHEDULE_SELECT + " WHERE rs.id = ?",
                    (cur.lastrowid,),
                ).fetchone()
                conn.execute("COMMIT")
                return _reschedule_out(row, replay=False)
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_reschedule(self, reschedule_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                self._RESCHEDULE_SELECT + " WHERE rs.id = ?",
                (reschedule_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"reschedule {reschedule_id} not found")
        return _reschedule_out(row, replay=False)

    def list_reschedules(
        self,
        *,
        idempotency_key: Optional[str] = None,
        operation_key: Optional[str] = None,
        decision: Optional[str] = None,
        limit: int = 200,
    ) -> list:
        sql = self._RESCHEDULE_SELECT
        clauses, params = [], []
        if idempotency_key is not None:
            clauses.append("rs.idempotency_key = ?")
            params.append(idempotency_key)
        if operation_key is not None:
            clauses.append("rs.operation_key = ?")
            params.append(operation_key)
        if decision is not None:
            clauses.append("rs.decision = ?")
            params.append(decision)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rs.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_reschedule_out(r, replay=False) for r in rows]

    # ------------------------------------------------------------------
    # physical copies: film-level profile, copy registry, session assignment
    # ------------------------------------------------------------------
    def register_copy(
        self,
        film_id: str,
        *,
        duration_minutes: int,
        transfer_minutes: int,
        copy_code: str,
    ) -> dict:
        """Register a physical copy for a film (idempotent per copy code).

        The first copy of a film also fixes the film-level profile
        (positive duration, non-negative cross-venue transfer minutes);
        later copies of the same film must repeat the same profile values.
        Registration is only possible while the film has **no approved
        verification flow** (cancelled ones included).  Re-registering the
        same copy code with the same profile replays the stored record
        (``replay=True``); a code already taken, or a profile mismatch,
        conflicts (409).  Runs under ``BEGIN IMMEDIATE`` so concurrent
        registrations and verifications serialise.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_copy = conn.execute(
                    "SELECT * FROM film_copies WHERE copy_code = ?", (copy_code,)
                ).fetchone()
                if existing_copy is not None:
                    profile = conn.execute(
                        "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                        (existing_copy["film_id"],),
                    ).fetchone()
                    if (
                        existing_copy["film_id"] == film_id
                        and profile is not None
                        and profile["duration_minutes"] == duration_minutes
                        and profile["transfer_minutes"] == transfer_minutes
                    ):
                        conn.execute("COMMIT")
                        return _copy_out(existing_copy, profile, replay=True)
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"copy code '{copy_code}' is already registered",
                        existing=_copy_out(existing_copy, profile, replay=True),
                        code="COPY_CODE_CONFLICT",
                    )

                approved = conn.execute(
                    """
                    SELECT 1 FROM verifications
                    WHERE film_id = ? AND decision = 'approved' LIMIT 1
                    """,
                    (film_id,),
                ).fetchone()
                if approved is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"film '{film_id}' already has approved verifications "
                        "and can no longer register copies",
                        code=COPY_REASON_HAS_APPROVED,
                    )

                profile = conn.execute(
                    "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                    (film_id,),
                ).fetchone()
                if profile is None:
                    conn.execute(
                        """
                        INSERT INTO film_copy_profiles
                            (film_id, duration_minutes, transfer_minutes, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (film_id, duration_minutes, transfer_minutes, now_iso()),
                    )
                    profile = conn.execute(
                        "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                        (film_id,),
                    ).fetchone()
                elif (
                    profile["duration_minutes"] != duration_minutes
                    or profile["transfer_minutes"] != transfer_minutes
                ):
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"film '{film_id}' copy profile is already fixed at "
                        f"duration={profile['duration_minutes']}min, "
                        f"transfer={profile['transfer_minutes']}min",
                        existing={
                            "film_id": film_id,
                            "duration_minutes": profile["duration_minutes"],
                            "transfer_minutes": profile["transfer_minutes"],
                        },
                        code="COPY_PROFILE_MISMATCH",
                    )

                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO film_copies (film_id, copy_code, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (film_id, copy_code, created_at),
                )
                copy_row = conn.execute(
                    "SELECT * FROM film_copies WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
                conn.execute("COMMIT")
                return _copy_out(copy_row, profile, replay=False)
            except (ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_film_copies(self, film_id: str) -> dict:
        """Film-level copy profile plus the list of registered copies."""
        with self._connection() as conn:
            profile = conn.execute(
                "SELECT * FROM film_copy_profiles WHERE film_id = ?", (film_id,)
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM film_copies WHERE film_id = ? ORDER BY id ASC",
                (film_id,),
            ).fetchall()
        return {
            "film_id": film_id,
            "configured": profile is not None,
            "duration_minutes": (
                profile["duration_minutes"] if profile is not None else None
            ),
            "transfer_minutes": (
                profile["transfer_minutes"] if profile is not None else None
            ),
            "copies": [
                {
                    "id": r["id"],
                    "copy_code": r["copy_code"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    # 拷贝当前占用：分配行联出场次的当前排片开场时刻（含改签），
    # 占用区间 [start, start+片长) 在输出时派生，不落地存储。
    _COPY_OCCUPANCY_SELECT = f"""
    SELECT
        a.id              AS assignment_id,
        a.assigned_at     AS assigned_at,
        v.id              AS verification_id,
        v.idempotency_key AS idempotency_key,
        v.film_id         AS film_id,
        v.venue_id        AS venue_id,
        {_EFFECTIVE_START_EXPR} AS start_time
    FROM copy_assignments a
    JOIN verifications v ON v.id = a.verification_id
    """

    def get_copy(self, copy_code: str) -> dict:
        """Copy detail plus its current occupancy (derived from schedules)."""
        with self._connection() as conn:
            copy = conn.execute(
                "SELECT * FROM film_copies WHERE copy_code = ?", (copy_code,)
            ).fetchone()
            if copy is None:
                raise NotFoundError(
                    f"copy '{copy_code}' not found", code=COPY_CODE_NOT_FOUND
                )
            profile = conn.execute(
                "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                (copy["film_id"],),
            ).fetchone()
            rows = conn.execute(
                self._COPY_OCCUPANCY_SELECT
                + " WHERE a.copy_id = ? ORDER BY start_time ASC",
                (copy["id"],),
            ).fetchall()
        duration = profile["duration_minutes"]
        return {
            "id": copy["id"],
            "film_id": copy["film_id"],
            "copy_code": copy["copy_code"],
            "duration_minutes": duration,
            "transfer_minutes": profile["transfer_minutes"],
            "created_at": copy["created_at"],
            "occupancy": [
                {
                    "verification_id": r["verification_id"],
                    "idempotency_key": r["idempotency_key"],
                    "film_id": r["film_id"],
                    "venue_id": r["venue_id"],
                    "start_time": r["start_time"],
                    "end_time": to_iso(
                        parse_iso(r["start_time"]) + timedelta(minutes=duration)
                    ),
                    "assigned_at": r["assigned_at"],
                }
                for r in rows
            ],
        }

    @staticmethod
    def _copy_conflicts(
        conn: sqlite3.Connection,
        *,
        copy_id: int,
        venue_id: str,
        start_iso: str,
        duration_minutes: int,
        transfer_minutes: int,
        exclude_verification_id: Optional[int] = None,
    ) -> list:
        """Occupancy conflicts of one copy against a candidate slot.

        Every assignment occupies ``[start, start+duration)`` of the
        *current* schedule.  Same-venue pairs must not overlap; cross-venue
        pairs must additionally leave ``transfer_minutes`` between them —
        an exactly touching boundary is usable.  The candidate's own
        verification (when re-checking a swap or a reschedule) is excluded.
        """
        rows = conn.execute(
            f"""
            SELECT
                a.verification_id AS verification_id,
                v.idempotency_key AS idempotency_key,
                v.venue_id        AS venue_id,
                {_EFFECTIVE_START_EXPR} AS start_time
            FROM copy_assignments a
            JOIN verifications v ON v.id = a.verification_id
            WHERE a.copy_id = ? AND v.id IS NOT ?
            ORDER BY start_time ASC
            """,
            (copy_id, exclude_verification_id or -1),
        ).fetchall()
        start = parse_iso(start_iso)
        end = start + timedelta(minutes=duration_minutes)
        conflicts = []
        for row in rows:
            other_start = parse_iso(row["start_time"])
            other_end = other_start + timedelta(minutes=duration_minutes)
            gap = (
                timedelta(0)
                if row["venue_id"] == venue_id
                else timedelta(minutes=transfer_minutes)
            )
            if start < other_end + gap and other_start < end + gap:
                conflicts.append(
                    {
                        "verification_id": row["verification_id"],
                        "idempotency_key": row["idempotency_key"],
                        "venue_id": row["venue_id"],
                        "start_time": row["start_time"],
                        "end_time": to_iso(other_end),
                    }
                )
        return conflicts

    def assign_copy(self, idempotency_key: str, *, copy_code: str) -> dict:
        """Assign a same-film copy to an approved, live, not-started session.

        The whole check-and-write runs in one ``BEGIN IMMEDIATE``
        transaction, so concurrent assignments can never oversell a copy:
        a conflicting candidate slot (same-venue overlap or insufficient
        cross-venue transfer gap) is rejected with 409 ``COPY_CONFLICT``
        and **nothing is persisted**.  Re-assigning the same copy to the
        same session returns the current state (``replay=True``); assigning
        a different copy atomically replaces the current row in the same
        transaction (the old copy's occupancy is released as the new one
        is taken).
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                verification = conn.execute(
                    f"""
                    SELECT v.*, {_EFFECTIVE_START_EXPR} AS eff_start_time
                    FROM verifications v
                    WHERE v.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if verification is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"verification '{idempotency_key}' not found"
                    )
                copy = conn.execute(
                    "SELECT * FROM film_copies WHERE copy_code = ?", (copy_code,)
                ).fetchone()
                if copy is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"copy '{copy_code}' not found", code=COPY_CODE_NOT_FOUND
                    )
                profile = conn.execute(
                    "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                    (copy["film_id"],),
                ).fetchone()

                existing = conn.execute(
                    """
                    SELECT a.id AS assignment_id, a.copy_id AS copy_id,
                           a.assigned_at AS assigned_at,
                           fc.copy_code AS copy_code
                    FROM copy_assignments a
                    JOIN film_copies fc ON fc.id = a.copy_id
                    WHERE a.verification_id = ?
                    """,
                    (verification["id"],),
                ).fetchone()
                # 同场次同拷贝重试：直接返回现状，不重复落账（即使此后已开场）
                if existing is not None and existing["copy_id"] == copy["id"]:
                    conn.execute("COMMIT")
                    return _assignment_out(
                        assignment_id=existing["assignment_id"],
                        verification=verification,
                        copy=copy,
                        profile=profile,
                        assigned_at=existing["assigned_at"],
                        replay=True,
                    )

                if verification["decision"] != DECISION_APPROVED:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"verification '{idempotency_key}' was rejected and "
                        "cannot be assigned a copy",
                        code=CODE_REASON_NOT_APPROVED,
                    )
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellations WHERE verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if cancelled is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' is cancelled and "
                        "cannot be assigned a copy",
                        code=CODE_REASON_CANCELLED,
                    )
                # 仅未开场的场次可分配/换拷贝（按当前排片判定）
                if verification["eff_start_time"] <= now_iso():
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already started "
                        "and cannot be assigned a copy",
                        code=CODE_REASON_ALREADY_STARTED,
                    )
                if copy["film_id"] != verification["film_id"]:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"copy '{copy_code}' belongs to film "
                        f"'{copy['film_id']}', not '{verification['film_id']}'",
                        code=COPY_REASON_FILM_MISMATCH,
                    )

                conflicts = self._copy_conflicts(
                    conn,
                    copy_id=copy["id"],
                    venue_id=verification["venue_id"],
                    start_iso=verification["eff_start_time"],
                    duration_minutes=profile["duration_minutes"],
                    transfer_minutes=profile["transfer_minutes"],
                    exclude_verification_id=verification["id"],
                )
                if conflicts:
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"copy '{copy_code}' cannot cover the slot: "
                        f"{len(conflicts)} conflicting assignment(s)",
                        code=COPY_CODE_CONFLICT,
                        extra={"conflicts": conflicts},
                    )

                assigned_at = now_iso()
                previous = None
                if existing is not None:
                    # 换拷贝：同一事务内原子替换（旧拷贝占用随本行更新即释放）
                    previous = {
                        "copy_id": existing["copy_id"],
                        "copy_code": existing["copy_code"],
                    }
                    conn.execute(
                        """
                        UPDATE copy_assignments
                        SET copy_id = ?, assigned_at = ?
                        WHERE verification_id = ?
                        """,
                        (copy["id"], assigned_at, verification["id"]),
                    )
                    assignment_id = existing["assignment_id"]
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO copy_assignments
                            (verification_id, copy_id, assigned_at)
                        VALUES (?, ?, ?)
                        """,
                        (verification["id"], copy["id"], assigned_at),
                    )
                    assignment_id = cur.lastrowid
                conn.execute("COMMIT")
                return _assignment_out(
                    assignment_id=assignment_id,
                    verification=verification,
                    copy=copy,
                    profile=profile,
                    assigned_at=assigned_at,
                    replay=False,
                    previous=previous,
                )
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_copy_assignment(self, idempotency_key: str) -> dict:
        """Current copy assignment of one verification (assigned flag)."""
        with self._connection() as conn:
            verification = conn.execute(
                f"""
                SELECT v.*, {_EFFECTIVE_START_EXPR} AS eff_start_time
                FROM verifications v
                WHERE v.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if verification is None:
                raise NotFoundError(
                    f"verification '{idempotency_key}' not found"
                )
            row = conn.execute(
                """
                SELECT a.id AS assignment_id, a.assigned_at AS assigned_at,
                       a.copy_id AS copy_id, fc.copy_code AS copy_code,
                       fc.film_id AS copy_film_id
                FROM copy_assignments a
                JOIN film_copies fc ON fc.id = a.copy_id
                WHERE a.verification_id = ?
                """,
                (verification["id"],),
            ).fetchone()
            if row is None:
                return {
                    "idempotency_key": idempotency_key,
                    "verification_id": verification["id"],
                    "film_id": verification["film_id"],
                    "venue_id": verification["venue_id"],
                    "assigned": False,
                    "copy_id": None,
                    "copy_code": None,
                    "start_time": None,
                    "end_time": None,
                    "assigned_at": None,
                }
            profile = conn.execute(
                "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                (row["copy_film_id"],),
            ).fetchone()
        end = parse_iso(verification["eff_start_time"]) + timedelta(
            minutes=profile["duration_minutes"]
        )
        return {
            "idempotency_key": idempotency_key,
            "verification_id": verification["id"],
            "film_id": verification["film_id"],
            "venue_id": verification["venue_id"],
            "assigned": True,
            "copy_id": row["copy_id"],
            "copy_code": row["copy_code"],
            "start_time": verification["eff_start_time"],
            "end_time": to_iso(end),
            "assigned_at": row["assigned_at"],
        }

    def check_copy_conflicts(
        self, copy_code: str, *, venue_id: str, start_time: datetime
    ) -> dict:
        """Preview which current assignments of a copy would conflict with a
        candidate venue/slot (read-only; same rules as assignment)."""
        start_iso = to_iso(start_time)
        with self._connection() as conn:
            copy = conn.execute(
                "SELECT * FROM film_copies WHERE copy_code = ?", (copy_code,)
            ).fetchone()
            if copy is None:
                raise NotFoundError(
                    f"copy '{copy_code}' not found", code=COPY_CODE_NOT_FOUND
                )
            profile = conn.execute(
                "SELECT * FROM film_copy_profiles WHERE film_id = ?",
                (copy["film_id"],),
            ).fetchone()
            conflicts = self._copy_conflicts(
                conn,
                copy_id=copy["id"],
                venue_id=venue_id,
                start_iso=start_iso,
                duration_minutes=profile["duration_minutes"],
                transfer_minutes=profile["transfer_minutes"],
            )
        end = parse_iso(start_iso) + timedelta(
            minutes=profile["duration_minutes"]
        )
        return {
            "copy_id": copy["id"],
            "copy_code": copy["copy_code"],
            "film_id": copy["film_id"],
            "venue_id": venue_id,
            "start_time": start_iso,
            "end_time": to_iso(end),
            "conflicts": conflicts,
        }

    # ------------------------------------------------------------------
    # one-time opening codes and venue redemption
    # ------------------------------------------------------------------
    def _generate_opening_code(self, conn: sqlite3.Connection) -> str:
        """Random, unguessable one-time code unique within the database."""
        while True:
            code = "OC-" + secrets.token_hex(12).upper()
            clash = conn.execute(
                "SELECT 1 FROM opening_codes WHERE code = ?", (code,)
            ).fetchone()
            if clash is None:
                return code

    def claim_opening_code(self, idempotency_key: str) -> dict:
        """Claim the one-time opening code for an approved session.

        Eligibility mirrors the cancellation gate: the verification must be
        approved, not cancelled and still before its planned start.  The
        whole check-insert runs inside ``BEGIN IMMEDIATE``; the UNIQUE
        constraint on ``opening_codes.verification_id`` is the final guard,
        so concurrent claims for the same session can never produce two
        codes.  Re-claiming returns status only (``replay=True``) — the
        original code is read by nobody on that path and never returned
        again.  A cancellation committed first makes the claim fail, and a
        claimed code reads back as ``voided`` once cancelled.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                verification = conn.execute(
                    f"""
                    SELECT
                        v.*,
                        {_EFFECTIVE_START_EXPR} AS eff_start_time
                    FROM verifications v
                    WHERE v.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if verification is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"verification '{idempotency_key}' not found"
                    )

                existing = conn.execute(
                    _OPENING_CODE_SELECT + " WHERE oc.verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    payload = _opening_status_out(existing, idempotency_key,
                                                  verification)
                    payload["code"] = None  # repeat claim reveals status only
                    payload["replay"] = True
                    return payload

                if verification["decision"] != DECISION_APPROVED:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"verification '{idempotency_key}' was rejected and "
                        "has no opening code",
                        code=CODE_REASON_NOT_APPROVED,
                    )
                cancelled = conn.execute(
                    "SELECT 1 FROM cancellations WHERE verification_id = ?",
                    (verification["id"],),
                ).fetchone()
                if cancelled is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' is cancelled and its "
                        "opening code is void",
                        code=CODE_REASON_CANCELLED,
                    )
                # 仅可在当前排片的计划开场时刻前领取（按改签后时刻判定）
                if verification["eff_start_time"] <= now_iso():
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already started "
                        "and no opening code can be claimed",
                        code=CODE_REASON_ALREADY_STARTED,
                    )
                # 已配置实体拷贝的影片：领码前须已有有效拷贝分配；
                # 未配置拷贝的影片沿用旧行为，不做此检查
                copy_profiled = conn.execute(
                    "SELECT 1 FROM film_copy_profiles WHERE film_id = ?",
                    (verification["film_id"],),
                ).fetchone()
                if copy_profiled is not None:
                    assigned = conn.execute(
                        "SELECT 1 FROM copy_assignments WHERE verification_id = ?",
                        (verification["id"],),
                    ).fetchone()
                    if assigned is None:
                        conn.execute("ROLLBACK")
                        raise UnprocessableError(
                            f"film '{verification['film_id']}' has physical "
                            "copies configured; assign a copy before claiming "
                            "the opening code",
                            code=COPY_REASON_ASSIGNMENT_REQUIRED,
                        )

                issued_at = now_iso()
                code = self._generate_opening_code(conn)
                conn.execute(
                    """
                    INSERT INTO opening_codes
                        (verification_id, idempotency_key, code, issued_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (verification["id"], idempotency_key, code, issued_at),
                )
                conn.execute("COMMIT")
                return {
                    "idempotency_key": idempotency_key,
                    "status": "issued",
                    "issued_at": issued_at,
                    "cancelled_at": None,
                    "redemption": None,
                    "code": code,
                    "replay": False,
                }
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_opening_code_status(self, idempotency_key: str) -> dict:
        """Status-only view: never returns the code itself."""
        with self._connection() as conn:
            verification = conn.execute(
                "SELECT * FROM verifications WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if verification is None:
                raise NotFoundError(
                    f"verification '{idempotency_key}' not found"
                )
            row = conn.execute(
                _OPENING_CODE_SELECT + " WHERE oc.verification_id = ?",
                (verification["id"],),
            ).fetchone()
        return _opening_status_out(row, idempotency_key, verification)

    def redeem_opening_code(self, *, code: str, venue_id: str) -> dict:
        """Redeem an opening code at the venue within the 30-minute window.

        The window is the half-open interval
        ``[start_time, start_time + 30min)`` measured against the server
        clock: redeeming before the planned start fails with
        ``REDEMPTION_TOO_EARLY`` and after the window with
        ``REDEMPTION_WINDOW_CLOSED``; an unknown code is 404; a wrong venue
        is ``VENUE_MISMATCH``; a cancelled code is ``SESSION_CANCELLED``.
        Every rejection rolls back and produces no receipt.

        A successful insert is guarded by the UNIQUE constraint on
        ``redemptions.opening_code_id``; repeating a successful redemption
        returns the first receipt (``replay=True``) — even past the window —
        and never counts the session twice.  The wrong venue is rejected
        unconditionally, so a replay can never be used to probe another
        venue's session.  Importantly the license is never re-validated: a
        later revocation or suspension does not change redemption
        eligibility, and redemption does not touch quota or pool usage.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    _OPENING_CODE_LOOKUP_SELECT + " WHERE oc.code = ?",
                    (code,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        "unknown opening code", code="OPENING_CODE_NOT_FOUND"
                    )

                if row["redemption_id"] is not None:
                    # another venue must never receive (or learn) the receipt
                    if row["venue_id"] != venue_id:
                        conn.execute("ROLLBACK")
                        raise UnprocessableError(
                            "opening code belongs to a different venue",
                            code=REDEEM_REASON_VENUE_MISMATCH,
                        )
                    conn.execute("COMMIT")
                    return _redemption_out(row, replay=True)

                if row["cancel_id"] is not None:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        "the screening for this opening code is cancelled",
                        code=CODE_REASON_CANCELLED,
                    )
                if row["venue_id"] != venue_id:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"opening code was issued for venue "
                        f"'{row['venue_id']}', not '{venue_id}'",
                        code=REDEEM_REASON_VENUE_MISMATCH,
                    )

                now = parse_iso(now_iso())
                start = parse_iso(row["start_time"])
                if now < start:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        "redemption opens at the planned start time, not before",
                        code=REDEEM_REASON_TOO_EARLY,
                    )
                if now >= start + REDEMPTION_WINDOW:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        "the 30-minute redemption window has closed",
                        code=REDEEM_REASON_WINDOW_CLOSED,
                    )

                redeemed_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO redemptions (opening_code_id, venue_id, redeemed_at)
                    VALUES (?, ?, ?)
                    """,
                    (row["opening_code_id"], venue_id, redeemed_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "idempotency_key": row["idempotency_key"],
                    "film_id": row["film_id"],
                    "venue_id": venue_id,
                    "start_time": row["start_time"],
                    "license_id": row["license_id"],
                    "pool_id": row["pool_id"],
                    "redeemed_at": redeemed_at,
                    "replay": False,
                }
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
