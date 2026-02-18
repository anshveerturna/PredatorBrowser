from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CircuitSnapshot:
    state: str
    opened_at: float


class ControlPlaneStore:
    """SQLite-backed shared control plane state.

    This enables multi-process consistency for quotas, session leases,
    action rates, artifact usage, and circuit breaker state.
    """

    def __init__(self, db_path: str = "/tmp/predator-control-plane/control.db") -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenant_quota (
                    tenant_id TEXT PRIMARY KEY,
                    quota_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_events (
                    tenant_id TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_events_tenant_ts ON action_events(tenant_id, ts);

                CREATE TABLE IF NOT EXISTS artifact_usage (
                    tenant_id TEXT PRIMARY KEY,
                    bytes_used INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_lease (
                    workflow_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    heartbeat_ts REAL NOT NULL,
                    created_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session_lease_tenant ON session_lease(tenant_id);

                CREATE TABLE IF NOT EXISTS circuit_state (
                    domain TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS circuit_failures (
                    domain TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_circuit_failures_domain_ts ON circuit_failures(domain, ts);
                """
            )
            conn.commit()

    # Quotas
    def set_quota(self, tenant_id: str, quota_payload: dict[str, Any]) -> None:
        now_ts = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tenant_quota(tenant_id, quota_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id)
                DO UPDATE SET quota_json=excluded.quota_json, updated_at=excluded.updated_at
                """,
                (tenant_id, json.dumps(quota_payload, sort_keys=True), now_ts),
            )
            conn.commit()

    def get_quota(self, tenant_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT quota_json FROM tenant_quota WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    # Action rate
    def register_action(self, tenant_id: str, ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO action_events(tenant_id, ts) VALUES (?, ?)", (tenant_id, ts))
            conn.commit()

    def count_recent_actions(self, tenant_id: str, since_ts: float) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_events WHERE tenant_id = ? AND ts >= ?",
                (tenant_id, since_ts),
            ).fetchone()
        return int(row[0] if row else 0)

    def prune_action_events(self, before_ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM action_events WHERE ts < ?", (before_ts,))
            conn.commit()

    # Artifact usage
    def add_artifact_bytes(self, tenant_id: str, bytes_added: int) -> None:
        now_ts = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifact_usage(tenant_id, bytes_used, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id)
                DO UPDATE SET bytes_used=artifact_usage.bytes_used + excluded.bytes_used, updated_at=excluded.updated_at
                """,
                (tenant_id, max(0, bytes_added), now_ts),
            )
            conn.commit()

    def get_artifact_bytes(self, tenant_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT bytes_used FROM artifact_usage WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    # Session lease
    def acquire_session_lease(
        self,
        tenant_id: str,
        workflow_id: str,
        owner_id: str,
        lease_ttl_seconds: int = 300,
    ) -> bool:
        now_ts = time.time()
        expiry_cutoff = now_ts - lease_ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM session_lease WHERE heartbeat_ts < ?", (expiry_cutoff,))
            existing = conn.execute(
                "SELECT owner_id FROM session_lease WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()

            if existing and existing[0] != owner_id:
                conn.commit()
                return False

            conn.execute(
                """
                INSERT INTO session_lease(workflow_id, tenant_id, owner_id, heartbeat_ts, created_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id)
                DO UPDATE SET tenant_id=excluded.tenant_id, owner_id=excluded.owner_id, heartbeat_ts=excluded.heartbeat_ts
                """,
                (workflow_id, tenant_id, owner_id, now_ts, now_ts),
            )
            conn.commit()
            return True

    def heartbeat_session_lease(self, workflow_id: str, owner_id: str) -> None:
        now_ts = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE session_lease
                SET heartbeat_ts = ?
                WHERE workflow_id = ? AND owner_id = ?
                """,
                (now_ts, workflow_id, owner_id),
            )
            conn.commit()

    def release_session_lease(self, workflow_id: str, owner_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM session_lease WHERE workflow_id = ? AND owner_id = ?",
                (workflow_id, owner_id),
            )
            conn.commit()

    def count_active_sessions(self, tenant_id: str, lease_ttl_seconds: int = 300) -> int:
        now_ts = time.time()
        expiry_cutoff = now_ts - lease_ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM session_lease WHERE heartbeat_ts < ?", (expiry_cutoff,))
            row = conn.execute(
                "SELECT COUNT(*) FROM session_lease WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            conn.commit()
        return int(row[0] if row else 0)

    def count_all_active_sessions(self, lease_ttl_seconds: int = 300) -> int:
        now_ts = time.time()
        expiry_cutoff = now_ts - lease_ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM session_lease WHERE heartbeat_ts < ?", (expiry_cutoff,))
            row = conn.execute("SELECT COUNT(*) FROM session_lease").fetchone()
            conn.commit()
        return int(row[0] if row else 0)

    # Circuit breaker
    def _circuit_key(self, domain: str, tenant_id: str | None = None) -> str:
        if tenant_id:
            return f"{tenant_id}::{domain}"
        return domain

    def get_circuit(self, domain: str, tenant_id: str | None = None) -> CircuitSnapshot:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT state, opened_at FROM circuit_state WHERE domain = ?",
                (key,),
            ).fetchone()
        if not row:
            return CircuitSnapshot(state="closed", opened_at=0.0)
        return CircuitSnapshot(state=str(row[0]), opened_at=float(row[1]))

    def list_circuit_domains(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT domain FROM circuit_state").fetchall()
        return [str(row[0]) for row in rows]

    def set_circuit(self, domain: str, state: str, opened_at: float, tenant_id: str | None = None) -> None:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        now_ts = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO circuit_state(domain, state, opened_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(domain)
                DO UPDATE SET state=excluded.state, opened_at=excluded.opened_at, updated_at=excluded.updated_at
                """,
                (key, state, opened_at, now_ts),
            )
            conn.commit()

    def add_circuit_failure(self, domain: str, ts: float, tenant_id: str | None = None) -> None:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO circuit_failures(domain, ts) VALUES (?, ?)", (key, ts))
            conn.commit()

    def count_circuit_failures(self, domain: str, since_ts: float, tenant_id: str | None = None) -> int:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM circuit_failures WHERE domain = ? AND ts >= ?",
                (key, since_ts),
            ).fetchone()
        return int(row[0] if row else 0)

    def prune_circuit_failures(self, domain: str, before_ts: float, tenant_id: str | None = None) -> None:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM circuit_failures WHERE domain = ? AND ts < ?",
                (key, before_ts),
            )
            conn.commit()

    def clear_circuit_failures(self, domain: str, tenant_id: str | None = None) -> None:
        key = self._circuit_key(domain=domain, tenant_id=tenant_id)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM circuit_failures WHERE domain = ?", (key,))
            conn.commit()

    def owner_id(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}"
