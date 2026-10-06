"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class PoolCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    max_sessions: int = Field(
        ge=1, le=1_000_000,
        description="影片级跨场馆共享放映次数上限（正整数）",
        examples=[10],
    )
    note: Optional[str] = Field(default=None, max_length=500)


class PoolOut(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    bound_licenses: int = Field(description="当前绑定该池的授权份数")
    note: Optional[str]
    created_at: str


class LicenseCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])
    valid_from: datetime = Field(examples=["2026-10-01T00:00:00Z"])
    valid_to: datetime = Field(examples=["2026-10-31T23:59:59Z"])
    max_sessions: int = Field(ge=1, le=1_000_000, examples=[5])
    pool_id: Optional[int] = Field(
        default=None,
        description="可选：绑定影片级共享额度池；绑定后不可修改。池不存在或影片不符返回 422。",
        examples=[1],
    )
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_window(self) -> "LicenseCreate":
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class PoolBrief(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int


class RevocationOut(BaseModel):
    reason: str
    revoked_at: str


class LicenseOut(BaseModel):
    id: int
    film_id: str
    venue_id: str
    valid_from: str
    valid_to: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    pool_id: Optional[int] = Field(description="所绑共享额度池 id；未绑定为 null")
    pool: Optional[PoolBrief] = Field(
        description="所绑池的实时已用/剩余快照；未绑定为 null"
    )
    revoked: bool
    revocation: Optional[RevocationOut]
    note: Optional[str]
    created_at: str


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时撤片"])


class RevokeOut(BaseModel):
    id: int
    license_id: int
    reason: str
    revoked_at: str


class SuspensionCreate(BaseModel):
    suspend_from: datetime = Field(
        description="暂停起（含），ISO-8601 且必须带时区偏移，如 2026-10-05T00:00:00+08:00",
        examples=["2026-10-05T00:00:00+08:00"],
    )
    suspend_to: datetime = Field(
        description="暂停止（不含），ISO-8601 且必须带时区偏移，须严格晚于 suspend_from",
        examples=["2026-10-06T00:00:00+08:00"],
    )
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时通知暂停放映"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @model_validator(mode="after")
    def _check_window(self) -> "SuspensionCreate":
        if self.suspend_from.tzinfo is None or self.suspend_to.tzinfo is None:
            raise ValueError("suspend_from and suspend_to must include a timezone offset")
        if self.suspend_to <= self.suspend_from:
            raise ValueError("suspend_to must be later than suspend_from")
        return self


class SuspensionOut(BaseModel):
    id: int = Field(description="暂停编号")
    license_id: int
    suspend_from: str = Field(description="暂停起（含），统一存储为 UTC")
    suspend_to: str = Field(description="暂停止（不含），统一存储为 UTC")
    reason: str
    created_at: str


class VerifyRequest(BaseModel):
    film_id: str = Field(min_length=1, max_length=128)
    venue_id: str = Field(min_length=1, max_length=128)
    start_time: datetime = Field(
        description="场次开始时间，ISO-8601；不带时区时按 UTC 处理",
        examples=["2026-10-05T19:30:00Z"],
    )
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="业务幂等键；重复提交返回原结果，内容变化则 409",
        examples=["order-20261005-0001"],
    )


# 一次集中提交允许的场次数上下限（含端点）
BATCH_MIN_ITEMS = 1
BATCH_MAX_ITEMS = 100


class BatchVerifyRequest(BaseModel):
    """发行方集中提交的一批待核验场次。"""

    items: list[VerifyRequest] = Field(
        min_length=BATCH_MIN_ITEMS,
        max_length=BATCH_MAX_ITEMS,
        description=(
            f"按输入顺序核验的场次列表，{BATCH_MIN_ITEMS}～{BATCH_MAX_ITEMS} 项；"
            "任一项格式错误整批拒绝（422），不落任何流水"
        ),
    )


class CancellationOut(BaseModel):
    reason: str
    cancelled_at: str


class VerificationOut(BaseModel):
    id: int
    idempotency_key: str
    film_id: str
    venue_id: str
    start_time: str
    decision: str = Field(description="approved 或 rejected；取消不回写，保持首次决定")
    license_id: Optional[int] = Field(description="命中并消耗额度的授权 id")
    pool_id: Optional[int] = Field(
        description="本次核准同时消耗的共享池 id；授权未绑池或被拒绝时为 null"
    )
    reject_reason: Optional[str] = Field(
        description="拒绝原因：NO_LICENSE / TIME_OUT_OF_WINDOW / LICENSE_REVOKED / "
                    "LICENSE_SUSPENDED / QUOTA_EXHAUSTED / POOL_EXHAUSTED"
    )
    remaining_quota: Optional[int] = Field(description="本次消耗后该授权自身的剩余额度（快照，取消不回写）")
    pool_remaining_quota: Optional[int] = Field(
        description="本次消耗后所绑共享池的剩余额度（快照，取消不回写）；授权未绑池或被拒绝时为 null"
    )
    verified_at: str
    replay: bool = Field(description="true 表示本次为幂等重放，未重新扣减额度")
    cancelled: bool = Field(description="该流水是否已登记场次取消")
    cancellation: Optional[CancellationOut] = Field(
        description="取消详情（原因与取消时间）；未取消为 null"
    )


class BatchVerificationOut(VerificationOut):
    """单条批量核验结果：完整单场核验结果 + 该项在输入中的位置（从 0 开始）。"""

    index: int = Field(description="该项在提交批次中的输入位置（从 0 开始）")


class LastRescheduleBrief(BaseModel):
    id: int
    operation_key: str
    from_start_time: str
    to_start_time: str
    from_license_id: Optional[int]
    to_license_id: Optional[int]
    from_pool_id: Optional[int]
    to_pool_id: Optional[int]
    rescheduled_at: str


class CurrentVerificationOut(VerificationOut):
    """GET /verifications 视图：顶层为**当前排片**（可被改签更新）。

    与首次核验响应相比，顶层 start_time/license_id/pool_id/remaining_quota/
    pool_remaining_quota 表示改签后的当前生效值；另以 original_* 暴露原核验
    不可变快照，并给出 rescheduled/reschedule_count/last_reschedule。
    decision/reject_reason/cancellation 语义不变。
    """

    rescheduled: bool = Field(description="该场次是否有过成功改签")
    reschedule_count: int = Field(description="成功改签次数")
    original_start_time: str = Field(description="原核验时的开场时刻（不可变快照）")
    original_license_id: Optional[int] = Field(description="原核验命中的授权 id（不可变快照）")
    original_pool_id: Optional[int] = Field(description="原核验消耗的池 id（不可变快照）")
    original_remaining_quota: Optional[int] = Field(description="原核验扣减后的授权剩余快照")
    original_pool_remaining_quota: Optional[int] = Field(description="原核验扣减后的池剩余快照")
    last_reschedule: Optional[LastRescheduleBrief] = Field(
        description="最近一次成功改签摘要；从未改签为 null"
    )


class BatchVerifyResponse(BaseModel):
    count: int = Field(description="本批核验项数（等于 items 长度）")
    results: list[BatchVerificationOut] = Field(
        description="各项核验结果，顺序与输入 items 完全一致"
    )


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["放映设备故障，场次取消"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

class CancelOut(BaseModel):
    id: int = Field(description="取消记录编号")
    idempotency_key: str = Field(description="原核验幂等键")
    verification_id: int = Field(description="被取消的核验流水 id")
    license_id: int = Field(description="释放一次已用次数的授权 id")
    pool_id: Optional[int] = Field(description="同步释放一次的共享池 id；授权未绑池为 null")
    reason: str
    cancelled_at: str = Field(description="取消登记时间（UTC）")
    replay: bool = Field(
        description="true 表示重复提交同一取消，返回首次记录，未重复释放额度"
    )


# ----------------------------------------------------------------------
# 排片改签（reschedule）
# ----------------------------------------------------------------------
class RescheduleRequest(BaseModel):
    new_start_time: datetime = Field(
        description=(
            "新开场时间，ISO-8601；同一影片与场馆，须晚于当前时刻且不同于"
            "当前排片。不带时区时按 UTC 处理。"
        ),
        examples=["2030-06-05T21:30:00Z"],
    )
    operation_key: str = Field(
        min_length=1, max_length=128,
        description="改签操作键；同键同内容重放不重复迁移，同键异内容 409",
        examples=["screening-0001-move-1"],
    )
    reason: Optional[str] = Field(
        default=None, max_length=500,
        description="可选改签原因（仅记录，不参与判定）",
        examples=["导演行程调整，顺延一天"],
    )


class RescheduleOut(BaseModel):
    id: int = Field(description="改签记录编号")
    idempotency_key: str = Field(description="原核验幂等键")
    verification_id: int = Field(description="对应核验流水 id")
    operation_key: str = Field(description="本次改签操作键")
    film_id: str
    venue_id: str
    from_start_time: str = Field(description="改签前的当前开场时刻（UTC）")
    to_start_time: str = Field(description="请求的新开场时刻（UTC；被拒也记录）")
    decision: str = Field(description="approved=改签成功并迁移占用；rejected=新时间无法核准，排片与占用不变")
    from_license_id: Optional[int] = Field(description="释放一次占用的授权 id")
    to_license_id: Optional[int] = Field(description="新占用一次的授权 id；被拒为 null")
    from_pool_id: Optional[int] = Field(description="释放一次占用的共享池 id；未绑池为 null")
    to_pool_id: Optional[int] = Field(description="新占用一次的共享池 id；未绑池/被拒为 null")
    reject_reason: Optional[str] = Field(
        description="新时间无法核准时的既有拒绝原因：NO_LICENSE / TIME_OUT_OF_WINDOW / "
                    "LICENSE_REVOKED / LICENSE_SUSPENDED / QUOTA_EXHAUSTED / POOL_EXHAUSTED；成功为 null"
    )
    remaining_quota: Optional[int] = Field(
        description="新授权占用后的自身剩余；被拒为 null"
    )
    pool_remaining_quota: Optional[int] = Field(
        description="新池占用后的剩余；未绑池/被拒为 null"
    )
    reason: Optional[str] = Field(description="运营方填写的改签原因")
    rescheduled_at: str = Field(description="改签登记时间（UTC）")
    replay: bool = Field(
        description="true 表示同操作键同内容重放，返回首次记录，未重复迁移"
    )


# ----------------------------------------------------------------------
# 一次性开场码领取 / 状态查询 / 场馆核销
# ----------------------------------------------------------------------
class OpeningCodeStatusOut(BaseModel):
    """开场码状态；除首次领取外任何途径都不回传原码。"""

    idempotency_key: str = Field(description="原核验幂等键")
    status: Literal["not_issued", "issued", "redeemed", "voided"] = Field(
        description=(
            "not_issued=尚未领取；issued=已领取未核销；"
            "redeemed=已核销（附回执）；voided=场次取消后立即失效"
        )
    )
    issued_at: Optional[str] = Field(description="首次领取时间（UTC）；尚未领取为 null")
    cancelled_at: Optional[str] = Field(
        description="场次取消时间（UTC），status=voided 时非空"
    )
    redemption: Optional["RedemptionOut"] = Field(
        default=None, description="已核销时附首次核销回执，否则为 null"
    )


class OpeningCodeOut(OpeningCodeStatusOut):
    """领取响应：首次领取（replay=false）回传一次性码，重复领取仅回状态。"""

    code: Optional[str] = Field(
        description="一次性开场码；仅首次领取（replay=false）返回，重复领取为 null"
    )
    replay: bool = Field(
        description="true 表示重复领取，只回状态，原码不会再次泄露"
    )


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64, examples=["OC-3f9a..."])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])

    @field_validator("code", "venue_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RedemptionOut(BaseModel):
    """核销回执；成功一次后重复核销始终返回首次回执（replay=true）。"""

    id: int = Field(description="回执编号")
    idempotency_key: str = Field(description="被核销场次的原核验幂等键")
    film_id: str = Field(description="场次影片")
    venue_id: str = Field(description="核销场馆编号（与核准场馆一致）")
    start_time: str = Field(description="计划开场时刻（UTC）")
    license_id: int = Field(description="关联授权 id")
    pool_id: Optional[int] = Field(description="关联共享池 id；授权未绑池为 null")
    redeemed_at: str = Field(description="实际核销时间（UTC）")
    replay: bool = Field(description="true 表示重复核销返回首次回执，不重复记场次")


OpeningCodeStatusOut.model_rebuild()


# ----------------------------------------------------------------------
# 实体放映拷贝：影片级配置登记 / 拷贝查询 / 场次分配
# ----------------------------------------------------------------------
class CopyRegisterRequest(BaseModel):
    """登记一份实体拷贝；同片首份拷贝同时固定影片级片长与转运配置。"""

    duration_minutes: int = Field(
        ge=1, le=100_000,
        description="正整数片长（分钟）；每份拷贝按当前排片占用 [开场, 开场+片长)",
        examples=[120],
    )
    transfer_minutes: int = Field(
        ge=0, le=100_000,
        description="非负跨场馆转运分钟数；跨场馆两场之间须留足该间隔，恰好满足边界可用",
        examples=[45],
    )
    copy_code: str = Field(
        min_length=1, max_length=128,
        description="唯一拷贝编号（全库唯一）",
        examples=["COPY-AR-001"],
    )

    @field_validator("copy_code")
    @classmethod
    def _code_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("copy_code must not be blank")
        return value


class CopyOut(BaseModel):
    id: int = Field(description="拷贝记录编号")
    film_id: str
    copy_code: str = Field(description="唯一拷贝编号")
    duration_minutes: int = Field(description="影片级片长（分钟）")
    transfer_minutes: int = Field(description="影片级跨场馆转运分钟数")
    created_at: str = Field(description="登记时间（UTC）")
    replay: bool = Field(
        description="true 表示同编号同参数重复登记，返回既有记录，未新建拷贝"
    )


class CopyBrief(BaseModel):
    id: int
    copy_code: str
    created_at: str


class FilmCopiesOut(BaseModel):
    """某部影片的拷贝配置与拷贝清单；未配置时 configured=false。"""

    film_id: str
    configured: bool = Field(
        description="是否已配置实体拷贝；未配置的影片领开场码沿用旧行为（无需分配）"
    )
    duration_minutes: Optional[int] = Field(description="片长（分钟）；未配置为 null")
    transfer_minutes: Optional[int] = Field(description="跨场馆转运分钟数；未配置为 null")
    copies: list[CopyBrief]


class CopyOccupancyItem(BaseModel):
    """一份拷贝当前占用的一场：占用区间 [start_time, end_time) 按当前排片派生。"""

    verification_id: int
    idempotency_key: str = Field(description="占用场次的原核验幂等键")
    film_id: str
    venue_id: str
    start_time: str = Field(description="当前排片开场时刻（UTC，含改签）")
    end_time: str = Field(description="占用结束时刻 = 开场 + 片长（左闭右开）")
    assigned_at: str = Field(description="该场最近一次分配时间（UTC）")


class CopyDetailOut(BaseModel):
    id: int
    film_id: str
    copy_code: str
    duration_minutes: int
    transfer_minutes: int
    created_at: str
    occupancy: list[CopyOccupancyItem] = Field(
        description="该拷贝当前全部占用场次（按当前排片开场时刻升序）"
    )


class CopyAssignRequest(BaseModel):
    copy_code: str = Field(
        min_length=1, max_length=128,
        description="目标拷贝编号；须与场次同片",
        examples=["COPY-AR-001"],
    )

    @field_validator("copy_code")
    @classmethod
    def _code_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("copy_code must not be blank")
        return value


class CopyAssignmentOut(BaseModel):
    """一次分配/替换的结果；同场次同拷贝重试返回现状（replay=true）。"""

    id: int = Field(description="分配记录编号（换拷贝原子替换时保持不变）")
    idempotency_key: str = Field(description="场次的原核验幂等键")
    verification_id: int
    film_id: str
    venue_id: str
    copy_id: int
    copy_code: str
    start_time: str = Field(description="当前排片开场时刻（UTC）")
    end_time: str = Field(description="占用结束时刻 = 开场 + 片长（左闭右开）")
    previous_copy_id: Optional[int] = Field(description="被原子替换的原拷贝 id；首次分配为 null")
    previous_copy_code: Optional[str] = Field(description="被原子替换的原拷贝编号；首次分配为 null")
    assigned_at: str = Field(description="最近一次（重）分配时间（UTC）")
    replay: bool = Field(
        description="true 表示同场次同拷贝重试，返回现状，未重复落账"
    )


class CopyAssignmentStatusOut(BaseModel):
    """某场次当前的拷贝分配状态；未分配时 assigned=false。"""

    idempotency_key: str
    verification_id: int
    film_id: str
    venue_id: str
    assigned: bool
    copy_id: Optional[int]
    copy_code: Optional[str]
    start_time: Optional[str] = Field(description="当前排片开场时刻；未分配为 null")
    end_time: Optional[str] = Field(description="占用结束时刻；未分配为 null")
    assigned_at: Optional[str]


class CopyConflictItem(BaseModel):
    """与候选时段冲突的一场既有占用（同拷贝）。"""

    verification_id: int
    idempotency_key: str
    venue_id: str
    start_time: str = Field(description="该场当前排片开场时刻（UTC）")
    end_time: str = Field(description="该场占用结束时刻 = 开场 + 片长")


class CopyConflictsOut(BaseModel):
    """冲突预检：某拷贝在指定场馆/时段放映时会冲突的既有占用场次。"""

    copy_id: int
    copy_code: str
    film_id: str
    venue_id: str = Field(description="候选场馆")
    start_time: str = Field(description="候选开场时刻（UTC）")
    end_time: str = Field(description="候选占用结束时刻 = 开场 + 片长")
    conflicts: list[CopyConflictItem]
