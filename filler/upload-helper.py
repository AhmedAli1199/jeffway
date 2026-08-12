"""
Upload WI-fill.xlsx to EasyBOP import_works page (headed test script).

Usage (from repo root):
  python filler/upload_wi_fill.py
  python filler/upload_wi_fill.py --file path/to/WI-fill.xlsx
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

log = logging.getLogger("upload_wi_fill")

LOGIN_URL = "https://easybop.co.uk/"
IMPORT_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/import_works.php?contract_id=321129"
)


def resolve_session_path() -> Path:
    p = Path(settings.session_file)
    if not p.is_absolute():
        p = _here / p
    if not p.exists():
        alt = _repo / "JIS-form" / "easybop_session.json"
        if alt.exists():
            return alt
    return p.resolve()


async def _page_looks_like_login(page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.first.is_visible():
            return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def login(page, username: str, password: str) -> bool:
    log.info("Opening login page")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await page.locator("a[href*='logout'], a[href*='log_out']").count() > 0:
        log.info("Already logged in")
        return True

    box = page.locator("#login_box")
    await box.wait_for(state="visible", timeout=12_000)
    await box.locator("input[type='text']").first.fill(username)
    await box.locator("input[type='password']").first.fill(password)
    await page.locator("#btn_login").click()
    await page.wait_for_function(
        "() => { const o=document.querySelector('a[href*=\"logout\"],a[href*=\"log_out\"]'); "
        "const lb=document.querySelector('#login_box'); "
        "return o!==null||(lb&&lb.offsetParent===null); }",
        timeout=20_000,
    )
    log.info("Login OK")
    return True


_FILEPOND_STATUS_JS = """() => {
  const items = Array.from(document.querySelectorAll('.filepond--item'));
  if (!items.length) {
    return { ready: false, reason: 'no_items', states: [], btnEnabled: false };
  }
  const states = items.map((el) => el.getAttribute('data-filepond-item-state') || 'unknown');
  const busy = new Set(['processing', 'load', 'busy', 'loading']);
  const done = new Set(['idle', 'processing-complete']);
  const anyBusy = states.some((s) => busy.has(s));
  const allDone = states.every((s) => done.has(s));
  const processingEl = document.querySelector(
    '.filepond--item[data-filepond-item-state="processing"], ' +
    '.filepond--item[data-filepond-item-state="load"]'
  );
  const statusText = (document.querySelector('.filepond--file-status-main')?.textContent || '')
    .toLowerCase();
  const stillUploading = /uploading|processing|loading/.test(statusText);
  const btn = document.getElementById('btn_upload');
  const btnEnabled = !!(btn && !btn.disabled && !btn.classList.contains('ui-state-disabled'));
  const ready = allDone && !anyBusy && !processingEl && !stillUploading && items.length > 0;
  return { ready, states, anyBusy, allDone, stillUploading, btnEnabled, reason: states.join(',') };
}"""


async def wait_for_filepond_ready(
    page,
    timeout_ms: int = 300_000,
    *,
    stable_polls: int = 4,
    poll_interval_s: float = 0.75,
) -> None:
    """Poll FilePond until the file is fully on the server (not still processing)."""
    await page.wait_for_selector(".filepond--root", state="visible", timeout=timeout_ms)
    await page.wait_for_selector(".filepond--item", state="attached", timeout=timeout_ms)

    deadline = time.monotonic() + (timeout_ms / 1000)
    stable = 0
    last: dict = {}
    poll_n = 0

    log.info("Waiting for FilePond upload to finish (dynamic poll, up to %ss)…", timeout_ms // 1000)

    while time.monotonic() < deadline:
        poll_n += 1
        last = await page.evaluate(_FILEPOND_STATUS_JS)
        states = last.get("states") or []
        ready = bool(last.get("ready"))
        btn_ok = bool(last.get("btnEnabled"))

        if ready and btn_ok:
            stable += 1
            if stable >= stable_polls:
                log.info(
                    "FilePond ready after %s stable checks — states=%s, Upload button enabled",
                    stable_polls,
                    states,
                )
                return
        else:
            stable = 0
            if poll_n == 1 or poll_n % 8 == 0:
                log.info(
                    "FilePond still busy — states=%s uploading_text=%s btn_enabled=%s",
                    states,
                    last.get("stillUploading"),
                    btn_ok,
                )

        await asyncio.sleep(poll_interval_s)

    raise TimeoutError(
        f"FilePond did not finish within {timeout_ms}ms. Last status: {last}"
    )


async def upload_wi_fill(
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
        launch_kwargs = {"headless": headless}
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
            log.info("Navigate to import page")
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

            log.info("Uploading file: %s", file_path.name)
            await file_input.set_input_files(str(file_path.resolve()))

            await wait_for_filepond_ready(page, timeout_ms=300_000)

            upload_btn = page.locator("#btn_upload")
            await upload_btn.wait_for(state="visible", timeout=timeout_ms)
            await page.wait_for_function(
                """() => {
                  const btn = document.getElementById('btn_upload');
                  return btn && !btn.disabled && !btn.classList.contains('ui-state-disabled');
                }""",
                timeout=timeout_ms,
            )
            log.info("Clicking Upload File (file on server + button enabled)")
            await upload_btn.click()

            # Give server time to process import after confirm
            await asyncio.sleep(5)
            shot = _here / "screenshots" / "test_upload_works_dialog.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            log.info("Screenshot saved (5s after Upload File): %s", shot)
            log.info("Upload triggered — check browser for success message")

            result = {
                "success": True,
                "file": str(file_path),
                "url": page.url,
                "message": "File attached and Upload File clicked (confirm accepted).",
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
    parser = argparse.ArgumentParser(description="Upload WI-fill.xlsx to EasyBOP")
    parser.add_argument(
        "--file",
        type=Path,
        default=_repo / "WI-fill.xlsx",
        help="Path to WI-fill.xlsx (default: repo root WI-fill.xlsx)",
    )
    parser.add_argument("--headless", action="store_true", help="Run without visible browser")
    parser.add_argument("--no-wait", action="store_true", help="Close browser immediately after upload")
    args = parser.parse_args()

    result = asyncio.run(
        upload_wi_fill(
            args.file.resolve(),
            headless=args.headless,
            keep_open=not args.no_wait,
        )
    )
    print(result)


if __name__ == "__main__":
    main()
