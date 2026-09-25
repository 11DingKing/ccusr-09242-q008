"""产能提醒编排接口。

提醒生成、到期扫描、按责任人领取、批量延期、转交、关闭、联系登记与依据查询。
节假日顺延规则由调用方在请求体中提供（holidays / weekend），服务端不内置日历。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from ..database import get_db
from .. import schemas
from ..enums import ReminderStatus
from ..errors import HTTPStatus, ERROR_NOT_FOUND
from ..services import reminders as svc
from ..services.reminder_scheduling import HolidaySchedule

router = APIRouter(prefix="/capacity/reminders", tags=["产能提醒编排"])


def _holidays(req) -> HolidaySchedule:
    return HolidaySchedule(holidays=req.holidays, weekend=req.weekend)


@router.post(
    "/generate",
    response_model=schemas.ReminderGenerateResponse,
    summary="生成提醒（按承诺/达产率/上次联系时间排期，自动去重，幂等可重跑）",
)
def generate_reminders(
    req: schemas.ReminderGenerateRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.generate_reminders(
            db,
            project_ids=req.project_ids,
            holidays=_holidays(req),
            timezone=req.timezone,
        )
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/mark-due",
    response_model=List[int],
    summary="推进到期提醒（定时任务/服务重启后补偿调用，纯状态扫描）",
)
def mark_due(db: Session = Depends(get_db)):
    return svc.mark_due_reminders(db)


@router.get(
    "",
    response_model=List[schemas.CapacityReminderOut],
    summary="查询提醒列表（支持责任人/状态/项目筛选）",
)
def list_reminders(
    project_id: Optional[int] = Query(None),
    assignee: Optional[str] = Query(None, description="责任人筛选"),
    status: Optional[ReminderStatus] = Query(None),
    active_only: bool = Query(True),
    skip: int = 0,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    return svc.list_reminders(
        db,
        project_id=project_id,
        assignee=assignee,
        status=status,
        active_only=active_only,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/{reminder_id}/basis",
    response_model=schemas.ReminderBasisResponse,
    summary="查询提醒生成依据与完整处理链（排期快照、顺延明细、事件流）",
)
def get_reminder_basis(reminder_id: int, db: Session = Depends(get_db)):
    data = svc.get_reminder_basis(db, reminder_id)
    if data is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reminder"],
        )
    return data


@router.post(
    "/{reminder_id}/claim",
    response_model=schemas.CapacityReminderOut,
    summary="领取单条到期提醒（并发下仅一人成功）",
)
def claim_reminder(
    reminder_id: int,
    req: schemas.ReminderClaimRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.claim_reminder(
            db,
            reminder_id,
            claimed_by=req.claimed_by,
            expected_assignee=req.expected_assignee,
        )
    except svc.ReminderConflict as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/claim-for-assignee",
    response_model=List[schemas.CapacityReminderOut],
    summary="按责任人领取其名下全部到期提醒",
)
def claim_for_assignee(
    req: schemas.ReminderClaimForAssigneeRequest,
    db: Session = Depends(get_db),
):
    return svc.claim_for_assignee(
        db,
        assignee=req.assignee,
        claimed_by=req.claimed_by,
        limit=req.limit,
    )


@router.post(
    "/batch-postpone",
    response_model=schemas.ReminderBatchPostponeResponse,
    summary="批量延期（记录延期原因，节假日顺延）",
)
def batch_postpone(
    req: schemas.ReminderBatchPostponeRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.batch_postpone(
            db,
            req.reminder_ids,
            days=req.days,
            reason=req.reason,
            actor=req.actor,
            holidays=_holidays(req),
        )
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/{reminder_id}/postpone",
    response_model=schemas.CapacityReminderOut,
    summary="单条延期",
)
def postpone_reminder(
    reminder_id: int,
    req: schemas.ReminderPostponeRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.postpone_reminder(
            db,
            reminder_id,
            days=req.days,
            reason=req.reason,
            actor=req.actor,
            holidays=_holidays(req),
        )
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/{reminder_id}/transfer",
    response_model=schemas.CapacityReminderOut,
    summary="转交责任人（保留原处理链，已领取提醒回到到期池）",
)
def transfer_reminder(
    reminder_id: int,
    req: schemas.ReminderTransferRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.transfer_reminder(
            db,
            reminder_id,
            to_assignee=req.to_assignee,
            actor=req.actor,
            reason=req.reason,
        )
    except svc.ReminderConflict as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/{reminder_id}/close",
    response_model=schemas.CapacityReminderOut,
    summary="关闭提醒（关闭后不再重复生成）",
)
def close_reminder(
    reminder_id: int,
    req: schemas.ReminderCloseRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.close_reminder(
            db, reminder_id, reason=req.reason, actor=req.actor
        )
    except svc.ReminderConflict as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/{reminder_id}/contacts",
    response_model=schemas.ReminderContactOut,
    summary="登记企业联系（按上次联系时间重排下一次提醒，可顺带关闭）",
)
def record_contact(
    reminder_id: int,
    req: schemas.ReminderContactRequest,
    db: Session = Depends(get_db),
):
    try:
        return svc.record_contact(
            db,
            reminder_id,
            contacted_by=req.contacted_by,
            contacted_at=req.contacted_at,
            channel=req.channel,
            content=req.content,
            interval_days=req.interval_days,
            holidays=_holidays(req),
            close=req.close,
            close_reason=req.close_reason,
        )
    except svc.ReminderError as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))
