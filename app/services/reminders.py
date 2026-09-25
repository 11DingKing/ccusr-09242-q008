"""产能提醒编排服务。

职责：
- generate_reminders：按项目承诺产能、实际达产率、上次联系时间生成提醒，去重；
- mark_due_reminders：将到期提醒置为「已到期」，纯状态扫描，重启后可继续处理；
- claim / claim_for_assignee / batch_postpone / transfer / close：
  领取、按责任人领取、批量延期、转交（保留原处理链）、关闭；
- get_reminder_basis：查询生成依据与完整处理链。

并发控制：所有状态流转均使用「条件 UPDATE」（WHERE status/active/assignee 守卫），
依据 rowcount 判断是否抢占成功，避免两个调用方同时领取/转交同一条提醒。
"""

import json
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..enums import (
    FollowUpStatus,
    ProjectStatus,
    ReminderEventType,
    ReminderStatus,
)
from .reminder_scheduling import (
    HolidaySchedule,
    compute_next_reminder,
    local_date,
)

# 关闭/解决跟进事项后，提醒随之关闭的状态
FOLLOW_UP_TERMINAL_STATUSES = {FollowUpStatus.RESOLVED, FollowUpStatus.CLOSED}


class ReminderError(ValueError):
    """业务规则错误，路由层映射为 4xx。"""


class ReminderConflict(ReminderError):
    """并发冲突（已被他人领取/转交/关闭），路由层映射为 409。"""


# ---------------------------------------------------------------- 内部工具

def _now() -> datetime:
    return datetime.utcnow()


def _dump_chain(chain: list[str]) -> str:
    return json.dumps(chain, ensure_ascii=False)


def _load_chain(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _dedup_key(project_id: int) -> str:
    return f"project:{project_id}"


def _get_project(db: Session, project_id: int) -> Optional[models.Project]:
    return db.query(models.Project).filter(models.Project.id == project_id).first()


def _latest_report(
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


def _latest_contact(
    db: Session, project_id: int
) -> Optional[models.CapacityContactLog]:
    return (
        db.query(models.CapacityContactLog)
        .filter(models.CapacityContactLog.project_id == project_id)
        .order_by(models.CapacityContactLog.contacted_at.desc())
        .first()
    )


def _open_follow_up(
    db: Session, project_id: int
) -> Optional[models.CapacityFollowUp]:
    return (
        db.query(models.CapacityFollowUp)
        .filter(
            models.CapacityFollowUp.project_id == project_id,
            models.CapacityFollowUp.status.in_(
                [FollowUpStatus.PENDING, FollowUpStatus.IN_PROGRESS]
            ),
        )
        .order_by(models.CapacityFollowUp.created_at.desc())
        .first()
    )


def _promised_monthly(project: models.Project) -> float:
    if project.promised_monthly_capacity_tonnes:
        return project.promised_monthly_capacity_tonnes
    if project.expected_annual_capacity_tonnes:
        return project.expected_annual_capacity_tonnes / 12.0
    return 0.0


def _get_reminder(db: Session, reminder_id: int) -> Optional[models.CapacityReminder]:
    return (
        db.query(models.CapacityReminder)
        .filter(models.CapacityReminder.id == reminder_id)
        .first()
    )


def _next_seq(db: Session, reminder_id: int) -> int:
    last = (
        db.query(models.CapacityReminderEvent)
        .filter(models.CapacityReminderEvent.reminder_id == reminder_id)
        .order_by(models.CapacityReminderEvent.seq.desc())
        .first()
    )
    return (last.seq + 1) if last else 1


def _add_event(
    db: Session,
    reminder: models.CapacityReminder,
    event_type: ReminderEventType,
    *,
    actor: Optional[str] = None,
    from_assignee: Optional[str] = None,
    to_assignee: Optional[str] = None,
    from_status: Optional[ReminderStatus] = None,
    to_status: Optional[ReminderStatus] = None,
    scheduled_at: Optional[datetime] = None,
    scheduled_date: Optional[date] = None,
    reason: Optional[str] = None,
    detail: Optional[dict] = None,
) -> models.CapacityReminderEvent:
    event = models.CapacityReminderEvent(
        reminder_id=reminder.id,
        seq=_next_seq(db, reminder.id),
        event_type=event_type,
        actor=actor,
        from_assignee=from_assignee,
        to_assignee=to_assignee,
        from_status=from_status,
        to_status=to_status,
        scheduled_at=scheduled_at,
        scheduled_date=scheduled_date,
        reason=reason,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.add(event)
    return event


def _parse_basis(reminder: models.CapacityReminder) -> dict:
    try:
        return json.loads(reminder.basis)
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- 生成（去重）

def generate_for_project(
    db: Session,
    project_id: int,
    *,
    holidays: Optional[HolidaySchedule] = None,
    timezone: str = "Asia/Shanghai",
    now: Optional[datetime] = None,
) -> Optional[models.CapacityReminder]:
    """为单个项目生成提醒；已存在未关闭提醒时原样返回（去重，幂等）。"""
    reminder, _created = _generate_for_project(
        db,
        project_id,
        holidays=holidays,
        timezone=timezone,
        now=now,
    )
    return reminder


def _generate_for_project(
    db: Session,
    project_id: int,
    *,
    holidays: Optional[HolidaySchedule] = None,
    timezone: str = "Asia/Shanghai",
    now: Optional[datetime] = None,
) -> tuple[models.CapacityReminder, bool]:
    """内部入口，返回 (提醒, 是否本次新建)。"""
    now = now or _now()
    project = _get_project(db, project_id)
    if project is None:
        raise ReminderError("项目不存在")
    if project.status != ProjectStatus.COMMISSIONED:
        raise ReminderError("仅已投产项目可编排提醒")

    # 去重：唯一约束 + 活动行查询双保险；INSERT 冲突由数据库兜底
    existing = (
        db.query(models.CapacityReminder)
        .filter(
            models.CapacityReminder.project_id == project_id,
            models.CapacityReminder.active.is_(True),
        )
        .first()
    )
    if existing is not None:
        return existing, False

    # 关闭后不再重复生成：若最近一条提醒已关闭，仅当关闭之后又出现了
    # 新的未结跟进事项（视为新的一桩事情）时才允许重新编排。
    latest = (
        db.query(models.CapacityReminder)
        .filter(models.CapacityReminder.project_id == project_id)
        .order_by(models.CapacityReminder.id.desc())
        .first()
    )
    if latest is not None and not latest.active:
        newer_follow_up = (
            db.query(models.CapacityFollowUp.id)
            .filter(
                models.CapacityFollowUp.project_id == project_id,
                models.CapacityFollowUp.status.in_(
                    [FollowUpStatus.PENDING, FollowUpStatus.IN_PROGRESS]
                ),
                models.CapacityFollowUp.created_at > latest.closed_at,
            )
            .first()
        )
        if newer_follow_up is None:
            return latest, False

    follow_up = _open_follow_up(db, project_id)
    report = _latest_report(db, project_id)
    contact = _latest_contact(db, project_id)
    promised = _promised_monthly(project)

    remind_at, basis = compute_next_reminder(
        project_id=project_id,
        promised_monthly_capacity_tonnes=promised,
        actual_utilization_rate=report.capacity_utilization_rate if report else None,
        actual_output_tonnes=report.actual_output_tonnes if report else None,
        last_contacted_at=contact.contacted_at if contact else None,
        commissioned_date=project.commissioned_date,
        tz_name=timezone,
        holidays=holidays,
        now_utc=now,
    )
    basis_dict = basis.to_dict()
    basis_dict.update(
        {
            "project_name": project.name,
            "follow_up_id": follow_up.id if follow_up else None,
            "report_id": report.id if report else None,
            "generated_at": now.isoformat() + "Z",
        }
    )

    initial_owner = follow_up.responsible_person if follow_up else project.project_leader
    chain = [initial_owner] if initial_owner else []
    due_local = local_date(remind_at, timezone)
    # 已落在过去时刻的排期直接视为到期
    initial_status = (
        ReminderStatus.DUE if remind_at <= now else ReminderStatus.SCHEDULED
    )

    reminder = models.CapacityReminder(
        project_id=project_id,
        follow_up_id=follow_up.id if follow_up else None,
        dedup_key=_dedup_key(project_id),
        active=True,
        status=initial_status,
        next_remind_at=remind_at,
        remind_date=due_local,
        timezone=timezone,
        basis=json.dumps(basis_dict, ensure_ascii=False),
        chain=_dump_chain(chain),
        assignee=initial_owner,
    )
    db.add(reminder)
    try:
        db.flush()
    except Exception as exc:  # 并发下唯一约束冲突 => 视为已生成
        db.rollback()
        existing = (
            db.query(models.CapacityReminder)
            .filter(
                models.CapacityReminder.project_id == project_id,
                models.CapacityReminder.active.is_(True),
            )
            .first()
        )
        if existing is not None:
            return existing, False
        raise ReminderError("提醒生成失败") from exc

    _add_event(
        db,
        reminder,
        ReminderEventType.GENERATED,
        actor=initial_owner,
        to_assignee=initial_owner,
        to_status=initial_status,
        scheduled_at=remind_at,
        scheduled_date=due_local,
        detail=basis_dict,
    )
    if initial_status == ReminderStatus.DUE:
        _add_event(
            db,
            reminder,
            ReminderEventType.DUE,
            scheduled_at=remind_at,
            scheduled_date=due_local,
        )
    db.commit()
    db.refresh(reminder)
    return reminder, True


def generate_reminders(
    db: Session,
    *,
    project_ids: Optional[Iterable[int]] = None,
    holidays: Optional[HolidaySchedule] = None,
    timezone: str = "Asia/Shanghai",
    now: Optional[datetime] = None,
) -> dict:
    """批量生成（定时任务入口）。只处理已投产项目，逐项目独立失败不影响整体。

    重启安全：该操作幂等——已存在活动提醒的项目直接跳过。
    """
    now = now or _now()
    if project_ids is not None:
        # 显式指定项目：逐一尝试，未投产/不存在的项目进入 failed 报告
        ids = list(dict.fromkeys(project_ids))
    else:
        ids = [
            row[0]
            for row in db.query(models.Project.id)
            .filter(models.Project.status == ProjectStatus.COMMISSIONED)
            .all()
        ]

    created, skipped, failed = [], [], []
    for pid in ids:
        try:
            reminder, was_created = _generate_for_project(
                db, project_id=pid, holidays=holidays, timezone=timezone, now=now
            )
            if was_created:
                created.append(reminder.id)
            else:
                skipped.append(pid)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed.append({"project_id": pid, "error": str(exc)})
    return {"generated": created, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- 到期扫描

def mark_due_reminders(
    db: Session, *, now: Optional[datetime] = None
) -> list[int]:
    """把到达排期时刻的「已排期」提醒置为「已到期」。

    纯 DB 状态推进：服务重启后重新扫描即可继续处理到期事项，无内存状态。
    """
    now = now or _now()
    rows = (
        db.query(models.CapacityReminder)
        .filter(
            models.CapacityReminder.active.is_(True),
            models.CapacityReminder.status == ReminderStatus.SCHEDULED,
            models.CapacityReminder.next_remind_at <= now,
        )
        .all()
    )
    due_ids = []
    for reminder in rows:
        reminder.status = ReminderStatus.DUE
        _add_event(
            db,
            reminder,
            ReminderEventType.DUE,
            from_status=ReminderStatus.SCHEDULED,
            to_status=ReminderStatus.DUE,
            scheduled_at=reminder.next_remind_at,
            scheduled_date=reminder.remind_date,
        )
        due_ids.append(reminder.id)
    if due_ids:
        db.commit()
    return due_ids


# ---------------------------------------------------------------- 领取

def claim_reminder(
    db: Session,
    reminder_id: int,
    claimed_by: str,
    *,
    expected_assignee: Optional[str] = None,
) -> models.CapacityReminder:
    """领取一条到期提醒。条件 UPDATE 保证并发下只有一人领取成功。"""
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        raise ReminderError("提醒不存在")
    if not reminder.active or reminder.status == ReminderStatus.CLOSED:
        raise ReminderError("提醒已关闭，不可领取")

    conditions = [
        models.CapacityReminder.id == reminder_id,
        models.CapacityReminder.status == ReminderStatus.DUE,
        models.CapacityReminder.active.is_(True),
    ]
    if expected_assignee is not None:
        conditions.append(models.CapacityReminder.assignee == expected_assignee)

    result = db.execute(
        update(models.CapacityReminder)
        .where(*conditions)
        .values(
            status=ReminderStatus.CLAIMED,
            claimed_by=claimed_by,
            claimed_at=_now(),
        )
    )
    if result.rowcount == 0:
        db.rollback()
        current = _get_reminder(db, reminder_id)
        if current and current.status == ReminderStatus.CLAIMED:
            raise ReminderConflict(
                f"该提醒已被 {current.claimed_by or '其他责任人'} 领取"
            )
        raise ReminderConflict("提醒当前状态不可领取")

    db.flush()
    _add_event(
        db,
        reminder,
        ReminderEventType.CLAIMED,
        actor=claimed_by,
        to_assignee=reminder.assignee,
        from_status=ReminderStatus.DUE,
        to_status=ReminderStatus.CLAIMED,
    )
    db.commit()
    db.refresh(reminder)
    return reminder


def claim_for_assignee(
    db: Session,
    assignee: str,
    claimed_by: Optional[str] = None,
    *,
    limit: int = 50,
) -> list[models.CapacityReminder]:
    """按责任人领取其名下全部到期提醒（原子领取，逐条条件更新）。"""
    claimed_by = claimed_by or assignee
    rows = (
        db.query(models.CapacityReminder)
        .filter(
            models.CapacityReminder.active.is_(True),
            models.CapacityReminder.status == ReminderStatus.DUE,
            models.CapacityReminder.assignee == assignee,
        )
        .order_by(models.CapacityReminder.next_remind_at)
        .limit(limit)
        .all()
    )
    claimed = []
    for reminder in rows:
        result = db.execute(
            update(models.CapacityReminder)
            .where(
                models.CapacityReminder.id == reminder.id,
                models.CapacityReminder.status == ReminderStatus.DUE,
                models.CapacityReminder.assignee == assignee,
                models.CapacityReminder.active.is_(True),
            )
            .values(
                status=ReminderStatus.CLAIMED,
                claimed_by=claimed_by,
                claimed_at=_now(),
            )
        )
        if result.rowcount == 0:
            continue  # 并发中被他人抢走
        db.flush()
        _add_event(
            db,
            reminder,
            ReminderEventType.CLAIMED,
            actor=claimed_by,
            from_assignee=assignee,
            to_assignee=assignee,
            from_status=ReminderStatus.DUE,
            to_status=ReminderStatus.CLAIMED,
        )
        claimed.append(reminder)
    db.commit()
    for reminder in claimed:
        db.refresh(reminder)
    return claimed


# ---------------------------------------------------------------- 重新排期

def _reschedule(
    db: Session,
    reminder: models.CapacityReminder,
    *,
    interval_days: int,
    holidays: Optional[HolidaySchedule],
    now: datetime,
    event_type: ReminderEventType,
    actor: Optional[str],
    reason: Optional[str],
    to_status: ReminderStatus = ReminderStatus.SCHEDULED,
) -> models.CapacityReminder:
    """按最新依据重新计算下一次提醒（延期/联系后复用）。"""
    project = _get_project(db, reminder.project_id)
    report = _latest_report(db, reminder.project_id)
    contact = _latest_contact(db, reminder.project_id)
    promised = _promised_monthly(project)

    remind_at, basis = compute_next_reminder(
        project_id=reminder.project_id,
        promised_monthly_capacity_tonnes=promised,
        actual_utilization_rate=report.capacity_utilization_rate if report else None,
        actual_output_tonnes=report.actual_output_tonnes if report else None,
        last_contacted_at=contact.contacted_at if contact else None,
        commissioned_date=project.commissioned_date,
        tz_name=reminder.timezone,
        holidays=holidays,
        now_utc=now,
        interval_days=interval_days,
        # 手动延期：从当天起算，且不被承诺截止日收紧
        anchor_today=event_type == ReminderEventType.POSTPONED,
        clamp_deadline=event_type != ReminderEventType.POSTPONED,
    )
    basis_dict = basis.to_dict()
    basis_dict["rescheduled_at"] = now.isoformat() + "Z"
    basis_dict["reschedule_reason"] = reason

    from_status = reminder.status
    reminder.next_remind_at = remind_at
    reminder.remind_date = basis.due_date
    reminder.basis = json.dumps(
        {**_parse_basis(reminder), "latest": basis_dict},
        ensure_ascii=False,
    )
    reminder.status = (
        ReminderStatus.DUE if remind_at <= now else to_status
    )
    reminder.claimed_by = None
    reminder.claimed_at = None

    _add_event(
        db,
        reminder,
        event_type,
        actor=actor,
        reason=reason,
        from_status=from_status,
        to_status=reminder.status,
        scheduled_at=remind_at,
        scheduled_date=basis.due_date,
        detail={"interval_days": interval_days, **basis_dict},
    )
    return reminder


def postpone_reminder(
    db: Session,
    reminder_id: int,
    *,
    days: int,
    reason: str,
    actor: Optional[str] = None,
    holidays: Optional[HolidaySchedule] = None,
    now: Optional[datetime] = None,
) -> models.CapacityReminder:
    """单条延期：从今天起按 days 天重新排期（仍会节假日顺延）。"""
    if days < 1:
        raise ReminderError("延期天数必须大于 0")
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        raise ReminderError("提醒不存在")
    if not reminder.active or reminder.status == ReminderStatus.CLOSED:
        raise ReminderError("提醒已关闭，不可延期")

    reminder = _reschedule(
        db,
        reminder,
        interval_days=days,
        holidays=holidays,
        now=now or _now(),
        event_type=ReminderEventType.POSTPONED,
        actor=actor,
        reason=reason,
    )
    db.commit()
    db.refresh(reminder)
    return reminder


def batch_postpone(
    db: Session,
    reminder_ids: Iterable[int],
    *,
    days: int,
    reason: str,
    actor: Optional[str] = None,
    holidays: Optional[HolidaySchedule] = None,
    now: Optional[datetime] = None,
) -> dict:
    """批量延期。已关闭的条目跳过并在结果中标记；整体一个事务。"""
    if days < 1:
        raise ReminderError("延期天数必须大于 0")
    ids = list(dict.fromkeys(reminder_ids))  # 去重保序
    postponed, skipped = [], []
    for rid in ids:
        reminder = _get_reminder(db, rid)
        if reminder is None:
            skipped.append({"reminder_id": rid, "reason": "提醒不存在"})
            continue
        if not reminder.active or reminder.status == ReminderStatus.CLOSED:
            skipped.append({"reminder_id": rid, "reason": "提醒已关闭"})
            continue
        _reschedule(
            db,
            reminder,
            interval_days=days,
            holidays=holidays,
            now=now or _now(),
            event_type=ReminderEventType.POSTPONED,
            actor=actor,
            reason=reason,
        )
        postponed.append(rid)
    db.commit()
    return {"postponed": postponed, "skipped": skipped}


# ---------------------------------------------------------------- 转交

def transfer_reminder(
    db: Session,
    reminder_id: int,
    *,
    to_assignee: str,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
) -> models.CapacityReminder:
    """责任人转交：保留完整处理链，条件 UPDATE 防止与领取/关闭并发交错。"""
    if not to_assignee or not to_assignee.strip():
        raise ReminderError("转交对象不能为空")
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        raise ReminderError("提醒不存在")
    if not reminder.active:
        raise ReminderError("提醒已关闭，不可转交")

    from_assignee = reminder.assignee
    old_chain = _load_chain(reminder.chain)
    new_chain = old_chain + ([to_assignee] if to_assignee not in old_chain else [])
    from_status = reminder.status
    target_status = (
        ReminderStatus.DUE
        if from_status == ReminderStatus.CLAIMED
        else from_status
    )

    # 守卫：转交期间必须仍处于活动且责任人未变（乐观版本控制）
    result = db.execute(
        update(models.CapacityReminder)
        .where(
            models.CapacityReminder.id == reminder_id,
            models.CapacityReminder.active.is_(True),
            models.CapacityReminder.assignee == from_assignee,
        )
        .values(
            assignee=to_assignee,
            chain=_dump_chain(new_chain),
            # 已领取的提醒转交后回到到期池，等待新责任人领取
            status=target_status,
            claimed_by=None,
            claimed_at=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise ReminderConflict("转交冲突：责任人已被其他操作变更，请重试")

    db.flush()
    db.refresh(reminder)
    _add_event(
        db,
        reminder,
        ReminderEventType.TRANSFERRED,
        actor=actor,
        from_assignee=from_assignee,
        to_assignee=to_assignee,
        from_status=from_status,
        to_status=target_status,
        reason=reason,
        detail={"chain": new_chain},
    )
    db.commit()
    db.refresh(reminder)
    return reminder


# ---------------------------------------------------------------- 关闭

def close_reminder(
    db: Session,
    reminder_id: int,
    *,
    reason: str,
    actor: Optional[str] = None,
) -> models.CapacityReminder:
    """关闭提醒：active 置 False 并释放去重键，之后不再重复生成。"""
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        raise ReminderError("提醒不存在")
    if not reminder.active:
        raise ReminderConflict("提醒已关闭，请勿重复操作")

    now = _now()
    # 去重键释放：关闭行改写为关闭专属键，项目可在未来按需重新编排
    closed_key = f"closed:{reminder.project_id}:{reminder.id}"
    from_status = reminder.status
    result = db.execute(
        update(models.CapacityReminder)
        .where(
            models.CapacityReminder.id == reminder_id,
            models.CapacityReminder.active.is_(True),
        )
        .values(
            active=False,
            status=ReminderStatus.CLOSED,
            dedup_key=closed_key,
            closed_at=now,
            close_reason=reason,
            claimed_by=None,
            claimed_at=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise ReminderConflict("提醒已被其他操作关闭")

    db.flush()
    _add_event(
        db,
        reminder,
        ReminderEventType.CLOSED,
        actor=actor,
        reason=reason,
        from_status=from_status,
        to_status=ReminderStatus.CLOSED,
    )
    db.commit()
    db.refresh(reminder)
    return reminder


# ---------------------------------------------------------------- 联系登记

def record_contact(
    db: Session,
    reminder_id: int,
    *,
    contacted_by: str,
    contacted_at: Optional[datetime] = None,
    channel: Optional[str] = None,
    content: Optional[str] = None,
    interval_days: Optional[int] = None,
    holidays: Optional[HolidaySchedule] = None,
    close: bool = False,
    close_reason: Optional[str] = None,
) -> models.CapacityContactLog:
    """登记一次企业联系；默认依据「上次联系时间」重排下一次提醒。

    close=True 时直接关闭提醒（如企业承诺整改完成），关闭后不再生成。
    """
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        raise ReminderError("提醒不存在")
    if not reminder.active:
        raise ReminderError("提醒已关闭，不可登记联系")

    contacted_at = contacted_at or _now()
    log = models.CapacityContactLog(
        project_id=reminder.project_id,
        reminder_id=reminder.id,
        contacted_by=contacted_by,
        contacted_at=contacted_at,
        channel=channel,
        content=content,
    )
    db.add(log)
    db.flush()
    reminder.contact_count = (reminder.contact_count or 0) + 1

    if close:
        db.refresh(reminder)
        db.commit()
        close_reminder(
            db,
            reminder_id,
            reason=close_reason or "联系后关闭",
            actor=contacted_by,
        )
    else:
        gap = interval_days
        if gap is None:
            # 回落到达产率对应档位
            report = _latest_report(db, reminder.project_id)
            from .reminder_scheduling import interval_for_utilization

            gap = interval_for_utilization(
                report.capacity_utilization_rate if report else None
            )
        _reschedule(
            db,
            reminder,
            interval_days=gap,
            holidays=holidays,
            now=_now(),
            event_type=ReminderEventType.REARMED,
            actor=contacted_by,
            reason=f"登记联系：{content[:80]}" if content else "登记联系，按上次联系时间重排",
        )
        db.commit()
    db.refresh(log)
    return log


# ---------------------------------------------------------------- 查询

def list_reminders(
    db: Session,
    *,
    project_id: Optional[int] = None,
    assignee: Optional[str] = None,
    status: Optional[ReminderStatus] = None,
    active_only: bool = True,
    skip: int = 0,
    limit: int = 100,
) -> list[models.CapacityReminder]:
    query = db.query(models.CapacityReminder)
    if active_only:
        query = query.filter(models.CapacityReminder.active.is_(True))
    if project_id is not None:
        query = query.filter(models.CapacityReminder.project_id == project_id)
    if assignee is not None:
        query = query.filter(models.CapacityReminder.assignee == assignee)
    if status is not None:
        query = query.filter(models.CapacityReminder.status == status)
    return (
        query.order_by(models.CapacityReminder.next_remind_at)
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_reminder_basis(db: Session, reminder_id: int) -> Optional[dict]:
    """查询生成依据与完整处理链（含每次排期、顺延明细与事件流）。"""
    reminder = _get_reminder(db, reminder_id)
    if reminder is None:
        return None
    events = (
        db.query(models.CapacityReminderEvent)
        .filter(models.CapacityReminderEvent.reminder_id == reminder_id)
        .order_by(models.CapacityReminderEvent.seq)
        .all()
    )
    contacts = (
        db.query(models.CapacityContactLog)
        .filter(models.CapacityContactLog.reminder_id == reminder_id)
        .order_by(models.CapacityContactLog.contacted_at)
        .all()
    )
    return {
        "reminder_id": reminder.id,
        "project_id": reminder.project_id,
        "status": reminder.status,
        "active": reminder.active,
        "assignee": reminder.assignee,
        "claimed_by": reminder.claimed_by,
        "chain": _load_chain(reminder.chain),
        "next_remind_at": reminder.next_remind_at,
        "remind_date": reminder.remind_date,
        "timezone": reminder.timezone,
        "basis": _parse_basis(reminder),
        "contact_count": reminder.contact_count,
        "closed_at": reminder.closed_at,
        "close_reason": reminder.close_reason,
        "events": [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "actor": e.actor,
                "from_assignee": e.from_assignee,
                "to_assignee": e.to_assignee,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "scheduled_at": e.scheduled_at,
                "scheduled_date": e.scheduled_date,
                "reason": e.reason,
                "detail": json.loads(e.detail) if e.detail else None,
                "created_at": e.created_at,
            }
            for e in events
        ],
        "contacts": [
            {
                "id": c.id,
                "contacted_by": c.contacted_by,
                "contacted_at": c.contacted_at,
                "channel": c.channel,
                "content": c.content,
            }
            for c in contacts
        ],
    }
