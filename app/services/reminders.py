"""产能跟进提醒编排。

按项目承诺产能、实际达产率与上次联系时间计算下一次提醒窗口；
节假日顺延规则（节假日列表 + 是否跳过周末）由调用方提供并随提醒留存，
处理完成后续排下一次提醒时沿用同一份规则。

并发约定：所有写操作采用「读阶段 → 释放读事务 → CAS 写阶段」，
CAS 以 id + version + is_open 为条件，配合 follow_up_id 上
is_open=1 的部分唯一索引，保证重复触发与并发转交不会产生脏数据。
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..enums import (
    FollowUpReminderStatus,
    FollowUpStatus,
    ReminderEventType,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"

# 缺口率分档：缺口越大提醒越频繁（与 crud._determine_priority 的档位一致）
INTERVAL_RULES: List[Tuple[float, int, str]] = [
    (50.0, 3, "缺口≥50%，每 3 天提醒"),
    (30.0, 7, "缺口≥30%，每 7 天提醒"),
    (15.0, 14, "缺口≥15%，每 14 天提醒"),
]
DEFAULT_INTERVAL_DAYS = 30
DEFAULT_INTERVAL_LABEL = "缺口<15%或缺少达产数据，每 30 天提醒"

OPEN_REMINDER_STATUSES = (
    FollowUpReminderStatus.PENDING,
    FollowUpReminderStatus.DUE,
    FollowUpReminderStatus.CLAIMED,
    FollowUpReminderStatus.DEFERRED,
)
CLAIMABLE_STATUSES = (
    FollowUpReminderStatus.PENDING,
    FollowUpReminderStatus.DUE,
    FollowUpReminderStatus.DEFERRED,
)
OPEN_FOLLOW_UP_STATUSES = (FollowUpStatus.PENDING, FollowUpStatus.IN_PROGRESS)

MAX_HOLIDAY_SHIFT_DAYS = 366


class ReminderError(ValueError):
    """提醒编排领域错误基类。"""


class ReminderNotFoundError(ReminderError):
    """提醒不存在。"""


class ReminderStateError(ReminderError):
    """提醒状态不允许该操作，或发生并发冲突（对应 HTTP 409）。"""


class ReminderValidationError(ReminderError):
    """请求参数不合法（对应 HTTP 400）。"""


# ---------------------------------------------------------------------------
# 时间与顺延计算（纯函数，便于针对时区边界做单元测试）
# ---------------------------------------------------------------------------


def _as_utc_aware(dt: datetime) -> datetime:
    """naive 时间一律按 UTC 解释（与全库 naive UTC 存储约定一致）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_naive_utc(dt: datetime) -> datetime:
    return _as_utc_aware(dt).replace(tzinfo=None)


def _now_utc(now: Optional[datetime] = None) -> datetime:
    return _as_utc_aware(now) if now is not None else datetime.now(timezone.utc)


def load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ReminderValidationError(f"无效时区：{timezone_name}")


def resolve_now(as_of: Optional[datetime], timezone_name: str) -> datetime:
    """接口的 as_of 基准时间：naive 时按请求时区解释，aware 时直接使用。"""
    if as_of is None:
        return datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=load_timezone(timezone_name)).astimezone(timezone.utc)
    return as_of.astimezone(timezone.utc)


def compute_interval_days(
    utilization_rate: Optional[float] = None,
    gap_percentage: Optional[float] = None,
) -> Tuple[int, str]:
    """按达产缺口确定提醒间隔天数。"""
    gap = gap_percentage
    if gap is None and utilization_rate is not None:
        gap = max(0.0, 100.0 - utilization_rate)
    if gap is None:
        return DEFAULT_INTERVAL_DAYS, DEFAULT_INTERVAL_LABEL
    for threshold, days, label in INTERVAL_RULES:
        if gap >= threshold:
            return days, label
    return DEFAULT_INTERVAL_DAYS, DEFAULT_INTERVAL_LABEL


def apply_holiday_deferral(
    candidate_local: datetime,
    holidays: Set[date],
    defer_weekends: bool,
) -> Tuple[datetime, int]:
    """节假日顺延：候选时间落在节假日（或周末，若启用）则顺延到下一工作日。"""
    shifted = candidate_local
    days = 0
    while shifted.date() in holidays or (defer_weekends and shifted.weekday() >= 5):
        shifted = shifted + timedelta(days=1)
        days += 1
        if days > MAX_HOLIDAY_SHIFT_DAYS:
            raise ReminderValidationError(
                f"节假日顺延超过 {MAX_HOLIDAY_SHIFT_DAYS} 天，请检查顺延规则"
            )
    return shifted, days


def compute_next_due_at(
    *,
    last_contact_at: Optional[datetime],
    interval_days: int,
    now: datetime,
    timezone_name: str,
    holidays: Set[date],
    defer_weekends: bool,
) -> Tuple[datetime, Dict[str, Any]]:
    """计算下一次提醒到期时间（naive UTC）及推算过程。

    从未联系过的跟进事项立即进入提醒窗口；否则以上次联系时间 + 间隔为候选，
    再按调用方提供的节假日规则顺延。节假日按请求时区的当地日期判断。
    """
    tz = load_timezone(timezone_name)
    now_local = _as_utc_aware(now).astimezone(tz)
    base_local: Optional[datetime] = None
    if last_contact_at is not None:
        base_local = _as_utc_aware(last_contact_at).astimezone(tz)
        candidate = base_local + timedelta(days=interval_days)
    else:
        candidate = now_local
    due_local, shift_days = apply_holiday_deferral(candidate, holidays, defer_weekends)
    due_naive_utc = due_local.astimezone(timezone.utc).replace(tzinfo=None)
    meta = {
        "base_local": base_local.isoformat() if base_local else None,
        "computed_local": candidate.isoformat(),
        "due_local": due_local.isoformat(),
        "holiday_shift_days": shift_days,
    }
    return due_naive_utc, meta


# ---------------------------------------------------------------------------
# 生成依据
# ---------------------------------------------------------------------------


def _get_promised_monthly_capacity(project: Optional[models.Project]) -> float:
    if project is None:
        return 0.0
    if project.promised_monthly_capacity_tonnes:
        return project.promised_monthly_capacity_tonnes
    if project.expected_annual_capacity_tonnes:
        return project.expected_annual_capacity_tonnes / 12.0
    return 0.0


def _get_latest_report(
    db: Session, project_id: int
) -> Optional[models.MonthlyCapacityReport]:
    return (
        db.query(models.MonthlyCapacityReport)
        .filter(models.MonthlyCapacityReport.project_id == project_id)
        .order_by(
            models.MonthlyCapacityReport.report_year.desc(),
            models.MonthlyCapacityReport.report_month.desc(),
        )
        .first()
    )


def _prepare_reminder_context(
    db: Session,
    follow_up: models.CapacityFollowUp,
    *,
    timezone_name: str,
    holidays: Set[date],
    defer_weekends: bool,
    now: datetime,
) -> Dict[str, Any]:
    """汇总生成依据并算出到期时间，返回可直接建表的字段字典。"""
    project = db.get(models.Project, follow_up.project_id)
    promised = _get_promised_monthly_capacity(project)
    report = _get_latest_report(db, follow_up.project_id)

    utilization: Optional[float] = None
    if report is not None:
        utilization = report.capacity_utilization_rate
        if utilization is None and promised > 0:
            utilization = round(report.actual_output_tonnes / promised * 100, 2)

    # 缺口优先按最新达产率折算（反映当前实际）；无报告时回退到跟进事项登记的历史缺口
    if utilization is not None:
        gap: Optional[float] = round(max(0.0, 100.0 - utilization), 2)
    else:
        gap = follow_up.gap_percentage

    interval_days, interval_rule = compute_interval_days(utilization, gap)
    due_at, meta = compute_next_due_at(
        last_contact_at=follow_up.last_contact_at,
        interval_days=interval_days,
        now=now,
        timezone_name=timezone_name,
        holidays=holidays,
        defer_weekends=defer_weekends,
    )

    owner = follow_up.responsible_person
    if not owner and project is not None:
        owner = project.project_leader

    basis = {
        "follow_up_id": follow_up.id,
        "project_id": follow_up.project_id,
        "promised_monthly_capacity_tonnes": round(promised, 2),
        "latest_report": (
            {
                "year": report.report_year,
                "month": report.report_month,
                "actual_output_tonnes": report.actual_output_tonnes,
            }
            if report
            else None
        ),
        "utilization_rate": utilization,
        "gap_percentage": gap,
        "follow_up_gap_percentage": follow_up.gap_percentage,
        "interval_days": interval_days,
        "interval_rule": interval_rule,
        "last_contact_at": (
            _as_utc_aware(follow_up.last_contact_at).isoformat()
            if follow_up.last_contact_at
            else None
        ),
        "timezone": timezone_name,
        "holidays": sorted(d.isoformat() for d in holidays),
        "defer_weekends": defer_weekends,
        **meta,
        "generated_at": _now_utc(now).isoformat(),
    }

    return {
        "follow_up_id": follow_up.id,
        "project_id": follow_up.project_id,
        "owner": owner,
        "status": FollowUpReminderStatus.PENDING,
        "due_at": due_at,
        "timezone": timezone_name,
        "interval_days": interval_days,
        "utilization_rate": utilization,
        "gap_percentage": gap,
        "basis": basis,
        "is_open": True,
    }


def _new_event(
    reminder_id: int,
    follow_up_id: int,
    event_type: ReminderEventType,
    *,
    actor: Optional[str] = None,
    from_owner: Optional[str] = None,
    to_owner: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> models.FollowUpReminderEvent:
    return models.FollowUpReminderEvent(
        reminder_id=reminder_id,
        follow_up_id=follow_up_id,
        event_type=event_type,
        actor=actor,
        from_owner=from_owner,
        to_owner=to_owner,
        reason=reason,
        detail=detail,
    )


def _cas_update(
    db: Session,
    reminder_id: int,
    expected_version: int,
    values: Dict[str, Any],
    extra_where: Optional[Sequence[Any]] = None,
) -> bool:
    """按 id + version + is_open 条件更新，冲突时不影响任何行。"""
    conditions = [
        models.FollowUpReminder.id == reminder_id,
        models.FollowUpReminder.version == expected_version,
        models.FollowUpReminder.is_open == True,  # noqa: E712
    ]
    if extra_where:
        conditions.extend(extra_where)
    stmt = (
        update(models.FollowUpReminder)
        .where(*conditions)
        .values(**values, version=expected_version + 1)
    )
    return db.execute(stmt).rowcount == 1


# ---------------------------------------------------------------------------
# 生成与到期扫描
# ---------------------------------------------------------------------------


def generate_reminders(
    db: Session,
    *,
    project_id: Optional[int] = None,
    follow_up_id: Optional[int] = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    holidays: Optional[Sequence[date]] = None,
    defer_weekends: bool = True,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """为开放的跟进事项编排提醒，幂等去重：已有未结提醒的事项跳过。"""
    load_timezone(timezone_name)
    now = resolve_now(as_of, timezone_name)
    holiday_set: Set[date] = set(holidays or [])

    # ---- 读阶段 ----
    query = db.query(models.CapacityFollowUp).filter(
        models.CapacityFollowUp.status.in_(OPEN_FOLLOW_UP_STATUSES)
    )
    if project_id is not None:
        query = query.filter(models.CapacityFollowUp.project_id == project_id)
    if follow_up_id is not None:
        query = query.filter(models.CapacityFollowUp.id == follow_up_id)
    follow_ups = query.all()

    open_rows = (
        db.query(models.FollowUpReminder.follow_up_id)
        .filter(models.FollowUpReminder.is_open == True)  # noqa: E712
        .all()
    )
    open_follow_up_ids = {row[0] for row in open_rows}

    contexts: Dict[int, Dict[str, Any]] = {}
    follow_up_ids: List[int] = []
    for fu in follow_ups:
        follow_up_ids.append(fu.id)
        if fu.id in open_follow_up_ids:
            continue
        contexts[fu.id] = _prepare_reminder_context(
            db,
            fu,
            timezone_name=timezone_name,
            holidays=holiday_set,
            defer_weekends=defer_weekends,
            now=now,
        )

    # 释放读事务：写阶段首条语句即为写操作，避免 SQLite 读锁升级冲突
    db.rollback()

    # ---- 写阶段 ----
    items: List[Dict[str, Any]] = []
    for fu_id in follow_up_ids:
        if fu_id in open_follow_up_ids:
            items.append(
                {
                    "follow_up_id": fu_id,
                    "result": "skipped_existing",
                    "reminder_id": None,
                    "reason": "已存在未结提醒，跳过",
                }
            )
            continue
        ctx = contexts[fu_id]
        reminder = models.FollowUpReminder(**ctx)
        try:
            with db.begin_nested():
                db.add(reminder)
                db.flush()
                db.add(
                    _new_event(
                        reminder.id,
                        fu_id,
                        ReminderEventType.GENERATED,
                        actor="系统编排",
                        to_owner=reminder.owner,
                        detail={
                            "due_at": reminder.due_at.isoformat(),
                            "interval_days": reminder.interval_days,
                        },
                    )
                )
                db.flush()
        except IntegrityError:
            # 并发生成撞唯一索引：按已存在处理，保证幂等
            items.append(
                {
                    "follow_up_id": fu_id,
                    "result": "skipped_existing",
                    "reminder_id": None,
                    "reason": "已存在未结提醒（并发去重），跳过",
                }
            )
            continue
        items.append(
            {
                "follow_up_id": fu_id,
                "result": "generated",
                "reminder_id": reminder.id,
                "reason": None,
            }
        )
    db.commit()

    generated = [i for i in items if i["result"] == "generated"]
    return {
        "generated_count": len(generated),
        "skipped_count": len(items) - len(generated),
        "items": items,
    }


def process_due_reminders(
    db: Session, now: Optional[datetime] = None
) -> List[int]:
    """把到期待提醒/已延期的提醒翻转为待处理。幂等，服务启动时自动调用。"""
    now_naive = _now_utc(now).replace(tzinfo=None)

    rows = (
        db.query(models.FollowUpReminder.id)
        .filter(
            models.FollowUpReminder.is_open == True,  # noqa: E712
            models.FollowUpReminder.status.in_(
                [FollowUpReminderStatus.PENDING, FollowUpReminderStatus.DEFERRED]
            ),
            models.FollowUpReminder.due_at <= now_naive,
        )
        .all()
    )
    candidate_ids = [row[0] for row in rows]
    db.rollback()

    processed: List[int] = []
    for reminder_id in candidate_ids:
        result = db.execute(
            update(models.FollowUpReminder)
            .where(
                models.FollowUpReminder.id == reminder_id,
                models.FollowUpReminder.status.in_(
                    [FollowUpReminderStatus.PENDING, FollowUpReminderStatus.DEFERRED]
                ),
                models.FollowUpReminder.due_at <= now_naive,
            )
            .values(
                status=FollowUpReminderStatus.DUE,
                version=models.FollowUpReminder.version + 1,
                updated_at=now_naive,
            )
        )
        if result.rowcount == 1:
            reminder = db.get(models.FollowUpReminder, reminder_id)
            db.add(
                _new_event(
                    reminder_id,
                    reminder.follow_up_id,
                    ReminderEventType.DUE,
                    actor="系统扫描",
                    detail={"due_at": reminder.due_at.isoformat()},
                )
            )
            processed.append(reminder_id)
    db.commit()
    return processed


# ---------------------------------------------------------------------------
# 领取 / 延期 / 转交 / 处理
# ---------------------------------------------------------------------------


def _snapshot_reminders(
    db: Session, reminder_ids: Sequence[int]
) -> Dict[int, Dict[str, Any]]:
    rows = (
        db.query(models.FollowUpReminder)
        .filter(models.FollowUpReminder.id.in_(reminder_ids))
        .all()
    )
    return {
        r.id: {
            "version": r.version,
            "is_open": r.is_open,
            "status": r.status,
            "owner": r.owner,
            "follow_up_id": r.follow_up_id,
            "due_at": r.due_at,
            "timezone": r.timezone,
            "basis": r.basis or {},
        }
        for r in rows
    }


def claim_reminders(
    db: Session,
    *,
    owner: str,
    reminder_ids: Sequence[int],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """按责任人领取：仅本人（或尚未分配）的开放提醒可领取。"""
    now_naive = _now_utc(now).replace(tzinfo=None)
    snapshots = _snapshot_reminders(db, reminder_ids)
    db.rollback()

    results: List[Dict[str, Any]] = []
    for reminder_id in reminder_ids:
        snap = snapshots.get(reminder_id)
        if snap is None:
            results.append(
                {"reminder_id": reminder_id, "ok": False, "error": "提醒事项不存在"}
            )
            continue
        if not snap["is_open"]:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": "提醒已处理或已取消，无法领取",
                }
            )
            continue
        if snap["status"] not in CLAIMABLE_STATUSES:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": f"当前状态「{snap['status'].value}」不可领取",
                }
            )
            continue
        current_owner = snap["owner"]
        if current_owner and current_owner != owner:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": f"该提醒责任人为「{current_owner}」，需先转交",
                }
            )
            continue
        ok = _cas_update(
            db,
            reminder_id,
            snap["version"],
            {
                "status": FollowUpReminderStatus.CLAIMED,
                "owner": owner,
                "claimed_at": now_naive,
                "updated_at": now_naive,
            },
            extra_where=[
                models.FollowUpReminder.status.in_(CLAIMABLE_STATUSES)
            ],
        )
        if not ok:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": "该提醒已被并发修改，请刷新后重试",
                }
            )
            continue
        db.add(
            _new_event(
                reminder_id,
                snap["follow_up_id"],
                ReminderEventType.CLAIMED,
                actor=owner,
                to_owner=owner,
            )
        )
        if not current_owner:
            follow_up = db.get(models.CapacityFollowUp, snap["follow_up_id"])
            if follow_up is not None:
                follow_up.responsible_person = owner
        results.append({"reminder_id": reminder_id, "ok": True, "error": None})
    db.commit()
    return results


def defer_reminders(
    db: Session,
    *,
    reminder_ids: Sequence[int],
    reason: str,
    defer_until: Optional[date] = None,
    defer_days: Optional[int] = None,
    operator: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """批量延期：按天数（沿用节假日顺延规则）或指定日期，必须填写延期原因。"""
    if not reason or not reason.strip():
        raise ReminderValidationError("延期原因必填")
    if (defer_until is None) == (defer_days is None):
        raise ReminderValidationError("defer_until 与 defer_days 须且只能提供一个")
    if defer_days is not None and defer_days < 1:
        raise ReminderValidationError("defer_days 必须 ≥ 1")

    now_utc = _now_utc(now)
    now_naive = now_utc.replace(tzinfo=None)
    snapshots = _snapshot_reminders(db, reminder_ids)
    db.rollback()

    results: List[Dict[str, Any]] = []
    for reminder_id in reminder_ids:
        snap = snapshots.get(reminder_id)
        if snap is None:
            results.append(
                {"reminder_id": reminder_id, "ok": False, "error": "提醒事项不存在"}
            )
            continue
        if not snap["is_open"]:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": "提醒已处理或已取消，无法延期",
                }
            )
            continue

        tz = load_timezone(snap["timezone"] or DEFAULT_TIMEZONE)
        current_due_local = _as_utc_aware(snap["due_at"]).astimezone(tz)
        if defer_days is not None:
            base_utc = max(_as_utc_aware(snap["due_at"]), now_utc)
            candidate_local = base_utc.astimezone(tz) + timedelta(days=defer_days)
            basis = snap["basis"]
            holidays = {
                date.fromisoformat(d) for d in basis.get("holidays", [])
            }
            new_due_local, _ = apply_holiday_deferral(
                candidate_local, holidays, basis.get("defer_weekends", True)
            )
        else:
            new_due_local = datetime.combine(
                defer_until, current_due_local.timetz()
            )
        new_due_utc = _as_utc_aware(new_due_local)
        if new_due_utc <= now_utc:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": "延期后的时间必须晚于当前时间",
                }
            )
            continue
        new_due_naive = new_due_utc.replace(tzinfo=None)

        ok = _cas_update(
            db,
            reminder_id,
            snap["version"],
            {
                "status": FollowUpReminderStatus.DEFERRED,
                "due_at": new_due_naive,
                "defer_reason": reason,
                "deferred_count": models.FollowUpReminder.deferred_count + 1,
                "updated_at": now_naive,
            },
        )
        if not ok:
            results.append(
                {
                    "reminder_id": reminder_id,
                    "ok": False,
                    "error": "该提醒已被并发修改，请刷新后重试",
                }
            )
            continue
        db.add(
            _new_event(
                reminder_id,
                snap["follow_up_id"],
                ReminderEventType.DEFERRED,
                actor=operator,
                reason=reason,
                detail={
                    "defer_until": defer_until.isoformat() if defer_until else None,
                    "defer_days": defer_days,
                    "new_due_at": new_due_naive.isoformat(),
                },
            )
        )
        results.append({"reminder_id": reminder_id, "ok": True, "error": None})
    db.commit()
    return results


def transfer_reminder(
    db: Session,
    *,
    reminder_id: int,
    to_owner: str,
    reason: Optional[str] = None,
    operator: Optional[str] = None,
    expected_version: Optional[int] = None,
    now: Optional[datetime] = None,
) -> models.FollowUpReminder:
    """转交责任人：CAS 防并发，事件表保留原处理链，同步跟进事项责任人。"""
    now_naive = _now_utc(now).replace(tzinfo=None)
    reminder = db.get(models.FollowUpReminder, reminder_id)
    if reminder is None:
        raise ReminderNotFoundError("提醒事项不存在")
    if not reminder.is_open:
        raise ReminderStateError("提醒已处理或已取消，无法转交")
    if reminder.owner == to_owner:
        raise ReminderValidationError("接收人与当前责任人相同，无需转交")
    if expected_version is not None and expected_version != reminder.version:
        raise ReminderStateError("该提醒已被他人修改，请刷新后重试")

    version = reminder.version
    from_owner = reminder.owner
    follow_up_id = reminder.follow_up_id
    db.rollback()

    ok = _cas_update(
        db,
        reminder_id,
        version,
        {"owner": to_owner, "updated_at": now_naive},
    )
    if not ok:
        raise ReminderStateError("转交冲突：该提醒已被并发修改，请刷新后重试")

    db.add(
        _new_event(
            reminder_id,
            follow_up_id,
            ReminderEventType.TRANSFERRED,
            actor=operator,
            from_owner=from_owner,
            to_owner=to_owner,
            reason=reason,
        )
    )
    follow_up = db.get(models.CapacityFollowUp, follow_up_id)
    if follow_up is not None:
        follow_up.responsible_person = to_owner
    db.commit()
    return db.get(models.FollowUpReminder, reminder_id)


def complete_reminder(
    db: Session,
    *,
    reminder_id: int,
    contacted_at: Optional[datetime] = None,
    note: Optional[str] = None,
    operator: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[int, Optional[int]]:
    """处理完成：记录联系时间，并按既定规则编排下一次提醒。

    返回 (已处理提醒ID, 下一次提醒ID或None)。
    """
    now_utc = _now_utc(now)
    now_naive = now_utc.replace(tzinfo=None)
    contacted_utc = _now_utc(contacted_at) if contacted_at else now_utc
    contacted_naive = contacted_utc.replace(tzinfo=None)

    reminder = db.get(models.FollowUpReminder, reminder_id)
    if reminder is None:
        raise ReminderNotFoundError("提醒事项不存在")
    if not reminder.is_open:
        raise ReminderStateError("提醒已处理或已取消，无法重复处理")

    version = reminder.version
    follow_up_id = reminder.follow_up_id
    timezone_name = reminder.timezone or DEFAULT_TIMEZONE
    basis = reminder.basis or {}
    db.rollback()

    ok = _cas_update(
        db,
        reminder_id,
        version,
        {
            "status": FollowUpReminderStatus.DONE,
            "is_open": False,
            "completed_at": contacted_naive,
            "updated_at": now_naive,
        },
    )
    if not ok:
        raise ReminderStateError("该提醒已被并发修改，请刷新后重试")

    db.add(
        _new_event(
            reminder_id,
            follow_up_id,
            ReminderEventType.COMPLETED,
            actor=operator,
            reason=note,
            detail={"contacted_at": contacted_utc.isoformat()},
        )
    )

    next_reminder_id: Optional[int] = None
    follow_up = db.get(models.CapacityFollowUp, follow_up_id)
    if follow_up is not None:
        follow_up.last_contact_at = contacted_naive
        if follow_up.status == FollowUpStatus.PENDING:
            follow_up.status = FollowUpStatus.IN_PROGRESS
        db.flush()
        if follow_up.status in OPEN_FOLLOW_UP_STATUSES:
            # 沿用本次生成时的节假日顺延规则续排下一次
            ctx = _prepare_reminder_context(
                db,
                follow_up,
                timezone_name=timezone_name,
                holidays={
                    date.fromisoformat(d) for d in basis.get("holidays", [])
                },
                defer_weekends=basis.get("defer_weekends", True),
                now=contacted_utc,
            )
            next_reminder = models.FollowUpReminder(**ctx)
            try:
                with db.begin_nested():
                    db.add(next_reminder)
                    db.flush()
                    db.add(
                        _new_event(
                            next_reminder.id,
                            follow_up_id,
                            ReminderEventType.GENERATED,
                            actor="系统编排",
                            to_owner=next_reminder.owner,
                            detail={
                                "due_at": next_reminder.due_at.isoformat(),
                                "interval_days": next_reminder.interval_days,
                            },
                        )
                    )
                    db.flush()
                next_reminder_id = next_reminder.id
            except IntegrityError:
                next_reminder_id = None
    db.commit()
    return reminder_id, next_reminder_id


def cancel_open_reminders_for_follow_up(
    db: Session,
    *,
    follow_up_id: int,
    reason: str,
    now: Optional[datetime] = None,
) -> List[int]:
    """跟进事项关闭/解决时取消其未结提醒。仅 flush，由调用方统一提交。"""
    now_naive = _now_utc(now).replace(tzinfo=None)
    reminders = (
        db.query(models.FollowUpReminder)
        .filter(
            models.FollowUpReminder.follow_up_id == follow_up_id,
            models.FollowUpReminder.is_open == True,  # noqa: E712
        )
        .all()
    )
    cancelled_ids: List[int] = []
    for reminder in reminders:
        reminder.status = FollowUpReminderStatus.CANCELLED
        reminder.is_open = False
        reminder.version += 1
        reminder.updated_at = now_naive
        db.add(
            _new_event(
                reminder.id,
                follow_up_id,
                ReminderEventType.CANCELLED,
                actor="系统",
                reason=reason,
            )
        )
        cancelled_ids.append(reminder.id)
    db.flush()
    return cancelled_ids
