from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
from urllib.parse import urlparse

from playwright.async_api import Frame, Page

from app.core.v2.network_observer import NetworkObserver
from app.core.v2.prompt_security import PromptInjectionFilter
from app.core.v2.state_models import (
    FormState,
    FrameState,
    InteractiveElementState,
    NetworkSummaryState,
    StructuredState,
    VisibleErrorState,
    estimate_tokens,
    stable_hash,
)


@dataclass(frozen=True)
class ExtractorBounds:
    max_frames: int = 8
    max_elements: int = 48
    max_forms: int = 6
    max_errors: int = 12


class StructuredStateExtractor:
    def __init__(
        self,
        page: Page,
        network_observer: NetworkObserver,
        bounds: ExtractorBounds | None = None,
        prompt_filter: PromptInjectionFilter | None = None,
    ) -> None:
        self._page = page
        self._network = network_observer
        self._bounds = bounds or ExtractorBounds()
        self._filter = prompt_filter or PromptInjectionFilter()

    def _short_hash(self, value: str) -> str:
        return blake2b(value.encode("utf-8"), digest_size=8).hexdigest()

    @property
    def network_sequence(self) -> int:
        return self._network.sequence

    def network_summary_since(self, seq: int) -> NetworkSummaryState:
        return self._network.summary_since(seq)

    def _fid(self, frame: Frame, parent_fid: str | None, index: int) -> str:
        seed = f"{parent_fid or 'root'}|{frame.url}|{index}"
        return f"f_{self._short_hash(seed)}"

    def _origin(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _extract_elements_for_frame(self, frame: Frame, fid: str) -> list[InteractiveElementState]:
        script = r"""
        () => {
          const selector = [
            'button', 'a[href]', 'input', 'select', 'textarea',
            '[role="button"]', '[role="link"]', '[role="textbox"]',
            '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
            '[tabindex]:not([tabindex="-1"])'
          ].join(',');

          const all = Array.from(document.querySelectorAll(selector));
          const out = [];
          const vw = Math.max(1, window.innerWidth || 1);
          const vh = Math.max(1, window.innerHeight || 1);

          for (const el of all) {
            if (out.length >= 120) break;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const visible = (
              rect.width > 2 && rect.height > 2 &&
              style.visibility !== 'hidden' &&
              style.display !== 'none' &&
              rect.bottom >= 0 && rect.right >= 0 && rect.top <= vh && rect.left <= vw
            );
            if (!visible) continue;

            const role = (el.getAttribute('role') || '').trim() || (el.tagName || '').toLowerCase();
            const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim();
            const tag = (el.tagName || '').toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            const enabled = !(el.disabled || el.getAttribute('aria-disabled') === 'true');
            const required = !!el.required;
            const checked = (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null;
            const valueHint = (el.value || '').toString().slice(0, 40) || null;

            const selectorHints = [];
            if (el.id) selectorHints.push('#' + CSS.escape(el.id));
            const testId = el.getAttribute('data-testid');
            if (testId) selectorHints.push('[data-testid="' + testId.replace(/"/g, '\\"') + '"]');
            const name = el.getAttribute('name');
            if (name) selectorHints.push(tag + '[name="' + name.replace(/"/g, '\\"') + '"]');
            const aria = el.getAttribute('aria-label');
            if (aria) selectorHints.push(tag + '[aria-label="' + aria.replace(/"/g, '\\"') + '"]');
            if ((tag === 'a' || tag === 'button') && text) {
              selectorHints.push(tag + ':has-text("' + text.slice(0, 60).replace(/"/g, '\\"') + '")');
            }

            out.push({
              role,
              nameShort: text.slice(0, 80),
              elementType: type || tag,
              enabled,
              visible,
              required,
              checked,
              valueHint,
              bboxNorm: [
                Number((Math.max(0, rect.x) / vw).toFixed(4)),
                Number((Math.max(0, rect.y) / vh).toFixed(4)),
                Number((Math.max(0, rect.width) / vw).toFixed(4)),
                Number((Math.max(0, rect.height) / vh).toFixed(4)),
              ],
              selectorHints
            });
          }

          return out;
        }
        """

        try:
            raw = await frame.evaluate(script)
        except Exception:
            return []

        elements: list[InteractiveElementState] = []
        for index, item in enumerate(raw):
            name_outcome = self._filter.sanitize(str(item.get("nameShort") or ""), max_len=80)
            value_outcome = self._filter.sanitize(str(item.get("valueHint") or ""), max_len=40)
            if name_outcome.redacted:
                self._redaction_count += 1
            if value_outcome.redacted:
                self._redaction_count += 1
            seed = f"{fid}|{item['role']}|{item['nameShort']}|{item['elementType']}|{index}"
            eid = f"e_{self._short_hash(seed)}"
            selector_hints = tuple(item.get("selectorHints") or ())
            selector_hint_id = f"sh_{self._short_hash('|'.join(selector_hints) if selector_hints else seed)}"
            elements.append(
                InteractiveElementState(
                    eid=eid,
                    fid=fid,
                    role=(item.get("role") or "unknown")[:32],
                    name_short=name_outcome.text,
                    element_type=(item.get("elementType") or "unknown")[:24],
                    enabled=bool(item.get("enabled", False)),
                    visible=bool(item.get("visible", False)),
                    required=bool(item.get("required", False)),
                    checked=item.get("checked"),
                    value_hint=(value_outcome.text or None),
                    bbox_norm=tuple(item.get("bboxNorm") or (0.0, 0.0, 0.0, 0.0)),
                    selector_hint_id=selector_hint_id,
                    stability_score=0.8 if selector_hints else 0.4,
                    selector_hints=selector_hints,
                )
            )
        return elements

    async def _extract_forms_for_frame(self, frame: Frame, fid: str) -> list[FormState]:
        script = r"""
        () => {
          const forms = Array.from(document.forms || []);
          const out = [];
          for (let i = 0; i < forms.length; i++) {
            if (out.length >= 24) break;
            const form = forms[i];
            const fields = Array.from(form.querySelectorAll('input,select,textarea'));
            const requiredMissing = fields.filter(f => f.required && !f.value).length;
            const invalid = fields.filter(f => f.getAttribute('aria-invalid') === 'true');
            const submit = form.querySelector('button[type="submit"],input[type="submit"]');
            out.push({
              localId: form.id || `form-${i}`,
              fieldKeys: fields.map((f, idx) => `${(f.tagName || '').toLowerCase()}:${f.name || f.id || idx}`).slice(0, 30),
              requiredMissing,
              submitKey: submit ? `${(submit.tagName || '').toLowerCase()}:${submit.id || submit.name || 'submit'}` : null,
              validationKeys: invalid.map((f, idx) => `${(f.tagName || '').toLowerCase()}:${f.name || f.id || idx}`).slice(0, 30)
            });
          }
          return out;
        }
        """
        try:
            raw = await frame.evaluate(script)
        except Exception:
            return []

        forms: list[FormState] = []
        for item in raw:
            form_seed = f"{fid}|{item['localId']}"
            form_id = f"form_{self._short_hash(form_seed)}"
            field_eids = tuple(f"e_{self._short_hash(f'{fid}|{field_key}')}" for field_key in item.get("fieldKeys", []))
            validation_eids = tuple(
                f"e_{self._short_hash(f'{fid}|{field_key}')}" for field_key in item.get("validationKeys", [])
            )
            submit_key = item.get("submitKey")
            submit_eid = f"e_{self._short_hash(f'{fid}|{submit_key}')}" if submit_key else None
            forms.append(
                FormState(
                    form_id=form_id,
                    fid=fid,
                    field_eids=field_eids,
                    required_missing_count=int(item.get("requiredMissing", 0)),
                    submit_eid=submit_eid,
                    validation_error_eids=validation_eids,
                )
            )
        return forms

    async def _extract_errors_for_frame(self, frame: Frame, fid: str) -> list[VisibleErrorState]:
        script = r"""
        () => {
          const selectors = [
            '[role="alert"]',
            '[aria-live="assertive"]',
            '.error',
            '.invalid-feedback',
            '.field-error',
            '.alert-danger'
          ].join(',');
          const out = [];
          for (const el of Array.from(document.querySelectorAll(selectors))) {
            if (out.length >= 40) break;
            const rect = el.getBoundingClientRect();
            if (rect.width < 2 || rect.height < 2) continue;
            const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!txt) continue;
            out.push({
              text: txt.slice(0, 120),
              kind: el.className && String(el.className).includes('alert') ? 'banner' : 'form',
            });
          }
          return out;
        }
        """
        try:
            raw = await frame.evaluate(script)
        except Exception:
            return []

        errors: list[VisibleErrorState] = []
        for index, item in enumerate(raw):
            text_outcome = self._filter.sanitize(str(item.get("text") or ""), max_len=120)
            if text_outcome.redacted:
                self._redaction_count += 1
            seed = f"{fid}|{item['kind']}|{item['text']}|{index}"
            errors.append(
                VisibleErrorState(
                    error_id=f"err_{self._short_hash(seed)}",
                    fid=fid,
                    kind=(item.get("kind") or "form")[:16],
                    text_short=text_outcome.text,
                    eid=None,
                )
            )
        return errors

    async def extract(self, prev_state_id: str | None, downloads: tuple[dict[str, str], ...]) -> StructuredState:
        page_phase = "unknown"
        try:
            page_phase = await self._page.evaluate("() => document.readyState")
        except Exception:
            pass

        frames: list[FrameState] = []
        elements: list[InteractiveElementState] = []
        forms: list[FormState] = []
        errors: list[VisibleErrorState] = []
        self._redaction_count = 0

        stack: list[tuple[Frame, str | None]] = [(self._page.main_frame, None)]
        frame_index = 0

        while stack and len(frames) < self._bounds.max_frames:
            frame, parent_fid = stack.pop(0)
            fid = self._fid(frame, parent_fid, frame_index)
            frame_index += 1

            frame_elements = await self._extract_elements_for_frame(frame, fid)
            frame_forms = await self._extract_forms_for_frame(frame, fid)
            frame_errors = await self._extract_errors_for_frame(frame, fid)

            frames.append(
                FrameState(
                    fid=fid,
                    parent_fid=parent_fid,
                    origin=self._origin(frame.url),
                    title_short="",
                    visible=True,
                    interactive_count=len(frame_elements),
                )
            )

            elements.extend(frame_elements)
            forms.extend(frame_forms)
            errors.extend(frame_errors)

            for child in frame.child_frames:
                stack.append((child, fid))

        frames.sort(key=lambda item: (item.parent_fid or "", item.origin, item.fid))
        elements.sort(key=lambda item: (item.fid, item.role, item.name_short, item.eid))
        forms.sort(key=lambda item: (item.fid, item.form_id))
        errors.sort(key=lambda item: (item.fid, item.kind, item.error_id))

        elements = elements[: self._bounds.max_elements]
        forms = forms[: self._bounds.max_forms]
        errors = errors[: self._bounds.max_errors]

        network_summary: NetworkSummaryState = self._network.summary_since(0)

        model_projection = {
            "url": self._page.url,
            "page_phase": page_phase,
            "frame_summary": [frame.__dict__ for frame in frames],
            "interactive_elements": [
                {
                    "eid": element.eid,
                    "fid": element.fid,
                    "role": element.role,
                    "name_short": element.name_short,
                    "type": element.element_type,
                    "enabled": element.enabled,
                    "visible": element.visible,
                    "required": element.required,
                    "checked": element.checked,
                    "value_hint": element.value_hint,
                    "bbox_norm": element.bbox_norm,
                    "selector_hint_id": element.selector_hint_id,
                    "stability_score": element.stability_score,
                }
                for element in elements
            ],
            "forms": [form.__dict__ for form in forms],
            "visible_errors": [error.__dict__ for error in errors],
            "network_summary": {
                "total_requests": network_summary.total_requests,
                "total_responses": network_summary.total_responses,
                "total_failures": network_summary.total_failures,
                "failures": [failure.__dict__ for failure in network_summary.failures],
            },
            "downloads": list(downloads),
        }

        section_hashes = {
            "frames": stable_hash(model_projection["frame_summary"]),
            "elements": stable_hash(model_projection["interactive_elements"]),
            "forms": stable_hash(model_projection["forms"]),
            "errors": stable_hash(model_projection["visible_errors"]),
            "network": stable_hash(model_projection["network_summary"]),
            "downloads": stable_hash(model_projection["downloads"]),
            "url": stable_hash(model_projection["url"]),
        }

        state_id = f"s_{stable_hash(section_hashes)}"

        budget_stats = {
            "estimated_tokens": estimate_tokens(model_projection),
            "element_count": len(elements),
            "frame_count": len(frames),
            "error_count": len(errors),
            "redaction_count": getattr(self, "_redaction_count", 0),
        }

        return StructuredState(
            state_id=state_id,
            prev_state_id=prev_state_id,
            url=self._page.url,
            page_phase=page_phase,
            frame_summary=tuple(frames),
            interactive_elements=tuple(elements),
            forms=tuple(forms),
            visible_errors=tuple(errors),
            network_summary=network_summary,
            downloads=downloads,
            state_hashes=section_hashes,
            budget_stats=budget_stats,
        )
