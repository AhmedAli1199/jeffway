"""
EasyBOP task automation for planned works order pages.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

from automation import ensure_jis_index_ready, parse_address_from_q12, _fill_search_dwelling


async def add_qs_review_task_from_row(
    page: Page,
    *,
    contract_id: str,
    q13_search_text: str,
    q52_description: str,
    q12_address_hint: Optional[str] = None,
    q12_raw: Optional[str] = None,
    timeout_ms: int = 20_000,
    relogin_username: Optional[str] = None,
    relogin_password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """
    Navigate to planned works order using Q1.3 search text, then create a task:
    - Tasks tab
    - Add Task
    - Category: "QS review for variations"
    - Description: Q5.2 value
    """
    search_text = (q13_search_text or "").strip()
    if not search_text:
        raise ValueError("Q1.3 is required to open the order page for task creation.")

    description = (q52_description or "").strip()
    if not description:
        raise ValueError("Q5.2 is required for task description.")

    await ensure_jis_index_ready(
        page,
        contract_id,
        relogin_username,
        relogin_password,
        timeout_ms,
        after_relogin=after_relogin,
    )

    q12_full = (q12_raw or "").strip() or None
    address_hint = (q12_address_hint or "").strip() or parse_address_from_q12(q12_full)
    await _fill_search_dwelling(
        page,
        search_text,
        address_hint=address_hint or None,
        q12=q12_full,
        timeout_ms=timeout_ms,
    )

    # Open Tasks tab
    tasks_tab = page.locator('.tab_nav_item[data-tab-nav-item="tasks"]').first
    await tasks_tab.wait_for(state="visible", timeout=timeout_ms)
    await tasks_tab.click()

    # Wait for tasks section and click Add Task
    add_task_btn = page.locator("#btn_add_task_5").first
    await add_task_btn.wait_for(state="visible", timeout=timeout_ms)
    await add_task_btn.click()

    # Popup fields
    category = page.locator("#category_id").first
    await category.wait_for(state="visible", timeout=timeout_ms)
    await category.select_option(label="QS review for variations")

    desc = page.locator("#description").first
    await desc.wait_for(state="visible", timeout=timeout_ms)
    await desc.fill(description)

    # Small settle for any onchange handlers in popup.
    await page.wait_for_timeout(300)

