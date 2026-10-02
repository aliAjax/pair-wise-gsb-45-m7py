import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, LeaseExpired


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'channel': 'C1', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.now = 2
        self.service = build_service(self.db_path, clock=lambda: self.now)
        self.actor = Actor("chief", "port_controller")
        self.shift = self.service.open_shift(self.actor, "夜班")

    def tearDown(self):
        self.temp.cleanup()

    def _draft(self, reference="VOY-1"):
        return self.service.create(self.actor, reference, CREATE_DATA, self.shift["id"])

    def _lease(self, channel="C1", vessel="HaiYun", start=4, end=20, key_suffix=""):
        payload = {'vessel': vessel, 'channel': channel, 'start_hour': start, 'end_hour': end}
        return self.service.acquire_lease(self.actor, payload, self.shift["id"], lease_key=("k-%s" % key_suffix) if key_suffix else "")

    def test_only_one_active_lease_per_channel_sequential(self):
        self._lease(key_suffix="a")
        with self.assertRaises(Conflict):
            self._lease(vessel="OtherShip", key_suffix="b")

    def test_two_controllers_concurrent_only_one_wins(self):
        """两个控制台同时放行同一段航道，只允许一份有效租约落库。"""
        results = []
        errors = []
        lock = threading.Lock()

        def submit(user, key):
            try:
                lease = self.service.acquire_lease(
                    Actor(user, "port_controller"),
                    {'vessel': 'Ship-%s' % user, 'channel': 'C1', 'start_hour': 4, 'end_hour': 20},
                    self.shift["id"],
                    lease_key=key,
                )
                with lock:
                    results.append(lease)
            except Exception as exc:  # noqa: BLE001 - 并发竞争方预期失败
                with lock:
                    errors.append(exc)

        t1 = threading.Thread(target=submit, args=("ctl1", "ctl1-key"))
        t2 = threading.Thread(target=submit, args=("ctl2", "ctl2-key"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        active = self.service.list_leases(self.actor, state="active", channel="C1")
        self.assertEqual(len(active), 1)

    def test_same_vessel_overlapping_window_on_other_channel_rejected(self):
        self._lease(channel="C1", key_suffix="a")
        with self.assertRaises(Conflict):
            self._lease(channel="C2", key_suffix="b")
        # 不重叠时段可以在别的航道申请
        other = self._lease(channel="C2", start=20, end=24, key_suffix="c")
        self.assertEqual(other["channel"], "C2")

    def test_renew_extends_and_late_renew_cannot_revive_released(self):
        lease = self._lease(key_suffix="a")
        renewed = self.service.renew_lease(self.actor, lease["id"], lease["version"], 22, self.shift["id"])
        self.assertEqual(renewed["end_hour"], 22)
        self.assertEqual(renewed["version"], 2)
        # 晚到的旧版本续租：版本栅栏拒绝
        with self.assertRaises(Conflict):
            self.service.renew_lease(self.actor, lease["id"], 1, 23, self.shift["id"])
        # 释放后，晚到续租不能把租约重新救活
        self.service.release_lease(self.actor, lease["id"], 2, self.shift["id"])
        with self.assertRaises(Conflict):
            self.service.renew_lease(self.actor, lease["id"], 2, 23, self.shift["id"])
        dead = self.service.get_lease(self.actor, lease["id"])
        self.assertEqual(dead["state"], "released")

    def test_expired_lease_rejects_confirm_and_recheck_window(self):
        record = self._draft()
        lease = self._lease(start=4, end=10, key_suffix="a")
        # 租约时段不覆盖计划时段（etd=18 > end=10）：重新核对时段后拒绝
        with self.assertRaises(LeaseExpired):
            self.service.act(
                self.actor, record["id"], record["version"], "confirm",
                {"pilot_id": "P-1", "lease_id": lease["id"], "lease_version": 1},
                self.shift["id"],
            )
        # 时间走到租约结束之后：租约过期，确认仍被拒绝
        self.now = 12
        with self.assertRaises(LeaseExpired):
            self.service.act(
                self.actor, record["id"], record["version"], "confirm",
                {"pilot_id": "P-1", "lease_id": lease["id"], "lease_version": 1},
                self.shift["id"],
            )
        # 重新申请覆盖计划时段的新租约后确认成功
        new_lease = self._lease(start=5, end=20, key_suffix="b")
        record = self.service.act(
            self.actor, record["id"], record["version"], "confirm",
            {"pilot_id": "P-1", "lease_id": new_lease["id"], "lease_version": 1},
            self.shift["id"],
        )
        self.assertEqual(record["state"], "confirmed")

    def test_crash_recovery_marks_old_leases_and_blocks_late_renew(self):
        lease = self._lease(start=0, end=4, key_suffix="a")
        # 进程崩溃后重启：晚到的续租到达前，恢复先把旧租约识别为expired
        recovered = build_service(self.db_path, clock=lambda: 10)
        self.assertIn(lease["id"], recovered.recovery["expired_lease_ids"])
        stale = recovered.get_lease(self.actor, lease["id"])
        self.assertEqual(stale["state"], "expired")
        with self.assertRaises(LeaseExpired):
            recovered.renew_lease(self.actor, lease["id"], 1, 22, self.shift["id"])
        # 恢复后同一航道可以发放新一代租约，fence epoch单调递增
        new_lease = recovered.acquire_lease(
            self.actor, {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 10, 'end_hour': 20}, self.shift["id"],
        )
        self.assertEqual(new_lease["fence_epoch"], lease["fence_epoch"] + 1)
        # 旧租约审计时间线记录了过期落定
        timeline = recovered.lease_timeline(self.actor, lease["id"])
        self.assertTrue(any(event["action"] == "lease_expired" for event in timeline))

    def test_reacquire_after_natural_expiry_gets_new_epoch(self):
        lease = self._lease(start=0, end=4, key_suffix="a")
        self.now = 5
        new_lease = self._lease(start=5, end=9, key_suffix="b")
        self.assertEqual(new_lease["fence_epoch"], lease["fence_epoch"] + 1)
        self.assertEqual(self.service.get_lease(self.actor, lease["id"])["state"], "expired")

    def test_confirm_rejects_lease_for_other_vessel_or_channel(self):
        record = self._draft()
        lease = self._lease(vessel="Intruder", channel="C9", key_suffix="x")
        with self.assertRaises(Conflict):
            self.service.act(
                self.actor, record["id"], record["version"], "confirm",
                {"pilot_id": "P-1", "lease_id": lease["id"], "lease_version": 1},
                self.shift["id"],
            )


if __name__ == '__main__':
    unittest.main()
