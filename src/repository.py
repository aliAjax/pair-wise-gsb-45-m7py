"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, LeaseConflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_leases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    vessel TEXT NOT NULL,
                    slot_start_hour INTEGER NOT NULL,
                    slot_end_hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    issued_by TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    renewed_count INTEGER NOT NULL DEFAULT 0,
                    released_at TEXT,
                    release_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS channel_tokens (
                    channel TEXT PRIMARY KEY,
                    last_token INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS duty_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    officer_id TEXT NOT NULL,
                    started_by TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_by TEXT,
                    ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_leases_channel ON channel_leases(channel, state);
                CREATE INDEX IF NOT EXISTS idx_leases_record ON channel_leases(record_id, state);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], tx_hook: Optional[Callable[[sqlite3.Connection, sqlite3.Row], None]] = None, release_lease_reason: Optional[str] = None, released_at: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if release_lease_reason is not None:
                self._release_active_lease(connection, record_id, release_lease_reason, released_at or now)
            if tx_hook is not None:
                tx_hook(connection, row)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _release_active_lease(connection: sqlite3.Connection, record_id: int, reason: str, released_at: str) -> Optional[sqlite3.Row]:
        lease = connection.execute(
            "SELECT * FROM channel_leases WHERE record_id=? AND state='active' ORDER BY id DESC LIMIT 1", (record_id,)
        ).fetchone()
        if lease is None:
            return None
        connection.execute(
            "UPDATE channel_leases SET state='released', released_at=?, release_reason=?, version=version+1 WHERE id=? AND state='active'",
            (released_at, reason, int(lease["id"])),
        )
        return lease

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    @staticmethod
    def _lease_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def get_lease(self, lease_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
        return self._lease_row(row) if row is not None else None

    def get_active_lease_for_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_leases WHERE record_id=? AND state='active' ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
        return self._lease_row(row) if row is not None else None

    def list_leases(self, channel: Optional[str] = None, record_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if channel:
            clauses.append("channel=?")
            params.append(channel)
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(int(record_id))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM channel_leases %s ORDER BY id DESC LIMIT ?" % where, (*params, limit)).fetchall()
        return [self._lease_row(row) for row in rows]

    def create_lease_tx(self, record_id: int, expected_version: int, lease: Dict[str, Any], actor_id: str, details: Dict[str, Any], now: datetime, payload_builder: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        now_iso = _iso(now)
        expires_iso = _iso(now + timedelta(seconds=int(lease["ttl_seconds"])))
        channel = lease["channel"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            connection.execute(
                "UPDATE channel_leases SET state='expired', version=version+1 WHERE channel=? AND state='active' AND expires_at<=?",
                (channel, now_iso),
            )
            overlap = connection.execute(
                "SELECT id FROM channel_leases WHERE channel=? AND state='active' AND slot_start_hour<? AND slot_end_hour>? LIMIT 1",
                (channel, int(lease["slot_end_hour"]), int(lease["slot_start_hour"])),
            ).fetchone()
            if overlap is not None:
                connection.rollback()
                raise LeaseConflict("同一航道该时段已存在有效租约")
            held = connection.execute(
                "SELECT id FROM channel_leases WHERE record_id=? AND state='active' LIMIT 1", (record_id,)
            ).fetchone()
            if held is not None:
                connection.rollback()
                raise LeaseConflict("该计划已持有有效租约，请续租或先释放")
            connection.execute(
                "INSERT INTO channel_tokens(channel,last_token) VALUES(?,1) ON CONFLICT(channel) DO UPDATE SET last_token=last_token+1",
                (channel,),
            )
            token = int(connection.execute("SELECT last_token FROM channel_tokens WHERE channel=?", (channel,)).fetchone()["last_token"])
            cursor = connection.execute(
                "INSERT INTO channel_leases(record_id,channel,vessel,slot_start_hour,slot_end_hour,state,fencing_token,issued_by,issued_at,expires_at,renewed_count,version) VALUES(?,?,?,?,?,?,?,?,?,?,0,1)",
                (record_id, channel, lease["vessel"], int(lease["slot_start_hour"]), int(lease["slot_end_hour"]), "active", token, actor_id, now_iso, expires_iso),
            )
            lease_id = int(cursor.lastrowid)
            lease_row = self._lease_row(connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone())
            version = int(expected_version) + 1
            final_payload = payload_builder(lease_row)
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(final_payload, ensure_ascii=False, sort_keys=True), actor_id, now_iso, record_id),
            )
            audit_details = dict(details)
            audit_details["lease_id"] = lease_id
            audit_details["fencing_token"] = token
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "request_channel", actor_id, version, json.dumps(audit_details, ensure_ascii=False, sort_keys=True), now_iso),
            )
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        result = self._row(record_row)
        result["lease"] = lease_row
        return result

    def renew_lease_tx(self, record_id: int, expected_version: int, lease_id: int, ttl_seconds: int, actor_id: str, details: Dict[str, Any], now: datetime, payload_builder: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        now_iso = _iso(now)
        expires_iso = _iso(now + timedelta(seconds=int(ttl_seconds)))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            lease = connection.execute("SELECT * FROM channel_leases WHERE id=? AND record_id=?", (lease_id, record_id)).fetchone()
            if lease is None:
                connection.rollback()
                raise NotFound("租约不存在")
            if lease["state"] != "active":
                connection.rollback()
                raise LeaseConflict("租约已释放或已过期，晚到的续租不能将其恢复")
            if str(lease["expires_at"]) <= now_iso:
                connection.execute("UPDATE channel_leases SET state='expired', version=version+1 WHERE id=?", (lease_id,))
                connection.commit()
                raise LeaseConflict("租约已过期，无法续租，请重新核对时段后重新申请")
            connection.execute(
                "UPDATE channel_leases SET expires_at=?, renewed_count=renewed_count+1, version=version+1 WHERE id=? AND state='active'",
                (expires_iso, lease_id),
            )
            lease_row = self._lease_row(connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone())
            version = int(expected_version) + 1
            final_payload = payload_builder(lease_row)
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(final_payload, ensure_ascii=False, sort_keys=True), actor_id, now_iso, record_id),
            )
            audit_details = dict(details)
            audit_details["lease_id"] = lease_id
            audit_details["fencing_token"] = int(lease_row["fencing_token"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "renew_channel", actor_id, version, json.dumps(audit_details, ensure_ascii=False, sort_keys=True), now_iso),
            )
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        result = self._row(record_row)
        result["lease"] = lease_row
        return result

    def release_lease_tx(self, record_id: int, expected_version: int, actor_id: str, details: Dict[str, Any], now: datetime, payload_builder: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        now_iso = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            lease = self._release_active_lease(connection, record_id, "manual_release", now_iso)
            if lease is None:
                connection.rollback()
                raise LeaseConflict("当前没有可释放的有效租约")
            lease_row = self._lease_row(connection.execute("SELECT * FROM channel_leases WHERE id=?", (int(lease["id"]),)).fetchone())
            version = int(expected_version) + 1
            final_payload = payload_builder(lease_row)
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(final_payload, ensure_ascii=False, sort_keys=True), actor_id, now_iso, record_id),
            )
            audit_details = dict(details)
            audit_details["lease_id"] = int(lease_row["id"])
            audit_details["fencing_token"] = int(lease_row["fencing_token"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "release_channel", actor_id, version, json.dumps(audit_details, ensure_ascii=False, sort_keys=True), now_iso),
            )
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        result = self._row(record_row)
        result["lease"] = lease_row
        return result

    def recover_leases(self, now: datetime) -> List[Dict[str, Any]]:
        now_iso = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM channel_leases WHERE state='active' AND expires_at<=?", (now_iso,)).fetchall()
            if rows:
                connection.execute(
                    "UPDATE channel_leases SET state='expired', version=version+1 WHERE state='active' AND expires_at<=?", (now_iso,)
                )
            connection.commit()
        return [self._lease_row(row) for row in rows]

    def current_shift(self) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM duty_shifts WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def handover_shift(self, actor_id: str, new_officer_id: str, now: datetime) -> Dict[str, Any]:
        now_iso = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM duty_shifts WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if current is not None:
                connection.execute("UPDATE duty_shifts SET ended_at=?, ended_by=? WHERE id=?", (now_iso, actor_id, int(current["id"])))
            cursor = connection.execute(
                "INSERT INTO duty_shifts(officer_id,started_by,started_at) VALUES(?,?,?)", (new_officer_id, actor_id, now_iso)
            )
            row = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (int(cursor.lastrowid),)).fetchone()
            connection.commit()
        return dict(row)

    def add_system_event(self, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO system_events(action,actor_id,details,created_at) VALUES(?,?,?,?)",
                (action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def list_system_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
