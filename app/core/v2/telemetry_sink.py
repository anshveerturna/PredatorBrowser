from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TelemetrySink:
    async def emit(self, event: dict[str, Any]) -> None:
        raise NotImplementedError


class NullTelemetrySink(TelemetrySink):
    async def emit(self, event: dict[str, Any]) -> None:
        return None


class JsonlTelemetrySink(TelemetrySink):
    def __init__(self, root_dir: str = "/tmp/predator-telemetry") -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._file = self._root / "events.jsonl"

    async def emit(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self._file.open("a", encoding="utf-8") as file_handle:
            file_handle.write(payload + "\n")
