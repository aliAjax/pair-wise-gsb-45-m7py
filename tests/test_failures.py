import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ShiftClosed


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'channel': 'C1', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 2
        self.service = build_service(str(Path(self.temp.name) / "test.db"), clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "VOY-21001", CREATE_DATA)
        # 越权访问必须进入拒绝审计
        denials = self.service.denials(Actor("chief", "port_controller"))
        self.assertTrue(any(d["action"] == "access" for d in denials))
        shift = self.service.open_shift(Actor("chief", "port_controller"), "夜班")
        self.service.create(Actor("creator", "port_controller"), "VOY-21001", CREATE_DATA, shift["id"])
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "port_controller"), "VOY-21001", CREATE_DATA, shift["id"])

    def test_stale_version_is_rejected(self):
        actor = Actor("chief", "port_controller")
        shift = self.service.open_shift(actor, "夜班")
        record = self.service.create(actor, "VOY-21001", CREATE_DATA, shift["id"])
        lease = self.service.acquire_lease(actor, {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 4, 'end_hour': 20}, shift["id"])
        record = self.service.act(
            actor, record["id"], record["version"], 'confirm',
            {'pilot_id': 'P-01', 'lease_id': lease['id'], 'lease_version': 1},
            shift["id"],
        )
        record = self.service.act(actor, record["id"], record["version"], 'berth', {'actual_draft_m': 10.4}, shift["id"])
        with self.assertRaises(Conflict):
            self.service.act(actor, record["id"], record["version"] - 1, 'depart', {'cargo_operation_complete': True}, shift["id"])

    def test_old_shift_rejected_after_handoff(self):
        actor = Actor("chief", "port_controller")
        first = self.service.open_shift(actor, "前半夜")
        record = self.service.create(actor, "VOY-21001", CREATE_DATA, first["id"])
        # 换班：旧班次被关闭，只留当前一个开放班次
        second = self.service.open_shift(Actor("chief2", "port_controller"), "后半夜")
        with self.assertRaises(ShiftClosed):
            self.service.acquire_lease(actor, {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 4, 'end_hour': 20}, first["id"])
        # 换班后的拒绝进入审计
        denials = self.service.denials(actor)
        self.assertTrue(any("换班" in d["details"]["reason"] for d in denials))
        # 新班次可继续操作
        lease = self.service.acquire_lease(actor, {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 4, 'end_hour': 20}, second["id"])
        self.assertEqual(lease["state"], "active")

    def test_unauthorized_role_is_denied_and_audited(self):
        actor = Actor("viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.open_shift(actor, "非法开班")
        denials = self.service.denials(Actor("chief", "port_controller"))
        self.assertTrue(any(d["actor_id"] == "viewer" for d in denials))


if __name__ == '__main__':
    unittest.main()
