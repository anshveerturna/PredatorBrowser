from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import ConsoleMessage, Page


@dataclass
class TimelineEvent:
    phase: str
    ts: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Telemetry:
    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._timeline: list[TimelineEvent] = []
        self._counters: dict[str, int] = {
            "console_count": 0,
            "pageerror_count": 0,
            "network_error_count": 0,
        }

    def event(self, phase: str, metadata: dict[str, Any] | None = None) -> None:
        self._timeline.append(
            TimelineEvent(
                phase=phase,
                ts=datetime.now(tz=timezone.utc).isoformat(),
                metadata=metadata or {},
            )
        )

    def incr(self, counter: str, value: int = 1) -> None:
        self._counters[counter] = self._counters.get(counter, 0) + value

    def snapshot(self) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - self._start) * 1000)
        return {
            "elapsed_ms": elapsed_ms,
            "counters": dict(self._counters),
            "timeline": [
                {"phase": event.phase, "ts": event.ts, "metadata": event.metadata}
                for event in self._timeline
            ],
        }


@dataclass(frozen=True)
class RuntimeEvent:
    seq: int
    ts: str
    kind: str
    message: str


class RuntimeTelemetryBuffer:
    def __init__(self, max_events: int = 256) -> None:
        self._events: list[RuntimeEvent] = []
        self._max_events = max_events
        self._seq = 0
        self._page: Page | None = None

    async def attach(self, page: Page) -> None:
        self._page = page
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    async def detach(self) -> None:
        if not self._page:
            return
        self._page.remove_listener("console", self._on_console)
        self._page.remove_listener("pageerror", self._on_page_error)
        self._page = None

    @property
    def sequence(self) -> int:
        return self._seq

    def _push(self, kind: str, message: str) -> None:
        self._seq += 1
        self._events.append(
            RuntimeEvent(
                seq=self._seq,
                ts=datetime.now(tz=timezone.utc).isoformat(),
                kind=kind,
                message=message[:240],
            )
        )
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

    def _on_console(self, message: ConsoleMessage) -> None:
        self._push("console", f"{message.type}: {message.text}")

    def _on_page_error(self, error: Any) -> None:
        self._push("pageerror", str(error))

    def events_since(self, seq: int) -> list[RuntimeEvent]:
        return [event for event in self._events if event.seq > seq]
