"""提醒排期的纯函数部分：策略计算、时区换算、节假日顺延。

设计约定：
- 业务「日期」在调用方指定的本地时区下计算（园区运营按本地日历工作）；
- 存储统一使用 naive UTC（与库内既有 DateTime 列保持一致），
  通过 local_date / to_utc 两个函数在边界处换算；
- 节假日顺延规则由调用方提供（HolidaySchedule），本模块不内置任何节假日数据。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Optional, Set
from zoneinfo import ZoneInfo

# 三档跟进节奏：达产率越低，缺口越大，间隔越短（天）
INTERVAL_BY_UTILIZATION = (
    (50.0, 3),   # 达产率 < 50%：每 3 天
    (80.0, 7),   # 50% <= 达产率 < 80%：每 7 天
    (100.0, 14), # 80% <= 达产率 < 100%：每 14 天
)
DEFAULT_INTERVAL_DAYS = 14
# 距项目承诺（达产截止日）越近越紧急的窗口
DEADLINE_WINDOW_DAYS = 7
REMIND_HOUR = 9  # 默认提醒时刻：本地时间 09:00


class HolidaySchedule:
    """节假日顺延规则，由调用方组装后注入。

    holidays: 法定节假日（含调休放假）日期集合，命中则顺延
    workweek: 周一至周日中需要顺延的星期（0=周一 ... 6=周日），
              标准双休传 {5, 6}；None 表示只看 holidays
    is_holiday: 可选的自定义判定（如对接外部日历服务），优先于集合判定
    max_adjust_days: 防止规则异常导致死循环的保护上限
    """

    def __init__(
        self,
        holidays: Optional[Iterable[date]] = None,
        weekend: Optional[Iterable[int]] = None,
        is_holiday: Optional[Callable[[date], bool]] = None,
        max_adjust_days: int = 30,
    ):
        self._holidays: Set[date] = set(holidays or [])
        self._weekend: Set[int] = set(weekend or [])
        self._is_holiday = is_holiday
        self.max_adjust_days = max_adjust_days

    def is_non_working_day(self, day: date) -> bool:
        if self._is_holiday is not None:
            try:
                if self._is_holiday(day):
                    return True
            except Exception:
                pass
        if day in self._holidays:
            return True
        return day.weekday() in self._weekend

    def next_working_day(self, day: date) -> date:
        """顺延到下一个工作日；若 day 本身即工作日则原样返回。"""
        adjusted = day
        moved = 0
        while self.is_non_working_day(adjusted):
            adjusted += timedelta(days=1)
            moved += 1
            if moved > self.max_adjust_days:
                # 规则明显异常时不再顺延，避免无限期挂起提醒
                return day + timedelta(days=moved - 1)
        return adjusted


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"不支持的时区: {tz_name}") from exc


def local_date(utc_naive: datetime, tz_name: str) -> date:
    """naive UTC -> 本地日期。"""
    if utc_naive.tzinfo is not None:
        utc_naive = utc_naive.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(get_zone(tz_name)).date()


def to_utc_naive(day: date, tz_name: str, hour: int = REMIND_HOUR,
                 minute: int = 0) -> datetime:
    """本地日期的某时刻 -> naive UTC 绝对时刻。"""
    local_dt = datetime.combine(day, time(hour=hour, minute=minute),
                                tzinfo=get_zone(tz_name))
    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def interval_for_utilization(utilization_rate: Optional[float]) -> int:
    """根据实际达产率选择跟进间隔（天）。达产率缺失按最保守档处理。"""
    if utilization_rate is None:
        return INTERVAL_BY_UTILIZATION[0][1]
    rate = float(utilization_rate)
    for threshold, days in INTERVAL_BY_UTILIZATION:
        if rate < threshold:
            return days
    return DEFAULT_INTERVAL_DAYS


@dataclass
class ScheduleBasis:
    """一次排期的输入快照。"""

    project_id: int
    promised_monthly_capacity_tonnes: float
    actual_utilization_rate: Optional[float]
    actual_output_tonnes: Optional[float]
    last_contacted_at: Optional[datetime]  # naive UTC
    anchor: str  # last_contact | commissioned | now
    utilization_interval_days: int
    base_interval_days: int
    raw_due_date: date
    due_date: date
    postponed_days: int
    hit_holidays: list = field(default_factory=list)
    deadline_date: Optional[date] = None
    deadline_urged: bool = False
    timezone: str = "Asia/Shanghai"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "promised_monthly_capacity_tonnes":
                self.promised_monthly_capacity_tonnes,
            "actual_utilization_rate": self.actual_utilization_rate,
            "actual_output_tonnes": self.actual_output_tonnes,
            "last_contacted_at": (
                self.last_contacted_at.isoformat() + "Z"
                if self.last_contacted_at else None
            ),
            "anchor": self.anchor,
            "interval_days": self.base_interval_days,
            "utilization_interval_days": self.utilization_interval_days,
            "deadline_date": (
                self.deadline_date.isoformat() if self.deadline_date else None
            ),
            "deadline_urged": self.deadline_urged,
            "raw_due_date": self.raw_due_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "postponed_days": self.postponed_days,
            "hit_holidays": [d.isoformat() for d in self.hit_holidays],
            "timezone": self.timezone,
        }


def _anchor_local_date(
    *,
    now_utc: datetime,
    tz_name: str,
    last_contacted_at: Optional[datetime],
    commissioned_date: Optional[date],
) -> tuple[date, str]:
    """决定排期锚点：优先上次联系时间，其次投产日，最后当前日期。"""
    if last_contacted_at is not None:
        return local_date(last_contacted_at, tz_name), "last_contact"
    if commissioned_date is not None:
        return commissioned_date, "commissioned"
    return local_date(now_utc, tz_name), "now"


def compute_next_reminder(
    *,
    project_id: int,
    promised_monthly_capacity_tonnes: float,
    actual_utilization_rate: Optional[float],
    actual_output_tonnes: Optional[float],
    last_contacted_at: Optional[datetime],
    commissioned_date: Optional[date],
    tz_name: str = "Asia/Shanghai",
    holidays: Optional[HolidaySchedule] = None,
    now_utc: Optional[datetime] = None,
    interval_days: Optional[int] = None,
    deadline_date: Optional[date] = None,
    remind_hour: int = REMIND_HOUR,
    anchor_today: bool = False,
    clamp_deadline: bool = True,
) -> tuple[datetime, ScheduleBasis]:
    """计算下一次提醒的 UTC 时刻与生成依据。

    - 间隔 = 达产率档位间隔；显式传入 interval_days 时（如手动延期）直接采用；
    - 锚点优先上次联系时间，其次投产日，最后当前日期；
      anchor_today=True（手动延期）时强制从当前本地日期起算；
    - 承诺截止日落入提醒窗口（DEADLINE_WINDOW_DAYS）内时向前提紧，
      clamp_deadline=False（显式间隔）时不收紧；
    - 命中非工作日由调用方提供的 HolidaySchedule 顺延。
    返回 (naive UTC 时刻, 依据快照)。
    """
    now_utc = now_utc or datetime.utcnow()
    holidays = holidays or HolidaySchedule()

    util_interval = interval_for_utilization(actual_utilization_rate)
    chosen_interval = interval_days if interval_days is not None else util_interval

    if anchor_today:
        anchor_day, anchor_name = local_date(now_utc, tz_name), "postpone"
    else:
        anchor_day, anchor_name = _anchor_local_date(
            now_utc=now_utc,
            tz_name=tz_name,
            last_contacted_at=last_contacted_at,
            commissioned_date=commissioned_date,
        )
    raw_due = anchor_day + timedelta(days=chosen_interval)

    today_local = local_date(now_utc, tz_name)
    deadline_urged = False
    if clamp_deadline and deadline_date is not None:
        deadline_floor = deadline_date - timedelta(days=DEADLINE_WINDOW_DAYS)
        if raw_due > deadline_floor and deadline_floor >= today_local:
            raw_due = deadline_floor
            deadline_urged = True

    due_day = raw_due
    hit: list[date] = []
    while holidays.is_non_working_day(due_day):
        hit.append(due_day)
        due_day += timedelta(days=1)

    # 顺延后若已早于本地今天（锚点间隔很短时可能发生），从今天开始顺延
    if due_day < today_local:
        due_day = holidays.next_working_day(today_local)

    remind_at_utc = to_utc_naive(due_day, tz_name, hour=remind_hour)
    basis = ScheduleBasis(
        project_id=project_id,
        promised_monthly_capacity_tonnes=float(promised_monthly_capacity_tonnes),
        actual_utilization_rate=actual_utilization_rate,
        actual_output_tonnes=actual_output_tonnes,
        last_contacted_at=last_contacted_at,
        anchor=anchor_name,
        utilization_interval_days=util_interval,
        base_interval_days=chosen_interval,
        raw_due_date=raw_due,
        due_date=due_day,
        postponed_days=(due_day - raw_due).days,
        hit_holidays=hit,
        deadline_date=deadline_date,
        deadline_urged=deadline_urged,
        timezone=tz_name,
    )
    return remind_at_utc, basis
