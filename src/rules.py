"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, LeaseExpired, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
LEASE_ROLES = {'port_controller'}
SHIFT_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}

LEASE_STATES = ('active', 'released', 'expired')
TERMINAL_LEASE_STATES = ('released', 'expired')


def windows_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """半开区间[start, end)重叠判定。"""
    return start_a < end_b and start_b < end_a


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    LEASE_STATES = LEASE_STATES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(LEASE_ROLES) | set(SHIFT_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_lease(self, role: str) -> bool:
        return role == "admin" or role in LEASE_ROLES

    def role_can_shift(self, role: str) -> bool:
        return role == "admin" or role in SHIFT_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        text(p, "channel")
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

    def validate_lease(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "vessel")
        text(p, "channel")
        start = integer(p, "start_hour", 0, 24)
        end = integer(p, "end_hour", 1, 24)
        if end <= start:
            raise ValidationError("end_hour必须晚于start_hour")
        return {"vessel": p["vessel"].strip(), "channel": p["channel"].strip(), "start_hour": start, "end_hour": end}

    def lease_covers_plan(self, lease: Dict[str, Any], plan: Dict[str, Any]) -> bool:
        p = plan["payload"]
        return (
            lease["state"] == "active"
            and lease["vessel"] == p.get("vessel")
            and lease["channel"] == p.get("channel")
            and int(lease["start_hour"]) <= int(p["eta_hour"])
            and int(lease["end_hour"]) >= int(p["etd_hour"])
        )

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

    def pilot_from_data(self, data: Dict[str, Any]) -> str:
        return text(data or {}, "pilot_id")

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = self.pilot_from_data(data)
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            if "lease_basis" in p:
                changes["lease_basis"] = p["lease_basis"]
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
