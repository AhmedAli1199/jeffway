"""EasyBOP month radio filter (#search_months) — shared by OJS register and scheduling register."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("stage5.month_filter")


def months_for_lookback(as_of: date, lookback_days: int) -> List[Tuple[int, int]]:
    """Return unique (year, month) tuples covering [as_of - lookback_days + 1, as_of]."""
    earliest = as_of - timedelta(days=max(lookback_days - 1, 0))
    months: List[Tuple[int, int]] = []
    cursor = date(earliest.year, earliest.month, 1)
    end = date(as_of.year, as_of.month, 1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


async def select_easybop_month(page: "Page", year: int, month: int, *, timeout_ms: int) -> bool:
    """
    Select a month on an EasyBOP page using #search_months radios (search_month_0 .. 11).
    Returns True if the month was selected or already active; False if controls not found.
    """
    month_index = month - 1
    target_id = f"search_month_{month_index}"
    month_label = date(year, month, 1).strftime("%b")

    try:
        await page.wait_for_selector("#search_months input[name='search_month']", timeout=timeout_ms)
    except Exception as exc:
        log.warning("month filter: #search_months not found on %s (%s)", page.url, exc)
        return False

    try:
        await page.wait_for_selector(
            "#search_months label.ui-checkboxradio-checked, #search_months label.ui-state-active",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    await asyncio.sleep(1.0)

    state = await page.evaluate(
        """(args) => {
            const targetId = args.targetId;
            const wantYear = String(args.year);
            const inputs = Array.from(document.querySelectorAll("#search_months input[name='search_month']"));
            let target = document.getElementById(targetId);
            if (target && wantYear) {
                const ty = target.getAttribute('data-year') || '';
                if (ty && ty !== wantYear) {
                    const alt = inputs.find((el) => el.id === targetId && (el.getAttribute('data-year') || '') === wantYear);
                    if (alt) target = alt;
                }
            }
            const current = document.querySelector("#search_months input[name='search_month']:checked");
            if (!target) return { found: false };
            return {
                found: true,
                alreadySelected: Boolean(current && current.id === target.id),
                currentId: current ? current.id : null,
                dataYear: target.getAttribute('data-year') || '',
            };
        }""",
        {"targetId": target_id, "year": year},
    )
    if not state.get("found"):
        log.warning("month filter: %s not found for %s %s", target_id, month_label, year)
        return False

    if state.get("alreadySelected"):
        log.info("month filter: already on %s %s", month_label, year)
        return True

    log.info(
        "month filter: selecting %s %s (%s, was %s)",
        month_label,
        year,
        target_id,
        state.get("currentId") or "none",
    )
    label = page.locator(f'label[for="{target_id}"]')
    if await label.count() > 0:
        await label.click()
    else:
        clicked = await page.evaluate(
            """(targetId) => {
                const label = document.querySelector(`label[for="${targetId}"]`);
                if (!label) return false;
                label.click();
                return true;
            }""",
            target_id,
        )
        if not clicked:
            log.warning("month filter: could not click label for %s", target_id)
            return False

    try:
        await page.wait_for_function(
            f"() => document.querySelector('#search_months input[name=\"search_month\"]:checked')?.id === '{target_id}'",
            timeout=timeout_ms,
        )
    except Exception as exc:
        log.warning("month filter: month did not switch to %s: %s", target_id, exc)
        return False

    await asyncio.sleep(0.3)
    try:
        await page.wait_for_function(
            "() => { const el = document.getElementById('load_list'); "
            "return !el || el.style.display === 'none' || !el.offsetParent; }",
            timeout=timeout_ms,
        )
    except Exception as exc:
        log.warning("month filter: grid reload wait: %s", exc)
    await asyncio.sleep(1.0)
    return True
