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
    copy_code: Optional[str] = Field(
        description="当前分配的实体放映拷贝编号；未分配（含未配置拷贝的影片）为 null"
    )
    copy_released: bool = Field(
        description="true 表示场次已取消、拷贝分配已释放（行保留留痕）"
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
    copy_code: Optional[str] = Field(
        default=None,
        description="取消时一并释放的拷贝分配编号（该场次已分配拷贝时非空）"
    )
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
    copy_conflicts: Optional[list["CopyConflictOut"]] = Field(
        default=None,
        description="reject_reason=COPY_CONFLICT 时附冲突场次快照；其他情况为 null"
    )
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


# ----------------------------------------------------------------------
# 实体放映拷贝（physical copies）登记 / 分配 / 占用冲突
# ----------------------------------------------------------------------
class CopyRegisterRequest(BaseModel):
    """运营方登记一份实体放映拷贝。

    影片在尚无核准流水时登记；同片追加拷贝的片长/转运分钟数须与首次一致。
    """

    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    copy_code: str = Field(
        min_length=1, max_length=128,
        description="唯一拷贝编号（全库唯一，运营方给定）",
        examples=["print-AKR-001"],
    )
    duration_minutes: int = Field(
        ge=1, le=1_000_000,
        description="正整数片长（分钟）",
        examples=[118],
    )
    transport_minutes: int = Field(
        ge=0, le=1_000_000,
        description="非负跨场馆转运分钟数（同片一致）",
        examples=[45],
    )
    note: Optional[str] = Field(default=None, max_length=500)


class CopyOccupancyOut(BaseModel):
    """一份拷贝的当前占用：未取消的已分配核准场次，按当前排片计时。"""

    idempotency_key: str = Field(description="占用该拷贝的场次幂等键")
    venue_id: str = Field(description="当前排片场馆（改签不改场馆）")
    start_time: str = Field(description="当前排片开场时刻（UTC）")
    end_time: str = Field(description="占用结束 = 开场 + 片长（UTC，不含）")
    ready_time: str = Field(
        description="拷贝最早可在另一场馆开演的时刻 = 结束 + 跨场馆转运分钟（UTC）"
    )


class CopyOut(BaseModel):
    id: int = Field(description="拷贝编号（库内自增 id）")
    film_id: str = Field(description="所属影片")
    copy_code: str = Field(description="唯一拷贝编号")
    duration_minutes: int = Field(description="片长（分钟，正整数）")
    transport_minutes: int = Field(description="跨场馆转运分钟数（非负）")
    note: Optional[str]
    created_at: str
    used_sessions: int = Field(description="当前持有该拷贝分配的未取消核准场次数")
    occupancies: Optional[list[CopyOccupancyOut]] = Field(
        default=None,
        description="当前占用该拷贝的场次（按开场时刻升序）；仅单拷贝详情返回",
    )


class CopyAssignmentRequest(BaseModel):
    """为已核准、未取消、未开场的场次指定一份同片拷贝。"""

    copy_code: str = Field(
        min_length=1, max_length=128,
        description="同片拷贝的唯一编号",
        examples=["print-AKR-001"],
    )


class CopyConflictOut(BaseModel):
    """与目标时段相冲突的一场既有拷贝占用（按当前排片）。"""

    idempotency_key: str = Field(description="冲突场次的幂等键")
    venue_id: str = Field(description="冲突场馆（与目标相同=同场馆时段重叠；不同=转运不达标）")
    start_time: str = Field(description="冲突场次当前开场时刻（UTC）")
    end_time: str = Field(description="冲突场次占用结束（开场+片长，UTC）")
    relation: Literal["same_venue_overlap", "cross_venue_transport"] = Field(
        description="同场馆时段重叠 / 跨场馆转运时间不足"
    )


class CopyAssignmentOut(BaseModel):
    idempotency_key: str = Field(description="场次原核验幂等键")
    copy_code: str = Field(description="当前分配到的拷贝编号")
    film_id: str
    venue_id: str = Field(description="场次场馆（当前排片）")
    start_time: str = Field(description="场次当前开场时刻（UTC）")
    assigned_at: str = Field(description="首次分配登记时间（UTC）")
    replaced: bool = Field(description="true 表示本次在同一事务内原子替换了原拷贝")
    released: bool = Field(description="true 表示该场次已取消，分配不再占用拷贝（保留留痕）")
    replay: bool = Field(
        description="true 表示同场次同拷贝重试，返回现状（不产生新的分配/替换）"
    )


class CopyAssignmentStatusOut(BaseModel):
    idempotency_key: str
    assigned: bool = Field(description="是否存在分配记录（含已取消后保留的留痕）")
    copy_code: Optional[str] = Field(description="当前/留痕的拷贝编号；从未分配为 null")
    assigned_at: Optional[str]
    released: bool = Field(description="场次已取消，分配已释放（行保留留痕）")


class CopyConflictProbeRequest(BaseModel):
    """拷贝占用冲突探针：不写任何分配，只返回目标时段的冲突场次。"""

    film_id: str = Field(min_length=1, max_length=128)
    copy_code: str = Field(min_length=1, max_length=128)
    venue_id: str = Field(min_length=1, max_length=128)
    start_time: datetime = Field(
        description="拟开场时间，ISO-8601；不带时区时按 UTC 处理",
        examples=["2026-10-10T19:30:00Z"],
    )
    exclude_idempotency_key: Optional[str] = Field(
        default=None,
        description="可选：模拟改签/换拷贝时排除本场自身（其占用视为先释放）",
    )


class CopyConflictProbeOut(BaseModel):
    film_id: str
    copy_code: str
    venue_id: str
    start_time: str
    duration_minutes: int
    transport_minutes: int
    conflict: bool = Field(description="目标时段是否与既有占用冲突")
    conflicts: list[CopyConflictOut] = Field(description="冲突场次列表（按开场升序）")


CopyOut.model_rebuild()
OpeningCodeStatusOut.model_rebuild()
RescheduleOut.model_rebuild()
