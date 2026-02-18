from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditRecord:
    record_id: str
    ts: str
    tenant_id: str
    workflow_id: str
    action_id: str
    contract_json: str
    action_hash: str
    success: bool
    failure_code: str | None
    pre_state_id: str | None
    post_state_id: str | None
    state_delta: dict[str, Any]
    network_summary: dict[str, Any]
    artifacts: list[dict[str, Any]]
    telemetry: dict[str, Any]
    metadata: dict[str, Any]
    previous_record_hash: str
    signature: str
    record_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "workflow_id": self.workflow_id,
            "action_id": self.action_id,
            "contract_json": self.contract_json,
            "action_hash": self.action_hash,
            "success": self.success,
            "failure_code": self.failure_code,
            "pre_state_id": self.pre_state_id,
            "post_state_id": self.post_state_id,
            "state_delta": self.state_delta,
            "network_summary": self.network_summary,
            "artifacts": self.artifacts,
            "telemetry": self.telemetry,
            "metadata": self.metadata,
            "previous_record_hash": self.previous_record_hash,
            "signature": self.signature,
            "record_hash": self.record_hash,
        }


class AuditTrail:
    """Append-only hash-chained audit trail persisted as JSONL per workflow."""

    def __init__(
        self,
        root_dir: str = "/tmp/predator-audit",
        signing_key: str | None = None,
    ) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._last_hash: dict[str, str] = {}
        self._signing_key = (signing_key or os.getenv("PREDATOR_AUDIT_SIGNING_KEY", "")).encode("utf-8")
        self._lock = asyncio.Lock()

    def _workflow_log(self, tenant_id: str, workflow_id: str) -> Path:
        safe_tenant = tenant_id.replace("/", "_")
        safe_workflow = workflow_id.replace("/", "_")
        tenant_dir = self._root / safe_tenant
        tenant_dir.mkdir(parents=True, exist_ok=True)
        return tenant_dir / f"{safe_workflow}.jsonl"

    def _canonical_json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _compute_record_hash(self, payload_without_hash: dict[str, Any]) -> str:
        return sha256(self._canonical_json(payload_without_hash).encode("utf-8")).hexdigest()

    def _action_hash(self, canonical_contract_json: str) -> str:
        return sha256(canonical_contract_json.encode("utf-8")).hexdigest()

    def _sign(self, payload: dict[str, Any]) -> str:
        if not self._signing_key:
            return ""
        message = self._canonical_json(payload).encode("utf-8")
        return hmac.new(self._signing_key, message, digestmod="sha256").hexdigest()

    def _verify_signature(self, payload: dict[str, Any], signature: str) -> bool:
        if not self._signing_key:
            return signature == ""
        expected = self._sign(payload)
        return hmac.compare_digest(expected, signature)

    async def append(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        canonical_contract_json: str,
        result: dict[str, Any],
    ) -> AuditRecord:
        async with self._lock:
            log_path = self._workflow_log(tenant_id=tenant_id, workflow_id=workflow_id)

            previous_hash = self._last_hash.get(f"{tenant_id}:{workflow_id}", "")
            if previous_hash == "" and log_path.exists() and log_path.stat().st_size > 0:
                previous = await self.list_records(tenant_id=tenant_id, workflow_id=workflow_id)
                if previous:
                    previous_hash = previous[-1].record_hash

            ts = datetime.now(tz=timezone.utc).isoformat()
            record_seed = f"{tenant_id}|{workflow_id}|{action_id}|{ts}|{previous_hash}"
            record_id = f"ar_{sha256(record_seed.encode('utf-8')).hexdigest()[:24]}"

            base_payload = {
                "record_id": record_id,
                "ts": ts,
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "action_id": action_id,
                "contract_json": canonical_contract_json,
                "action_hash": self._action_hash(canonical_contract_json),
                "success": bool(result.get("success", False)),
                "failure_code": result.get("failure_code"),
                "pre_state_id": result.get("pre_state_id"),
                "post_state_id": result.get("post_state_id"),
                "state_delta": result.get("state_delta", {}),
                "network_summary": result.get("network_summary", {}),
                "artifacts": result.get("artifacts", []),
                "telemetry": result.get("telemetry", {}),
                "metadata": result.get("metadata", {}),
                "previous_record_hash": previous_hash,
            }

            signature = self._sign(base_payload)
            record_hash = self._compute_record_hash(base_payload)
            payload = dict(base_payload)
            payload["signature"] = signature
            payload["record_hash"] = record_hash

            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(self._canonical_json(payload) + "\n")
                log_file.flush()
                os.fsync(log_file.fileno())

            self._last_hash[f"{tenant_id}:{workflow_id}"] = record_hash
            return AuditRecord(**payload)

    async def list_records(self, tenant_id: str, workflow_id: str) -> list[AuditRecord]:
        log_path = self._workflow_log(tenant_id=tenant_id, workflow_id=workflow_id)
        if not log_path.exists():
            return []

        records: list[AuditRecord] = []
        with log_path.open("r", encoding="utf-8") as log_file:
            for line in log_file:
                raw = line.strip()
                if not raw:
                    continue
                records.append(AuditRecord(**json.loads(raw)))
        return records

    async def get_record_by_action(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
    ) -> AuditRecord | None:
        records = await self.list_records(tenant_id=tenant_id, workflow_id=workflow_id)
        for record in records:
            if record.action_id == action_id:
                return record
        return None

    async def verify_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]:
        records = await self.list_records(tenant_id=tenant_id, workflow_id=workflow_id)
        previous_hash = ""

        for index, record in enumerate(records):
            expected_prev = previous_hash
            if record.previous_record_hash != expected_prev:
                return False, f"chain_link_mismatch_at_index_{index}"

            payload_without_hash = record.to_dict()
            payload_without_hash.pop("record_hash", None)
            signature = payload_without_hash.pop("signature", "")
            computed = self._compute_record_hash(payload_without_hash)
            if computed != record.record_hash:
                return False, f"record_hash_mismatch_at_index_{index}"
            if not self._verify_signature(payload_without_hash, signature):
                return False, f"record_signature_mismatch_at_index_{index}"

            previous_hash = record.record_hash

        return True, "ok"
