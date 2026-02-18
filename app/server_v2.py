"""Predator v2 MCP server.

Exposes deterministic action-contract execution and control-plane tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from app.core.v2 import (
    ActionContract,
    ActionSpec,
    ActionType,
    PredatorEngineV2,
    SecurityPolicy,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)
from app.core.v2.contracts import EscalationPolicy, RetryPolicy, TimeoutPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("predator.server.v2")

_engine: PredatorEngineV2 | None = None


def _to_action_spec(payload: dict[str, Any]) -> ActionSpec:
    return ActionSpec(
        action_type=ActionType(payload["action_type"]),
        target_eid=payload.get("target_eid"),
        target_fid=payload.get("target_fid"),
        selector=payload.get("selector"),
        selector_candidates=tuple(payload.get("selector_candidates", [])),
        text=payload.get("text"),
        url=payload.get("url"),
        select_value=payload.get("select_value"),
        upload_artifact_id=payload.get("upload_artifact_id"),
        js_expression=payload.get("js_expression"),
        js_argument=payload.get("js_argument"),
    )


def _to_rules(payload: list[dict[str, Any]]) -> tuple[VerificationRule, ...]:
    return tuple(
        VerificationRule(
            rule_type=VerificationRuleType(item["rule_type"]),
            severity=item.get("severity", "hard"),
            payload=item.get("payload", {}),
        )
        for item in payload
    )


def _to_waits(payload: list[dict[str, Any]]) -> tuple[WaitCondition, ...]:
    return tuple(
        WaitCondition(
            kind=item["kind"],
            payload=item.get("payload", {}),
            timeout_ms=item.get("timeout_ms"),
        )
        for item in payload
    )


def _to_contract(payload: dict[str, Any]) -> ActionContract:
    return ActionContract(
        workflow_id=payload["workflow_id"],
        run_id=payload["run_id"],
        step_index=int(payload["step_index"]),
        intent=payload["intent"],
        preconditions=_to_rules(payload.get("preconditions", [])),
        action_spec=_to_action_spec(payload["action_spec"]),
        expected_postconditions=_to_rules(payload.get("expected_postconditions", [])),
        verification_rules=_to_rules(payload.get("verification_rules", [])),
        wait_conditions=_to_waits(payload.get("wait_conditions", [])),
        timeout=TimeoutPolicy(**payload.get("timeout", {})),
        retry=RetryPolicy(**payload.get("retry", {})),
        escalation=EscalationPolicy(**payload.get("escalation", {})),
        metadata=payload.get("metadata", {}),
    )


def _to_policy(payload: dict[str, Any]) -> SecurityPolicy:
    return SecurityPolicy(
        allow_domains=tuple(payload.get("allow_domains", [])),
        deny_domains=tuple(payload.get("deny_domains", [])),
        allow_custom_js=bool(payload.get("allow_custom_js", False)),
    )


async def get_engine() -> PredatorEngineV2:
    global _engine
    if _engine is None:
        _engine = PredatorEngineV2(
            audit_root_dir=os.getenv("PREDATOR_V2_AUDIT_DIR", "/tmp/predator-audit"),
            artifact_root_dir=os.getenv("PREDATOR_V2_ARTIFACT_DIR", "/tmp/predator-artifacts"),
            control_db_path=os.getenv("PREDATOR_V2_CONTROL_DB", "/tmp/predator-control-plane/control.db"),
            telemetry_dir=os.getenv("PREDATOR_V2_TELEMETRY_DIR", "/tmp/predator-telemetry"),
        )
        await _engine.initialize()
    return _engine


async def cleanup_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.close()
        _engine = None


server = Server("predator-browser-v2")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="v2_execute_action",
            description="Execute a deterministic ActionContract in Predator v2.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                    "policy": {"type": "object"},
                    "contract": {"type": "object"},
                },
                "required": ["tenant_id", "workflow_id", "policy", "contract"],
            },
        ),
        Tool(
            name="v2_verify_audit_chain",
            description="Verify hash-chain integrity for workflow audit records.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                },
                "required": ["tenant_id", "workflow_id"],
            },
        ),
        Tool(
            name="v2_get_replay_trace",
            description="Get workflow replay trace records from immutable audit trail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                },
                "required": ["tenant_id", "workflow_id"],
            },
        ),
        Tool(
            name="v2_get_health",
            description="Get control-plane and circuit breaker health snapshot.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="v2_open_tab",
            description="Open and switch to a new tab under workflow session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                    "policy": {"type": "object"},
                    "url": {"type": "string"},
                },
                "required": ["tenant_id", "workflow_id", "policy", "url"],
            },
        ),
        Tool(
            name="v2_switch_tab",
            description="Switch active tab for workflow session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "tab_id": {"type": "string"},
                },
                "required": ["workflow_id", "tab_id"],
            },
        ),
        Tool(
            name="v2_list_tabs",
            description="List tabs in workflow session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    engine = await get_engine()

    try:
        if name == "v2_execute_action":
            result = await engine.execute_contract(
                tenant_id=arguments["tenant_id"],
                workflow_id=arguments["workflow_id"],
                policy=_to_policy(arguments["policy"]),
                contract=_to_contract(arguments["contract"]),
            )
            return [TextContent(type="text", text=json.dumps(result.to_dict(), indent=2))]

        if name == "v2_verify_audit_chain":
            ok, reason = await engine.verify_audit_chain(
                tenant_id=arguments["tenant_id"],
                workflow_id=arguments["workflow_id"],
            )
            return [TextContent(type="text", text=json.dumps({"ok": ok, "reason": reason}, indent=2))]

        if name == "v2_get_replay_trace":
            trace = await engine.get_replay_trace(
                tenant_id=arguments["tenant_id"],
                workflow_id=arguments["workflow_id"],
            )
            return [TextContent(type="text", text=json.dumps(trace, indent=2))]

        if name == "v2_get_health":
            return [TextContent(type="text", text=json.dumps(engine.get_health(), indent=2))]

        if name == "v2_open_tab":
            tab_id = await engine.open_tab(
                tenant_id=arguments["tenant_id"],
                workflow_id=arguments["workflow_id"],
                policy=_to_policy(arguments["policy"]),
                url=arguments["url"],
            )
            return [TextContent(type="text", text=json.dumps({"tab_id": tab_id}, indent=2))]

        if name == "v2_switch_tab":
            await engine.switch_tab(workflow_id=arguments["workflow_id"], tab_id=arguments["tab_id"])
            return [TextContent(type="text", text=json.dumps({"ok": True}, indent=2))]

        if name == "v2_list_tabs":
            tabs = await engine.list_tabs(workflow_id=arguments["workflow_id"])
            return [TextContent(type="text", text=json.dumps(tabs, indent=2))]

        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as exc:
        logger.exception("v2 tool failure: %s", exc)
        return [TextContent(type="text", text=json.dumps({"error": str(exc), "tool": name}))]


async def main() -> None:
    logger.info("[Server-v2] Starting Predator v2 MCP Server...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await cleanup_engine()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
