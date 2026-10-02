"""审计事件封装，时间线查询保持只读。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, record_id: int) -> List[Dict[str, Any]]:
        return self.repository.audit_timeline(record_id)

    def note(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.repository.add_audit(record_id, actor_id, action, details)

    def note_system(self, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        self.repository.add_system_event(action, actor_id, details)

    def system_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.repository.list_system_events(limit)
