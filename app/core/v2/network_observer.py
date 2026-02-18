from __future__ import annotations

import json
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from typing import Any, Deque, Optional
from urllib.parse import urlparse

from playwright.async_api import Page, Request, Response

from app.core.v2.state_models import NetworkFailureState, NetworkSummaryState


@dataclass(frozen=True)
class NetworkEvent:
    seq: int
    ts: str
    kind: str
    method: str
    url: str
    route_key: str
    status: int | None = None
    latency_ms: int | None = None
    status_class: str | None = None
    content_type: str | None = None
    json_shape_hash: str | None = None
    silent_failure: bool = False
    error_signature: str | None = None


class NetworkObserver:
    def __init__(self, max_events: int = 256) -> None:
        self._page: Optional[Page] = None
        self._events: Deque[NetworkEvent] = deque(maxlen=max_events)
        self._active = False
        self._seq = 0
        self._request_starts: dict[Request, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def sequence(self) -> int:
        return self._seq

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def attach(self, page: Page) -> None:
        if self._active:
            return
        self._page = page
        self._active = True
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    async def detach(self) -> None:
        if not self._page:
            return
        self._page.remove_listener("request", self._on_request)
        self._page.remove_listener("response", self._on_response)
        self._page.remove_listener("requestfailed", self._on_request_failed)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._active = False
        self._page = None

    def _route_key(self, url: str) -> str:
        parsed = urlparse(url)
        chunks = [p for p in parsed.path.split("/") if p]
        route = "/" + "/".join(chunks[:2]) if chunks else "/"
        return f"{parsed.netloc}{route}"

    def _status_class(self, status: int | None) -> str:
        if status is None:
            return "none"
        return f"{status // 100}xx"

    def _json_shape_hash(self, payload: Any) -> str:
        def walk(obj: Any, depth: int = 0) -> Any:
            if depth > 2:
                return "..."
            if isinstance(obj, dict):
                return {k: walk(v, depth + 1) for k, v in list(sorted(obj.items()))[:12]}
            if isinstance(obj, list):
                return [walk(obj[0], depth + 1)] if obj else []
            return type(obj).__name__

        shaped = walk(payload)
        blob = json.dumps(shaped, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return blake2b(blob.encode("utf-8"), digest_size=8).hexdigest()

    def _silent_failure(self, payload: Any) -> tuple[bool, str | None]:
        if isinstance(payload, dict):
            if isinstance(payload.get("success"), bool) and payload.get("success") is False:
                return True, "json_success_false"
            if isinstance(payload.get("error"), (str, dict, list)):
                return True, "json_error_present"
            if isinstance(payload.get("errors"), list) and payload.get("errors"):
                return True, "json_errors_nonempty"
        return False, None

    def _track_task(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_request(self, request: Request) -> None:
        self._request_starts[request] = datetime.now(tz=timezone.utc).timestamp()
        self._events.append(
            NetworkEvent(
                seq=self._next_seq(),
                ts=datetime.now(tz=timezone.utc).isoformat(),
                kind="request",
                method=request.method,
                url=request.url,
                route_key=self._route_key(request.url),
            )
        )

    def _on_response(self, response: Response) -> None:
        self._track_task(self._handle_response(response))

    async def _handle_response(self, response: Response) -> None:
        request = response.request
        start = self._request_starts.pop(request, None)
        latency_ms = None
        if start is not None:
            latency_ms = int((datetime.now(tz=timezone.utc).timestamp() - start) * 1000)

        content_type = response.headers.get("content-type", "")
        shape_hash = None
        silent_failure = False
        error_signature = None

        if "application/json" in content_type:
            try:
                payload = await response.json()
                shape_hash = self._json_shape_hash(payload)
                silent_failure, error_signature = self._silent_failure(payload)
            except Exception:
                error_signature = "json_parse_error"
                silent_failure = True

        self._events.append(
            NetworkEvent(
                seq=self._next_seq(),
                ts=datetime.now(tz=timezone.utc).isoformat(),
                kind="response",
                method=request.method,
                url=response.url,
                route_key=self._route_key(response.url),
                status=response.status,
                latency_ms=latency_ms,
                status_class=self._status_class(response.status),
                content_type=content_type,
                json_shape_hash=shape_hash,
                silent_failure=silent_failure,
                error_signature=error_signature,
            )
        )

    def _on_request_failed(self, request: Request) -> None:
        self._request_starts.pop(request, None)
        failure = request.failure
        self._events.append(
            NetworkEvent(
                seq=self._next_seq(),
                ts=datetime.now(tz=timezone.utc).isoformat(),
                kind="request_failed",
                method=request.method,
                url=request.url,
                route_key=self._route_key(request.url),
                error_signature=(failure or {}).get("errorText", "request_failed"),
            )
        )

    def events_since(self, seq: int) -> list[NetworkEvent]:
        return [event for event in self._events if event.seq > seq]

    def summary_since(self, seq: int) -> NetworkSummaryState:
        events = self.events_since(seq)
        requests = [event for event in events if event.kind == "request"]
        responses = [event for event in events if event.kind == "response"]
        failures: list[NetworkFailureState] = []

        for event in responses:
            if (event.status is not None and event.status >= 400) or event.silent_failure:
                failures.append(
                    NetworkFailureState(
                        route_key=event.route_key,
                        status=event.status or 0,
                        status_class=event.status_class or "none",
                        error_signature=event.error_signature or "response_failure",
                        latency_ms=event.latency_ms or 0,
                    )
                )

        for event in events:
            if event.kind == "request_failed":
                failures.append(
                    NetworkFailureState(
                        route_key=event.route_key,
                        status=0,
                        status_class="none",
                        error_signature=event.error_signature or "request_failed",
                        latency_ms=0,
                    )
                )

        return NetworkSummaryState(
            total_requests=len(requests),
            total_responses=len(responses),
            total_failures=len(failures),
            failures=tuple(failures[:20]),
        )
