from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Frame, Locator, Page

from app.core.v2.contracts import ActionSpec
from app.core.v2.state_models import StructuredState


@dataclass(frozen=True)
class BoundTarget:
    eid: str | None
    fid: str | None
    selector: str
    confidence: float


class Navigator:
    def __init__(self, page: Page) -> None:
        self._page = page

    def _frame_by_fid(self, state: StructuredState, fid: str | None) -> Frame:
        if not fid:
            return self._page.main_frame

        fid_to_origin = {frame.fid: frame.origin for frame in state.frame_summary}
        origin = fid_to_origin.get(fid)
        if not origin:
            return self._page.main_frame

        for frame in self._page.frames:
            if frame.url.startswith(origin):
                return frame
        return self._page.main_frame

    def _selector_from_eid(self, state: StructuredState, eid: str) -> tuple[str | None, str | None]:
        for element in state.interactive_elements:
            if element.eid != eid:
                continue
            if element.selector_hints:
                return element.selector_hints[0], element.fid

            role = element.role
            name = element.name_short
            if role and name:
                return f'role={role}[name="{name}"]', element.fid
            if name:
                return f'text="{name}"', element.fid
            break
        return None, None

    def bind_target(self, action_spec: ActionSpec, state: StructuredState) -> BoundTarget:
        if action_spec.selector:
            return BoundTarget(eid=action_spec.target_eid, fid=action_spec.target_fid, selector=action_spec.selector, confidence=1.0)

        if action_spec.target_eid:
            selector, fid = self._selector_from_eid(state, action_spec.target_eid)
            if selector:
                return BoundTarget(eid=action_spec.target_eid, fid=fid, selector=selector, confidence=0.9)

        if action_spec.selector_candidates:
            return BoundTarget(eid=action_spec.target_eid, fid=action_spec.target_fid, selector=action_spec.selector_candidates[0], confidence=0.7)

        raise ValueError("Unable to bind target selector")

    def locator_for_target(self, target: BoundTarget, state: StructuredState) -> Locator:
        frame = self._frame_by_fid(state=state, fid=target.fid)
        return frame.locator(target.selector).first
