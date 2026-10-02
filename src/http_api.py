"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
LEASE_RE = re.compile(r"^/api/leases/(\d+)$")
LEASE_ACTION_RE = re.compile(r"^/api/leases/(\d+)/(renew|release|audit)$")
SHIFT_RE = re.compile(r"^/api/shifts/(\d+)/close$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "port-berth/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _shift_id(self, body: Dict[str, Any]):
            raw = self.headers.get("X-Shift-Id")
            if raw is None or raw.strip() == "":
                raw = body.get("shift_id") if isinstance(body, dict) else None
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise ValidationError("X-Shift-Id必须是整数")

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "port-berth", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                if parsed.path == "/api/leases":
                    query = parse_qs(parsed.query)
                    leases = service.list_leases(
                        self._actor(),
                        state=query.get("state", [None])[0],
                        channel=query.get("channel", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    )
                    self._send(200, {"items": leases})
                    return
                if parsed.path == "/api/shifts/active":
                    self._send(200, {"shift": service.active_shift(self._actor())})
                    return
                if parsed.path == "/api/denials":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.denials(self._actor(), limit=int(query.get("limit", ["100"])[0]))})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                match = LEASE_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_lease(self._actor(), int(match.group(1))))
                    return
                match = LEASE_ACTION_RE.match(parsed.path)
                if match and match.group(2) == "audit":
                    self._send(200, {"items": service.lease_timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}), self._shift_id(body))
                    self._send(201, record)
                    return
                if parsed.path == "/api/leases":
                    lease = service.acquire_lease(self._actor(), body.get("data", {}), self._shift_id(body), body.get("lease_key", ""))
                    self._send(201, lease)
                    return
                if parsed.path == "/api/shifts":
                    shift = service.open_shift(self._actor(), body.get("note", ""))
                    self._send(201, shift)
                    return
                match = SHIFT_RE.match(parsed.path)
                if match:
                    self._send(200, service.close_shift(self._actor(), int(match.group(1))))
                    return
                match = LEASE_ACTION_RE.match(parsed.path)
                if match:
                    lease_id = int(match.group(1))
                    action = match.group(2)
                    if action == "renew":
                        version = body.get("lease_version")
                        end_hour = body.get("end_hour")
                        if not isinstance(version, int):
                            raise ValidationError("lease_version必须是整数")
                        if not isinstance(end_hour, int):
                            raise ValidationError("end_hour必须是整数")
                        lease = service.renew_lease(self._actor(), lease_id, version, end_hour, self._shift_id(body))
                    else:
                        version = body.get("lease_version")
                        if not isinstance(version, int):
                            raise ValidationError("lease_version必须是整数")
                        lease = service.release_lease(self._actor(), lease_id, version, self._shift_id(body), body.get("reason", "manual_release"))
                    self._send(200, lease)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}), self._shift_id(body))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
