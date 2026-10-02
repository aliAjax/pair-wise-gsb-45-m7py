"""业务用例编排、权限检查、值班班次、航道租约与审计。"""
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ShiftClosed, ValidationError, text
from .repository import Repository
from .rules import DomainRules


def default_clock_hour() -> int:
    """当前UTC小时（0-23）。测试可注入固定时钟。"""
    return datetime.now(timezone.utc).hour


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, clock: Callable[[], int] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.clock = clock or default_clock_hour
        # 崩溃恢复：重启先把所有旧租约落定为expired并审计，再对外服务
        self.recovery = {"expired_lease_ids": self.repository.sweep_expired_leases(self.clock())}

    def now_hour(self) -> int:
        return int(self.clock())

    # ---------- 身份与权限闸门 ----------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _deny(self, actor: Optional[Actor], action: str, reason: str, details: Dict[str, Any] = None, shift_id: Optional[int] = None) -> None:
        actor_id = actor.user_id if actor and actor.user_id else "anonymous"
        extra = {"actor_role": actor.role if actor and actor.role else "unknown"}
        extra.update(details or {})
        self.repository.add_denial(actor_id, action, reason, extra, shift_id=shift_id)

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            self._deny(actor, "access", "未知角色或无权访问该服务")
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, action: str, allowed: Callable[[str], bool]) -> None:
        if not allowed(actor.role):
            self._deny(actor, action, "角色无权执行该操作")
            raise PermissionDenied("角色无权执行该操作")

    def _require_shift(self, actor: Actor, action: str, shift_id: Any, require_role: Callable[[str], bool] = None) -> Dict[str, Any]:
        """所有写操作必须落在当前开放班次内；换班后的旧班次或越权一律拒绝并审计。"""
        if require_role is not None and not require_role(actor.role):
            self._deny(actor, action, "角色无权执行该操作", {"shift_id": shift_id})
            raise PermissionDenied("角色无权执行该操作")
        if not isinstance(shift_id, int) or isinstance(shift_id, bool):
            self._deny(actor, action, "缺少有效班次(X-Shift-Id)", {"shift_id": shift_id})
            raise PermissionDenied("缺少有效班次(X-Shift-Id)")
        shift = self.repository.get_active_shift()
        if shift is None or int(shift["id"]) != int(shift_id):
            self._deny(actor, action, "班次已换班或不存在", {"shift_id": shift_id, "active_shift_id": shift["id"] if shift else None})
            raise ShiftClosed("当前值班班次已结束或不存在，请使用新班次")
        return shift

    # ---------- 值班班次 ----------

    def open_shift(self, actor: Actor, note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, "open_shift", self.rules.role_can_shift)
        shift = self.repository.open_shift(actor.user_id, note.strip() if isinstance(note, str) else "")
        return shift

    def close_shift(self, actor: Actor, shift_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, "close_shift", self.rules.role_can_shift)
        if not isinstance(shift_id, int) or isinstance(shift_id, bool):
            self._deny(actor, "close_shift", "缺少有效班次", {"shift_id": shift_id})
            raise PermissionDenied("缺少有效班次")
        shift = self.repository.get_shift(shift_id)
        if shift["status"] != "open" or self.repository.get_active_shift() is None or int(self.repository.get_active_shift()["id"]) != int(shift_id):
            self._deny(actor, "close_shift", "班次已换班", {"shift_id": shift_id})
            raise ShiftClosed("该班次已结束")
        return self.repository.close_shift(shift_id, actor.user_id)

    def active_shift(self, actor: Actor) -> Optional[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_active_shift()

    # ---------- 靠泊计划 ----------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any], shift_id: Any = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        shift = self._require_shift(actor, "create", shift_id, self.rules.role_can_create)
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, shift_id=int(shift["id"]))

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any], shift_id: Any = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        shift = self._require_shift(actor, action, shift_id, lambda role: self.rules.role_can_action(role, action))
        record = self.repository.get(record_id)

        if action == "confirm":
            # 靠泊确认必须绑定航道通行租约：核对状态/版本/船舶/航道/时段，过期则拒绝
            data = data or {}
            lease_id = data.get("lease_id")
            lease_version = data.get("lease_version")
            pilot_id = self.rules.pilot_from_data(data)
            if not isinstance(lease_id, int) or isinstance(lease_id, bool):
                self._deny(actor, "confirm", "靠泊确认缺少lease_id", {"record_id": record_id}, shift_id=int(shift["id"]))
                raise ValidationError("confirm必须提供整数lease_id")
            if not isinstance(lease_version, int) or isinstance(lease_version, bool):
                self._deny(actor, "confirm", "靠泊确认缺少lease_version", {"record_id": record_id}, shift_id=int(shift["id"]))
                raise ValidationError("confirm必须提供整数lease_version")
            new_state, new_payload, _summary = self.rules.apply_action(record, "confirm", {"pilot_id": pilot_id})
            return self.repository.confirm_with_lease(
                record_id=record_id,
                expected_version=int(expected_version),
                lease_id=int(lease_id),
                lease_version=int(lease_version),
                new_payload=new_payload,
                pilot_id=pilot_id,
                actor_id=actor.user_id,
                current_hour=self.now_hour(),
                shift_id=int(shift["id"]),
            )

        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            shift_id=int(shift["id"]),
        )

    # ---------- 航道租约 ----------

    def acquire_lease(self, actor: Actor, payload: Dict[str, Any], shift_id: Any = None, lease_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        shift = self._require_shift(actor, "acquire_lease", shift_id, self.rules.role_can_lease)
        lease_input = self.rules.validate_lease(payload or {})
        lease_key = (lease_key or "").strip() or "%(vessel)s|%(channel)s|%(start)02d-%(end)02d" % {
            "vessel": lease_input["vessel"], "channel": lease_input["channel"], "start": lease_input["start_hour"], "end": lease_input["end_hour"],
        }
        record_ref = (payload or {}).get("record_id")
        record_id = None
        if record_ref is not None:
            if not isinstance(record_ref, int) or isinstance(record_ref, bool):
                raise ValidationError("record_id必须是整数")
            plan = self.repository.get(record_ref)
            plan_payload = plan["payload"]
            if plan_payload.get("vessel") != lease_input["vessel"] or plan_payload.get("channel") != lease_input["channel"]:
                raise Conflict("申请的船舶/航道与靠泊计划不一致")
            record_id = record_ref
        return self.repository.acquire_lease(
            lease_key=lease_key,
            vessel=lease_input["vessel"],
            channel=lease_input["channel"],
            start_hour=lease_input["start_hour"],
            end_hour=lease_input["end_hour"],
            actor_id=actor.user_id,
            current_hour=self.now_hour(),
            shift_id=int(shift["id"]),
            record_id=record_id,
        )

    def renew_lease(self, actor: Actor, lease_id: int, expected_version: int, new_end_hour: int, shift_id: Any = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        shift = self._require_shift(actor, "renew_lease", shift_id, self.rules.role_can_lease)
        if not isinstance(lease_id, int) or isinstance(lease_id, bool):
            raise ValidationError("lease_id必须是整数")
        new_end = new_end_hour
        if isinstance(new_end, bool) or not isinstance(new_end, int):
            raise ValidationError("end_hour必须是整数")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("lease_version必须是整数")
        return self.repository.renew_lease(
            lease_id=lease_id,
            expected_version=expected_version,
            new_end_hour=new_end,
            actor_id=actor.user_id,
            current_hour=self.now_hour(),
            shift_id=int(shift["id"]),
        )

    def release_lease(self, actor: Actor, lease_id: int, expected_version: int, shift_id: Any = None, reason: str = "manual_release") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        shift = self._require_shift(actor, "release_lease", shift_id, self.rules.role_can_lease)
        if not isinstance(lease_id, int) or isinstance(lease_id, bool):
            raise ValidationError("lease_id必须是整数")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("lease_version必须是整数")
        return self.repository.release_lease(
            lease_id=lease_id,
            expected_version=expected_version,
            actor_id=actor.user_id,
            current_hour=self.now_hour(),
            shift_id=int(shift["id"]),
            reason=reason,
        )

    def get_lease(self, actor: Actor, lease_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_lease(lease_id)

    def list_leases(self, actor: Actor, state: Optional[str] = None, channel: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_leases(state=state, channel=channel, limit=limit)

    def lease_timeline(self, actor: Actor, lease_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.lease_audit_timeline(lease_id)

    # ---------- 审计 ----------

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def denials(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_denials(limit=limit)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
