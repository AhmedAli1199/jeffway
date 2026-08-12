"""
Fill JIS SmartForm appointment sections Q7.1–Q11.2 (Trade | Hours | Notes per visit).

After Add, EasyBOP opens an ion-modal with Trade, Hours, Notes on one screen (motion::div_section_0).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

from appointment_loader import appointment_slots_from_row

log = logging.getLogger("easybop.jis.appointments")

def _rich_text_html(val: str) -> str:
    parts = val.replace("\r\n", "\n").split("\n")
    return "<br>".join(html.escape(p) for p in parts)


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _q_section_label_pattern(q_section: str) -> re.Pattern[str]:
    s = (q_section or "").strip()
    core = s[1:].strip() if s.upper().startswith("Q") else s
    return re.compile(rf"\bQ\s*{re.escape(core)}\b", re.I)


async def _smart_form_root(page: Page) -> Any:
    form = page.locator("form#smartForm-form")
    if await form.count() > 0:
        return form.first
    return page


async def _click_toolbar_all(page: Page) -> None:
    for sel in (
        page.locator("ion-footer ion-button").filter(has_text=re.compile(r"^\s*all\s*$", re.I)),
        page.locator("ion-toolbar ion-button").filter(has_text=re.compile(r"^\s*all\s*$", re.I)),
    ):
        if await sel.count() == 0:
            continue
        btn = sel.first
        try:
            if await btn.is_visible():
                await btn.click(timeout=5_000)
                await asyncio.sleep(0.5)
                log.info("jis appointments: clicked All")
                return
        except Exception:
            continue


async def _ensure_form_expanded(page: Page) -> Any:
    ctx = await _smart_form_root(page)
    if await ctx.locator("ion-label").filter(has_text=_q_section_label_pattern("7.1")).count() > 0:
        return ctx
    await _click_toolbar_all(page)
    await asyncio.sleep(0.4)
    return ctx


async def _footer_next(ctx: Any) -> bool:
    footer = ctx.locator("ion-footer")
    if await footer.count() == 0:
        return False
    for pattern in (re.compile(r"^\s*next\s*$", re.I), re.compile(r"^\s*forward\s*$", re.I)):
        btn = footer.get_by_role("button", name=pattern)
        if await btn.count() > 0:
            try:
                b = btn.first
                if await b.is_visible():
                    await b.click()
                    await asyncio.sleep(0.45)
                    return True
            except Exception:
                pass
    return False


async def _find_appointment_details_block(ctx: Any, q_section: str, timeout_ms: int) -> Optional[Any]:
    """Q7.1 / Q8.1 card with prop-data-set Add — not the Trade sub-label."""
    pat = _q_section_label_pattern(q_section)
    for attempt in range(24):
        labels = ctx.locator("ion-label").filter(has_text=pat)
        n = await labels.count()
        for i in range(n):
            lab = labels.nth(i)
            try:
                txt = (await lab.inner_text() or "").strip()
            except Exception:
                continue
            if "appointment details" not in txt.lower():
                continue
            try:
                await lab.scroll_into_view_if_needed(timeout=min(timeout_ms, 10_000))
            except Exception:
                pass
            block = lab.locator('xpath=ancestor::div[starts-with(@id, "dcl-question")][1]')
            if await block.count() == 0:
                block = lab.locator("xpath=ancestor::ion-card[1]")
            if await block.count() > 0 and await block.first.locator("prop-data-set").count() > 0:
                return block.first
        if attempt < 23 and not await _footer_next(ctx):
            await asyncio.sleep(0.25)
    return None


async def _click_add_on_block(block: Any) -> bool:
    ds = block.locator("prop-data-set").first
    if await ds.count() == 0:
        return False
    add = ds.locator("ion-button").filter(has_text=re.compile(r"\badd\b", re.I))
    if await add.count() == 0:
        return False
    btn = add.first
    await btn.scroll_into_view_if_needed(timeout=8_000)
    await btn.click(timeout=8_000)
    return True


async def _wait_appointment_modal(page: Page, timeout_ms: int) -> Any:
    """Add opens ion-modal; Trade/Hours/Notes live inside it."""
    modal = page.locator("ion-modal").filter(
        has=page.locator("ion-label", has_text=re.compile(r"^\s*Trade\s*$", re.I))
    )
    if await modal.count() == 0:
        modal = page.locator("ion-modal").last
    m = modal.first
    await m.wait_for(state="visible", timeout=min(timeout_ms, 15_000))
    await asyncio.sleep(0.35)
    return m


async def _fill_rich_text_element(rich: Any, value: str) -> None:
    await rich.scroll_into_view_if_needed(timeout=8_000)
    await rich.click(timeout=5_000)
    try:
        await rich.fill(value)
    except Exception:
        await rich.evaluate(
            """(el, html) => {
              el.innerHTML = html;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.blur();
            }""",
            _rich_text_html(value),
        )
    await asyncio.sleep(0.25)


async def _fill_exact_label_in_scope(scope: Any, label: str, value: str) -> bool:
    """Fill rich-text for ion-label exactly matching Trade / Hours / Notes."""
    if not value:
        return True
    want = _norm_label(label)
    labels = scope.locator("ion-label")
    n = await labels.count()
    for i in range(n):
        lab = labels.nth(i)
        try:
            txt = _norm_label(await lab.inner_text())
        except Exception:
            continue
        if txt != want:
            continue
        block = lab.locator('xpath=ancestor::div[starts-with(@id, "dcl-question")][1]')
        if await block.count() == 0:
            block = lab.locator("xpath=ancestor::ion-card[1]")
        target = block.first if await block.count() > 0 else lab
        rich = target.locator("rich-text div[contenteditable='true']").first
        if await rich.count() == 0:
            return False
        await _fill_rich_text_element(rich, value)
        return True
    return False


async def _fill_trade_hours_notes_modal(modal: Any, trade: str, hours: str, notes: str, errors: List[str]) -> bool:
    ok = True
    for label, val in (("Trade", trade), ("Hours", hours), ("Notes", notes)):
        if not val:
            continue
        if not await _fill_exact_label_in_scope(modal, label, val):
            errors.append(f"[appointment {label}] not found in Add modal")
            ok = False
    return ok


async def _try_click_locator(loc: Any, *, label: str, timeout_ms: int = 8_000) -> bool:
    try:
        if await loc.count() == 0:
            return False
        btn = loc.first
        if not await btn.is_visible():
            return False
        await btn.scroll_into_view_if_needed(timeout=min(5_000, timeout_ms))
        for click_mode in ("normal", "force", "js"):
            try:
                if click_mode == "normal":
                    await btn.click(timeout=timeout_ms)
                elif click_mode == "force":
                    await btn.click(timeout=timeout_ms, force=True)
                else:
                    await btn.evaluate("el => el.click()")
                log.info("jis appointments: clicked %s (%s)", label, click_mode)
                return True
            except Exception as exc:
                log.warning("jis appointments: %s click (%s) failed: %s", label, click_mode, exc)
    except Exception as exc:
        log.warning("jis appointments: %s failed: %s", label, exc)
    return False


async def _click_appointment_modal_save(page: Page, modal: Any) -> bool:
    """After Trade/Hours/Notes, click block Save inside the Add modal."""
    save_re = re.compile(r"^\s*save\s*$", re.I)
    log.info("jis appointments: Save in Add modal")
    for i, loc in enumerate(
        (
            modal.locator("ion-button[expand='block']").filter(has_text=save_re),
            modal.locator("ion-button.button-block").filter(has_text=save_re),
            modal.locator("ion-button.ion-color-success").filter(has_text=save_re),
            modal.locator("ion-footer ion-button").filter(has_text=save_re),
            modal.locator("ion-button").filter(has_text=save_re),
            page.locator("ion-modal:not([style*='display: none']) ion-button[expand='block']").filter(
                has_text=save_re
            ),
        )
    ):
        if await _try_click_locator(loc, label=f"modal Save [{i}]", timeout_ms=8_000):
            await asyncio.sleep(0.45)
            try:
                await modal.wait_for(state="hidden", timeout=8_000)
            except Exception:
                pass
            return True
    return False


async def _click_modal_back(page: Page, modal: Any) -> bool:
    for loc in (
        modal.locator("ion-toolbar ion-buttons[slot='start'] ion-button"),
        modal.locator("prop-icon[name='back']").locator("xpath=ancestor::ion-button[1]"),
        page.locator("ion-modal prop-icon[name='back']").locator("xpath=ancestor::ion-button[1]"),
        page.locator("ion-toolbar ion-buttons[slot='start'] ion-button").first,
    ):
        if await loc.count() == 0:
            continue
        try:
            btn = loc.first
            if await btn.is_visible():
                await btn.click(timeout=8_000)
                await asyncio.sleep(0.5)
                try:
                    await modal.wait_for(state="hidden", timeout=8_000)
                except Exception:
                    pass
                return True
        except Exception:
            continue
    return False


def _norm_option(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


async def _ion_item_selected(it: Any) -> bool:
    try:
        icon = it.locator("prop-icon").first
        if await icon.count() == 0:
            return False
        cls = (await icon.get_attribute("class") or "").lower()
        return "icon-primary" in cls
    except Exception:
        return False


async def _fill_yes_no_question(ctx: Any, q_yn: str, answer: str, errors: List[str]) -> bool:
    yn = (answer or "no").strip().lower()
    choice = "Yes" if yn in ("yes", "y", "true", "1") else "No"
    want = _norm_option(choice)
    pat = _q_section_label_pattern(q_yn)
    for _ in range(20):
        labels = ctx.locator("ion-label").filter(has_text=pat)
        n = await labels.count()
        for i in range(n):
            lab = labels.nth(i)
            try:
                txt = (await lab.inner_text() or "").lower()
            except Exception:
                continue
            if "another appointment" not in txt:
                continue
            try:
                await lab.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            block = lab.locator('xpath=ancestor::div[starts-with(@id, "dcl-question")][1]')
            if await block.count() == 0:
                block = lab.locator('xpath=ancestor::div[starts-with(@id, "dcl-question")][1]')
            if await block.count() == 0:
                continue
            items = block.first.locator('ion-item[role="listitem"]')
            ni = await items.count()
            for j in range(ni):
                it = items.nth(j)
                try:
                    tnorm = _norm_option(await it.inner_text())
                except Exception:
                    continue
                if want == tnorm or want in tnorm:
                    if await _ion_item_selected(it):
                        return True
                    await it.scroll_into_view_if_needed(timeout=5_000)
                    await it.click(timeout=5_000)
                    await asyncio.sleep(0.35)
                    return True
        if not await _footer_next(ctx):
            errors.append(f"[appointment Q{q_yn}] Yes/No not found")
            return False
    return False


async def _fill_one_appointment_slot(
    page: Page,
    ctx: Any,
    slot: Dict[str, str],
    errors: List[str],
    timeout_ms: int,
) -> bool:
    q_section = slot["q_section"]
    block = await _find_appointment_details_block(ctx, q_section, timeout_ms)
    if block is None:
        errors.append(f"[appointment Q{q_section}] Appointment details / Add block not found")
        return False

    if not await _click_add_on_block(block):
        errors.append(f"[appointment Q{q_section}] Add button not found")
        return False

    try:
        modal = await _wait_appointment_modal(page, timeout_ms)
    except Exception as exc:
        errors.append(f"[appointment Q{q_section}] Add modal did not open: {exc}")
        return False

    ok = await _fill_trade_hours_notes_modal(
        modal, slot["trade"], slot["hours"], slot["notes"], errors
    )

    if not await _click_appointment_modal_save(page, modal):
        log.warning("jis appointments: Q%s modal Save not found — trying back", q_section)
        if not await _click_modal_back(page, modal):
            errors.append(f"[appointment Q{q_section}] modal Save/back not found")
            ok = False
    else:
        await asyncio.sleep(0.35)

    ctx = await _smart_form_root(page)
    if not await _fill_yes_no_question(ctx, slot["q_yn"], slot["another"], errors):
        errors.append(f"[appointment Q{slot['q_yn']}] Yes/No not set")
        ok = False

    log.info(
        "jis appointments: Q%s trade=%r hours=%r yn=%s ok=%s",
        q_section,
        slot["trade"][:40],
        slot["hours"],
        slot["another"],
        ok,
    )
    return ok


async def fill_appointments_on_smart_form(
    page: Page,
    appointment_row: Dict[str, Optional[str]],
    *,
    timeout_ms: int = 25_000,
) -> tuple[List[str], int]:
    errors: List[str] = []
    slots = appointment_slots_from_row(appointment_row)
    if not slots:
        log.info("jis appointments: no appointment slots in row — skipping")
        return errors, 0

    ctx = await _ensure_form_expanded(page)
    filled = 0
    for slot in slots:
        if await _fill_one_appointment_slot(page, ctx, slot, errors, timeout_ms):
            filled += 1
        else:
            log.warning("jis appointments: slot Q%s failed", slot["q_section"])
        ctx = await _smart_form_root(page)

    return errors, filled
