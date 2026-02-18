from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Optional

from playwright.async_api import Download, Page


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    workflow_id: str
    action_id: str
    path: str
    mime: str
    size: int
    sha256: str


class ArtifactManager:
    def __init__(self, root_dir: str = "/tmp/predator-artifacts") -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, ArtifactRecord] = {}

    def _workflow_dir(self, workflow_id: str) -> Path:
        safe = workflow_id.replace("/", "_")
        path = self._root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register_existing_upload(self, workflow_id: str, action_id: str, source_path: str) -> ArtifactRecord:
        src = Path(source_path)
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(source_path)

        digest = self._sha256(src)
        artifact_id = f"up_{digest[:20]}"
        record = ArtifactRecord(
            artifact_id=artifact_id,
            workflow_id=workflow_id,
            action_id=action_id,
            path=str(src),
            mime="application/octet-stream",
            size=src.stat().st_size,
            sha256=digest,
        )
        self._records[artifact_id] = record
        return record

    def _sha256(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    async def save_download(
        self,
        workflow_id: str,
        action_id: str,
        download: Download,
    ) -> ArtifactRecord:
        workflow_dir = self._workflow_dir(workflow_id)
        suggested = download.suggested_filename or "download.bin"
        target_path = workflow_dir / suggested
        await download.save_as(str(target_path))

        digest = self._sha256(target_path)
        artifact_id = f"dl_{digest[:20]}"
        record = ArtifactRecord(
            artifact_id=artifact_id,
            workflow_id=workflow_id,
            action_id=action_id,
            path=str(target_path),
            mime="application/octet-stream",
            size=target_path.stat().st_size,
            sha256=digest,
        )
        self._records[artifact_id] = record
        return record

    @asynccontextmanager
    async def expect_download(self, page: Page) -> AsyncGenerator[object, None]:
        async with page.expect_download() as dl_info:
            yield dl_info

    def get_record(self, artifact_id: str) -> Optional[ArtifactRecord]:
        return self._records.get(artifact_id)

    def list_workflow_records(self, workflow_id: str) -> list[ArtifactRecord]:
        return [record for record in self._records.values() if record.workflow_id == workflow_id]

    def purge_workflow(self, workflow_id: str) -> None:
        workflow_dir = self._workflow_dir(workflow_id)
        for path in workflow_dir.glob("**/*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        if workflow_dir.exists():
            for child in workflow_dir.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            try:
                workflow_dir.rmdir()
            except OSError:
                pass

        self._records = {
            artifact_id: record
            for artifact_id, record in self._records.items()
            if record.workflow_id != workflow_id
        }
