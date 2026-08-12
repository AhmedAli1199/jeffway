"""
Upload BOQ-verify.xlsx to EasyBOP import_boqs page (headed test script).

Usage (from repo root):
  python filler/upload_boq_verify.py
  python filler/upload_boq_verify.py --file path/to/BOQ-verify.xlsx
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

_here = Path(__file__).resolve().parent
_repo = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from config import settings  # noqa: E402
from upload_wi_fill import (  # noqa: E402
    _page_looks_like_login,
    login,
    resolve_session_path,
    wait_for_filepond_ready,
)

log = logging.getLogger("upload_boq_verify")

IMPORT_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/import_boqs.php?contract_id=321129"
)


async def _wait_for_enabled_button(page, selector: str, *, label: str, timeout_ms: int) -> None:
    """Poll until a jQuery UI button is visible and enabled (logs progress)."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        state = await page.evaluate(
            """(sel) => {
              const btn = document.querySelector(sel);
              if (!btn) return { visible: false, enabled: false };
              const style = window.getComputedStyle(btn);
              const visible = style.display !== 'none' && style.visibility !== 'hidden' && btn.offsetParent !== null;
              const enabled = !btn.disabled && !btn.classList.contains('ui-state-disabled');
              return { visible, enabled };
            }""",
            selector,
        )
        if state.get("visible") and state.get("enabled"):
            log.info("%s is ready (after %s checks)", label, poll)
            return
        if poll == 1 or poll % 10 == 0:
            log.info("Still waiting for %s… visible=%s enabled=%s", label, state.get("visible"), state.get("enabled"))
        await asyncio.sleep(1.0)
    raise TimeoutError(f"{label} not ready within {timeout_ms}ms")


async def upload_boq_verify(
    file_path: Path,
    *,
    headless: bool = False,
    keep_open: bool = True,
) -> dict:
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    session_path = resolve_session_path()
    username = settings.username
    password = settings.password
    timeout_ms = settings.request_timeout_ms

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "args": ["--window-size=1400,900", "--disable-dev-shm-usage"],
        }
        if settings.slow_mo_ms:
            launch_kwargs["slow_mo"] = settings.slow_mo_ms

        browser = await p.chromium.launch(**launch_kwargs)
        context_kwargs = {}
        if session_path.exists():
            log.info("Using session: %s", session_path)
            context_kwargs["storage_state"] = str(session_path)
        else:
            log.warning("No session file — will log in with .env credentials")

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        async def _accept_dialog(dialog) -> None:
            log.info('Browser dialog: "%s" — clicking OK', dialog.message)
            await dialog.accept()

        page.on("dialog", _accept_dialog)

        try:
            log.info("Navigate to BOQ import page")
            await page.goto(IMPORT_URL, wait_until="domcontentloaded")

            if await _page_looks_like_login(page):
                if not username or not password:
                    raise RuntimeError("Login required — set username/password in .env")
                if not await login(page, username, password):
                    raise RuntimeError("Login failed")
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))
                await page.goto(IMPORT_URL, wait_until="domcontentloaded")

            if await _page_looks_like_login(page):
                raise RuntimeError("Still on login page after auth")

            file_input = page.locator("input[type='file']").first
            await file_input.wait_for(state="attached", timeout=timeout_ms)

            log.info("Uploading file: %s (%s bytes)", file_path.name, file_path.stat().st_size)
            await file_input.set_input_files(str(file_path.resolve()))

            await wait_for_filepond_ready(page, timeout_ms=120_000)

            upload_btn = page.locator("#btn_upload")
            await upload_btn.wait_for(state="visible", timeout=timeout_ms)
            await _wait_for_enabled_button(page, "#btn_upload", label="Upload File button", timeout_ms=timeout_ms)
            log.info("Clicking Upload File")
            await upload_btn.click()

            # EasyBOP may show Check Template / Import records dialogs (handled by page.on dialog)
            await asyncio.sleep(2)

            save_btn = page.locator("#btn_save_and_mark_as_agreed")
            log.info("Waiting for Save changes button (after Check Template confirm)…")
            try:
                await save_btn.wait_for(state="visible", timeout=180_000)
                await _wait_for_enabled_button(
                    page,
                    "#btn_save_and_mark_as_agreed",
                    label="Save changes button",
                    timeout_ms=180_000,
                )
            except Exception as exc:
                snippet = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 500)"
                )
                log.error("Save changes button did not appear: %s — page snippet: %s", exc, snippet)
                raise
            log.info("Clicking Save changes (overwrite existing BOQ items)")
            await save_btn.click()

            await asyncio.sleep(3)
            log.info("BOQ save triggered — check browser for result")

            result = {
                "success": True,
                "file": str(file_path),
                "url": page.url,
                "message": "BOQ uploaded, template confirmed, Save changes clicked.",
            }

            if keep_open:
                log.info("Leaving browser open 30s so you can inspect…")
                await asyncio.sleep(30)
            return result

        finally:
            if not keep_open:
                await browser.close()
            else:
                try:
                    await context.storage_state(path=str(session_path))
                except Exception:
                    pass
                await browser.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Upload BOQ-verify.xlsx to EasyBOP")
    parser.add_argument(
        "--file",
        type=Path,
        default=_repo / "excel_downloads" / "BOQ-verify.xlsx",
        help="Path to BOQ xlsx (default: excel_downloads/BOQ-verify.xlsx)",
    )
    parser.add_argument("--headless", action="store_true", help="Run without visible browser")
    parser.add_argument("--no-wait", action="store_true", help="Close browser immediately after upload")
    args = parser.parse_args()

    result = asyncio.run(
        upload_boq_verify(
            args.file.resolve(),
            headless=args.headless,
            keep_open=not args.no_wait,
        )
    )
    print(result)


if __name__ == "__main__":
    main()
