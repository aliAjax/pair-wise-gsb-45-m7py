"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, LeaseExpired, NotFound
from .rules import windows_overlap


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    shift_id INTEGER,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_leases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lease_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    vessel TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    fence_epoch INTEGER NOT NULL,
                    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
                    shift_id INTEGER,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS duty_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    opened_by TEXT NOT NULL,
                    closed_by TEXT NOT NULL DEFAULT '',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'business',
                    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
                    lease_id INTEGER REFERENCES channel_leases(id) ON DELETE CASCADE,
                    shift_id INTEGER REFERENCES duty_shifts(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_lease ON audit_events(lease_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_events(kind, id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lease_per_channel
                    ON channel_leases(channel) WHERE state = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lease_key
                    ON channel_leases(lease_key) WHERE state = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_shift_single_open
                    ON duty_shifts(status) WHERE status = 'open';
                """
            )
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
        if "shift_id" not in existing:
            connection.execute("ALTER TABLE records ADD COLUMN shift_id INTEGER")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _lease_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _add_event(
        self,
        connection: sqlite3.Connection,
        action: str,
        actor_id: str,
        details: Dict[str, Any],
        kind: str = "business",
        version: Optional[int] = None,
        record_id: Optional[int] = None,
        lease_id: Optional[int] = None,
        shift_id: Optional[int] = None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(kind,record_id,lease_id,shift_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, record_id, lease_id, shift_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def add_denial(self, actor_id: str, action: str, reason: str, details: Dict[str, Any], shift_id: Optional[int] = None) -> None:
        payload = {"reason": reason, "attempted": action}
        payload.update(details or {})
        try:
            with self._connect() as connection:
                self._add_event(connection, action, actor_id, payload, kind="denial", shift_id=shift_id)
        except sqlite3.Error:
            # 审计失败不应掩盖原始拒绝
            pass

    # ---------- 靠泊计划 ----------

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, shift_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,shift_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), shift_id, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                self._add_event(connection, "created", actor_id, {"state": state}, version=1, record_id=record_id, shift_id=shift_id)
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], shift_id: Optional[int] = None) -> Dict[str, Any]:
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
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,shift_id=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), shift_id, actor_id, now, record_id),
            )
            details = dict(details)
            details["shift_id"] = shift_id
            self._add_event(connection, action, actor_id, details, version=version, record_id=record_id, shift_id=shift_id)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---------- 租约：过期清扫（崩溃恢复与事务内复用） ----------

    @staticmethod
    def _sweep_expired(connection: sqlite3.Connection, current_hour: int, actor_id: str = "system") -> List[int]:
        rows = connection.execute(
            "SELECT id, channel, vessel, fence_epoch FROM channel_leases WHERE state='active' AND end_hour<=?",
            (current_hour,),
        ).fetchall()
        swept = []
        for row in rows:
            connection.execute(
                "UPDATE channel_leases SET state='expired',updated_by=?,updated_at=? WHERE id=? AND state='active'",
                (actor_id, _now(), row["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(kind,lease_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    "business",
                    row["id"],
                    "lease_expired",
                    actor_id,
                    None,
                    json.dumps(
                        {"from": "active", "to": "expired", "reason": "expiry_sweep", "at_hour": current_hour, "fence_epoch": row["fence_epoch"], "channel": row["channel"], "vessel": row["vessel"]},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    _now(),
                ),
            )
            swept.append(int(row["id"]))
        return swept

    def sweep_expired_leases(self, current_hour: int) -> List[int]:
        """服务启动（崩溃恢复）时调用：识别并落定所有旧租约。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            swept = self._sweep_expired(connection, current_hour)
            connection.commit()
        return swept

    # ---------- 租约：抢占申请 / 续租 / 释放 ----------

    def acquire_lease(
        self,
        lease_key: str,
        vessel: str,
        channel: str,
        start_hour: int,
        end_hour: int,
        actor_id: str,
        current_hour: int,
        shift_id: Optional[int] = None,
        record_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired(connection, current_hour, actor_id)
            rows = connection.execute(
                "SELECT * FROM channel_leases WHERE state='active' AND (channel=? OR vessel=?)",
                (channel, vessel),
            ).fetchall()
            for row in rows:
                if not windows_overlap(start_hour, end_hour, int(row["start_hour"]), int(row["end_hour"])):
                    continue
                if row["channel"] == channel:
                    connection.rollback()
                    raise Conflict("航道%s在该时段已有有效租约（lease_id=%s）" % (channel, row["id"]))
                connection.rollback()
                raise Conflict("船舶%s在该时段已有航道租约（lease_id=%s）" % (vessel, row["id"]))
            epoch_row = connection.execute("SELECT COALESCE(MAX(fence_epoch),0)+1 AS next_epoch FROM channel_leases WHERE channel=?", (channel,)).fetchone()
            epoch = int(epoch_row["next_epoch"])
            try:
                cursor = connection.execute(
                    "INSERT INTO channel_leases(lease_key,channel,vessel,start_hour,end_hour,state,version,fence_epoch,record_id,shift_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lease_key, channel, vessel, start_hour, end_hour, "active", 1, epoch, record_id, shift_id, actor_id, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("航道%s只能存在一份有效租约" % channel) from exc
            lease_id = int(cursor.lastrowid)
            self._add_event(
                connection,
                "lease_acquired",
                actor_id,
                {"channel": channel, "vessel": vessel, "start_hour": start_hour, "end_hour": end_hour, "fence_epoch": epoch},
                version=1,
                lease_id=lease_id,
                record_id=record_id,
                shift_id=shift_id,
            )
            result = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
            connection.commit()
        return self._lease_row(result)

    def _load_lease(self, connection: sqlite3.Connection, lease_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise NotFound("租约不存在")
        return row

    def renew_lease(
        self,
        lease_id: int,
        expected_version: int,
        new_end_hour: int,
        actor_id: str,
        current_hour: int,
        shift_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_lease(connection, lease_id)
            if row["state"] == "released":
                connection.rollback()
                raise Conflict("租约已释放，晚到的续租不能重新生效，请重新申请")
            if row["state"] == "expired":
                connection.rollback()
                raise LeaseExpired("租约已过期，无法续租，请重新申请")
            self._sweep_expired(connection, current_hour, actor_id)
            row = self._load_lease(connection, lease_id)
            if row["state"] == "expired":
                connection.rollback()
                raise LeaseExpired("租约已过期，无法续租，请重新申请")
            if row["state"] == "released":
                connection.rollback()
                raise Conflict("租约已释放，晚到的续租不能重新生效，请重新申请")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("租约版本冲突，续租请求已过期")
            if new_end_hour <= int(row["end_hour"]):
                connection.rollback()
                raise Conflict("续租结束时间必须晚于当前结束时间")
            if new_end_hour > 24:
                connection.rollback()
                raise Conflict("续租结束时间超出当日调度范围")
            # 续租延段仍需抢占：同一航道/同一船舶的其他有效租约不得重叠
            rows = connection.execute(
                "SELECT * FROM channel_leases WHERE state='active' AND id<>? AND (channel=? OR vessel=?)",
                (lease_id, row["channel"], row["vessel"]),
            ).fetchall()
            for other in rows:
                if windows_overlap(int(row["start_hour"]), new_end_hour, int(other["start_hour"]), int(other["end_hour"])):
                    connection.rollback()
                    which = "航道" if other["channel"] == row["channel"] else "船舶"
                    raise Conflict("续租时段与%s已有租约(lease_id=%s)冲突" % (which, other["id"]))
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE channel_leases SET end_hour=?,version=?,shift_id=?,updated_by=?,updated_at=? WHERE id=?",
                (new_end_hour, version, shift_id, actor_id, _now(), lease_id),
            )
            self._add_event(
                connection,
                "lease_renewed",
                actor_id,
                {"from_end_hour": row["end_hour"], "to_end_hour": new_end_hour, "fence_epoch": row["fence_epoch"]},
                version=version,
                lease_id=lease_id,
                shift_id=shift_id,
            )
            result = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
            connection.commit()
        return self._lease_row(result)

    def release_lease(
        self,
        lease_id: int,
        expected_version: int,
        actor_id: str,
        current_hour: int,
        shift_id: Optional[int] = None,
        reason: str = "manual_release",
    ) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired(connection, current_hour, actor_id)
            row = self._load_lease(connection, lease_id)
            if row["state"] == "released":
                connection.rollback()
                raise Conflict("租约已释放，无法重复释放")
            if row["state"] == "expired":
                connection.rollback()
                raise LeaseExpired("租约已过期，无法释放")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("租约版本冲突，释放请求已过期")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE channel_leases SET state='released',version=?,shift_id=?,updated_by=?,updated_at=? WHERE id=?",
                (version, shift_id, actor_id, _now(), lease_id),
            )
            self._add_event(
                connection,
                "lease_released",
                actor_id,
                {"from": "active", "to": "released", "reason": reason, "fence_epoch": row["fence_epoch"]},
                version=version,
                lease_id=lease_id,
                shift_id=shift_id,
            )
            result = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
            connection.commit()
        return self._lease_row(result)

    def get_lease(self, lease_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise NotFound("租约不存在")
        return self._lease_row(row)

    def list_leases(self, state: Optional[str] = None, channel: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if channel:
            clauses.append("channel=?")
            params.append(channel)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM channel_leases%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._lease_row(row) for row in rows]

    # ---------- 靠泊确认：租约核对与状态推进在同一事务内原子完成 ----------

    def confirm_with_lease(
        self,
        record_id: int,
        expected_version: int,
        lease_id: int,
        lease_version: int,
        new_payload: Dict[str, Any],
        pilot_id: str,
        actor_id: str,
        current_hour: int,
        shift_id: int,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 崩溃恢复口径：事务内先落定过期租约，再核对
            self._sweep_expired(connection, current_hour, actor_id)
            lease = connection.execute("SELECT * FROM channel_leases WHERE id=?", (lease_id,)).fetchone()
            if lease is None:
                connection.rollback()
                raise NotFound("租约不存在")
            if lease["state"] == "expired" or (lease["state"] == "active" and int(lease["end_hour"]) <= current_hour):
                connection.rollback()
                raise LeaseExpired("航道租约已过期，靠泊确认被拒绝，请重新核对时段并申请新租约")
            if lease["state"] == "released":
                connection.rollback()
                raise Conflict("航道租约已释放，靠泊确认被拒绝，请重新核对时段")
            if int(lease["version"]) != int(lease_version):
                connection.rollback()
                raise Conflict("租约版本已变化，请刷新后重新核对")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if record["state"] != "draft":
                connection.rollback()
                raise Conflict("当前状态不允许执行confirm")
            plan_payload = json.loads(record["payload"])
            if lease["vessel"] != plan_payload.get("vessel") or lease["channel"] != plan_payload.get("channel"):
                connection.rollback()
                raise Conflict("租约船舶/航道与靠泊计划不一致，请重新核对")
            if int(lease["start_hour"]) > int(plan_payload["eta_hour"]) or int(lease["end_hour"]) < int(plan_payload["etd_hour"]):
                connection.rollback()
                raise LeaseExpired("租约时段未覆盖靠泊计划时段，请重新核对时段并申请新租约")
            version = int(expected_version) + 1
            payload = dict(new_payload)
            payload["lease_id"] = lease_id
            payload["lease_fence_epoch"] = int(lease["fence_epoch"])
            payload["lease_basis"] = {
                "lease_id": lease_id,
                "lease_version": int(lease_version),
                "fence_epoch": int(lease["fence_epoch"]),
                "channel": lease["channel"],
                "vessel": lease["vessel"],
                "start_hour": int(lease["start_hour"]),
                "end_hour": int(lease["end_hour"]),
                "basis_at_hour": current_hour,
                "basis_at": now,
                "basis_shift_id": shift_id,
                "basis_by": actor_id,
            }
            connection.execute(
                "UPDATE records SET state='confirmed',version=?,payload=?,shift_id=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), shift_id, actor_id, now, record_id),
            )
            self._add_event(
                connection,
                "confirm",
                actor_id,
                {
                    "summary": "已确认引航员",
                    "input": {"pilot_id": pilot_id},
                    "from": "draft",
                    "to": "confirmed",
                    "lease_id": lease_id,
                    "lease_version": int(lease_version),
                    "fence_epoch": int(lease["fence_epoch"]),
                    "shift_id": shift_id,
                },
                version=version,
                record_id=record_id,
                lease_id=lease_id,
                shift_id=shift_id,
            )
            self._add_event(
                connection,
                "lease_used",
                actor_id,
                {"record_id": record_id, "fence_epoch": int(lease["fence_epoch"])},
                version=version,
                record_id=record_id,
                lease_id=lease_id,
                shift_id=shift_id,
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---------- 值班班次 ----------

    def open_shift(self, actor_id: str, note: str = "") -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            open_rows = connection.execute("SELECT id FROM duty_shifts WHERE status='open'").fetchall()
            for row in open_rows:
                connection.execute("UPDATE duty_shifts SET status='closed',closed_by=?,closed_at=? WHERE id=?", (actor_id, now, row["id"]))
                self._add_event(connection, "shift_closed", actor_id, {"reason": "superseded_by_new_shift"}, shift_id=row["id"])
            cursor = connection.execute(
                "INSERT INTO duty_shifts(status,note,opened_by,opened_at) VALUES('open',?,?,?)",
                (note, actor_id, now),
            )
            shift_id = int(cursor.lastrowid)
            self._add_event(connection, "shift_opened", actor_id, {"note": note}, shift_id=shift_id)
            result = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
            connection.commit()
        return dict(result)

    def close_shift(self, shift_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("班次不存在")
            if row["status"] != "open":
                connection.rollback()
                raise Conflict("班次已关闭，无法重复换班")
            connection.execute("UPDATE duty_shifts SET status='closed',closed_by=?,closed_at=? WHERE id=?", (actor_id, now, shift_id))
            self._add_event(connection, "shift_closed", actor_id, {"reason": "manual"}, shift_id=shift_id)
            result = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
            connection.commit()
        return dict(result)

    def get_shift(self, shift_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
        if row is None:
            raise NotFound("班次不存在")
        return dict(row)

    def get_active_shift(self) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM duty_shifts WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    # ---------- 审计 ----------

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self._add_event(connection, action, actor_id, details, version=int(row["version"]), record_id=record_id)

    def _timeline(self, column: str, target_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE %s=? ORDER BY id" % column, (target_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        return self._timeline("record_id", record_id)

    def lease_audit_timeline(self, lease_id: int) -> List[Dict[str, Any]]:
        self.get_lease(lease_id)
        return self._timeline("lease_id", lease_id)

    def list_denials(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        return self._timeline_query(limit)

    def _timeline_query(self, limit: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE kind='denial' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
