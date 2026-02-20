from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

from aiohttp import web

from app.core.v2 import (
    ActionContract,
    ActionSpec,
    ActionType,
    ClusterSchedulerConfig,
    NodeAdmissionSLO,
    PredatorShardedCluster,
    SecurityPolicy,
    SessionConfig,
    TimeoutPolicy,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)


async def start_demo_site() -> tuple[web.AppRunner, int]:
    async def login_page(_: web.Request) -> web.Response:
        html = """
        <!doctype html>
        <html>
          <body>
            <h1>Predator Demo Portal</h1>
            <label>Username <input id="username" /></label>
            <label>Password <input id="password" type="password" /></label>
            <button id="login" onclick="login()">Login</button>
            <div id="status">Signed out</div>
            <script>
              window.__loggedIn = false;
              async function login() {
                const username = document.getElementById("username").value;
                const password = document.getElementById("password").value;
                const response = await fetch("/api/login", {
                  method: "POST",
                  headers: {"Content-Type": "application/json"},
                  body: JSON.stringify({username, password})
                });
                const payload = await response.json();
                if (payload.success) {
                  window.__loggedIn = true;
                  document.getElementById("status").innerText = "Welcome " + payload.user;
                  history.pushState({}, "", "/dashboard");
                } else {
                  window.__loggedIn = false;
                  document.getElementById("status").innerText = "Denied";
                }
              }
            </script>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def reports_page(_: web.Request) -> web.Response:
        html = """
        <!doctype html>
        <html>
          <body>
            <h1>Reports</h1>
            <button id="run-report" onclick="runReport()">Run Report</button>
            <div id="report-status">Idle</div>
            <script>
              window.__reportReady = false;
              async function runReport() {
                const response = await fetch("/api/report");
                const payload = await response.json();
                if (payload.success) {
                  window.__reportReady = true;
                  document.getElementById("report-status").innerText = "Report ready";
                } else {
                  window.__reportReady = false;
                  document.getElementById("report-status").innerText = "Report failed";
                }
              }
            </script>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def api_login(request: web.Request) -> web.Response:
        body = await request.json()
        user = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if user == "alice" and password == "s3cr3t":
            return web.json_response({"success": True, "user": user})
        return web.json_response({"success": False, "error": "bad_credentials"})

    async def api_report(_: web.Request) -> web.Response:
        await asyncio.sleep(0.1)
        return web.json_response({"success": True, "rows": 42})

    app = web.Application()
    app.router.add_get("/", login_page)
    app.router.add_get("/dashboard", login_page)
    app.router.add_get("/reports", reports_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_get("/api/report", api_report)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    if not sockets:
        raise RuntimeError("failed to bind demo site")
    port = int(sockets[0].getsockname()[1])
    return runner, port


def make_policy(port: int) -> SecurityPolicy:
    return SecurityPolicy(allow_domains=(f"127.0.0.1:{port}",), deny_domains=(), allow_custom_js=False)


def login_contracts(workflow_id: str, run_id: str, port: int) -> list[ActionContract]:
    base = f"http://127.0.0.1:{port}"
    route_login = f"127.0.0.1:{port}/api/login"
    route_report = f"127.0.0.1:{port}/api/report"
    return [
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=0,
            intent="open_portal",
            action_spec=ActionSpec(action_type=ActionType.NAVIGATE, url=f"{base}/"),
            wait_conditions=(
                WaitCondition(kind="selector", payload={"selector": "#username", "state": "visible"}, timeout_ms=5000),
            ),
            expected_postconditions=(
                VerificationRule(rule_type=VerificationRuleType.URL_PATTERN, payload={"pattern": r"/?$"}),
            ),
            metadata={"high_risk_approved": False},
        ),
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=1,
            intent="type_username",
            action_spec=ActionSpec(action_type=ActionType.TYPE, selector="#username", text="alice"),
            wait_conditions=(
                WaitCondition(
                    kind="function",
                    payload={"expression": "() => document.querySelector('#username').value === 'alice'"},
                    timeout_ms=5000,
                ),
            ),
            metadata={"high_risk_approved": False},
        ),
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=2,
            intent="type_password",
            action_spec=ActionSpec(action_type=ActionType.TYPE, selector="#password", text="s3cr3t"),
            wait_conditions=(
                WaitCondition(
                    kind="function",
                    payload={"expression": "() => document.querySelector('#password').value === 's3cr3t'"},
                    timeout_ms=5000,
                ),
            ),
            metadata={"high_risk_approved": False},
        ),
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=3,
            intent="submit_login",
            action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#login"),
            wait_conditions=(
                WaitCondition(
                    kind="response",
                    payload={"url_pattern": r"/api/login", "status_min": 200, "status_max": 299},
                    timeout_ms=5000,
                ),
                WaitCondition(kind="function", payload={"expression": "() => window.__loggedIn === true"}, timeout_ms=5000),
            ),
            verification_rules=(
                VerificationRule(
                    rule_type=VerificationRuleType.JSON_FIELD,
                    payload={"route_key": route_login, "require_no_silent_failure": True},
                ),
                VerificationRule(
                    rule_type=VerificationRuleType.TEXT_STATE,
                    payload={"selector": "#status", "expected": "Welcome", "mode": "contains"},
                ),
                VerificationRule(
                    rule_type=VerificationRuleType.URL_PATTERN,
                    payload={"pattern": r"/dashboard$"},
                ),
            ),
            timeout=TimeoutPolicy(total_timeout_ms=30000, execute_timeout_ms=10000, wait_timeout_ms=10000),
            metadata={"high_risk_approved": False},
        ),
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=4,
            intent="open_reports",
            action_spec=ActionSpec(action_type=ActionType.NAVIGATE, url=f"{base}/reports"),
            wait_conditions=(
                WaitCondition(kind="selector", payload={"selector": "#run-report", "state": "visible"}, timeout_ms=5000),
            ),
            metadata={"high_risk_approved": False},
        ),
        ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=5,
            intent="run_report",
            action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#run-report"),
            wait_conditions=(
                WaitCondition(
                    kind="response",
                    payload={"url_pattern": r"/api/report", "status_min": 200, "status_max": 299},
                    timeout_ms=5000,
                ),
                WaitCondition(
                    kind="function",
                    payload={"expression": "() => window.__reportReady === true"},
                    timeout_ms=5000,
                ),
            ),
            verification_rules=(
                VerificationRule(
                    rule_type=VerificationRuleType.JSON_FIELD,
                    payload={"route_key": route_report, "require_no_silent_failure": True},
                ),
                VerificationRule(
                    rule_type=VerificationRuleType.TEXT_STATE,
                    payload={"selector": "#report-status", "expected": "Report ready", "mode": "equals"},
                ),
            ),
            metadata={"high_risk_approved": False},
        ),
    ]


async def run_workflow(
    cluster: PredatorShardedCluster,
    tenant_id: str,
    workflow_id: str,
    policy: SecurityPolicy,
    contracts: list[ActionContract],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for contract in contracts:
        result = await cluster.execute_contract(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
            contract=contract,
        )
        results.append(
            {
                "step": contract.step_index,
                "intent": contract.intent,
                "action_id": result.action_id,
                "success": result.success,
                "failure_code": result.failure_code,
            }
        )
        if not result.success:
            raise RuntimeError(f"{workflow_id} failed at step {contract.step_index}: {result.failure_code}")
    return results


async def main() -> None:
    runner, port = await start_demo_site()
    cluster = PredatorShardedCluster(
        scheduler=ClusterSchedulerConfig(shard_count=2, light_weight=3, heavy_weight=1),
        slo=NodeAdmissionSLO(max_active_sessions=12, max_inflight_actions=12, max_loop_lag_p95_ms=2000),
        session_config=SessionConfig(
            headless=True,
            max_total_sessions=12,
            prewarmed_contexts=4,
            max_pooled_contexts=12,
            session_acquire_timeout_ms=120000,
        ),
        artifact_root_dir="/tmp/predator-live-demo/artifacts",
        audit_root_dir="/tmp/predator-live-demo/audit",
        control_db_path="/tmp/predator-live-demo/control.db",
        telemetry_dir="/tmp/predator-live-demo/telemetry",
    )
    await cluster.initialize()

    policy = make_policy(port)
    try:
        wf_a_contracts = login_contracts("wf-live-a", "run-live-a", port)
        wf_b_contracts = login_contracts("wf-live-b", "run-live-b", port)

        workflow_a, workflow_b = await asyncio.gather(
            run_workflow(cluster, "tenant-demo", "wf-live-a", policy, wf_a_contracts),
            run_workflow(cluster, "tenant-demo", "wf-live-b", policy, wf_b_contracts),
        )

        idempotency_contract = ActionContract(
            workflow_id="wf-live-a",
            run_id="run-live-a",
            step_index=99,
            intent="idempotency_probe",
            action_spec=ActionSpec(action_type=ActionType.WAIT_ONLY),
            wait_conditions=(WaitCondition(kind="function", payload={"expression": "() => true"}, timeout_ms=1000),),
            metadata={"high_risk_approved": False},
        )

        t1 = time.perf_counter()
        first = await cluster.execute_contract("tenant-demo", "wf-live-a", policy, idempotency_contract)
        first_ms = (time.perf_counter() - t1) * 1000

        t2 = time.perf_counter()
        second = await cluster.execute_contract("tenant-demo", "wf-live-a", policy, idempotency_contract)
        second_ms = (time.perf_counter() - t2) * 1000

        output = {
            "demo_site": f"http://127.0.0.1:{port}",
            "workflow_results": {
                "wf-live-a": workflow_a,
                "wf-live-b": workflow_b,
            },
            "idempotency_probe": {
                "action_id_first": first.action_id,
                "action_id_second": second.action_id,
                "first_success": first.success,
                "second_success": second.success,
                "first_ms": round(first_ms, 2),
                "second_ms": round(second_ms, 2),
            },
            "cluster_health": cluster.get_health(),
        }
        print(json.dumps(output, indent=2))
    finally:
        await cluster.close_workflow_session("wf-live-a")
        await cluster.close_workflow_session("wf-live-b")
        await cluster.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
