"""
msg_downloader.py
=================
Download files from EasyBOP using the saved Playwright session cookies.

Uses `requests` with the PHPSESSID / csrf_session cookies loaded from
filler/easybop_session.json — same session the Playwright browser uses.

Functions
---------
load_session_cookies(session_json_path)
    Read the easybop_session.json and return a requests.Session.

download_file(url, session, *, timeout=60)
    Stream-download a file from EasyBOP; return (bytes, filename_hint).

fetch_pre_works_html(contract_id, session, *, timeout=30)
    GET pre_works.php?contract_id=... and return the HTML string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("pre_works_jis.msg_downloader")

_EASYBOP_BASE = "https://easybop.co.uk"

_PRE_WORKS_PAGE_INFO_JS = r"""
() => {
    const inp = document.querySelector(
        '#pg_list_pager input.ui-pg-input, #pager_list input.ui-pg-input, input.ui-pg-input'
    );
    const tot = document.getElementById('sp_1_list_pager');
    const nextTd = document.getElementById('next_list_pager');
    // jqGrid ui-pg-input is already 1-based (page 1 => "1"); do NOT add +1.
    const currentPage = inp ? (parseInt(inp.value, 10) || 1) : 1;
    const totalPages = tot ? (parseInt((tot.textContent || '').trim(), 10) || 1) : 1;
    const hasNext = nextTd
        ? !nextTd.classList.contains('ui-state-disabled')
        : false;
    const pagingInfo = document.querySelector('.ui-paging-info');
    let recordsTotal = null;
    if (pagingInfo) {
        const m = (pagingInfo.textContent || '').match(/of\\s+([\\d,]+)/i);
        if (m) recordsTotal = parseInt(m[1].replace(/,/g, ''), 10) || null;
    }
    return { currentPage, totalPages, hasNext, recordsTotal };
}
"""

_PRE_WORKS_ROW_HTML_JS = r"""
() => {
    const rows = [];
    document.querySelectorAll('table.ui-jqgrid-btable tbody tr[id]').forEach((tr) => {
        if (tr.id && /^\d+$/.test(tr.id)) rows.push(tr.outerHTML);
    });
    return rows;
}
"""

_ROW_TR_RE = re.compile(r'<tr[^>]+id="(\d+)"[^>]*>.*?</tr>', re.I | re.S)


async def _scrape_rows_on_page(page) -> Dict[str, str]:
    """Extract data rows from current jqGrid page (DOM first, then full HTML fallback)."""
    out: Dict[str, str] = {}
    chunk: List[str] = await page.evaluate(_PRE_WORKS_ROW_HTML_JS) or []
    for row_html in chunk:
        m = re.search(r'\bid="(\d+)"', row_html)
        if m:
            out[m.group(1)] = row_html
    if out:
        return out
    page_html = await page.content()
    for m in _ROW_TR_RE.finditer(page_html):
        out[m.group(1)] = m.group(0)
    return out


def load_session_cookies(session_json_path: Optional[str | Path] = None) -> requests.Session:
    """
    Load Playwright storage-state JSON and return a requests.Session with
    the EasyBOP cookies set.

    If session_json_path is None, auto-discovers filler/easybop_session.json
    relative to this file's repo root.
    """
    if session_json_path is None:
        # Walk up to repo root: stage2/pre_works_jis → stage2 → repo root
        here = Path(__file__).resolve().parent
        session_json_path = here.parent.parent / "filler" / "easybop_session.json"

    path = Path(session_json_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"EasyBOP session file not found: {path}\n"
            "Run POST /login on the filler API first to save a session."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    sess = requests.Session()

    for cookie in data.get("cookies", []):
        sess.cookies.set(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie.get("domain", ".easybop.co.uk").lstrip("."),
            path=cookie.get("path", "/"),
        )

    # Set a browser-like User-Agent so EasyBOP doesn't reject the request
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://easybop.co.uk/",
        }
    )
    return sess


def fetch_pre_works_html(
    contract_id: str,
    session: requests.Session,
    *,
    base_url: str = _EASYBOP_BASE,
    timeout: int = 30,
) -> str:
    """
    Fetch the pre_works.php page HTML for a given contract_id.
    Returns the full HTML string.

    NOTE: The pre_works page uses jqGrid with AJAX — the table rows are
    rendered client-side by JavaScript. Use fetch_pre_works_table_html()
    with a Playwright context instead to get the rendered table.
    """
    url = f"{base_url.rstrip('/')}/a_planned_works/z_works_reports/pre_works.php"
    resp = session.get(url, params={"contract_id": contract_id}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text
    # Detect redirect to login page
    if "login" in resp.url.lower() or "Please log in" in html or "id=\"password\"" in html:
        raise RuntimeError(
            "EasyBOP redirected to login — session may have expired. "
            "Run POST /login on the filler API to refresh the session."
        )
    return html


async def _wait_for_pre_works_grid_ready(page, timeout_ms: int) -> None:
    await page.wait_for_function(
        r"""() => {
            let n = 0;
            document.querySelectorAll('table.ui-jqgrid-btable tbody tr[id]').forEach((tr) => {
                if (tr.id && /^\d+$/.test(tr.id)) n++;
            });
            return n > 0;
        }""",
        timeout=min(60_000, timeout_ms),
    )


async def _next_pre_works_page_enabled(page) -> bool:
    """True when jqGrid next-page control exists and is not disabled."""
    return bool(
        await page.evaluate(
            r"""() => {
                const nextTd = document.getElementById('next_list_pager');
                if (!nextTd) return false;
                return !nextTd.classList.contains('ui-state-disabled');
            }"""
        )
    )


async def _go_to_next_pre_works_page(page, *, timeout_ms: int = 15_000) -> bool:
    """Click #next_list_pager and wait for jqGrid page to advance."""
    if not await _next_pre_works_page_enabled(page):
        return False

    info_before: Dict[str, Any] = await page.evaluate(_PRE_WORKS_PAGE_INFO_JS) or {}
    current = int(info_before.get("currentPage") or 1)

    first_id_before = await page.evaluate(
        r"""() => {
            const tr = document.querySelector('table.ui-jqgrid-btable tbody tr[id]');
            return tr && /^\d+$/.test(tr.id) ? tr.id : '';
        }"""
    )

    next_td = page.locator("#next_list_pager")

    icon = next_td.locator(".ui-icon-seek-next")
    if await icon.count() > 0:
        await icon.click()
    else:
        await next_td.click()

    try:
        await page.wait_for_function(
            f"""() => {{
                const inp = document.querySelector(
                    '#pg_list_pager input.ui-pg-input, #pager_list input.ui-pg-input, input.ui-pg-input'
                );
                const pageNum = inp ? (parseInt(inp.value, 10) || 0) : 0;
                if (pageNum > {current}) return true;
                const tr = document.querySelector('table.ui-jqgrid-btable tbody tr[id]');
                const rid = tr && /^\\d+$/.test(tr.id) ? tr.id : '';
                return rid && rid !== '{first_id_before}';
            }}""",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    try:
        await page.wait_for_function(
            r"""() => {
                const el = document.getElementById('load_list');
                return !el || el.style.display === 'none' || !el.offsetParent;
            }""",
            timeout=15_000,
        )
    except Exception:
        pass
    await asyncio.sleep(0.4)
    return True


async def fetch_pre_works_table_html_playwright_meta(
    context,
    contract_id: str,
    *,
    base_url: str = _EASYBOP_BASE,
    timeout_ms: int = 30000,
    max_pages: int = 200,
) -> Dict[str, Any]:
    """Scrape all pre_works jqGrid pages (#next_list_pager) and return combined HTML."""
    url = f"{base_url.rstrip('/')}/a_planned_works/z_works_reports/pre_works.php?contract_id={contract_id}"
    page = await context.new_page()
    row_html_by_id: Dict[str, str] = {}
    pages_scraped = 0

    try:
        try:
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception:
            pass

        try:
            await page.wait_for_selector(
                "table.ui-jqgrid-btable tbody tr[id]",
                timeout=timeout_ms,
            )
            await _wait_for_pre_works_grid_ready(page, timeout_ms)
        except Exception as exc:
            log.warning("pre_works jqGrid not ready within %sms: %s", timeout_ms, exc)

        records_total: Optional[int] = None
        for page_num in range(1, max_pages + 1):
            await asyncio.sleep(0.35)
            page_rows = await _scrape_rows_on_page(page)
            row_html_by_id.update(page_rows)
            pages_scraped = page_num

            info: Dict[str, Any] = await page.evaluate(_PRE_WORKS_PAGE_INFO_JS) or {}
            current = int(info.get("currentPage") or 1)
            total = int(info.get("totalPages") or 1)
            if info.get("recordsTotal"):
                records_total = int(info["recordsTotal"])
            log.info(
                "pre_works: page %d/%d — %d row(s) on page (%d unique so far, records=%s)",
                current,
                total,
                len(page_rows),
                len(row_html_by_id),
                records_total or "?",
            )

            if not await _next_pre_works_page_enabled(page):
                break

            if not await _go_to_next_pre_works_page(page, timeout_ms=min(20_000, timeout_ms)):
                log.warning("pre_works: next page click failed on page %d/%d", current, total)
                break
            try:
                await _wait_for_pre_works_grid_ready(page, timeout_ms)
            except Exception as nav_err:
                log.warning("pre_works: grid not ready after page advance: %s", nav_err)
                break

        html = (
            '<table class="ui-jqgrid-btable"><tbody>'
            + "".join(row_html_by_id.values())
            + "</tbody></table>"
        )
        log.info(
            "pre_works: scraped %d unique row(s) across %d page(s)",
            len(row_html_by_id),
            pages_scraped,
        )
        return {
            "html": html,
            "pages_scraped": pages_scraped,
            "rows_total": len(row_html_by_id),
            "records_total": records_total,
            "contract_id": contract_id,
        }
    finally:
        await page.close()


async def fetch_pre_works_table_html_playwright(
    context,
    contract_id: str,
    *,
    base_url: str = _EASYBOP_BASE,
    timeout_ms: int = 30000,
    max_pages: int = 200,
) -> str:
    """Return combined pre_works jqGrid HTML from all pages."""
    result = await fetch_pre_works_table_html_playwright_meta(
        context,
        contract_id,
        base_url=base_url,
        timeout_ms=timeout_ms,
        max_pages=max_pages,
    )
    return result.get("html") or ""


def _filename_from_response(resp: requests.Response, url: str) -> str:
    """Extract filename from Content-Disposition or URL."""
    cd = resp.headers.get("Content-Disposition", "")
    # Content-Disposition: attachment; filename="foo.msg"
    m = re.search(r'filename[^=]*=\s*["\']?([^;"\']+)', cd)
    if m:
        return m.group(1).strip().strip('"\'')
    # Fall back to URL query param filename_orig=...
    m2 = re.search(r'filename_orig=([^&]+)', url)
    if m2:
        return requests.utils.unquote(m2.group(1)).strip()
    # Last resort: last segment of path
    return url.split("/")[-1].split("?")[0] or "download"


def download_file(
    url: str,
    session: requests.Session,
    *,
    base_url: str = _EASYBOP_BASE,
    timeout: int = 60,
) -> Tuple[bytes, str]:
    """
    Download a file from EasyBOP.
    Returns (file_bytes, filename_hint).

    url may be absolute or relative to base_url.
    """
    if not url.startswith("http"):
        url = base_url.rstrip("/") + "/" + url.lstrip("/")

    resp = session.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type:
        # Check first chunk for login redirect
        first = next(resp.iter_content(chunk_size=512), b"")
        if b"login" in first.lower() or b"Please log in" in first:
            raise RuntimeError(
                "EasyBOP returned HTML instead of file — session may have expired."
            )
        data = first + b"".join(resp.iter_content(chunk_size=8192))
    else:
        data = b"".join(resp.iter_content(chunk_size=8192))

    filename = _filename_from_response(resp, url)
    return data, filename
