import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, LeaseConflict, PermissionDenied


PLAN = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': '', 'channel': 'C1'}
PLAN_B = dict(PLAN, vessel='LuBo', berth='B13')
SLOT = {'channel': 'C1', 'slot_start_hour': 6, 'slot_end_hour': 18, 'ttl_seconds': 300}
START = datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=START):
        self.moment = start

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


class LeaseTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.clock = FakeClock()
        self.service = build_service(self.db, clock=self.clock)
        self.creator = Actor("planner", "port_controller")
        self.officer = Actor("officer-a", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def claim_shift(self, officer_id="officer-a", actor=None):
        return self.service.handover_shift(actor or Actor(officer_id, "port_controller"), officer_id)

    def create_plan(self, reference="VOY-1", data=None):
        return self.service.create(self.creator, reference, dict(data or PLAN))

    def request_lease(self, record, actor=None, slot=None):
        return self.service.act(actor or self.officer, record["id"], record["version"], "request_channel", dict(slot or SLOT))


class LeaseAllocationTest(LeaseTestBase):
    def setUp(self):
        super().setUp()
        self.claim_shift()

    def test_single_valid_lease_per_channel_and_slot(self):
        first = self.create_plan("VOY-1")
        second = self.create_plan("VOY-2", PLAN_B)
        leased = self.request_lease(first)
        self.assertEqual(leased["lease"]["state"], "active")
        self.assertEqual(leased["lease"]["fencing_token"], 1)
        self.assertEqual(leased["payload"]["channel_lease"]["lease_id"], leased["lease"]["id"])
        with self.assertRaises(LeaseConflict):
            self.request_lease(second)
        with self.assertRaises(LeaseConflict):
            self.request_lease(self.service.get_record(self.creator, first["id"]))
        evening = self.create_plan("VOY-3", dict(PLAN_B, berth="B14", eta_hour=18, etd_hour=22))
        other_slot = self.service.act(self.officer, evening["id"], evening["version"], "request_channel", dict(SLOT, slot_start_hour=18, slot_end_hour=22))
        self.assertEqual(other_slot["lease"]["fencing_token"], 2)

    def test_concurrent_requests_only_one_wins(self):
        first = self.create_plan("VOY-1")
        second = self.create_plan("VOY-2", PLAN_B)
        results = []

        def attempt(record):
            try:
                self.request_lease(record)
                results.append("ok")
            except LeaseConflict:
                results.append("conflict")

        threads = [threading.Thread(target=attempt, args=(record,)) for record in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        leases = self.service.list_leases(self.creator, channel="C1")
        self.assertEqual(sum(1 for lease in leases if lease["effective_state"] == "active"), 1)

    def test_lease_action_requires_on_duty_officer(self):
        record = self.create_plan("VOY-1")
        with self.assertRaises(PermissionDenied):
            self.request_lease(record, actor=Actor("officer-b", "port_controller"))
        denied = [event for event in self.service.timeline(self.creator, record["id"]) if event["action"] == "channel_op_denied"]
        self.assertTrue(denied)
        self.assertTrue(any(event["action"] == "channel_op_denied" for event in self.service.system_events(self.creator)))


class LeaseConfirmTest(LeaseTestBase):
    def setUp(self):
        super().setUp()
        self.claim_shift()

    def test_confirm_requires_valid_lease(self):
        record = self.create_plan("VOY-1")
        with self.assertRaises(LeaseConflict):
            self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.request_lease(record)
        confirmed = self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        basis = confirmed["payload"]["channel_lease_basis"]
        self.assertEqual(basis["lease_id"], confirmed["payload"]["channel_lease"]["lease_id"])
        self.assertEqual(basis["fencing_token"], 1)
        self.assertEqual(basis["channel"], "C1")

    def test_expired_lease_rejects_confirm_then_recheck_slot(self):
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        first_lease_id = record["lease"]["id"]
        self.clock.advance(seconds=301)
        with self.assertRaises(LeaseConflict):
            self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        denied = [event for event in self.service.timeline(self.creator, record["id"]) if event["action"] == "channel_op_denied"]
        self.assertTrue(any("过期" in event["details"]["reason"] for event in denied))
        record = self.service.get_record(self.creator, record["id"])
        record = self.request_lease(record)
        self.assertNotEqual(record["lease"]["id"], first_lease_id)
        self.assertEqual(record["lease"]["fencing_token"], 2)
        confirmed = self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        self.assertEqual(confirmed["state"], "confirmed")

    def test_berthed_record_keeps_lease_basis(self):
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        record = self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.service.act(self.creator, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        self.assertEqual(record["state"], "berthed")
        self.clock.advance(hours=2)
        recovered = self.service.recover_expired_leases()
        self.assertEqual(len(recovered), 1)
        berthed = self.service.get_record(self.creator, record["id"])
        basis = berthed["payload"]["channel_lease_basis"]
        self.assertEqual(basis["lease_id"], record["payload"]["channel_lease"]["lease_id"])
        lease = self.service.list_leases(self.creator, record_id=record["id"])[0]
        self.assertEqual(lease["state"], "expired")


class LeaseRenewalTest(LeaseTestBase):
    def setUp(self):
        super().setUp()
        self.claim_shift()

    def test_renew_extends_expiry(self):
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        lease_id = record["lease"]["id"]
        self.clock.advance(seconds=200)
        renewed = self.service.act(self.officer, record["id"], record["version"], "renew_channel", {"lease_id": lease_id, "ttl_seconds": 600})
        self.assertEqual(renewed["lease"]["renewed_count"], 1)
        self.assertEqual(renewed["payload"]["channel_lease"]["expires_at"], renewed["lease"]["expires_at"])
        self.clock.advance(seconds=400)
        confirmed = self.service.act(self.creator, renewed["id"], renewed["version"], "confirm", {"pilot_id": "P-01"})
        self.assertEqual(confirmed["state"], "confirmed")

    def test_late_renewal_cannot_revive_released_lease(self):
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        lease_id = record["lease"]["id"]
        record = self.service.act(self.officer, record["id"], record["version"], "release_channel", {})
        self.assertEqual(record["lease"]["state"], "released")
        with self.assertRaises(LeaseConflict):
            self.service.act(self.officer, record["id"], record["version"], "renew_channel", {"lease_id": lease_id, "ttl_seconds": 600})
        lease = self.service.list_leases(self.creator, record_id=record["id"])[0]
        self.assertEqual(lease["state"], "released")
        denied = [event for event in self.service.timeline(self.creator, record["id"]) if event["action"] == "channel_op_denied"]
        self.assertTrue(denied)

    def test_expired_lease_cannot_be_renewed(self):
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        lease_id = record["lease"]["id"]
        self.clock.advance(seconds=301)
        with self.assertRaises(LeaseConflict):
            self.service.act(self.officer, record["id"], record["version"], "renew_channel", {"lease_id": lease_id})
        lease = self.service.list_leases(self.creator, record_id=record["id"])[0]
        self.assertEqual(lease["state"], "expired")


class LeaseRecoveryTest(LeaseTestBase):
    def test_restart_identifies_old_leases_and_rejects_late_renewal(self):
        self.claim_shift()
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        lease_id = record["lease"]["id"]
        self.clock.advance(hours=2)
        restarted = build_service(self.db, clock=self.clock)
        lease = restarted.list_leases(self.creator, record_id=record["id"])[0]
        self.assertEqual(lease["state"], "expired")
        fresh = restarted.get_record(self.creator, record["id"])
        with self.assertRaises(LeaseConflict):
            restarted.act(self.officer, fresh["id"], fresh["version"], "renew_channel", {"lease_id": lease_id})
        self.assertEqual(restarted.list_leases(self.creator, record_id=record["id"])[0]["state"], "expired")
        events = restarted.system_events(self.creator)
        self.assertTrue(any(event["action"] == "lease_recovery" for event in events))
        timeline = restarted.timeline(self.creator, record["id"])
        self.assertTrue(any(event["action"] == "lease_expired" for event in timeline))

    def test_depart_and_cancel_release_lease(self):
        self.claim_shift()
        record = self.create_plan("VOY-1")
        record = self.request_lease(record)
        record = self.service.act(self.creator, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.service.act(self.creator, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        record = self.service.act(self.creator, record["id"], record["version"], "depart", {"cargo_operation_complete": True})
        lease = self.service.list_leases(self.creator, record_id=record["id"])[0]
        self.assertEqual(lease["state"], "released")
        self.assertEqual(lease["release_reason"], "departed")
        second = self.create_plan("VOY-2", PLAN_B)
        leased = self.request_lease(second)
        self.assertEqual(leased["lease"]["state"], "active")
        cancelled = self.create_plan("VOY-3", dict(PLAN, vessel="AnPing", berth="B14", channel="C2"))
        cancelled = self.service.act(self.officer, cancelled["id"], cancelled["version"], "request_channel", dict(SLOT, channel="C2"))
        cancelled = self.service.act(self.creator, cancelled["id"], cancelled["version"], "cancel", {"cancel_reason": "气象管制"})
        lease = self.service.list_leases(self.creator, record_id=cancelled["id"])[0]
        self.assertEqual(lease["state"], "released")
        self.assertEqual(lease["release_reason"], "cancelled")


class DutyShiftTest(LeaseTestBase):
    def test_no_duty_officer_rejects_lease_ops_and_audits(self):
        record = self.create_plan("VOY-1")
        with self.assertRaises(PermissionDenied):
            self.request_lease(record)
        events = self.service.system_events(self.creator)
        self.assertTrue(any(event["action"] == "channel_op_denied" for event in events))

    def test_handover_rules_and_audit(self):
        self.claim_shift("officer-a")
        with self.assertRaises(PermissionDenied):
            self.service.handover_shift(Actor("officer-b", "port_controller"), "officer-b")
        events = self.service.system_events(self.creator)
        self.assertTrue(any(event["action"] == "shift_handover_denied" for event in events))
        shift = self.service.handover_shift(self.officer, "officer-b")
        self.assertEqual(shift["shift"]["officer_id"], "officer-b")
        record = self.create_plan("VOY-1")
        with self.assertRaises(PermissionDenied):
            self.request_lease(record, actor=self.officer)
        leased = self.request_lease(record, actor=Actor("officer-b", "port_controller"))
        self.assertEqual(leased["lease"]["state"], "active")
        admin = Actor("supervisor", "admin")
        shift = self.service.handover_shift(admin, "officer-a")
        self.assertEqual(shift["shift"]["officer_id"], "officer-a")
        self.assertTrue(any(event["action"] == "shift_handover" for event in self.service.system_events(self.creator)))

    def test_stale_version_rejected_on_lease_actions(self):
        self.claim_shift()
        record = self.create_plan("VOY-1")
        self.request_lease(record)
        stale = self.service.get_record(self.creator, record["id"])
        with self.assertRaises(Conflict):
            self.service.act(self.officer, stale["id"], stale["version"] - 1, "release_channel", {})


if __name__ == "__main__":
    unittest.main()
