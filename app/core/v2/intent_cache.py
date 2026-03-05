from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkflowCacheKey:
    instruction: str
    start_url: str
    environment: str

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "instruction": self.instruction.strip(),
                "start_url": self.start_url.strip(),
                "environment": self.environment.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


class IntentWorkflowCache:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intent_cache (
                    cache_key TEXT PRIMARY KEY,
                    contract_json TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    hits INTEGER DEFAULT 0,
                    cache_version INTEGER DEFAULT 1
                )
                """
            )

    def get(self, key: WorkflowCacheKey, cache_version: int = 1) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT contract_json
                FROM intent_cache
                WHERE cache_key = ? AND cache_version = ?
                """,
                (key.digest(), cache_version),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE intent_cache SET hits = hits + 1 WHERE cache_key = ?", (key.digest(),))
            return dict(json.loads(row[0]))

    def put(self, key: WorkflowCacheKey, contract: dict[str, Any], cache_version: int = 1) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intent_cache(cache_key, contract_json, cache_version)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key)
                DO UPDATE SET
                    contract_json = excluded.contract_json,
                    cache_version = excluded.cache_version,
                    created_at = CURRENT_TIMESTAMP
                """,
                (key.digest(), json.dumps(contract, sort_keys=True), cache_version),
            )

    def invalidate(self, key: WorkflowCacheKey) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM intent_cache WHERE cache_key = ?", (key.digest(),))
