import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'channel': 'C1', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}


def _open_shift(service, user='chief'):
    return service.open_shift(Actor(user, 'port_controller'), '夜航班次')


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 2
        self.service = build_service(str(Path(self.temp.name) / "test.db"), clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        actor = Actor("operator", "port_controller")
        shift = _open_shift(self.service)
        record = self.service.create(actor, "VOY-21001", CREATE_DATA, shift["id"])
        self.assertEqual(record["state"], "draft")
        lease = self.service.acquire_lease(
            actor,
            {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 4, 'end_hour': 20, 'record_id': record['id']},
            shift["id"],
        )
        record = self.service.act(
            actor, record["id"], record["version"], 'confirm',
            {'pilot_id': 'P-01', 'lease_id': lease['id'], 'lease_version': lease['version']},
            shift["id"],
        )
        self.assertEqual(record["state"], "confirmed")
        self.assertEqual(record["payload"]["lease_basis"]["fence_epoch"], lease["fence_epoch"])
        for action, _role, data, expected_state in [
            ('berth', 'port_controller', {'actual_draft_m': 10.3}, 'berthed'),
            ('depart', 'port_controller', {'cargo_operation_complete': True}, 'departed'),
        ]:
            record = self.service.act(Actor("operator", _role), record["id"], record["version"], action, data, shift["id"])
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "port_controller"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions[0], "created")
        self.assertEqual(actions[-1], "depart")
        # confirm事件必须挂有当时的租约依据
        confirm_event = next(event for event in timeline if event["action"] == "confirm")
        self.assertEqual(confirm_event["lease_id"], lease["id"])

    def test_berthed_record_keeps_lease_basis_after_release(self):
        actor = Actor("operator", "port_controller")
        shift = _open_shift(self.service)
        record = self.service.create(actor, "VOY-21002", CREATE_DATA, shift["id"])
        lease = self.service.acquire_lease(
            actor, {'vessel': 'HaiYun', 'channel': 'C1', 'start_hour': 4, 'end_hour': 20}, shift["id"],
        )
        record = self.service.act(
            actor, record["id"], record["version"], 'confirm',
            {'pilot_id': 'P-01', 'lease_id': lease['id'], 'lease_version': 1},
            shift["id"],
        )
        self.service.release_lease(actor, lease['id'], lease['version'], shift["id"])
        # 租约释放后，靠泊不依赖活租约，记录仍保留当时依据
        record = self.service.act(actor, record["id"], record["version"], 'berth', {'actual_draft_m': 10.3}, shift["id"])
        self.assertEqual(record["state"], "berthed")
        self.assertEqual(record["payload"]["lease_id"], lease["id"])
        self.assertEqual(record["payload"]["lease_basis"]["channel"], "C1")


if __name__ == '__main__':
    unittest.main()
