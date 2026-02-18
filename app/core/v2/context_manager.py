from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import BrowserContext, Page


@dataclass(frozen=True)
class TabInfo:
    tab_id: str
    url: str
    title: str
    is_active: bool


class TabManager:
    def __init__(self, context: BrowserContext, initial_page: Page) -> None:
        self._context = context
        self._pages: dict[str, Page] = {}
        self._active_tab_id = self._register_page(initial_page)

    def _register_page(self, page: Page) -> str:
        seed = f"{id(page)}:{len(self._pages)}"
        tab_id = f"tab_{abs(hash(seed))}"
        self._pages[tab_id] = page
        return tab_id

    async def open_tab(self, url: str) -> str:
        page = await self._context.new_page()
        tab_id = self._register_page(page)
        await page.goto(url, wait_until="domcontentloaded")
        self._active_tab_id = tab_id
        return tab_id

    def list_tab_ids(self) -> list[str]:
        return list(self._pages.keys())

    def get_page(self, tab_id: str | None = None) -> Page:
        key = tab_id or self._active_tab_id
        if key not in self._pages:
            raise KeyError(f"Unknown tab_id: {key}")
        return self._pages[key]

    def set_active_tab(self, tab_id: str) -> None:
        if tab_id not in self._pages:
            raise KeyError(f"Unknown tab_id: {tab_id}")
        self._active_tab_id = tab_id

    @property
    def active_tab_id(self) -> str:
        return self._active_tab_id

    async def list_tabs(self) -> list[TabInfo]:
        tabs: list[TabInfo] = []
        for tab_id, page in self._pages.items():
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append(
                TabInfo(
                    tab_id=tab_id,
                    url=page.url,
                    title=title,
                    is_active=(tab_id == self._active_tab_id),
                )
            )
        return tabs
