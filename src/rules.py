"""港口泊位与航道调度领域规则与状态转换。"""
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, LeaseConflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}, 'request_channel': {'port_controller'}, 'renew_channel': {'port_controller'}, 'release_channel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}

LEASE_ACTIONS = ('request_channel', 'renew_channel', 'release_channel')
LEASE_REQUEST_STATES = {'draft', 'confirmed'}
LEASE_ACTIVE = 'active'
LEASE_RELEASED = 'released'
LEASE_EXPIRED = 'expired'
LEASE_TERMINAL_STATES = {LEASE_RELEASED, LEASE_EXPIRED}
DEFAULT_LEASE_TTL_SECONDS = 300
MIN_LEASE_TTL_SECONDS = 10
MAX_LEASE_TTL_SECONDS = 7200


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def validate_lease_request(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] not in LEASE_REQUEST_STATES:
            raise Conflict("当前状态不允许申请航道租约")
        payload = record["payload"]
        channel = text(data, "channel")
        planned = payload.get("channel")
        if planned and planned != channel:
            raise ValidationError("申请航道与靠泊计划不一致")
        slot_start = integer(data, "slot_start_hour", 0, 23)
        slot_end = integer(data, "slot_end_hour", 1, 24)
        if slot_end <= slot_start:
            raise ValidationError("slot_end_hour必须晚于slot_start_hour")
        if slot_start > int(payload["eta_hour"]) or slot_end < int(payload["etd_hour"]):
            raise ValidationError("申请时段必须覆盖靠泊窗口")
        ttl = data.get("ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
        ttl = integer({"ttl_seconds": ttl}, "ttl_seconds", MIN_LEASE_TTL_SECONDS, MAX_LEASE_TTL_SECONDS)
        return {"channel": channel, "slot_start_hour": slot_start, "slot_end_hour": slot_end, "ttl_seconds": ttl}

    def validate_lease_renew(self, data: Dict[str, Any]) -> Dict[str, Any]:
        lease_id = integer(data, "lease_id", 1)
        ttl = data.get("ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
        ttl = integer({"ttl_seconds": ttl}, "ttl_seconds", MIN_LEASE_TTL_SECONDS, MAX_LEASE_TTL_SECONDS)
        return {"lease_id": lease_id, "ttl_seconds": ttl}

    @staticmethod
    def slots_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
        return int(a_start) < int(b_end) and int(a_end) > int(b_start)

    def lease_effective_state(self, lease: Dict[str, Any], now: datetime) -> str:
        if lease["state"] == LEASE_ACTIVE and _parse_instant(lease["expires_at"]) <= now:
            return LEASE_EXPIRED
        return lease["state"]

    def require_lease_usable(self, lease: Optional[Dict[str, Any]], record: Dict[str, Any], now: datetime) -> None:
        if lease is None:
            raise LeaseConflict("缺少有效航道租约，请先申请")
        if lease["record_id"] != record["id"]:
            raise LeaseConflict("租约不属于该靠泊计划")
        state = self.lease_effective_state(lease, now)
        if state == LEASE_RELEASED:
            raise LeaseConflict("航道租约已释放，请重新申请")
        if state == LEASE_EXPIRED:
            raise LeaseConflict("航道租约已过期，请重新核对时段后重新申请")
        payload = record["payload"]
        if lease["channel"] != payload.get("channel"):
            raise LeaseConflict("租约航道与靠泊计划不一致")
        if int(lease["slot_start_hour"]) > int(payload["eta_hour"]) or int(lease["slot_end_hour"]) < int(payload["etd_hour"]):
            raise LeaseConflict("租约时段不覆盖靠泊窗口，请重新核对时段")

    def build_lease_basis(self, lease: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
        return {
            "lease_id": int(lease["id"]),
            "fencing_token": int(lease["fencing_token"]),
            "channel": lease["channel"],
            "slot_start_hour": int(lease["slot_start_hour"]),
            "slot_end_hour": int(lease["slot_end_hour"]),
            "expires_at": lease["expires_at"],
            "confirmed_at": now_iso,
        }


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value)
