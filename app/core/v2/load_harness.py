from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from app.core.v2.contracts import (
    ActionContract,
    ActionSpec,
    ActionType,
    RetryPolicy,
    TimeoutPolicy,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)
from app.core.v2.predator_v2 import PredatorEngineV2
from app.core.v2.quota_manager import TenantQuota
from app.core.v2.security_layer import SecurityPolicy
from app.core.v2.session_manager import SessionConfig
from app.core.v2.wait_manager import ChaosPolicy


@dataclass
class Timings:
    audit_append_ms: list[float] = field(default_factory=list)
    control_plane_write_ms: list[float] = field(default_factory=list)


@dataclass
class Snapshot:
    ts: float
    rss_mb: float
    fd_count: int
    loop_lag_ms: float
    active_sessions: int
    pooled_contexts: int
    open_circuits: int


@dataclass
class RunSummary:
    name: str
    workflows: int
    concurrency: int
    success: int
    failures: int
    failure_codes: dict[str, int]
    failure_by_wait_kind: dict[str, int]
    p50_latency_ms: float
    p95_latency_ms: float
    max_latency_ms: float
    peak_rss_mb: float
    peak_fd_count: int
    peak_loop_lag_ms: float
    peak_active_sessions: int
    peak_pooled_contexts: int
    peak_open_circuits: int
    avg_audit_append_ms: float
    p95_audit_append_ms: float
    avg_control_plane_write_ms: float
    zombie_sessions: int


@dataclass
class DomainServer:
    name: str
    runner: web.AppRunner
    site: web.TCPSite
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def allow_domain(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def close(self) -> None:
        await self.site.stop()
        await self.runner.cleanup()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * percentile))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def _fd_count() -> int:
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.exists(path):
            try:
                return len(os.listdir(path))
            except OSError:
                continue
    return -1


def _instrument_engine(engine: PredatorEngineV2, timings: Timings) -> None:
    original_append = engine._audit.append  # type: ignore[attr-defined]

    async def wrapped_append(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return await original_append(*args, **kwargs)
        finally:
            timings.audit_append_ms.append((time.perf_counter() - started) * 1000)

    engine._audit.append = wrapped_append  # type: ignore[attr-defined]

    store = engine._control_store  # type: ignore[attr-defined]
    for method_name in (
        "register_action",
        "add_artifact_bytes",
        "acquire_session_lease",
        "release_session_lease",
        "heartbeat_session_lease",
        "set_circuit",
        "add_circuit_failure",
    ):
        if not hasattr(store, method_name):
            continue
        original = getattr(store, method_name)

        def _wrap(fn: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                finally:
                    timings.control_plane_write_ms.append((time.perf_counter() - started) * 1000)

            return wrapper

        setattr(store, method_name, _wrap(original))


async def _start_domain_server(seed: int, name: str) -> DomainServer:
    rng = random.Random(seed)

    async def root(_: web.Request) -> web.Response:
        ready_delay = rng.randint(50, 300)
        api_delay = rng.randint(25, 350)
        html = f"""
        <!doctype html>
        <html>
          <body>
            <button id="action-btn" onclick="window.__clicked = true;">Action</button>
            <div id="ready" style="display:none">ready</div>
            <script>
              window.__ready = false;
              window.__clicked = false;
              setTimeout(() => {{
                const el = document.getElementById("ready");
                if (el) el.style.display = "block";
                window.__ready = true;
              }}, {ready_delay});
              fetch("/api/ping?delay={api_delay}&mode=ok")
                .then((r) => r.json())
                .then((j) => {{ window.__api = j; }})
                .catch(() => {{ window.__api = {{error: true}}; }});
            </script>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def scenario(request: web.Request) -> web.Response:
        scenario_name = request.match_info["name"]
        if scenario_name == "disappearing_button":
            html = """
            <html><body>
            <button id="action-btn" onclick="window.__clicked=true">click</button>
            <div id="ready" style="display:none">ready</div>
            <script>
              window.__ready=false; window.__clicked=false;
              setTimeout(()=>{ const b=document.getElementById("action-btn"); if (b) b.remove(); }, 60);
              setTimeout(()=>{ const r=document.getElementById("ready"); if (r) r.style.display='block'; window.__ready=true; }, 120);
              fetch('/api/ping?delay=60&mode=ok').then((r)=>r.json()).catch(()=>null);
            </script>
            </body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "rerender":
            html = """
            <html><body>
            <div id="root"><div id="ready">booting</div></div>
            <script>
              window.__ready = false;
              setTimeout(()=>{ document.getElementById("ready").textContent = "ready"; window.__ready = true; }, 100);
              setTimeout(()=>{ document.getElementById("root").innerHTML = "<div id='ready'>ready</div>"; }, 160);
              fetch('/api/ping?delay=40&mode=ok').then((r)=>r.json()).catch(()=>null);
            </script>
            </body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "malformed_json":
            html = """
            <html><body><div id="ready">ready</div>
            <script>
              window.__ready=true;
              fetch('/api/ping?delay=10&mode=malformed').catch(()=>null);
            </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "slow_spa":
            html = """
            <html><body><div id="ready" style="display:none">ready</div>
            <script>
              window.__ready=false;
              setTimeout(()=>{ document.getElementById("ready").style.display='block'; window.__ready=true; }, 2200);
              fetch('/api/ping?delay=900&mode=ok').then((r)=>r.json()).catch(()=>null);
            </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "infinite_scroll":
            html = """
            <html><body style="height:4000px">
            <div id="items"></div>
            <script>
              window.__ready=false;
              let i = 0;
              const timer = setInterval(() => {
                for (let j = 0; j < 5; j++) {
                  const div = document.createElement("div");
                  div.textContent = "item-" + i;
                  div.id = "item-" + i;
                  document.getElementById("items").appendChild(div);
                  i += 1;
                }
                if (i >= 60) { clearInterval(timer); window.__ready = true; }
              }, 120);
              fetch('/api/ping?delay=80&mode=ok').then((r)=>r.json()).catch(()=>null);
            </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "response_inversion":
            html = """
            <html><body><div id="ready">pending</div>
            <script>
              window.__ready=false;
              Promise.all([
                fetch('/api/ping?delay=400&mode=ok&seq=1'),
                fetch('/api/ping?delay=40&mode=ok&seq=2')
              ]).then(() => {
                document.getElementById('ready').textContent='ready';
                window.__ready=true;
              });
            </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")
        if scenario_name == "frame_reload":
            html = """
            <html><body>
            <iframe id="inner" src="/frame/content"></iframe>
            <div id="ready">loading</div>
            <script>
              window.__ready=false;
              const f = document.getElementById("inner");
              let n = 0;
              const timer = setInterval(() => {
                n += 1;
                f.src = "/frame/content?nonce=" + n;
                if (n >= 3) {
                  clearInterval(timer);
                  document.getElementById("ready").textContent="ready";
                  window.__ready=true;
                }
              }, 140);
              fetch('/api/ping?delay=70&mode=ok').then((r)=>r.json()).catch(()=>null);
            </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")
        return web.Response(status=404, text="missing")

    async def frame_content(_: web.Request) -> web.Response:
        return web.Response(text="<html><body><button id='frame-btn'>frame</button></body></html>", content_type="text/html")

    async def api_ping(request: web.Request) -> web.StreamResponse:
        delay_ms = int(request.query.get("delay", "0"))
        mode = request.query.get("mode", "ok")
        seq = request.query.get("seq", "0")
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if mode == "malformed":
            return web.Response(text="{bad_json", headers={"Content-Type": "application/json"}, status=200)
        if mode == "error":
            return web.json_response({"success": False, "error": "backend"}, status=200)
        return web.json_response({"success": True, "seq": seq, "ts": time.time()}, status=200)

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/scenario/{name}", scenario)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/frame/content", frame_content)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    if not sockets:
        raise RuntimeError("Server failed to bind")
    port = int(sockets[0].getsockname()[1])
    return DomainServer(name=name, runner=runner, site=site, port=port)


def _policy_for_domains(domains: list[DomainServer], allow_custom_js: bool = False) -> SecurityPolicy:
    return SecurityPolicy(
        allow_domains=tuple(sorted({domain.allow_domain for domain in domains})),
        deny_domains=(),
        allow_custom_js=allow_custom_js,
    )


def _wait_condition(wait_kind: str) -> WaitCondition:
    if wait_kind == "selector":
        return WaitCondition(kind="selector", payload={"selector": "#ready", "state": "visible"}, timeout_ms=20_000)
    if wait_kind == "response":
        return WaitCondition(
            kind="response",
            payload={"url_pattern": r"/api/ping", "status_min": 200, "status_max": 299},
            timeout_ms=20_000,
        )
    if wait_kind == "url":
        return WaitCondition(kind="url", payload={"url_pattern": r"^http://127\.0\.0\.1:\d+/$"}, timeout_ms=20_000)
    return WaitCondition(kind="function", payload={"expression": "() => window.__ready === true"}, timeout_ms=20_000)


def _contract_for_url(workflow_id: str, run_id: str, step_index: int, url: str, wait_kind: str) -> ActionContract:
    netloc = urlparse(url).netloc
    route_key = f"{netloc}/api/ping"
    response_wait = WaitCondition(
        kind="response",
        payload={"url_pattern": r"/api/ping", "status_min": 200, "status_max": 299},
        timeout_ms=20_000,
    )
    waits = (response_wait,) if wait_kind == "response" else (response_wait, _wait_condition(wait_kind))
    return ActionContract(
        workflow_id=workflow_id,
        run_id=run_id,
        step_index=step_index,
        intent="load_harness_navigation",
        action_spec=ActionSpec(action_type=ActionType.NAVIGATE, url=url),
        wait_conditions=waits,
        verification_rules=(
            VerificationRule(
                rule_type=VerificationRuleType.NETWORK_STATUS,
                payload={"url_pattern": r"/api/ping", "status_min": 200, "status_max": 299},
            ),
            VerificationRule(
                rule_type=VerificationRuleType.JSON_FIELD,
                payload={"route_key": route_key, "require_no_silent_failure": True},
            ),
        ),
        expected_postconditions=(
            VerificationRule(
                rule_type=VerificationRuleType.URL_PATTERN,
                payload={"pattern": r"^http://127\.0\.0\.1:\d+"},
            ),
        ),
        timeout=TimeoutPolicy(
            total_timeout_ms=120_000,
            bind_timeout_ms=10_000,
            execute_timeout_ms=20_000,
            wait_timeout_ms=20_000,
            verify_timeout_ms=10_000,
        ),
        retry=RetryPolicy(max_attempts=2),
        metadata={"high_risk_approved": False},
    )


async def _snapshot_monitor(engine: PredatorEngineV2, snapshots: list[Snapshot], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    interval = 0.25
    next_tick = loop.time() + interval
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = loop.time()
        lag_ms = max(0.0, (now - next_tick) * 1000.0)
        next_tick = now + interval
        health = engine.get_health()
        snapshots.append(
            Snapshot(
                ts=time.time(),
                rss_mb=_rss_mb(),
                fd_count=_fd_count(),
                loop_lag_ms=lag_ms,
                active_sessions=int(health.get("active_sessions", 0)),
                pooled_contexts=int(health.get("pooled_contexts", 0)),
                open_circuits=int(health.get("open_circuits", 0)),
            )
        )


async def run_load_test(
    workflows: int,
    concurrency: int,
    tenants: int,
    chaos: bool = False,
) -> RunSummary:
    domains = [
        await _start_domain_server(seed=11, name="alpha"),
        await _start_domain_server(seed=22, name="beta"),
        await _start_domain_server(seed=33, name="gamma"),
    ]
    policy = _policy_for_domains(domains)
    run_root = tempfile.mkdtemp(prefix="predator-harness-load-")
    chaos_policy = (
        ChaosPolicy(
            enabled=True,
            seed=7,
            pre_action_delay_ms_min=2,
            pre_action_delay_ms_max=30,
            post_action_delay_ms_min=1,
            post_action_delay_ms_max=20,
            dom_mutation_probability=0.03,
        )
        if chaos
        else None
    )
    stable_max_sessions = min(120, max(40, concurrency))
    engine = PredatorEngineV2(
        artifact_root_dir=f"{run_root}/artifacts",
        audit_root_dir=f"{run_root}/audit",
        control_db_path=f"{run_root}/control.db",
        telemetry_dir=f"{run_root}/telemetry",
        session_config=SessionConfig(
            max_total_sessions=stable_max_sessions,
            session_acquire_timeout_ms=600_000,
            prewarmed_contexts=min(max(4, stable_max_sessions // 6), 24),
            max_pooled_contexts=max(24, stable_max_sessions // 2),
            max_context_reuses=80,
        ),
        wait_chaos_policy=chaos_policy,
    )
    timings = Timings()
    _instrument_engine(engine, timings)
    await engine.initialize()
    for tenant_index in range(max(1, tenants)):
        engine.set_tenant_quota(
            f"tenant-{tenant_index}",
            TenantQuota(
                max_concurrent_sessions=max(concurrency + 5, 20),
                max_actions_per_minute=max(workflows * 2, 500),
                max_artifact_bytes=512 * 1024 * 1024,
                max_step_tokens=1200,
                max_state_delta_tokens=500,
                max_network_summary_tokens=250,
                max_metadata_tokens=250,
            ),
        )

    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    results: list[Any] = []
    wait_kind_failures: Counter[str] = Counter()
    snapshots: list[Snapshot] = []
    stop = asyncio.Event()
    monitor_task = asyncio.create_task(_snapshot_monitor(engine=engine, snapshots=snapshots, stop=stop))

    wait_kinds = ("selector", "response", "function", "url")

    async def run_one(index: int) -> None:
        domain = domains[index % len(domains)]
        wait_kind = wait_kinds[index % len(wait_kinds)]
        workflow_id = f"wf-load-{index}"
        tenant_id = f"tenant-{index % max(1, tenants)}"
        run_id = f"run-{index}"
        contract = _contract_for_url(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=0,
            url=f"{domain.base_url}/",
            wait_kind=wait_kind,
        )
        started = time.perf_counter()
        async with semaphore:
            try:
                result = await engine.execute_contract(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    policy=policy,
                    contract=contract,
                )
                latencies.append((time.perf_counter() - started) * 1000)
                results.append(result)
                if not result.success:
                    wait_kind_failures[wait_kind] += 1
            finally:
                await engine.close_workflow_session(workflow_id)

    try:
        await asyncio.gather(*(run_one(i) for i in range(workflows)))
    finally:
        stop.set()
        await monitor_task
        await engine.close()
        for domain in domains:
            await domain.close()

    failure_codes = Counter(str(result.failure_code or "NONE") for result in results if not result.success)
    successes = sum(1 for result in results if result.success)
    failures = len(results) - successes
    zombie_sessions = engine.get_health().get("active_sessions", 0)

    peak_rss = max((snapshot.rss_mb for snapshot in snapshots), default=0.0)
    peak_fd = max((snapshot.fd_count for snapshot in snapshots), default=-1)
    peak_lag = max((snapshot.loop_lag_ms for snapshot in snapshots), default=0.0)
    peak_sessions = max((snapshot.active_sessions for snapshot in snapshots), default=0)
    peak_pooled = max((snapshot.pooled_contexts for snapshot in snapshots), default=0)
    peak_circuits = max((snapshot.open_circuits for snapshot in snapshots), default=0)

    return RunSummary(
        name="load_test",
        workflows=workflows,
        concurrency=concurrency,
        success=successes,
        failures=failures,
        failure_codes=dict(failure_codes),
        failure_by_wait_kind=dict(wait_kind_failures),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        max_latency_ms=max(latencies) if latencies else 0.0,
        peak_rss_mb=peak_rss,
        peak_fd_count=peak_fd,
        peak_loop_lag_ms=peak_lag,
        peak_active_sessions=peak_sessions,
        peak_pooled_contexts=peak_pooled,
        peak_open_circuits=peak_circuits,
        avg_audit_append_ms=statistics.mean(timings.audit_append_ms) if timings.audit_append_ms else 0.0,
        p95_audit_append_ms=_percentile(timings.audit_append_ms, 0.95),
        avg_control_plane_write_ms=(
            statistics.mean(timings.control_plane_write_ms) if timings.control_plane_write_ms else 0.0
        ),
        zombie_sessions=int(zombie_sessions),
    )


async def run_adversarial_test(iterations: int) -> dict[str, Any]:
    domain = await _start_domain_server(seed=44, name="chaos")
    policy = _policy_for_domains([domain], allow_custom_js=True)
    run_root = tempfile.mkdtemp(prefix="predator-harness-adversarial-")
    chaos_policy = ChaosPolicy(
        enabled=True,
        seed=13,
        pre_action_delay_ms_min=5,
        pre_action_delay_ms_max=40,
        post_action_delay_ms_min=2,
        post_action_delay_ms_max=25,
        dom_mutation_probability=0.18,
    )
    engine = PredatorEngineV2(
        artifact_root_dir=f"{run_root}/artifacts",
        audit_root_dir=f"{run_root}/audit",
        control_db_path=f"{run_root}/control.db",
        telemetry_dir=f"{run_root}/telemetry",
        session_config=SessionConfig(max_total_sessions=64, prewarmed_contexts=6),
        wait_chaos_policy=chaos_policy,
    )
    await engine.initialize()

    scenarios = (
        "disappearing_button",
        "rerender",
        "malformed_json",
        "slow_spa",
        "infinite_scroll",
        "response_inversion",
        "frame_reload",
    )
    outcomes: Counter[str] = Counter()
    failures: Counter[str] = Counter()

    try:
        for i in range(iterations):
            scenario_name = scenarios[i % len(scenarios)]
            workflow_id = f"wf-adversarial-{i}"
            url = f"{domain.base_url}/scenario/{scenario_name}"

            navigate = _contract_for_url(
                workflow_id=workflow_id,
                run_id=f"run-nav-{i}",
                step_index=0,
                url=url,
                wait_kind="function",
            )
            nav_result = await engine.execute_contract(
                tenant_id="tenant-chaos",
                workflow_id=workflow_id,
                policy=policy,
                contract=navigate,
            )
            if nav_result.success:
                outcomes[f"{scenario_name}:navigate_success"] += 1
            else:
                outcomes[f"{scenario_name}:navigate_failure"] += 1
                failures[str(nav_result.failure_code or "NONE")] += 1
                await engine.close_workflow_session(workflow_id)
                continue

            if scenario_name == "disappearing_button":
                click_contract = ActionContract(
                    workflow_id=workflow_id,
                    run_id=f"run-click-{i}",
                    step_index=1,
                    intent="click_disappearing_button",
                    action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#action-btn"),
                    wait_conditions=(
                        WaitCondition(kind="function", payload={"expression": "() => window.__clicked === true"}, timeout_ms=1200),
                    ),
                    retry=RetryPolicy(max_attempts=1, retryable_failure_codes=()),
                    metadata={"high_risk_approved": False},
                )
                click_result = await engine.execute_contract(
                    tenant_id="tenant-chaos",
                    workflow_id=workflow_id,
                    policy=policy,
                    contract=click_contract,
                )
                if click_result.success:
                    outcomes[f"{scenario_name}:click_success"] += 1
                else:
                    outcomes[f"{scenario_name}:click_failure"] += 1
                    failures[str(click_result.failure_code or "NONE")] += 1

            await engine.close_workflow_session(workflow_id)
    finally:
        await engine.close()
        await domain.close()

    return {
        "iterations": iterations,
        "outcomes": dict(outcomes),
        "failure_codes": dict(failures),
    }


async def run_quota_breaker_test() -> dict[str, Any]:
    domain = await _start_domain_server(seed=55, name="quota")
    policy = _policy_for_domains([domain])
    run_root = tempfile.mkdtemp(prefix="predator-harness-quota-")
    engine = PredatorEngineV2(
        artifact_root_dir=f"{run_root}/artifacts",
        audit_root_dir=f"{run_root}/audit",
        control_db_path=f"{run_root}/control.db",
        telemetry_dir=f"{run_root}/telemetry",
        session_config=SessionConfig(max_total_sessions=32, prewarmed_contexts=4),
    )
    await engine.initialize()

    engine.set_tenant_quota(
        "tenant-a",
        TenantQuota(
            max_concurrent_sessions=1,
            max_actions_per_minute=200,
            max_artifact_bytes=128 * 1024 * 1024,
            max_step_tokens=1200,
            max_state_delta_tokens=500,
            max_network_summary_tokens=250,
            max_metadata_tokens=250,
        ),
    )
    engine.set_tenant_quota(
        "tenant-b",
        TenantQuota(
            max_concurrent_sessions=4,
            max_actions_per_minute=200,
            max_artifact_bytes=128 * 1024 * 1024,
            max_step_tokens=1200,
            max_state_delta_tokens=500,
            max_network_summary_tokens=250,
            max_metadata_tokens=250,
        ),
    )

    async def _run_contract(tenant_id: str, workflow_id: str, url: str, wait_kind: str) -> Any:
        contract = _contract_for_url(
            workflow_id=workflow_id,
            run_id=f"run-{tenant_id}-{workflow_id}",
            step_index=0,
            url=url,
            wait_kind=wait_kind,
        )
        return await engine.execute_contract(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
            contract=contract,
        )

    try:
        slow_url = f"{domain.base_url}/scenario/slow_spa"
        first = asyncio.create_task(_run_contract("tenant-a", "wf-a-1", slow_url, "function"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(_run_contract("tenant-a", "wf-a-2", slow_url, "function"))
        first_result, second_result = await asyncio.gather(first, second)
        await engine.close_workflow_session("wf-a-1")
        await engine.close_workflow_session("wf-a-2")

        breaker_failures = 0
        for i in range(6):
            failing = ActionContract(
                workflow_id=f"wf-breaker-a-{i}",
                run_id=f"run-breaker-a-{i}",
                step_index=0,
                intent="force_postcondition_failure",
                action_spec=ActionSpec(action_type=ActionType.NAVIGATE, url=f"{domain.base_url}/"),
                wait_conditions=(WaitCondition(kind="selector", payload={"selector": "#ready"}, timeout_ms=3000),),
                expected_postconditions=(
                    VerificationRule(
                        rule_type=VerificationRuleType.TEXT_STATE,
                        payload={"selector": "#ready", "expected": "never-happens", "mode": "equals"},
                    ),
                ),
                retry=RetryPolicy(max_attempts=1, retryable_failure_codes=()),
                metadata={"high_risk_approved": False},
            )
            result = await engine.execute_contract(
                tenant_id="tenant-a",
                workflow_id=f"wf-breaker-a-{i}",
                policy=policy,
                contract=failing,
            )
            if not result.success:
                breaker_failures += 1
            await engine.close_workflow_session(f"wf-breaker-a-{i}")

        blocked = await _run_contract("tenant-a", "wf-breaker-a-blocked", f"{domain.base_url}/", "selector")
        tenant_b_result = await _run_contract("tenant-b", "wf-breaker-b-ok", f"{domain.base_url}/", "selector")
        await engine.close_workflow_session("wf-breaker-a-blocked")
        await engine.close_workflow_session("wf-breaker-b-ok")
    finally:
        health = engine.get_health()
        await engine.close()
        await domain.close()

    return {
        "tenant_a_first_success": bool(first_result.success),
        "tenant_a_second_failure_code": second_result.failure_code,
        "breaker_failures_recorded": breaker_failures,
        "tenant_a_blocked_failure_code": blocked.failure_code,
        "tenant_b_success": bool(tenant_b_result.success),
        "open_circuits": int(health.get("open_circuits", 0)),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if args.mode in {"load", "all"}:
        load_result = await run_load_test(
            workflows=args.workflows,
            concurrency=args.concurrency,
            tenants=args.tenants,
            chaos=args.chaos,
        )
        results["load"] = asdict(load_result)
    if args.mode in {"adversarial", "all"}:
        results["adversarial"] = await run_adversarial_test(iterations=args.adversarial_iterations)
    if args.mode in {"quota-breaker", "all"}:
        results["quota_breaker"] = await run_quota_breaker_test()
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predator v2 stress and chaos harness")
    parser.add_argument(
        "--mode",
        choices=("load", "adversarial", "quota-breaker", "all"),
        default="all",
        help="Harness mode",
    )
    parser.add_argument("--workflows", type=int, default=200, help="Total workflows for load mode")
    parser.add_argument("--concurrency", type=int, default=200, help="Concurrent workflows for load mode")
    parser.add_argument("--tenants", type=int, default=20, help="Tenant count for load mode")
    parser.add_argument("--chaos", action="store_true", help="Enable chaos in load mode")
    parser.add_argument("--adversarial-iterations", type=int, default=28, help="Iterations for adversarial mode")
    parser.add_argument("--output", type=str, default="", help="Optional output path for JSON summary")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    results = asyncio.run(_run(args))
    payload = json.dumps(results, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    print(payload)


if __name__ == "__main__":
    run()
