"""
Navigate to EasyBOP BOQ import page and download the
"Download Template" spreadsheet (no existing data) into this folder.

Uses the same BrowserContext session as the FastAPI app (cookies / storage_state).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from playwright.async_api import BrowserContext, Page

log = logging.getLogger("easybop.boq_template")

TEMPLATE_BUTTON_SELECTOR = "#btn_without_data"
CANONICAL_TEMPLATE_FILENAME = "BOQ-Import-Template.xlsx"


def default_output_dir() -> Path:
    return Path(__file__).resolve().parent


async def _on_login_screen(page: Page) -> bool:
    if "easybop login" in (await page.title()).lower():
        return True
    return (await page.locator("input[type='password']").count()) > 0


async def download_boq_import_template(
    page: Page,
    *,
    context: BrowserContext,
    base_url: str,
    contract_id: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    username: str,
    password: str,
    session_path: Path,
    timeout_ms: int,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, str]:
    """
    Open import_boqs.php, ensure session (login + save storage if needed),
    click #btn_without_data, save download to output_dir.

    Returns (absolute_path_saved, final_page_url).
    """
    out = output_dir or default_output_dir()
    out.mkdir(parents=True, exist_ok=True)

    url = f"{base_url.rstrip('/')}/a_planned_works/z_works/import_boqs.php?contract_id={contract_id}"
    log.info("boq template: navigating to %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(500)

    if await _on_login_screen(page):
        log.info("boq template: login screen — signing in")
        ok = await login(page, username, password)
        if not ok:
            raise RuntimeError("EasyBOP login failed")
        await context.storage_state(path=str(session_path))
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(500)

    if await _on_login_screen(page):
        raise RuntimeError("Still on login after sign-in")

    btn = page.locator(TEMPLATE_BUTTON_SELECTOR)
    count = await btn.count()
    if count == 0:
        raise RuntimeError(f"Download button not found: {TEMPLATE_BUTTON_SELECTOR}")
    await btn.first.wait_for(state="visible", timeout=timeout_ms)

    async with page.expect_download(timeout=timeout_ms) as download_info:
        await btn.first.click()

    download = await download_info.value
    dest = out / CANONICAL_TEMPLATE_FILENAME
    await download.save_as(str(dest))
    size = dest.stat().st_size
    log.info("boq template: saved %s (%s bytes)", dest, size)
    return dest.resolve(), page.url
