from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.core.v2.control_plane_store import ControlPlaneStore


@dataclass(frozen=True)
class TenantQuota:
    max_concurrent_sessions: int = 10
    max_actions_per_minute: int = 120
    max_artifact_bytes: int = 512 * 1024 * 1024
    max_step_tokens: int = 1_200
    max_state_delta_tokens: int = 500
    max_network_summary_tokens: int = 250
    max_metadata_tokens: int = 250


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    code: str
    detail: str = ""


class QuotaManager:
    def __init__(
        self,
        default_quota: TenantQuota | None = None,
        store: ControlPlaneStore | None = None,
    ) -> None:
        self._default = default_quota or TenantQuota()
        self._store = store
        self._quotas: dict[str, TenantQuota] = {}
        self._action_timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._artifact_usage_bytes: dict[str, int] = defaultdict(int)

    def set_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        self._quotas[tenant_id] = quota
        if self._store:
            self._store.set_quota(tenant_id=tenant_id, quota_payload=quota.__dict__)

    def quota_for(self, tenant_id: str) -> TenantQuota:
        if self._store:
            stored = self._store.get_quota(tenant_id=tenant_id)
            if stored:
                base = TenantQuota().__dict__.copy()
                base.update(stored)
                return TenantQuota(**base)
        return self._quotas.get(tenant_id, self._default)

    def check_session_quota(self, tenant_id: str, active_sessions: int) -> QuotaDecision:
        quota = self.quota_for(tenant_id)
        if active_sessions >= quota.max_concurrent_sessions:
            return QuotaDecision(
                allowed=False,
                code="QUOTA_SESSION_LIMIT",
                detail=f"active_sessions={active_sessions}, max={quota.max_concurrent_sessions}",
            )
        return QuotaDecision(allowed=True, code="OK")

    def check_action_rate(self, tenant_id: str, now: float | None = None) -> QuotaDecision:
        now_ts = now if now is not None else time.time()
        quota = self.quota_for(tenant_id)

        if self._store:
            since_ts = now_ts - 60.0
            count = self._store.count_recent_actions(tenant_id=tenant_id, since_ts=since_ts)
            if count >= quota.max_actions_per_minute:
                return QuotaDecision(
                    allowed=False,
                    code="QUOTA_ACTION_RATE",
                    detail=f"count_60s={count}, max={quota.max_actions_per_minute}",
                )
            return QuotaDecision(allowed=True, code="OK")

        action_window = self._action_timestamps[tenant_id]

        cutoff = now_ts - 60.0
        while action_window and action_window[0] < cutoff:
            action_window.popleft()

        if len(action_window) >= quota.max_actions_per_minute:
            return QuotaDecision(
                allowed=False,
                code="QUOTA_ACTION_RATE",
                detail=f"count_60s={len(action_window)}, max={quota.max_actions_per_minute}",
            )

        return QuotaDecision(allowed=True, code="OK")

    def register_action(self, tenant_id: str, now: float | None = None) -> None:
        now_ts = now if now is not None else time.time()
        if self._store:
            self._store.register_action(tenant_id=tenant_id, ts=now_ts)
            self._store.prune_action_events(before_ts=now_ts - 3600.0)
            return
        self._action_timestamps[tenant_id].append(now_ts)

    def check_artifact_quota(self, tenant_id: str, additional_bytes: int) -> QuotaDecision:
        quota = self.quota_for(tenant_id)
        if self._store:
            current = self._store.get_artifact_bytes(tenant_id=tenant_id)
            projected = current + max(0, additional_bytes)
            if projected > quota.max_artifact_bytes:
                return QuotaDecision(
                    allowed=False,
                    code="QUOTA_ARTIFACT_BYTES",
                    detail=f"projected={projected}, max={quota.max_artifact_bytes}",
                )
            return QuotaDecision(allowed=True, code="OK")

        current = self._artifact_usage_bytes[tenant_id]
        projected = current + max(0, additional_bytes)
        if projected > quota.max_artifact_bytes:
            return QuotaDecision(
                allowed=False,
                code="QUOTA_ARTIFACT_BYTES",
                detail=f"projected={projected}, max={quota.max_artifact_bytes}",
            )
        return QuotaDecision(allowed=True, code="OK")

    def register_artifact_bytes(self, tenant_id: str, size_bytes: int) -> None:
        if self._store:
            self._store.add_artifact_bytes(tenant_id=tenant_id, bytes_added=size_bytes)
            return
        self._artifact_usage_bytes[tenant_id] += max(0, size_bytes)
