"""提醒编排测试。

覆盖：
- 时区边界（UTC/本地日期换算、跨日、DST、到期判定边界、节假日顺延）；
- 重复触发幂等（重复生成、重复扫描、关闭后不再生成）；
- 转交中的并发（乐观守卫、线程抢占领取）；
- 已关闭事项（禁止领取/延期、批量延期跳过、跟进事项联动关闭、关闭后重启重开）；
- 服务重启恢复（新会话扫描到期事项）；
- 按责任人领取、批量延期、生成依据与处理链查询。
"""

import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app import crud, models, schemas
from app.enums import (
    FollowUpStatus,
    ParkType,
    ProjectStatus,
    Region,
    ReminderEventType,
    ReminderStatus,
)
from app.services import reminders as svc
from app.services.reminder_scheduling import (
    HolidaySchedule,
    compute_next_reminder,
    interval_for_utilization,
    local_date,
    to_utc_naive,
)

SH = "Asia/Shanghai"
NY = "America/New_York"
NOW = datetime(2026, 9, 25, 2, 0, 0)  # 2026-09-25 10:00 上海（周五）


# ---------------------------------------------------------------- 纯函数：时区边界

class SchedulingTimezoneTests(unittest.TestCase):
    def test_utc_to_local_date_crosses_midnight(self):
        # 9/25 16:00 UTC == 9/26 00:00 上海
        boundary = datetime(2026, 9, 25, 16, 0, 0)
        self.assertEqual(local_date(boundary, SH), date(2026, 9, 26))
        # 早一分钟仍是 9/25
        self.assertEqual(
            local_date(boundary - timedelta(minutes=1), SH), date(2026, 9, 25)
        )

    def test_local_nine_am_to_utc_instant(self):
        # 上海 09:00 == UTC 01:00（同一天）
        utc_dt = to_utc_naive(date(2026, 9, 28), SH)
        self.assertEqual(utc_dt, datetime(2026, 9, 28, 1, 0, 0))

    def test_dst_timezone_uses_offset_in_effect(self):
        # 2026-11-02 美国已结束夏令时（11/1 回拨），纽约 UTC-5，09:00 == 14:00 UTC
        utc_dt = to_utc_naive(date(2026, 11, 2), NY)
        self.assertEqual(utc_dt, datetime(2026, 11, 2, 14, 0, 0))
        # 夏令时期间（7 月，UTC-4），09:00 == 13:00 UTC
        utc_dt_dst = to_utc_naive(date(2026, 7, 2), NY)
        self.assertEqual(utc_dt_dst, datetime(2026, 7, 2, 13, 0, 0))

    def test_weekend_then_holiday_chained_postponement(self):
        # 锚点 9/20（周日本地概念，直接给定时刻），达产率 60% → 间隔 7 天
        contact = datetime(2026, 9, 19, 16, 0, 0)  # 上海 9/20 00:00
        # 7 天后 = 9/27（周日），双休顺延到 9/28（周一），
        # 调用方又把 9/28 列为节假日 → 9/29（周二）
        holidays = HolidaySchedule(
            holidays=[date(2026, 9, 28)], weekend={5, 6}
        )
        utc_dt, basis = compute_next_reminder(
            project_id=1,
            promised_monthly_capacity_tonnes=100.0,
            actual_utilization_rate=60.0,
            actual_output_tonnes=60.0,
            last_contacted_at=contact,
            commissioned_date=None,
            tz_name=SH,
            holidays=holidays,
            now_utc=NOW,
        )
        self.assertEqual(basis.raw_due_date, date(2026, 9, 27))
        self.assertEqual(basis.due_date, date(2026, 9, 29))
        self.assertEqual(basis.postponed_days, 2)
        self.assertEqual(
            basis.hit_holidays, [date(2026, 9, 27), date(2026, 9, 28)]
        )
        self.assertEqual(utc_dt, datetime(2026, 9, 29, 1, 0, 0))

    def test_custom_holiday_predicate_injected_by_caller(self):
        # 调用方提供自定义判定（如外部日历服务）
        holidays = HolidaySchedule(is_holiday=lambda d: d.day == 25)
        utc_dt, basis = compute_next_reminder(
            project_id=1,
            promised_monthly_capacity_tonnes=100.0,
            actual_utilization_rate=40.0,
            actual_output_tonnes=40.0,
            last_contacted_at=None,
            commissioned_date=date(2026, 9, 22),
            tz_name=SH,
            holidays=holidays,
            now_utc=NOW,
        )
        # 9/22 + 3 = 9/25 命中自定义节假日 → 9/26
        self.assertEqual(basis.due_date, date(2026, 9, 26))

    def test_utilization_intervals(self):
        self.assertEqual(interval_for_utilization(None), 3)
        self.assertEqual(interval_for_utilization(10), 3)
        self.assertEqual(interval_for_utilization(49.99), 3)
        self.assertEqual(interval_for_utilization(50), 7)
        self.assertEqual(interval_for_utilization(79.9), 7)
        self.assertEqual(interval_for_utilization(80), 14)
        self.assertEqual(interval_for_utilization(120), 14)

    def test_due_boundary_is_inclusive_compare(self):
        # 纯函数排期时刻可复现（本地 09:00 的 UTC 绝对时刻）
        self.assertEqual(
            to_utc_naive(date(2026, 9, 30), SH), datetime(2026, 9, 30, 1, 0, 0)
        )


# ---------------------------------------------------------------- 数据库夹具

class ReminderDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, autoflush=False)

        def override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        # 每个用例清空业务表
        with self.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())

    def _make_commissioned_project(
        self,
        name: str = "某榴莲加工项目",
        leader: str = "张三",
        promised: float = 100.0,
        actual: float = 60.0,
        report_month: tuple = (2026, 8),
        commissioned: date = date(2026, 1, 10),
    ):
        db = self.Session()
        try:
            park = models.IndustrialPark(
                name=f"园区-{name}",
                park_type=ParkType.BORDER_PORT,
                city="崇左市",
            )
            entity = models.Entity(
                name=f"主体-{name}",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="联系人",
                contact_phone="123",
            )
            db.add_all([park, entity])
            db.flush()
            project = models.Project(
                name=name,
                status=ProjectStatus.COMMISSIONED,
                investment_direction="榴莲加工",
                planned_investment_10k=1000.0,
                promised_monthly_capacity_tonnes=promised,
                commissioned_date=commissioned,
                project_leader=leader,
                park_id=park.id,
                initiator_id=entity.id,
            )
            db.add(project)
            db.flush()
            if actual is not None:
                crud.create_capacity_report(
                    db,
                    schemas.MonthlyCapacityReportCreate(
                        project_id=project.id,
                        report_year=report_month[0],
                        report_month=report_month[1],
                        actual_output_tonnes=actual,
                    ),
                )
            db.refresh(project)
            return project.id
        finally:
            db.close()

    def _get(self, model, row_id):
        db = self.Session()
        try:
            return db.query(model).filter(model.id == row_id).first()
        finally:
            db.close()

    def _generate(self, project_id, holidays=None, weekend=None, tz=SH):
        """通过 API 生成（验证序列化链路）。"""
        payload = {"timezone": tz}
        if holidays:
            payload["holidays"] = [d.isoformat() for d in holidays]
        if weekend:
            payload["weekend"] = list(weekend)
        resp = self.client.post(
            f"/api/v1/capacity/reminders/generate",
            json={**payload, "project_ids": [project_id]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()


# ---------------------------------------------------------------- 文件库辅助（真并发用）

def _file_session():
    """独立的 SQLite 文件库：跨会话/线程测试需要真正的多连接隔离。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    return engine, Session, path


def _seed_commissioned_project(db, name="并发项目", leader="张三"):
    park = models.IndustrialPark(
        name=f"园区-{name}", park_type=ParkType.BORDER_PORT, city="崇左市"
    )
    entity = models.Entity(
        name=f"主体-{name}", region=Region.GUANGXI,
        country_or_province="广西", contact_person="x", contact_phone="1",
    )
    db.add_all([park, entity])
    db.flush()
    project = models.Project(
        name=name,
        status=ProjectStatus.COMMISSIONED,
        investment_direction="x",
        planned_investment_10k=1.0,
        promised_monthly_capacity_tonnes=100.0,
        commissioned_date=date(2026, 1, 1),
        project_leader=leader,
        park_id=park.id,
        initiator_id=entity.id,
    )
    db.add(project)
    db.flush()
    db.add(
        models.CapacityFollowUp(
            project_id=project.id,
            title="到期事项",
            status=FollowUpStatus.PENDING,
            responsible_person=leader,
        )
    )
    db.commit()
    return project.id


# ---------------------------------------------------------------- 生成与依据

class GenerateReminderTests(ReminderDBTests):
    def test_basis_records_promised_utilization_and_contact(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid)
            data = svc.get_reminder_basis(db, reminder.id)
        finally:
            db.close()
        basis = data["basis"]
        self.assertEqual(basis["promised_monthly_capacity_tonnes"], 100.0)
        self.assertEqual(basis["actual_utilization_rate"], 60.0)
        self.assertEqual(basis["actual_output_tonnes"], 60.0)
        self.assertIn(basis["anchor"], ("commissioned", "last_contact"))
        self.assertEqual(data["chain"], ["张三"])
        self.assertEqual(data["events"][0]["event_type"], ReminderEventType.GENERATED.value)

    def test_last_contact_time_drives_schedule(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            # 9/20 上海本地刚联系过 → 7 天后到期（双休不顺延时）
            contact_at = datetime(2026, 9, 19, 16, 0, 0)  # 9/20 00:00 上海
            db.add(
                models.CapacityContactLog(
                    project_id=pid,
                    contacted_by="张三",
                    contacted_at=contact_at,
                )
            )
            db.commit()
            reminder = svc.generate_for_project(
                db, project_id=pid, now=NOW,
                holidays=HolidaySchedule(),
            )
            self.assertEqual(reminder.remind_date, date(2026, 9, 27))
            data = svc.get_reminder_basis(db, reminder.id)
            self.assertEqual(data["basis"]["last_contacted_at"],
                             contact_at.isoformat() + "Z")
        finally:
            db.close()


# ---------------------------------------------------------------- 重复触发

class DeduplicationTests(ReminderDBTests):
    def test_repeated_generation_is_idempotent(self):
        pid = self._make_commissioned_project()
        r1 = self._generate(pid)
        r2 = self._generate(pid)
        self.assertEqual(len(r1["generated"]), 1)
        self.assertEqual(r2["generated"], [])
        self.assertEqual(r2["skipped"], [pid])

        db = self.Session()
        try:
            count = (
                db.query(models.CapacityReminder)
                .filter(models.CapacityReminder.project_id == pid)
                .count()
            )
            self.assertEqual(count, 1)
        finally:
            db.close()

    def test_mark_due_does_not_duplicate_events_on_repeat_scan(self):
        # 投产日很近 => 首次排期在未来，初始状态为「已排期」
        pid = self._make_commissioned_project(commissioned=date(2026, 9, 24))
        db = self.Session()
        try:
            reminder = svc.generate_for_project(
                db, project_id=pid,
                holidays=HolidaySchedule(weekend={5, 6}), now=NOW,
            )
            rid = reminder.id
            self.assertEqual(reminder.status, ReminderStatus.SCHEDULED)
        finally:
            db.close()

        # 模拟服务重启：用全新会话推进到期
        db1 = self.Session()
        db2 = self.Session()
        try:
            first = svc.mark_due_reminders(db1, now=NOW + timedelta(days=400))
            second = svc.mark_due_reminders(db2, now=NOW + timedelta(days=401))
            self.assertEqual(first, [rid])
            self.assertEqual(second, [])
        finally:
            db1.close()
            db2.close()

        db = self.Session()
        try:
            due_events = (
                db.query(models.CapacityReminderEvent)
                .filter(
                    models.CapacityReminderEvent.reminder_id == rid,
                    models.CapacityReminderEvent.event_type == ReminderEventType.DUE,
                )
                .count()
            )
            self.assertEqual(due_events, 1)
        finally:
            db.close()

    def test_closed_reminder_is_never_regenerated(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid, now=NOW)
            rid = reminder.id
            svc.close_reminder(db, rid, reason="企业已达产", actor="张三")
            again = svc.generate_for_project(db, project_id=pid, now=NOW)
            self.assertEqual(again.id, rid)
            self.assertFalse(again.active)
            self.assertEqual(again.status, ReminderStatus.CLOSED)
            self.assertEqual(
                db.query(models.CapacityReminder)
                .filter(models.CapacityReminder.project_id == pid)
                .count(),
                1,
            )
        finally:
            db.close()

    def test_closed_follow_up_auto_closes_reminder(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid, now=NOW)
            follow_up = (
                db.query(models.CapacityFollowUp)
                .filter(models.CapacityFollowUp.project_id == pid)
                .first()
            )
            self.assertIsNotNone(follow_up)
            crud.update_follow_up(
                db,
                follow_up.id,
                schemas.CapacityFollowUpUpdate(status=FollowUpStatus.RESOLVED),
            )
            db.refresh(reminder)
            self.assertEqual(reminder.status, ReminderStatus.CLOSED)
            self.assertFalse(reminder.active)
        finally:
            db.close()

    def test_new_follow_up_after_close_reopens_reminder_generation(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            first = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.close_reminder(db, first.id, reason="首期问题已闭环", actor="张三")
            # 关闭之后出现新的未结跟进事项 → 允许重新编排
            db.add(
                models.CapacityFollowUp(
                    project_id=pid,
                    title="二期产能未达标",
                    status=FollowUpStatus.PENDING,
                    responsible_person="张三",
                )
            )
            db.commit()
            second = svc.generate_for_project(db, project_id=pid, now=NOW)
            self.assertNotEqual(second.id, first.id)
            self.assertTrue(second.active)
        finally:
            db.close()


# ---------------------------------------------------------------- 到期与领取

class ClaimTests(ReminderDBTests):
    def _due_reminder(self, leader="张三", name="某榴莲加工项目"):
        pid = self._make_commissioned_project(leader=leader, name=name)
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid, now=NOW)
            # 产能报告触发的跟进事项默认即到期
            svc.mark_due_reminders(db, now=NOW)
            db.refresh(reminder)
            self.assertEqual(reminder.status, ReminderStatus.DUE)
            return reminder.id
        finally:
            db.close()

    def test_claim_once_only_with_concurrent_attempts(self):
        # 两个线程在独立会话中同时抢占同一条到期提醒，必须只有一人成功
        engine2, Session2, path = _file_session()
        try:
            db0 = Session2()
            pid = _seed_commissioned_project(db0)
            reminder = svc.generate_for_project(db0, project_id=pid, now=NOW)
            rid2 = reminder.id
            svc.mark_due_reminders(db0, now=NOW + timedelta(days=400))
            db0.close()

            outcomes = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def file_worker(who):
                db = Session2()
                try:
                    barrier.wait()
                    r = svc.claim_reminder(db, rid2, claimed_by=who)
                    with lock:
                        outcomes.append(("ok", who, r.claimed_by))
                except svc.ReminderConflict as e:
                    with lock:
                        outcomes.append(("conflict", who, str(e)))
                except Exception as e:  # noqa: BLE001
                    with lock:
                        outcomes.append(("error", who, repr(e)))
                finally:
                    db.close()

            t1 = threading.Thread(target=file_worker, args=("王五",))
            t2 = threading.Thread(target=file_worker, args=("赵六",))
            t1.start(); t2.start(); t1.join(); t2.join()

            statuses = [o[0] for o in outcomes]
            self.assertEqual(sorted(statuses), ["conflict", "ok"], outcomes)
            db = Session2()
            try:
                final = db.get(models.CapacityReminder, rid2)
                self.assertEqual(final.status, ReminderStatus.CLAIMED)
                self.assertIn(final.claimed_by, ("王五", "赵六"))
            finally:
                db.close()

            # 串行二次领取同样被拒（已被前两人之一领取）
            db = Session2()
            try:
                with self.assertRaises(svc.ReminderConflict):
                    svc.claim_reminder(db, rid2, claimed_by="李四")
            finally:
                db.close()
        finally:
            engine2.dispose()
            os.unlink(path)

    def test_claim_for_assignee_only_takes_own_due_items(self):
        rid_a1 = self._due_reminder(leader="张三")
        rid_a2 = self._due_reminder(name="第二个项目", leader="张三")
        rid_b = self._due_reminder(name="第三个项目", leader="李四")

        db = self.Session()
        try:
            claimed = svc.claim_for_assignee(db, "张三")
            ids = {r.id for r in claimed}
            self.assertEqual(ids, {rid_a1, rid_a2})
            other = (
                db.query(models.CapacityReminder)
                .filter(models.CapacityReminder.id == rid_b)
                .first()
            )
            self.assertEqual(other.status, ReminderStatus.DUE)
        finally:
            db.close()


# ---------------------------------------------------------------- 转交并发与处理链

class TransferTests(ReminderDBTests):
    def test_transfer_keeps_full_chain(self):
        pid = self._make_commissioned_project(leader="张三")
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.transfer_reminder(
                db, reminder.id, to_assignee="李四", actor="张三", reason="分组调整"
            )
            svc.transfer_reminder(
                db, reminder.id, to_assignee="王五", actor="李四"
            )
            db.refresh(reminder)
            self.assertEqual(reminder.assignee, "王五")
            self.assertEqual(
                svc._load_chain(reminder.chain), ["张三", "李四", "王五"]
            )
            data = svc.get_reminder_basis(db, reminder.id)
            transfers = [
                e for e in data["events"]
                if e["event_type"] == ReminderEventType.TRANSFERRED.value
            ]
            self.assertEqual(len(transfers), 2)
            self.assertEqual(transfers[0]["from_assignee"], "张三")
            self.assertEqual(transfers[0]["to_assignee"], "李四")
            self.assertEqual(transfers[1]["to_assignee"], "王五")
        finally:
            db.close()

    def test_concurrent_transfer_optimistic_guard(self):
        # 文件库 + 独立连接：s2 先读到「责任人=张三」的视图，
        # s1 随后完成转交，s2 再基于过期视图转交必须被守卫拒绝
        engine2, Session2, path = _file_session()
        try:
            db0 = Session2()
            pid = _seed_commissioned_project(db0)
            reminder = svc.generate_for_project(db0, project_id=pid, now=NOW)
            rid = reminder.id
            db0.close()

            s1 = Session2()
            s2 = Session2()
            try:
                # s2 先建立过期视图（此刻责任人仍是张三）
                stale = s2.get(models.CapacityReminder, rid)
                self.assertEqual(stale.assignee, "张三")

                svc.transfer_reminder(s1, rid, to_assignee="李四", actor="张三")
                # s2 基于过期视图再转交 → 条件 UPDATE 守卫命中，必须失败
                with self.assertRaises(svc.ReminderConflict):
                    svc.transfer_reminder(s2, rid, to_assignee="王五", actor="张三")
            finally:
                s1.close()
                s2.close()

            db = Session2()
            try:
                final = db.get(models.CapacityReminder, rid)
                self.assertEqual(final.assignee, "李四")
                self.assertEqual(svc._load_chain(final.chain), ["张三", "李四"])
            finally:
                db.close()
        finally:
            engine2.dispose()
            os.unlink(path)

    def test_claimed_reminder_returns_to_due_pool_after_transfer(self):
        pid = self._make_commissioned_project(leader="张三")
        db = self.Session()
        try:
            reminder = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.mark_due_reminders(db, now=NOW)
            svc.claim_reminder(db, reminder.id, claimed_by="张三")
            db.refresh(reminder)
            self.assertEqual(reminder.status, ReminderStatus.CLAIMED)
            svc.transfer_reminder(db, reminder.id, to_assignee="李四")
            db.refresh(reminder)
            self.assertEqual(reminder.status, ReminderStatus.DUE)
            self.assertIsNone(reminder.claimed_by)
            # 新责任人可以领取
            svc.claim_reminder(db, reminder.id, claimed_by="李四")
            db.refresh(reminder)
            self.assertEqual(reminder.status, ReminderStatus.CLAIMED)
        finally:
            db.close()


# ---------------------------------------------------------------- 批量延期

class PostponeTests(ReminderDBTests):
    def test_batch_postpone_applies_holidays_and_skips_closed(self):
        pid1 = self._make_commissioned_project(name="项目甲")
        pid2 = self._make_commissioned_project(name="项目乙")
        db = self.Session()
        try:
            r1 = svc.generate_for_project(db, project_id=pid1, now=NOW)
            r2 = svc.generate_for_project(db, project_id=pid2, now=NOW)
            svc.close_reminder(db, r2.id, reason="无需跟进", actor="张三")
            result = svc.batch_postpone(
                db,
                [r1.id, r2.id, 99999],
                days=3,
                reason="企业负责人出差",
                actor="张三",
                holidays=HolidaySchedule(
                    holidays=[date(2026, 9, 28)], weekend={5, 6}
                ),
            )
            self.assertEqual(result["postponed"], [r1.id])
            skipped_reasons = {s["reminder_id"]: s["reason"] for s in result["skipped"]}
            self.assertIn(r2.id, skipped_reasons)
            self.assertIn(99999, skipped_reasons)

            db.refresh(r1)
            # 9/25（周五）+ 3 = 9/28（周一，节假日）→ 9/29
            self.assertEqual(r1.remind_date, date(2026, 9, 29))
            self.assertEqual(r1.status, ReminderStatus.SCHEDULED)
            data = svc.get_reminder_basis(db, r1.id)
            postponed = [
                e for e in data["events"]
                if e["event_type"] == ReminderEventType.POSTPONED.value
            ]
            self.assertEqual(postponed[-1]["reason"], "企业负责人出差")
        finally:
            db.close()

    def test_postpone_must_have_positive_days(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            r = svc.generate_for_project(db, project_id=pid, now=NOW)
            with self.assertRaises(svc.ReminderError):
                svc.postpone_reminder(db, r.id, days=0, reason="x")
        finally:
            db.close()


# ---------------------------------------------------------------- 已关闭事项的操作约束

class ClosedReminderTests(ReminderDBTests):
    def test_closed_reminder_rejects_all_actions(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            r = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.mark_due_reminders(db, now=NOW)
            svc.close_reminder(db, r.id, reason="误报，关闭", actor="张三")

            with self.assertRaises(svc.ReminderError):
                svc.claim_reminder(db, r.id, claimed_by="李四")
            with self.assertRaises(svc.ReminderError):
                svc.postpone_reminder(db, r.id, days=3, reason="再试试")
            with self.assertRaises(svc.ReminderError):
                svc.transfer_reminder(db, r.id, to_assignee="李四")
            with self.assertRaises(svc.ReminderError):
                svc.record_contact(db, r.id, contacted_by="李四")
        finally:
            db.close()

    def test_double_close_is_conflict(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            r = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.close_reminder(db, r.id, reason="第一次关闭")
            with self.assertRaises(svc.ReminderConflict):
                svc.close_reminder(db, r.id, reason="第二次关闭")
        finally:
            db.close()


# ---------------------------------------------------------------- 联系登记与重排

class ContactLogTests(ReminderDBTests):
    def test_contact_reschedules_from_last_contact_time(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            r = svc.generate_for_project(db, project_id=pid, now=NOW)
            original = r.next_remind_at
            svc.mark_due_reminders(db, now=NOW + timedelta(days=400))
            svc.record_contact(
                db,
                r.id,
                contacted_by="张三",
                contacted_at=datetime(2026, 9, 24, 3, 0, 0),  # 上海 9/24 11:00
                content="企业承诺本周完成技改",
            )
            db.refresh(r)
            self.assertGreater(r.next_remind_at, original)
            # 60% 档间隔 7 天：9/24 + 7 = 10/1（国庆，调用方提供节假日规则）
            # 不传节假日时为 10/1
            self.assertEqual(r.remind_date, date(2026, 10, 1))
            self.assertEqual(r.contact_count, 1)
            data = svc.get_reminder_basis(db, r.id)
            self.assertEqual(len(data["contacts"]), 1)
            self.assertIn("技改", data["contacts"][0]["content"])
            self.assertEqual(
                data["events"][-1]["event_type"], ReminderEventType.REARMED.value
            )
        finally:
            db.close()

    def test_contact_can_close_reminder(self):
        pid = self._make_commissioned_project()
        db = self.Session()
        try:
            r = svc.generate_for_project(db, project_id=pid, now=NOW)
            svc.record_contact(
                db, r.id, contacted_by="张三",
                close=True, close_reason="企业已补足产能",
            )
            db.refresh(r)
            self.assertFalse(r.active)
            self.assertEqual(r.status, ReminderStatus.CLOSED)
        finally:
            db.close()


# ---------------------------------------------------------------- 重启恢复

class RestartRecoveryTests(ReminderDBTests):
    def test_due_items_processed_after_restart_with_fresh_session(self):
        # 投产日很近 => 首排在未来（9/24 + 3 天遇双休顺延至 9/28）
        pid = self._make_commissioned_project(commissioned=date(2026, 9, 24))
        # 第一次「进程运行」：生成提醒
        db = self.Session()
        try:
            r = svc.generate_for_project(
                db, project_id=pid,
                holidays=HolidaySchedule(weekend={5, 6}), now=NOW,
            )
            rid = r.id
            self.assertEqual(r.status, ReminderStatus.SCHEDULED)
        finally:
            db.close()

        # 「重启」：全新会话，无任何内存状态，扫描到期事项后继续处理
        db = self.Session()
        try:
            due = svc.mark_due_reminders(db, now=NOW + timedelta(days=30))
            self.assertEqual(due, [rid])
            claimed = svc.claim_reminder(db, rid, claimed_by="张三")
            self.assertEqual(claimed.status, ReminderStatus.CLAIMED)
        finally:
            db.close()

    def test_generation_after_restart_still_dedupes(self):
        pid = self._make_commissioned_project()
        self._generate(pid)
        db = self.Session()
        try:
            result = svc.generate_reminders(db, now=NOW)
            self.assertIn(pid, result["skipped"])
            self.assertNotIn(pid, result["generated"])
        finally:
            db.close()


# ---------------------------------------------------------------- API 链路

class ReminderApiTests(ReminderDBTests):
    def test_full_api_flow(self):
        pid = self._make_commissioned_project()
        gen = self.client.post(
            "/api/v1/capacity/reminders/generate",
            json={"project_ids": [pid], "weekend": [5, 6]},
        ).json()
        rid = gen["generated"][0]

        # 投产日较早、排期已过 => 生成即到期；到期扫描幂等不产生重复事件
        marked = self.client.post("/api/v1/capacity/reminders/mark-due").json()
        self.assertEqual(marked, [])
        detail = self.client.get(
            "/api/v1/capacity/reminders", params={"project_id": pid}
        ).json()
        self.assertEqual(detail[0]["status"], ReminderStatus.DUE.value)

        # 按责任人领取
        claimed = self.client.post(
            "/api/v1/capacity/reminders/claim-for-assignee",
            json={"assignee": "张三"},
        )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.json()[0]["claimed_by"], "张三")

        # 再次领取 → 空列表（没有到期项）
        again = self.client.post(
            "/api/v1/capacity/reminders/claim-for-assignee",
            json={"assignee": "张三"},
        ).json()
        self.assertEqual(again, [])

        # 批量延期
        post = self.client.post(
            "/api/v1/capacity/reminders/batch-postpone",
            json={
                "reminder_ids": [rid],
                "days": 5,
                "reason": "等企业反馈",
                "holidays": ["2026-09-28"],
                "weekend": [5, 6],
            },
        )
        self.assertEqual(post.status_code, 200, post.text)
        self.assertEqual(post.json()["postponed"], [rid])

        # 查询生成依据
        basis = self.client.get(f"/api/v1/capacity/reminders/{rid}/basis")
        self.assertEqual(basis.status_code, 200)
        body = basis.json()
        self.assertEqual(body["chain"], ["张三"])
        self.assertGreaterEqual(len(body["events"]), 4)
        self.assertEqual(body["basis"]["promised_monthly_capacity_tonnes"], 100.0)

        # 关闭后查询列表默认不含该提醒
        closed = self.client.post(
            f"/api/v1/capacity/reminders/{rid}/close",
            json={"reason": "问题解决"},
        )
        self.assertEqual(closed.status_code, 200)
        lst = self.client.get("/api/v1/capacity/reminders").json()
        self.assertNotIn(rid, [x["id"] for x in lst])
        # 显式包含已关闭
        lst_all = self.client.get(
            "/api/v1/capacity/reminders", params={"active_only": False}
        ).json()
        self.assertIn(rid, [x["id"] for x in lst_all])

        # 已关闭不能再领取：重新置到期尝试
        self.client.post("/api/v1/capacity/reminders/mark-due")
        claim = self.client.post(
            f"/api/v1/capacity/reminders/{rid}/claim",
            json={"claimed_by": "李四"},
        )
        self.assertEqual(claim.status_code, 400)

    def test_generate_for_non_commissioned_project_reports_failure(self):
        db = self.Session()
        try:
            park = models.IndustrialPark(
                name="未投产园区", park_type=ParkType.BORDER_PORT, city="x"
            )
            entity = models.Entity(
                name="未投产主体", region=Region.GUANGXI,
                country_or_province="广西", contact_person="x", contact_phone="1",
            )
            db.add_all([park, entity])
            db.flush()
            project = models.Project(
                name="建设中项目",
                status=ProjectStatus.UNDER_CONSTRUCTION,
                investment_direction="x",
                planned_investment_10k=1.0,
                park_id=park.id,
                initiator_id=entity.id,
            )
            db.add(project)
            db.commit()
            pid = project.id
        finally:
            db.close()
        resp = self.client.post(
            "/api/v1/capacity/reminders/generate",
            json={"project_ids": [pid]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["generated"], [])
        self.assertEqual(resp.json()["failed"][0]["project_id"], pid)


if __name__ == "__main__":
    unittest.main()
