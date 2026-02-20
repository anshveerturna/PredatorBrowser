# Predator v2 Execution Stack

This document defines the runtime boundaries for Predator v2.

## Temporal Boundary

- Temporal Workflow:
  - Owns orchestration state, retries across Activities, branching, and compensation.
  - Never touches browser internals.
- Predator Activity:
  - Executes exactly one `ActionContract`.
  - Returns deterministic evidence (`ActionExecutionResult`) with state delta and verification output.

## Core Modules

- `SessionManager`: workflow-scoped browser context isolation.
- `TabManager`: explicit multi-tab context and active-tab switching.
- `SecurityLayer`: domain policy + high-risk action controls.
- `NetworkObserver`: request/response/failure observation and compact summaries.
- `StructuredStateExtractor`: bounded state projection (no HTML dump).
- `DeltaStateTracker`: hash + structural diffs for token-efficient updates.
- `WaitManager`: event-driven waits and composite conditions.
- `Navigator`: frame-aware target binding.
- `ActionEngine`: deterministic execution pipeline with retry policy.
- `VerificationEngine`: typed postcondition assertions.
- `ArtifactManager`: upload/download lifecycle + SHA-256 evidence.
- `QuotaManager`: tenant session/action/artifact/token quotas.
- `DomainCircuitBreaker`: domain-level resilience and failure isolation.
- `TokenBudgetManager`: hard payload budget enforcement with deterministic trimming.
- `AuditTrail`: immutable append-only hash-chained action ledger.
- `ControlPlaneStore`: shared SQLite backing for quotas/rate/circuit/session leases.
- `PromptInjectionFilter`: redacts instruction-like page text before model exposure.
- `TelemetrySink`: structured event export (JSONL sink included by default).
- `PredatorEngineV2`: Activity-facing orchestrator with idempotency + audit persistence.
- `PredatorShardedCluster`: deterministic shard router + queue-aware scheduler + node SLO admission.

## Token Discipline

- State payloads use `StructuredState.to_model_dict()` only.
- Selector hints are retained internally for execution and not emitted in model payload.
- Deltas include changed sections and capped operation lists.

## Determinism Controls

- Canonicalized `ActionContract` yields stable `action_id`.
- Idempotency ledger returns cached `ActionExecutionResult` for duplicate action IDs.
- Cross-process dedupe checks persisted audit records by `action_id`.
- All waits are Playwright event waiters; no page-level fixed sleep delays.
- Audit records are hash-chained and verifiable (`verify_audit_chain`).

## Runtime Guards

- Domain allow/deny policy is checked for every navigation and action.
- High-risk actions (`upload`, `download_trigger`, `custom_js_restricted`) require explicit approval metadata.
- Browser session ownership is guarded by workflow leases in shared control-plane storage.
- Per-tenant quotas enforce:
  - max concurrent sessions
  - max actions per minute
  - max artifact bytes
  - max step token budget
- Circuit breaker blocks unstable domains after repeated failures and re-opens with half-open probing.

## Horizontal Sharding

- `PredatorShardedCluster` routes actions deterministically by `hash(tenant_id, workflow_id) % shard_count`.
- Workflow affinity is pinned to a shard for session-local browser state continuity.
- Queue scheduler is tenant-fair and work-class aware:
  - per-tenant round-robin inside each shard queue
  - weighted work classes (`light`/`heavy`) for head-of-line isolation
- Per-node SLO admission gates dispatch:
  - active session cap
  - inflight action cap
  - loop-lag p95 ceiling
  - FD and RSS ceilings
  - breaker-open ratio ceiling
- Nodes that breach SLOs enter drain mode (`admit=false`) and only execute already-dispatched actions.

## Deployment Entry Points

- MCP server for v2 runtime operations: `python -m app.server_v2`
- Temporal worker adapter (optional `temporalio` dependency): `python -m app.temporal_worker_v2`

### Sharded Mode Environment

- `PREDATOR_V2_SHARDS` (default `1`): set `>1` to enable `PredatorShardedCluster` in MCP/Temporal entrypoints.
- `PREDATOR_V2_DISPATCH_INTERVAL_MS` (default `20`)
- `PREDATOR_V2_MONITOR_INTERVAL_MS` (default `250`)
- `PREDATOR_V2_LIGHT_WEIGHT` / `PREDATOR_V2_HEAVY_WEIGHT` (default `3` / `1`)
- `PREDATOR_V2_SLO_MAX_ACTIVE_SESSIONS` (default `120`)
- `PREDATOR_V2_SLO_MAX_INFLIGHT_ACTIONS` (default `120`)
- `PREDATOR_V2_SLO_MAX_LOOP_LAG_MS` (default `1200`)
- `PREDATOR_V2_SLO_MAX_FD` (default `1024`)
- `PREDATOR_V2_SLO_MAX_RSS_MB` (default `1024`)
- `PREDATOR_V2_SLO_MAX_BREAKER_RATIO` (default `0.5`)
