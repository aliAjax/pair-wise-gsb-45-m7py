"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, LeaseConflict, PermissionDenied, text
from .repository import Repository, _iso
from .rules import LEASE_ACTIONS, DomainRules


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, clock=None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.clock = clock or _utcnow

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_duty_officer(self, actor: Actor, record_id: int, action: str) -> None:
        if actor.role == "admin":
            return
        shift = self.repository.current_shift()
        if shift is None:
            self._audit_denial(record_id, actor, action, "当前无值班员，请先接班")
            raise PermissionDenied("当前无值班员，请先接班")
        if shift["officer_id"] != actor.user_id:
            self._audit_denial(record_id, actor, action, "仅当班值班员可执行航道租约操作")
            raise PermissionDenied("仅当班值班员可执行航道租约操作")

    def _audit_denial(self, record_id: int, actor: Actor, action: str, reason: str) -> None:
        details = {"action": action, "reason": reason, "actor": actor.user_id}
        try:
            self.audit.note(record_id, actor.user_id, "channel_op_denied", details)
        except Exception:
            pass
        self.audit.note_system("channel_op_denied", actor.user_id, dict(details, record_id=record_id))

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action in LEASE_ACTIONS:
            return self._act_lease(actor, record_id, int(expected_version), action, data or {})
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        now = self.clock()
        tx_hook = None
        release_reason = None
        if action == "confirm" and record["payload"].get("channel"):
            lease = self._record_lease(record)
            try:
                self.rules.require_lease_usable(lease, record, now)
            except LeaseConflict as exc:
                self._audit_denial(record_id, actor, action, str(exc))
                raise
            new_payload["channel_lease_basis"] = self.rules.build_lease_basis(lease, _iso(now))
            tx_hook = self._lease_guard(record, int(lease["id"]), now)
        if action in ("depart", "cancel"):
            active = self.repository.get_active_lease_for_record(record_id)
            if active is not None:
                release_reason = "departed" if action == "depart" else "cancelled"
                lease_summary = dict(new_payload.get("channel_lease") or {})
                if lease_summary:
                    lease_summary["state"] = "released"
                    new_payload["channel_lease"] = lease_summary
        try:
            return self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
                tx_hook=tx_hook,
                release_lease_reason=release_reason,
                released_at=_iso(now),
            )
        except LeaseConflict as exc:
            self._audit_denial(record_id, actor, action, str(exc))
            raise

    def _record_lease(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        summary = record["payload"].get("channel_lease") or {}
        lease_id = summary.get("lease_id")
        if lease_id:
            lease = self.repository.get_lease(int(lease_id))
            if lease is not None:
                return lease
        return self.repository.get_active_lease_for_record(int(record["id"]))

    def _lease_guard(self, record: Dict[str, Any], lease_id: int, now: datetime):
        def guard(connection, _row) -> None:
            row = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
            lease = dict(row) if row is not None else None
            self.rules.require_lease_usable(lease, record, now)

        return guard

    def _act_lease(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        self._require_duty_officer(actor, record_id, action)
        now = self.clock()
        try:
            if action == "request_channel":
                return self._request_channel(actor, record, expected_version, data, now)
            if action == "renew_channel":
                return self._renew_channel(actor, record, expected_version, data, now)
            return self._release_channel(actor, record, expected_version, now)
        except LeaseConflict as exc:
            self._audit_denial(record_id, actor, action, str(exc))
            raise

    def _lease_summary(self, lease: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "lease_id": int(lease["id"]),
            "fencing_token": int(lease["fencing_token"]),
            "channel": lease["channel"],
            "slot_start_hour": int(lease["slot_start_hour"]),
            "slot_end_hour": int(lease["slot_end_hour"]),
            "issued_at": lease["issued_at"],
            "expires_at": lease["expires_at"],
            "renewed_count": int(lease["renewed_count"]),
            "state": lease["state"],
        }

    def _request_channel(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        request = self.rules.validate_lease_request(record, data)
        base_payload = dict(record["payload"])
        base_payload["channel"] = request["channel"]
        lease_fields = dict(request)
        lease_fields["vessel"] = base_payload["vessel"]

        def build_payload(lease: Dict[str, Any]) -> Dict[str, Any]:
            payload = dict(base_payload)
            payload["channel_lease"] = self._lease_summary(lease)
            return payload

        details = {"summary": "航道租约已签发", "input": data}
        return self.repository.create_lease_tx(
            record_id=int(record["id"]),
            expected_version=expected_version,
            lease=lease_fields,
            actor_id=actor.user_id,
            details=details,
            now=now,
            payload_builder=build_payload,
        )

    def _renew_channel(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        request = self.rules.validate_lease_renew(data)
        base_payload = dict(record["payload"])

        def build_payload(lease: Dict[str, Any]) -> Dict[str, Any]:
            payload = dict(base_payload)
            payload["channel_lease"] = self._lease_summary(lease)
            return payload

        details = {"summary": "航道租约已续租", "input": data}
        return self.repository.renew_lease_tx(
            record_id=int(record["id"]),
            expected_version=expected_version,
            lease_id=int(request["lease_id"]),
            ttl_seconds=int(request["ttl_seconds"]),
            actor_id=actor.user_id,
            details=details,
            now=now,
            payload_builder=build_payload,
        )

    def _release_channel(self, actor: Actor, record: Dict[str, Any], expected_version: int, now: datetime) -> Dict[str, Any]:
        base_payload = dict(record["payload"])

        def build_payload(lease: Dict[str, Any]) -> Dict[str, Any]:
            payload = dict(base_payload)
            payload["channel_lease"] = self._lease_summary(lease)
            return payload

        details = {"summary": "航道租约已释放"}
        return self.repository.release_lease_tx(
            record_id=int(record["id"]),
            expected_version=expected_version,
            actor_id=actor.user_id,
            details=details,
            now=now,
            payload_builder=build_payload,
        )

    def list_leases(self, actor: Actor, channel: Optional[str] = None, record_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now = self.clock()
        leases = self.repository.list_leases(channel=channel, record_id=record_id, limit=limit)
        for lease in leases:
            lease["effective_state"] = self.rules.lease_effective_state(lease, now)
        return leases

    def current_shift(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        shift = self.repository.current_shift()
        return {"shift": shift}

    def handover_shift(self, actor: Actor, new_officer_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "request_channel"):
            self.audit.note_system("shift_handover_denied", actor.user_id, {"reason": "角色无权换班", "new_officer": new_officer_id})
            raise PermissionDenied("角色无权换班")
        new_officer_id = text({"officer": new_officer_id}, "officer")
        current = self.repository.current_shift()
        if current is not None and current["officer_id"] != actor.user_id and actor.role != "admin":
            self.audit.note_system(
                "shift_handover_denied",
                actor.user_id,
                {"reason": "只有当前值班员或管理员可以交班", "current_officer": current["officer_id"], "new_officer": new_officer_id},
            )
            raise PermissionDenied("只有当前值班员或管理员可以交班")
        shift = self.repository.handover_shift(actor.user_id, new_officer_id, self.clock())
        self.audit.note_system(
            "shift_handover",
            actor.user_id,
            {"from": current["officer_id"] if current else None, "to": new_officer_id},
        )
        return {"shift": shift}

    def system_events(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.system_events(limit)

    def recover_expired_leases(self) -> List[Dict[str, Any]]:
        expired = self.repository.recover_leases(self.clock())
        for lease in expired:
            try:
                self.audit.note(
                    int(lease["record_id"]),
                    "system",
                    "lease_expired",
                    {"lease_id": int(lease["id"]), "fencing_token": int(lease["fencing_token"]), "channel": lease["channel"], "reason": "recovery_sweep"},
                )
            except Exception:
                pass
        if expired:
            self.audit.note_system(
                "lease_recovery",
                "system",
                {"expired_lease_ids": [int(lease["id"]) for lease in expired], "count": len(expired)},
            )
        return expired

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
