from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List

from ..database import get_db
from .. import crud, schemas
from ..enums import FollowUpReminderStatus
from ..errors import (
    HTTPStatus,
    ERROR_NOT_FOUND,
)
from ..services import reminders as reminder_svc
from ..services.reminders import (
    ReminderError,
    ReminderNotFoundError,
    ReminderStateError,
    ReminderValidationError,
)

router = APIRouter(prefix="/capacity/reminders", tags=["产能跟进提醒编排"])


def _handle_domain_error(e: ReminderError):
    if isinstance(e, ReminderNotFoundError):
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(e))
    if isinstance(e, ReminderStateError):
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


@router.post(
    "/generate",
    response_model=schemas.ReminderGenerateResponse,
    summary="编排生成提醒（按承诺产能/达产率/上次联系时间，幂等去重）",
)
def generate_reminders(
    req: schemas.ReminderGenerateRequest,
    db: Session = Depends(get_db),
):
    try:
        return reminder_svc.generate_reminders(
            db,
            project_id=req.project_id,
            follow_up_id=req.follow_up_id,
            timezone_name=req.timezone,
            holidays=req.holidays,
            defer_weekends=req.defer_weekends,
            as_of=req.as_of,
        )
    except ReminderError as e:
        _handle_domain_error(e)


@router.post(
    "/process-due",
    response_model=schemas.ReminderProcessDueResponse,
    summary="扫描到期提醒（服务启动时自动执行，可手动/定时补跑）",
)
def process_due_reminders(
    req: schemas.ReminderProcessDueRequest,
    db: Session = Depends(get_db),
):
    try:
        now = reminder_svc.resolve_now(req.as_of, req.timezone)
    except ReminderError as e:
        _handle_domain_error(e)
    processed = reminder_svc.process_due_reminders(db, now=now)
    return {"processed_count": len(processed), "reminder_ids": processed}


@router.post(
    "/claim",
    response_model=schemas.ReminderBatchResponse,
    summary="按责任人领取提醒（批量）",
)
def claim_reminders(
    req: schemas.ReminderClaimRequest,
    db: Session = Depends(get_db),
):
    results = reminder_svc.claim_reminders(
        db, owner=req.owner, reminder_ids=req.reminder_ids
    )
    return {"results": results}


@router.post(
    "/defer",
    response_model=schemas.ReminderBatchResponse,
    summary="批量延期提醒（必须填写延期原因）",
)
def defer_reminders(
    req: schemas.ReminderDeferRequest,
    db: Session = Depends(get_db),
):
    try:
        results = reminder_svc.defer_reminders(
            db,
            reminder_ids=req.reminder_ids,
            reason=req.reason,
            defer_until=req.defer_until,
            defer_days=req.defer_days,
            operator=req.operator,
        )
    except ReminderError as e:
        _handle_domain_error(e)
    return {"results": results}


@router.get(
    "",
    response_model=List[schemas.FollowUpReminder],
    summary="查询提醒列表（责任人工作清单，可按状态/到期过滤）",
)
def list_reminders(
    owner: Optional[str] = Query(None, description="按责任人筛选"),
    status: Optional[FollowUpReminderStatus] = Query(None, description="按提醒状态筛选"),
    follow_up_id: Optional[int] = Query(None, description="按跟进事项筛选"),
    project_id: Optional[int] = Query(None, description="按项目筛选"),
    due_only: bool = Query(False, description="仅看已到期的开放提醒"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return crud.list_reminders(
        db=db,
        owner=owner,
        status=status,
        follow_up_id=follow_up_id,
        project_id=project_id,
        due_only=due_only,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/{reminder_id}",
    response_model=schemas.FollowUpReminder,
    summary="查询单条提醒详情",
)
def get_reminder(reminder_id: int, db: Session = Depends(get_db)):
    reminder = crud.get_reminder(db, reminder_id=reminder_id)
    if not reminder:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reminder"],
        )
    return reminder


@router.get(
    "/{reminder_id}/basis",
    response_model=schemas.ReminderBasisResponse,
    summary="查询提醒生成依据（承诺产能、达产率、上次联系时间、顺延规则）",
)
def get_reminder_basis(reminder_id: int, db: Session = Depends(get_db)):
    reminder = crud.get_reminder(db, reminder_id=reminder_id)
    if not reminder:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reminder"],
        )
    return {
        "reminder_id": reminder.id,
        "follow_up_id": reminder.follow_up_id,
        "project_id": reminder.project_id,
        "basis": reminder.basis or {},
    }


@router.get(
    "/{reminder_id}/events",
    response_model=List[schemas.FollowUpReminderEvent],
    summary="查询提醒处理链（生成/领取/延期/转交/处理/取消全程留痕）",
)
def list_reminder_events(reminder_id: int, db: Session = Depends(get_db)):
    reminder = crud.get_reminder(db, reminder_id=reminder_id)
    if not reminder:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reminder"],
        )
    return crud.list_reminder_events(db, reminder_id=reminder_id)


@router.post(
    "/{reminder_id}/transfer",
    response_model=schemas.FollowUpReminder,
    summary="转交责任人（保留原处理链，乐观锁防并发）",
)
def transfer_reminder(
    reminder_id: int,
    req: schemas.ReminderTransferRequest,
    db: Session = Depends(get_db),
):
    try:
        return reminder_svc.transfer_reminder(
            db,
            reminder_id=reminder_id,
            to_owner=req.to_owner,
            reason=req.reason,
            operator=req.operator,
            expected_version=req.expected_version,
        )
    except ReminderError as e:
        _handle_domain_error(e)


@router.post(
    "/{reminder_id}/complete",
    response_model=schemas.ReminderCompleteResponse,
    summary="处理完成（记录联系时间并自动编排下一次提醒）",
)
def complete_reminder(
    reminder_id: int,
    req: schemas.ReminderCompleteRequest,
    db: Session = Depends(get_db),
):
    try:
        done_id, next_id = reminder_svc.complete_reminder(
            db,
            reminder_id=reminder_id,
            contacted_at=req.contacted_at,
            note=req.note,
            operator=req.operator,
        )
    except ReminderError as e:
        _handle_domain_error(e)
    return {
        "reminder_id": done_id,
        "status": FollowUpReminderStatus.DONE,
        "next_reminder_id": next_id,
    }
