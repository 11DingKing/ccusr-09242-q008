"""产能跟进提醒编排测试。

覆盖：时区边界、重复触发去重、转交并发、已关闭事项、按责任人领取、
批量延期、生成依据查询、处理链留痕、服务重启后续处理到期事项。

注意：本模块在导入 app 前把 DATABASE_URL 指到临时文件，避免污染业务库。
"""

import itertools
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="reminder_tests_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models
from app.enums import (
    FollowUpReminderStatus,
    FollowUpStatus,
    ParkType,
    ProjectStatus,
    Region,
    ReminderEventType,
)
from app.services import reminders as svc
from app.services.reminders import (
    ReminderStateError,
    ReminderValidationError,
)

API = "/api/v1/capacity"


def _utc(y, m, d, hh=0, mm=0):
    """naive UTC 时间（与库存储约定一致）。"""
    return datetime(y, m, d, hh, mm)


class ReminderTestBase(unittest.TestCase):
    _counter = itertools.count(1)

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        db = SessionLocal()
        try:
            for table in (
                models.FollowUpReminderEvent,
                models.FollowUpReminder,
                models.CapacityFollowUp,
                models.MonthlyCapacityReport,
                models.ProjectStatusLog,
                models.ProjectMilestone,
                models.ProjectApproval,
                models.NegotiationRecord,
                models.CooperationIntent,
                models.ProjectCategory,
                models.Project,
                models.EntityCapability,
                models.Entity,
                models.IndustrialPark,
            ):
                db.query(table).delete()
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def _make_follow_up(
        self,
        *,
        promised=120.0,
        gap=55.0,
        owner="张三",
        last_contact_at=None,
        utilization=None,
        with_report=True,
    ):
        # 缺省让达产率与缺口一致（utilization = 100 - gap）
        if utilization is None and gap is not None:
            utilization = round(100.0 - gap, 2)
        n = next(self._counter)
        db = SessionLocal()
        try:
            park = models.IndustrialPark(
                name=f"测试园区{n}", park_type=ParkType.KEY_INDUSTRIAL, city="南宁市"
            )
            entity = models.Entity(
                name=f"测试主体{n}",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="联系人",
                contact_phone="0771-0000000",
            )
            db.add_all([park, entity])
            db.flush()
            project = models.Project(
                name=f"测试项目{n}",
                project_code=f"TEST-{n}",
                status=ProjectStatus.COMMISSIONED,
                investment_direction="测试方向",
                planned_investment_10k=1000.0,
                promised_monthly_capacity_tonnes=promised,
                park_id=park.id,
                initiator_id=entity.id,
                project_leader=owner,
            )
            db.add(project)
            db.flush()
            if with_report:
                db.add(
                    models.MonthlyCapacityReport(
                        project_id=project.id,
                        report_year=2026,
                        report_month=8,
                        actual_output_tonnes=round(promised * utilization / 100, 2),
                        capacity_utilization_rate=utilization,
                    )
                )
            fu = models.CapacityFollowUp(
                project_id=project.id,
                title=f"测试跟进{n}",
                status=FollowUpStatus.PENDING,
                gap_percentage=gap,
                responsible_person=owner,
                last_contact_at=last_contact_at,
            )
            db.add(fu)
            db.commit()
            return fu.id, project.id
        finally:
            db.close()

    def _generate(self, **overrides):
        body = {
            "as_of": "2026-09-25T09:00:00",
            "timezone": "Asia/Shanghai",
            "defer_weekends": False,
        }
        body.update(overrides)
        return self.client.post(f"{API}/reminders/generate", json=body)

    def _get_reminder(self, reminder_id):
        return self.client.get(f"{API}/reminders/{reminder_id}")

    def _reminder_count(self, follow_up_id):
        db = SessionLocal()
        try:
            return (
                db.query(models.FollowUpReminder)
                .filter(models.FollowUpReminder.follow_up_id == follow_up_id)
                .count()
            )
        finally:
            db.close()


class IntervalPolicyTests(ReminderTestBase):
    """提醒间隔分档（承诺缺口 → 提醒频率）。"""

    def test_interval_policy_boundaries(self):
        self.assertEqual(svc.compute_interval_days(None, 55.0)[0], 3)
        self.assertEqual(svc.compute_interval_days(None, 50.0)[0], 3)
        self.assertEqual(svc.compute_interval_days(None, 49.9)[0], 7)
        self.assertEqual(svc.compute_interval_days(None, 30.0)[0], 7)
        self.assertEqual(svc.compute_interval_days(None, 29.9)[0], 14)
        self.assertEqual(svc.compute_interval_days(None, 15.0)[0], 14)
        self.assertEqual(svc.compute_interval_days(None, 14.9)[0], 30)
        # 缺口缺失时按达产率折算
        self.assertEqual(svc.compute_interval_days(40.0, None)[0], 3)
        # 都缺失时默认 30 天
        self.assertEqual(svc.compute_interval_days(None, None)[0], 30)


class TimezoneBoundaryTests(ReminderTestBase):
    """时区边界：节假日/周末判断必须按调用方时区的当地日期。"""

    def test_holiday_check_uses_local_date_not_utc(self):
        # 上次联系 UTC 2026-09-23 16:30 = 北京 2026-09-24 00:30
        # 候选：北京 2026-09-27 00:30（UTC 2026-09-26 16:30）
        # 当地日期 9-27 是节假日 → 顺延；若误用 UTC 日期（9-26）则不会顺延
        due, meta = svc.compute_next_due_at(
            last_contact_at=_utc(2026, 9, 23, 16, 30),
            interval_days=3,
            now=_utc(2026, 9, 23, 17, 0),
            timezone_name="Asia/Shanghai",
            holidays={date(2026, 9, 27)},
            defer_weekends=False,
        )
        self.assertEqual(due, _utc(2026, 9, 27, 16, 30))
        self.assertEqual(meta["holiday_shift_days"], 1)
        self.assertEqual(meta["due_local"], "2026-09-28T00:30:00+08:00")

    def test_weekend_check_uses_local_date_not_utc(self):
        # 上次联系 UTC 2026-09-24 16:30 = 北京 2026-09-25 00:30（周五）
        # 候选北京 2026-09-26 00:30（周六），UTC 侧仍是周五 —— 必须按当地日期顺延
        due, meta = svc.compute_next_due_at(
            last_contact_at=_utc(2026, 9, 24, 16, 30),
            interval_days=1,
            now=_utc(2026, 9, 24, 17, 0),
            timezone_name="Asia/Shanghai",
            holidays=set(),
            defer_weekends=True,
        )
        # 顺延两天到周一 2026-09-28 00:30+08 = UTC 2026-09-27 16:30
        self.assertEqual(due, _utc(2026, 9, 27, 16, 30))
        self.assertEqual(meta["holiday_shift_days"], 2)

    def test_never_contacted_reminds_immediately(self):
        now = _utc(2026, 9, 25, 1, 0)
        due, meta = svc.compute_next_due_at(
            last_contact_at=None,
            interval_days=7,
            now=now,
            timezone_name="Asia/Shanghai",
            holidays=set(),
            defer_weekends=False,
        )
        self.assertEqual(due, now)
        self.assertIsNone(meta["base_local"])

    def test_holiday_overrun_rejected(self):
        holidays = {date(2026, 1, 1) + timedelta(days=i) for i in range(400)}
        with self.assertRaises(ReminderValidationError):
            svc.compute_next_due_at(
                last_contact_at=_utc(2025, 12, 31),
                interval_days=1,
                now=_utc(2026, 1, 1),
                timezone_name="Asia/Shanghai",
                holidays=holidays,
                defer_weekends=False,
            )

    def test_invalid_timezone_rejected(self):
        fu_id, _ = self._make_follow_up()
        resp = self._generate(timezone="Mars/Olympus")
        self.assertEqual(resp.status_code, 400)


class GenerateAndBasisTests(ReminderTestBase):
    """提醒生成与生成依据查询。"""

    def test_generate_creates_reminder_with_basis(self):
        fu_id, project_id = self._make_follow_up(gap=55.0, utilization=45.0)
        resp = self._generate(holidays=["2026-10-01"])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["generated_count"], 1)
        self.assertEqual(data["skipped_count"], 0)
        reminder_id = data["items"][0]["reminder_id"]

        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["status"], "待提醒")
        self.assertEqual(reminder["owner"], "张三")
        self.assertEqual(reminder["interval_days"], 3)
        self.assertEqual(reminder["due_at"], "2026-09-25T01:00:00")
        self.assertEqual(reminder["follow_up_id"], fu_id)
        self.assertEqual(reminder["project_id"], project_id)

        basis_resp = self.client.get(f"{API}/reminders/{reminder_id}/basis")
        self.assertEqual(basis_resp.status_code, 200)
        basis = basis_resp.json()["basis"]
        self.assertEqual(basis["promised_monthly_capacity_tonnes"], 120.0)
        self.assertEqual(basis["utilization_rate"], 45.0)
        self.assertEqual(basis["gap_percentage"], 55.0)
        self.assertEqual(basis["interval_days"], 3)
        self.assertIn("3 天", basis["interval_rule"])
        self.assertEqual(basis["timezone"], "Asia/Shanghai")
        self.assertEqual(basis["holidays"], ["2026-10-01"])
        self.assertIsNone(basis["last_contact_at"])
        self.assertEqual(basis["latest_report"]["year"], 2026)

    def test_generate_missing_reminder_404(self):
        self.assertEqual(self._get_reminder(9999).status_code, 404)
        self.assertEqual(
            self.client.get(f"{API}/reminders/9999/basis").status_code, 404
        )


class DuplicateTriggerTests(ReminderTestBase):
    """重复触发：连续生成与并发生成都不得产生重复提醒。"""

    def test_sequential_generate_is_idempotent(self):
        fu_id, _ = self._make_follow_up()
        first = self._generate().json()
        self.assertEqual(first["generated_count"], 1)

        second = self._generate().json()
        self.assertEqual(second["generated_count"], 0)
        self.assertEqual(second["skipped_count"], 1)
        self.assertEqual(second["items"][0]["result"], "skipped_existing")
        self.assertEqual(self._reminder_count(fu_id), 1)

    def test_concurrent_generate_keeps_single_reminder(self):
        fu_id, _ = self._make_follow_up()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            db = SessionLocal()
            try:
                barrier.wait()
                result = svc.generate_reminders(
                    db,
                    timezone_name="Asia/Shanghai",
                    defer_weekends=False,
                    as_of=datetime(2026, 9, 25, 1, 0),
                )
                outcomes.append(result["generated_count"])
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self._reminder_count(fu_id), 1)

    def test_process_due_is_idempotent(self):
        self._make_follow_up()
        self._generate(as_of="2026-09-20T09:00:00")

        first = self.client.post(
            f"{API}/reminders/process-due",
            json={"as_of": "2026-09-21T00:00:00", "timezone": "Asia/Shanghai"},
        ).json()
        self.assertEqual(first["processed_count"], 1)

        second = self.client.post(
            f"{API}/reminders/process-due",
            json={"as_of": "2026-09-21T00:00:00", "timezone": "Asia/Shanghai"},
        ).json()
        self.assertEqual(second["processed_count"], 0)

        reminder_id = first["reminder_ids"][0]
        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["status"], "待处理")


class RestartRecoveryTests(ReminderTestBase):
    """服务重启后继续处理到期事项（启动时自动扫描）。"""

    def test_restart_processes_due_reminders(self):
        fu_id, _ = self._make_follow_up()
        reminder_id = self._generate(as_of="2026-09-20T09:00:00").json()["items"][0][
            "reminder_id"
        ]
        # 重启前仍是待提醒
        self.assertEqual(self._get_reminder(reminder_id).json()["status"], "待提醒")

        # 模拟服务重启：进入 TestClient 上下文触发 lifespan 启动扫描
        with TestClient(app) as client:
            reminder = client.get(f"{API}/reminders/{reminder_id}").json()
            self.assertEqual(reminder["status"], "待处理")
            events = client.get(f"{API}/reminders/{reminder_id}/events").json()
            due_events = [e for e in events if e["event_type"] == "已到期"]
            self.assertEqual(len(due_events), 1)

        # 再次重启不产生重复到期事件
        with TestClient(app) as client:
            events = client.get(f"{API}/reminders/{reminder_id}/events").json()
            due_events = [e for e in events if e["event_type"] == "已到期"]
            self.assertEqual(len(due_events), 1)


class ClaimTests(ReminderTestBase):
    """按责任人领取。"""

    def test_claim_rules(self):
        self._make_follow_up(owner="张三")
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        # 他人领取被拒绝：需先转交
        resp = self.client.post(
            f"{API}/reminders/claim",
            json={"owner": "李四", "reminder_ids": [reminder_id]},
        )
        self.assertFalse(resp.json()["results"][0]["ok"])
        self.assertIn("转交", resp.json()["results"][0]["error"])

        # 本人领取成功
        resp = self.client.post(
            f"{API}/reminders/claim",
            json={"owner": "张三", "reminder_ids": [reminder_id]},
        )
        self.assertTrue(resp.json()["results"][0]["ok"])
        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["status"], "已领取")
        self.assertIsNotNone(reminder["claimed_at"])

        # 已领取不可重复领取
        resp = self.client.post(
            f"{API}/reminders/claim",
            json={"owner": "张三", "reminder_ids": [reminder_id]},
        )
        self.assertFalse(resp.json()["results"][0]["ok"])

    def test_claim_unassigned_sets_owner(self):
        # 未分配责任人的跟进事项（项目负责人也为空），领取人即成为责任人
        fu_id, _ = self._make_follow_up(owner=None)
        reminder_id = self._generate().json()["items"][0]["reminder_id"]
        resp = self.client.post(
            f"{API}/reminders/claim",
            json={"owner": "李四", "reminder_ids": [reminder_id]},
        )
        self.assertTrue(resp.json()["results"][0]["ok"])
        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["owner"], "李四")
        # 跟进事项责任人同步
        follow_up = self.client.get(f"{API}/follow-ups/{fu_id}").json()
        self.assertEqual(follow_up["responsible_person"], "李四")

    def test_claim_missing_reminder(self):
        resp = self.client.post(
            f"{API}/reminders/claim",
            json={"owner": "张三", "reminder_ids": [9999]},
        )
        self.assertFalse(resp.json()["results"][0]["ok"])
        self.assertIn("不存在", resp.json()["results"][0]["error"])


class DeferTests(ReminderTestBase):
    """批量延期：原因必填，顺延规则沿用生成时的规则。"""

    def test_batch_defer_with_reason(self):
        self._make_follow_up(owner="张三")
        self._make_follow_up(owner="李四")
        resp = self._generate(as_of="2027-06-01T09:00:00")
        ids = [item["reminder_id"] for item in resp.json()["items"]]
        self.assertEqual(len(ids), 2)

        resp = self.client.post(
            f"{API}/reminders/defer",
            json={
                "reminder_ids": ids,
                "defer_days": 5,
                "reason": "企业停产检修，下周再联系",
                "operator": "王五",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(all(r["ok"] for r in resp.json()["results"]))

        for rid in ids:
            reminder = self._get_reminder(rid).json()
            self.assertEqual(reminder["status"], "已延期")
            # 原到期 2027-06-01T01:00Z（未来），顺延 5 天
            self.assertEqual(reminder["due_at"], "2027-06-06T01:00:00")
            self.assertEqual(reminder["defer_reason"], "企业停产检修，下周再联系")
            self.assertEqual(reminder["deferred_count"], 1)
            events = self.client.get(f"{API}/reminders/{rid}/events").json()
            deferred = [e for e in events if e["event_type"] == "已延期"]
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0]["reason"], "企业停产检修，下周再联系")

    def test_defer_until_date_keeps_local_time(self):
        self._make_follow_up()
        reminder_id = self._generate(as_of="2027-06-01T09:00:00").json()["items"][0][
            "reminder_id"
        ]
        resp = self.client.post(
            f"{API}/reminders/defer",
            json={
                "reminder_ids": [reminder_id],
                "defer_until": "2027-06-10",
                "reason": "负责人出差",
            },
        )
        self.assertTrue(resp.json()["results"][0]["ok"])
        # 保留原时刻（北京 09:00 = UTC 01:00）
        self.assertEqual(
            self._get_reminder(reminder_id).json()["due_at"], "2027-06-10T01:00:00"
        )

    def test_defer_applies_stored_holiday_rules(self):
        # 生成时启用周末顺延：延期 5 天落在周日应再顺延到周一
        fu_id, _ = self._make_follow_up(last_contact_at=_utc(2026, 11, 28, 1, 0))
        db = SessionLocal()
        try:
            result = svc.generate_reminders(
                db,
                timezone_name="Asia/Shanghai",
                defer_weekends=True,
                as_of=datetime(2026, 11, 28, 2, 0),
            )
            reminder_id = result["items"][0]["reminder_id"]
            # 到期：上次联系 2026-11-28 09:00+08 + 3 天 = 2026-12-01 09:00+08（周二）
            results = svc.defer_reminders(
                db,
                reminder_ids=[reminder_id],
                reason="顺延规则验证",
                defer_days=5,
                now=datetime(2026, 11, 1, tzinfo=timezone.utc),
            )
            self.assertTrue(results[0]["ok"])
            reminder = db.get(models.FollowUpReminder, reminder_id)
            # 2026-12-01 + 5 天 = 2026-12-06（周日）→ 顺延到周一 2026-12-07
            self.assertEqual(reminder.due_at, _utc(2026, 12, 7, 1, 0))
        finally:
            db.close()

    def test_defer_validation(self):
        self._make_follow_up()
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        # 原因必填（pydantic 校验）
        resp = self.client.post(
            f"{API}/reminders/defer",
            json={"reminder_ids": [reminder_id], "defer_days": 3, "reason": ""},
        )
        self.assertEqual(resp.status_code, 422)
        # defer_until 与 defer_days 二选一
        resp = self.client.post(
            f"{API}/reminders/defer",
            json={
                "reminder_ids": [reminder_id],
                "defer_days": 3,
                "defer_until": "2027-01-01",
                "reason": "两者都给了",
            },
        )
        self.assertEqual(resp.status_code, 400)
        # 延期到过去的时间
        resp = self.client.post(
            f"{API}/reminders/defer",
            json={
                "reminder_ids": [reminder_id],
                "defer_until": "2020-01-01",
                "reason": "时间倒流",
            },
        )
        self.assertFalse(resp.json()["results"][0]["ok"])
        self.assertIn("晚于当前时间", resp.json()["results"][0]["error"])

    def test_deferred_reminder_becomes_due_again(self):
        self._make_follow_up()
        reminder_id = self._generate(as_of="2027-06-01T09:00:00").json()["items"][0][
            "reminder_id"
        ]
        self.client.post(
            f"{API}/reminders/defer",
            json={
                "reminder_ids": [reminder_id],
                "defer_days": 5,
                "reason": "暂缓联系",
            },
        )
        # 到期扫描：延期时间过后重新进入待处理
        resp = self.client.post(
            f"{API}/reminders/process-due",
            json={"as_of": "2027-06-07T00:00:00", "timezone": "Asia/Shanghai"},
        )
        self.assertEqual(resp.json()["processed_count"], 1)
        self.assertEqual(self._get_reminder(reminder_id).json()["status"], "待处理")


class TransferTests(ReminderTestBase):
    """责任人转交：保留原处理链，并发下只允许一个成功者。"""

    def test_transfer_preserves_handling_chain(self):
        fu_id, _ = self._make_follow_up(owner="张三")
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/transfer",
            json={"to_owner": "李四", "reason": "张三休假一周", "operator": "主管"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["owner"], "李四")
        self.assertEqual(resp.json()["version"], 2)

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/transfer",
            json={"to_owner": "王五"},
        )
        self.assertEqual(resp.json()["owner"], "王五")

        # 跟进事项责任人同步为最新接收人
        follow_up = self.client.get(f"{API}/follow-ups/{fu_id}").json()
        self.assertEqual(follow_up["responsible_person"], "王五")

        # 处理链完整保留：生成(张三) → 张三→李四 → 李四→王五
        events = self.client.get(f"{API}/reminders/{reminder_id}/events").json()
        chain = [(e["event_type"], e["from_owner"], e["to_owner"]) for e in events]
        self.assertEqual(
            chain,
            [
                ("已生成", None, "张三"),
                ("已转交", "张三", "李四"),
                ("已转交", "李四", "王五"),
            ],
        )
        self.assertEqual(events[1]["reason"], "张三休假一周")

    def test_transfer_rejects_stale_version_and_same_owner(self):
        self._make_follow_up(owner="张三")
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/transfer",
            json={"to_owner": "李四", "expected_version": 99},
        )
        self.assertEqual(resp.status_code, 409)

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/transfer",
            json={"to_owner": "张三"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_transfer_concurrent_only_one_wins(self):
        self._make_follow_up(owner="张三")
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name):
            db = SessionLocal()
            try:
                barrier.wait()
                try:
                    svc.transfer_reminder(
                        db,
                        reminder_id=reminder_id,
                        to_owner=name,
                        expected_version=1,
                    )
                    outcomes[name] = "ok"
                except ReminderStateError:
                    outcomes[name] = "conflict"
            finally:
                db.close()

        threads = [
            threading.Thread(target=worker, args=("李四",)),
            threading.Thread(target=worker, args=("王五",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 恰好一个成功，最终责任人唯一，处理链只有一条转交记录
        self.assertEqual(sorted(outcomes.values()), ["conflict", "ok"])
        winner = [name for name, r in outcomes.items() if r == "ok"][0]
        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["owner"], winner)
        self.assertEqual(reminder["version"], 2)
        events = self.client.get(f"{API}/reminders/{reminder_id}/events").json()
        transferred = [e for e in events if e["event_type"] == "已转交"]
        self.assertEqual(len(transferred), 1)
        self.assertEqual(transferred[0]["from_owner"], "张三")
        self.assertEqual(transferred[0]["to_owner"], winner)


class CompleteTests(ReminderTestBase):
    """处理完成：记录联系时间并自动编排下一次提醒。"""

    def test_complete_schedules_next_reminder(self):
        fu_id, _ = self._make_follow_up(gap=55.0)
        reminder_id = self._generate(as_of="2026-09-20T09:00:00").json()["items"][0][
            "reminder_id"
        ]

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/complete",
            json={"contacted_at": "2026-09-22T10:00:00+08:00", "note": "已电话沟通"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "已处理")
        next_id = data["next_reminder_id"]
        self.assertIsNotNone(next_id)

        # 上次联系时间落库（UTC 存储）
        follow_up = self.client.get(f"{API}/follow-ups/{fu_id}").json()
        self.assertEqual(follow_up["last_contact_at"], "2026-09-22T02:00:00")
        self.assertEqual(follow_up["status"], "跟进中")

        # 下一次提醒 = 联系时间 + 3 天（缺口 55%），北京 2026-09-25 10:00（周五）
        next_reminder = self._get_reminder(next_id).json()
        self.assertEqual(next_reminder["due_at"], "2026-09-25T02:00:00")
        self.assertEqual(next_reminder["owner"], "张三")
        basis = self.client.get(f"{API}/reminders/{next_id}/basis").json()["basis"]
        self.assertEqual(
            basis["last_contact_at"], "2026-09-22T02:00:00+00:00"
        )

        # 原提醒已处理，重复处理返回 409
        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/complete", json={}
        )
        self.assertEqual(resp.status_code, 409)

    def test_complete_uses_latest_utilization_for_next_interval(self):
        # 处理后达产率回升 → 下一次提醒间隔按最新数据重新分档
        fu_id, project_id = self._make_follow_up(gap=55.0, utilization=45.0)
        reminder_id = self._generate(as_of="2026-09-20T09:00:00").json()["items"][0][
            "reminder_id"
        ]
        db = SessionLocal()
        try:
            db.add(
                models.MonthlyCapacityReport(
                    project_id=project_id,
                    report_year=2026,
                    report_month=9,
                    actual_output_tonnes=102.0,
                    capacity_utilization_rate=85.0,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/complete",
            json={"contacted_at": "2026-09-22T10:00:00+08:00"},
        )
        next_id = resp.json()["next_reminder_id"]
        next_reminder = self._get_reminder(next_id).json()
        # 最新缺口 15% → 间隔 14 天
        self.assertEqual(next_reminder["interval_days"], 14)
        self.assertEqual(next_reminder["due_at"], "2026-10-06T02:00:00")


class ClosedFollowUpTests(ReminderTestBase):
    """已关闭事项：自动取消未结提醒，且不再重复生成。"""

    def test_close_cancels_open_reminder_and_stops_regeneration(self):
        fu_id, _ = self._make_follow_up()
        reminder_id = self._generate().json()["items"][0]["reminder_id"]

        resp = self.client.put(
            f"{API}/follow-ups/{fu_id}", json={"status": "已关闭"}
        )
        self.assertEqual(resp.status_code, 200)

        reminder = self._get_reminder(reminder_id).json()
        self.assertEqual(reminder["status"], "已取消")
        events = self.client.get(f"{API}/reminders/{reminder_id}/events").json()
        cancelled = [e for e in events if e["event_type"] == "已取消"]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("已关闭", cancelled[0]["reason"])

        # 关闭后再次生成：不产生新提醒
        resp = self._generate()
        self.assertEqual(resp.json()["generated_count"], 0)
        self.assertEqual(resp.json()["items"], [])
        self.assertEqual(self._reminder_count(fu_id), 1)

        # 已取消的提醒不可再处理
        resp = self.client.post(
            f"{API}/reminders/{reminder_id}/complete", json={}
        )
        self.assertEqual(resp.status_code, 409)

        # 重新打开后恢复编排
        resp = self.client.put(
            f"{API}/follow-ups/{fu_id}", json={"status": "待跟进"}
        )
        self.assertEqual(resp.status_code, 200)
        resp = self._generate()
        self.assertEqual(resp.json()["generated_count"], 1)

    def test_resolve_also_cancels_reminder(self):
        fu_id, _ = self._make_follow_up()
        reminder_id = self._generate().json()["items"][0]["reminder_id"]
        resp = self.client.put(
            f"{API}/follow-ups/{fu_id}", json={"status": "已解决"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_reminder(reminder_id).json()["status"], "已取消")
        resp = self._generate()
        self.assertEqual(resp.json()["generated_count"], 0)


class WorklistTests(ReminderTestBase):
    """运营工作清单：按责任人/状态/到期过滤。"""

    def test_worklist_filters(self):
        self._make_follow_up(owner="张三")
        self._make_follow_up(owner="李四")
        # 张三的已到期，李四的未到期
        self._generate(follow_up_id=None, as_of="2026-09-20T09:00:00")
        db = SessionLocal()
        try:
            fu_li = (
                db.query(models.CapacityFollowUp)
                .filter(models.CapacityFollowUp.responsible_person == "李四")
                .first()
            )
            reminder_li = (
                db.query(models.FollowUpReminder)
                .filter(models.FollowUpReminder.follow_up_id == fu_li.id)
                .first()
            )
            reminder_li.due_at = _utc(2027, 6, 1, 1, 0)
            db.commit()
        finally:
            db.close()

        resp = self.client.get(f"{API}/reminders", params={"owner": "张三"})
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["owner"], "张三")

        resp = self.client.get(f"{API}/reminders", params={"due_only": True})
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["owner"], "张三")

        resp = self.client.get(
            f"{API}/reminders", params={"status": "待提醒", "owner": "李四"}
        )
        self.assertEqual(len(resp.json()), 1)


if __name__ == "__main__":
    unittest.main()
