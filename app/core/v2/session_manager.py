from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.core.v2.control_plane_store import ControlPlaneStore
from app.core.v2.context_manager import TabManager
from app.core.v2.network_observer import NetworkObserver
from app.core.v2.security_layer import SecurityLayer, SecurityPolicy
from app.core.v2.telemetry import RuntimeTelemetryBuffer


@dataclass(frozen=True)
class SessionConfig:
    headless: bool = True
    viewport_width: int = 1440
    viewport_height: int = 900
    default_timeout_ms: int = 20_000
    max_total_sessions: int = 200
    session_acquire_timeout_ms: int = 300_000
    prewarmed_contexts: int = 8
    max_pooled_contexts: int = 64
    max_context_reuses: int = 50
    max_context_age_seconds: int = 1_800
    sandbox_enabled: bool = True
    service_workers_blocked: bool = True
    session_lease_ttl_seconds: int = 300
    extra_chromium_args: tuple[str, ...] = (
        "--disable-background-networking",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-breakpad",
        "--disable-component-update",
        "--disable-features=Translate,BackForwardCache",
    )


@dataclass
class BrowserSession:
    tenant_id: str
    workflow_id: str
    context: BrowserContext
    tab_manager: TabManager
    page: Page  # Active page pointer for convenience.
    network_observer: NetworkObserver
    runtime_telemetry: RuntimeTelemetryBuffer
    security_layer: SecurityLayer
    pooled_context: "PooledContext"


@dataclass
class PooledContext:
    context: BrowserContext
    tenant_id: str | None
    created_ts: float
    use_count: int = 0


class SessionManager:
    def __init__(
        self,
        config: SessionConfig | None = None,
        control_store: ControlPlaneStore | None = None,
    ) -> None:
        self._config = config or SessionConfig()
        self._store = control_store
        self._owner_id = control_store.owner_id() if control_store else "local-owner"
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._sessions: dict[str, BrowserSession] = {}
        self._pool: deque[PooledContext] = deque()
        self._session_slots = asyncio.Semaphore(self._config.max_total_sessions)

    async def initialize(self) -> None:
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        launch_options = {
            "headless": self._config.headless,
            "args": list(self._config.extra_chromium_args),
            "chromium_sandbox": self._config.sandbox_enabled,
        }
        if not self._config.sandbox_enabled:
            launch_options["args"].extend(["--no-sandbox", "--disable-setuid-sandbox"])
        self._browser = await self._playwright.chromium.launch(**launch_options)
        await self._prewarm_pool()

    async def _new_context(self) -> BrowserContext:
        if not self._browser:
            raise RuntimeError("Browser not initialized")
        return await self._browser.new_context(
            viewport={"width": self._config.viewport_width, "height": self._config.viewport_height},
            service_workers="block" if self._config.service_workers_blocked else "allow",
            accept_downloads=True,
        )

    async def _prewarm_pool(self) -> None:
        target = max(0, min(self._config.prewarmed_contexts, self._config.max_pooled_contexts))
        while len(self._pool) < target:
            context = await self._new_context()
            pooled = PooledContext(
                context=context,
                tenant_id=None,
                created_ts=time.time(),
                use_count=0,
            )
            self._pool.append(pooled)

    async def _acquire_context(self, tenant_id: str) -> PooledContext:
        for pooled in list(self._pool):
            if pooled.tenant_id not in (None, tenant_id):
                continue
            self._pool.remove(pooled)
            pooled.tenant_id = tenant_id
            pooled.use_count += 1
            return pooled

        context = await self._new_context()
        return PooledContext(
            context=context,
            tenant_id=tenant_id,
            created_ts=time.time(),
            use_count=1,
        )

    async def _acquire_session_slot(self) -> None:
        timeout_s = max(0.001, self._config.session_acquire_timeout_ms / 1000.0)
        try:
            await asyncio.wait_for(self._session_slots.acquire(), timeout=timeout_s)
        except TimeoutError as exc:
            raise RuntimeError("GLOBAL_SESSION_LIMIT") from exc

    async def _reset_context(self, context: BrowserContext) -> bool:
        try:
            await context.clear_permissions()
        except Exception:
            pass
        try:
            await context.clear_cookies()
        except Exception:
            pass

        try:
            pages = list(context.pages)
            if not pages:
                pages = [await context.new_page()]

            for page in pages[1:]:
                try:
                    await page.close()
                except Exception:
                    pass

            primary = pages[0]
            try:
                await primary.goto(
                    "about:blank",
                    wait_until="domcontentloaded",
                    timeout=self._config.default_timeout_ms,
                )
            except Exception:
                try:
                    await primary.close()
                except Exception:
                    pass
                primary = await context.new_page()
                await primary.goto(
                    "about:blank",
                    wait_until="domcontentloaded",
                    timeout=self._config.default_timeout_ms,
                )

            await primary.evaluate(
                "() => {"
                "try { localStorage.clear(); } catch (_) {}"
                "try { sessionStorage.clear(); } catch (_) {}"
                "}"
            )
            await primary.evaluate(
                "() => {"
                "if (!('indexedDB' in window) || typeof indexedDB.databases !== 'function') return Promise.resolve();"
                "return indexedDB.databases().then((dbs) => Promise.all((dbs || []).map((db) => new Promise((resolve) => {"
                "try {"
                "const req = indexedDB.deleteDatabase(db.name);"
                "req.onsuccess = () => resolve(true);"
                "req.onerror = () => resolve(false);"
                "req.onblocked = () => resolve(false);"
                "} catch (_) { resolve(false); }"
                "})));"
                "}"
            )
            return True
        except Exception:
            return False

    def _should_retire(self, pooled: PooledContext) -> bool:
        age = time.time() - pooled.created_ts
        if pooled.use_count >= self._config.max_context_reuses:
            return True
        if age >= self._config.max_context_age_seconds:
            return True
        return False

    async def _release_context(self, pooled: PooledContext) -> None:
        if self._should_retire(pooled):
            await pooled.context.close()
            return

        reset_ok = await self._reset_context(pooled.context)
        if not reset_ok:
            await pooled.context.close()
            return

        if len(self._pool) >= self._config.max_pooled_contexts:
            await pooled.context.close()
            return
        pooled.tenant_id = None
        self._pool.append(pooled)

    async def close(self) -> None:
        for workflow_id in list(self._sessions.keys()):
            await self.close_session(workflow_id)
        while self._pool:
            pooled = self._pool.popleft()
            await pooled.context.close()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def get_or_create_session(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
    ) -> BrowserSession:
        if workflow_id in self._sessions:
            if self._store:
                self._store.heartbeat_session_lease(workflow_id=workflow_id, owner_id=self._owner_id)
            return self._sessions[workflow_id]
        await self._acquire_session_slot()
        lease_acquired = False

        if self._store:
            acquired = self._store.acquire_session_lease(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                owner_id=self._owner_id,
                lease_ttl_seconds=self._config.session_lease_ttl_seconds,
            )
            if not acquired:
                self._session_slots.release()
                raise RuntimeError("SESSION_LEASE_NOT_ACQUIRED")
            lease_acquired = True

        try:
            if not self._browser:
                await self.initialize()
            if not self._browser:
                raise RuntimeError("Browser initialization failed")

            pooled = await self._acquire_context(tenant_id=tenant_id)
            context = pooled.context
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(self._config.default_timeout_ms)
            tab_manager = TabManager(context=context, initial_page=page)

            network_observer = NetworkObserver()
            await network_observer.attach(page)
            runtime_telemetry = RuntimeTelemetryBuffer()
            await runtime_telemetry.attach(page)

            session = BrowserSession(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                context=context,
                tab_manager=tab_manager,
                page=page,
                network_observer=network_observer,
                runtime_telemetry=runtime_telemetry,
                security_layer=SecurityLayer(policy),
                pooled_context=pooled,
            )
            self._sessions[workflow_id] = session
            return session
        except Exception:
            if lease_acquired and self._store:
                self._store.release_session_lease(workflow_id=workflow_id, owner_id=self._owner_id)
            self._session_slots.release()
            raise

    async def close_session(self, workflow_id: str) -> None:
        session = self._sessions.pop(workflow_id, None)
        if not session:
            return
        await session.runtime_telemetry.detach()
        await session.network_observer.detach()
        await self._release_context(session.pooled_context)
        self._session_slots.release()
        if self._store:
            self._store.release_session_lease(workflow_id=workflow_id, owner_id=self._owner_id)

    def active_session_count_for_tenant(self, tenant_id: str) -> int:
        if self._store:
            return self._store.count_active_sessions(
                tenant_id=tenant_id,
                lease_ttl_seconds=self._config.session_lease_ttl_seconds,
            )
        return sum(1 for session in self._sessions.values() if session.tenant_id == tenant_id)

    def has_session(self, workflow_id: str) -> bool:
        return workflow_id in self._sessions

    def get_session(self, workflow_id: str) -> BrowserSession:
        if workflow_id not in self._sessions:
            raise KeyError(f"Unknown workflow session: {workflow_id}")
        if self._store:
            self._store.heartbeat_session_lease(workflow_id=workflow_id, owner_id=self._owner_id)
        return self._sessions[workflow_id]

    def total_active_sessions(self) -> int:
        if self._store:
            return self._store.count_all_active_sessions(
                lease_ttl_seconds=self._config.session_lease_ttl_seconds
            )
        return len(self._sessions)

    def pooled_context_count(self) -> int:
        return len(self._pool)
