"""
EasyBOP JIS (planned works): login, open index list, search dwelling, fill Q fields by label row.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, Page

log = logging.getLogger("easybop.jis")

_POLL_FAST_S = 0.06


def _jis_phase_ms(timeout_ms: int, *, floor: int = 8_000, cap: int = 20_000) -> int:
    """Cap per-step waits — request_timeout_ms is a ceiling, not a minimum budget."""
    t = int(timeout_ms) if timeout_ms else floor
    return min(max(t, floor), cap)


_jis_dir = Path(__file__).resolve().parent
_appointment_automation_mod = None


def _get_appointment_automation():
    global _appointment_automation_mod
    if _appointment_automation_mod is not None:
        return _appointment_automation_mod
    cached = sys.modules.get("appointment_automation")
    if cached is not None:
        _appointment_automation_mod = cached
        return cached
    path = _jis_dir / "appointment_automation.py"
    spec = importlib.util.spec_from_file_location("_appointment_automation", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load appointment_automation from {path}")
    if "appointment_loader" not in sys.modules:
        lp = _jis_dir / "appointment_loader.py"
        lspec = importlib.util.spec_from_file_location("_appointment_loader", lp)
        if lspec is None or lspec.loader is None:
            raise ImportError(f"Could not load appointment_loader from {lp}")
        lmod = importlib.util.module_from_spec(lspec)
        sys.modules["appointment_loader"] = lmod
        lspec.loader.exec_module(lmod)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["appointment_automation"] = mod
    spec.loader.exec_module(mod)
    _appointment_automation_mod = mod
    return mod

def _parse_data_item_args(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    s = html.unescape(raw.strip())
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


LOGIN_URL = "https://easybop.co.uk/"


def url_indicates_session_expired(url: str) -> bool:
    """EasyBOP redirects here when the session timed out — must log in again."""
    u = (url or "").lower()
    return "home.php" in u and "err=1" in u


def jis_index_url(contract_id: str) -> str:
    cid = (contract_id or "321129").strip()
    return f"https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={cid}"


def works_wi_url(works_id: str, contract_id: str) -> str:
    """Direct WI tab URL (Stage 6 pattern) — skip index dwelling search."""
    wid = str(works_id or "").strip()
    cid = (contract_id or "321129").strip()
    return (
        "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
        f"?works_id={wid}&contract_id={cid}&tab_nav_item=wi"
    )


async def navigate_to_works_wi_page(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
    *,
    relogin_username: Optional[str] = None,
    relogin_password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """Open works_details WI tab by works_id, then click JIS pre-item from there."""
    wid = str(works_id or "").strip()
    if not wid:
        raise ValueError("works_id is required for direct WI navigation")
    url = works_wi_url(wid, contract_id)
    log.info("jis: navigate works WI direct -> %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(15_000, timeout_ms))
    except Exception:
        pass

    if await page_looks_like_login_screen(page):
        if not relogin_username or not relogin_password:
            raise RuntimeError("Login required but credentials not set in .env")
        await relogin_easybop(page, relogin_username, relogin_password, after_relogin)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    if await page_looks_like_login_screen(page):
        raise RuntimeError(f"Still on login wall for works_id={wid}")

    try:
        await page.wait_for_selector(
            'a[data-action="pw_works_show_pre_item_dialog"]',
            state="visible",
            timeout=min(12_000, timeout_ms),
        )
    except Exception as e:
        log.warning("jis: pre_item links not visible on WI page works_id=%s: %s", wid, e)


_SMART_FORM_ACTION = "works_ajax_openSmartForm"
_DIALOG_LOCATOR = (
    ".ui-dialog:visible, [role='dialog']:visible, .modal.show:visible, .modal.in:visible"
)


async def shows_login_wall(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        return await lb.count() > 0 and await lb.first.is_visible()
    except Exception:
        return False


async def page_looks_like_login_screen(page: Page) -> bool:
    try:
        if url_indicates_session_expired(page.url or ""):
            return True
        if await shows_login_wall(page):
            return True
        if "login" in (await page.title() or "").lower():
            return True
        h = page.locator("h1").first
        if await h.count() > 0:
            txt = (await h.inner_text() or "").lower()
            if "easybop" in txt and "login" in txt:
                return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def login(page: Page, username: str, password: str) -> bool:
    log.info("login: opening %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await page.locator("a[href*='logout'], a[href*='log_out'], a[href*='sign_out']").count() > 0:
        log.info("login: already authenticated")
        return True

    box = page.locator("#login_box")
    try:
        await box.wait_for(state="visible", timeout=12_000)
    except Exception as e:
        log.error("login: login_box not visible: %s", e)
        return False

    try:
        await box.locator("input[type='text']").first.fill(username)
        await box.locator("input[type='password']").first.fill(password)
        await page.locator("#btn_login").click()
        await page.wait_for_function(
            "() => { const o=document.querySelector('a[href*=\"logout\"],a[href*=\"log_out\"]'); "
            "const lb=document.querySelector('#login_box'); "
            "return o!==null||(lb&&lb.offsetParent===null); }",
            timeout=20_000,
        )
        log.info("login: success url=%s", page.url)
        return True
    except Exception as e:
        log.exception("login: failed: %s", e)
        return False


async def relogin_easybop(
    page: Page,
    username: str,
    password: str,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    log.info("relogin: session expired (e.g. home.php?err=1) — logging in again")
    if not await login(page, username, password):
        raise RuntimeError("EasyBOP re-login failed")
    if after_relogin:
        await after_relogin()


async def ensure_jis_index_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = jis_index_url(contract_id)
    log.info("ensure_jis_index: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await page_looks_like_login_screen(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        await relogin_easybop(page, username, password, after_relogin)
        await page.goto(url, wait_until="domcontentloaded")

    try:
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if not await page_looks_like_login_screen(page):
            log.info("ensure_jis_index: session OK (search field visible)")
            return
    except Exception as e:
        log.warning("ensure_jis_index: search field not ready: %s", e)

    if await page_looks_like_login_screen(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        await relogin_easybop(page, username, password, after_relogin)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if await page_looks_like_login_screen(page):
            raise RuntimeError("Still on login wall after re-login — check credentials or contract access")
        return

    try:
        n = await page.locator("#search_dwelling").count()
        visible = n > 0 and await page.locator("#search_dwelling").first.is_visible()
    except Exception:
        visible = False
    if not visible:
        log.info("ensure_jis_index: search field still not usable — reloading index once")
        await page.goto(url, wait_until="domcontentloaded")
        if await page_looks_like_login_screen(page):
            if not username or not password:
                raise RuntimeError("Login required but credentials not set in .env")
            await relogin_easybop(page, username, password, after_relogin)
            await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if not await page_looks_like_login_screen(page):
            return

    if await page_looks_like_login_screen(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        await relogin_easybop(page, username, password, after_relogin)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)

    if await page.locator("#search_dwelling").count() == 0:
        raise RuntimeError("Planned works index did not load (#search_dwelling missing).")


async def _dismiss_stale_index_dialogs(page: Page) -> None:
    """Close leftover jQuery UI dialogs that block the next pre-item / SmartForm open."""
    for _ in range(6):
        if await page.locator(".ui-dialog:visible").count() == 0:
            return
        close = page.locator(
            ".ui-dialog .ui-dialog-titlebar-close, button.ui-dialog-titlebar-close"
        )
        if await close.count() > 0:
            try:
                await close.first.click(timeout=2_000)
                await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=3_000)
                continue
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(_POLL_FAST_S)
        except Exception:
            break


async def return_to_planned_works_index(
    page: Page,
    contract_id: str,
    timeout_ms: int,
    *,
    relogin_username: Optional[str] = None,
    relogin_password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> Page:
    """
    After filling one JIS: close SmartForm tabs, hard-reload index (clears stale dialogs), clear search.
    """
    ctx = page.context
    keeper = page
    for p in list(ctx.pages):
        if p is keeper:
            continue
        try:
            await p.close()
        except Exception:
            pass

    url = jis_index_url(contract_id)
    log.info("jis: return_to_index — reload %s", url)
    await keeper.goto(url, wait_until="domcontentloaded")
    await _dismiss_stale_index_dialogs(keeper)

    await ensure_jis_index_ready(
        keeper,
        contract_id,
        relogin_username,
        relogin_password,
        timeout_ms,
        after_relogin=after_relogin,
    )
    try:
        field = keeper.locator("#search_dwelling")
        if await field.count() > 0:
            await field.click()
            await field.fill("")
    except Exception as e:
        log.debug("return_to_planned_works_index: could not clear #search_dwelling: %s", e)

    log.info("jis: returned to planned works index — ready for next job")
    return keeper


def parse_address_from_q12(q12: Optional[str]) -> str:
    """
    Q1.2 format e.g. ``1212/1-701 BISHPORT AVENUEWITHYWOOD - 1272226`` → ``701 BISHPORT AVENUEWITHYWOOD``.
    Legacy: ``84395/6-106A COTHAM BROWCOTHAM - 1109451`` → ``106A COTHAM BROWCOTHAM``.
    """
    s = (q12 or "").strip()
    if not s:
        return ""
    left = s
    uprn_tail = re.match(r"^(.+?)\s*-\s*(\d{5,})\s*$", s)
    if uprn_tail:
        left = uprn_tail.group(1).strip()
    for pat in (
        r"^\d+/\d+\s*-\s*(.+)$",
        r"^\d+/\d+-(.+)$",
    ):
        m = re.match(pat, left, re.I)
        if m:
            return m.group(1).strip()
    return left


def parse_q12_number_and_street_prefix(q12: Optional[str]) -> Tuple[str, str]:
    """
    From Q1.2, return house/building number token and first 3 letters of the street name
  after the job-ref dash, e.g. ``1212/1-701 BISHPORT...`` → (``701``, ``BIS``).
    """
    addr = parse_address_from_q12(q12)
    if not addr:
        return "", ""
    m = re.match(r"^(\d+[A-Za-z]?)\s*(.*)$", addr.strip(), re.I)
    if not m:
        return "", ""
    num = m.group(1).upper()
    rest = m.group(2) or ""
    wm = re.search(r"[A-Za-z]{3,}", rest)
    if not wm:
        return num, ""
    return num, wm.group(0)[:3].upper()


def _norm_addr_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _address_matches_number_and_prefix(num: str, prefix: str, candidate_text: str) -> bool:
    """True when option text contains the Q1.2 number and the first 3 street letters (e.g. 701 + BIS)."""
    if not num or not prefix or len(prefix) < 3:
        return False
    hay = _norm_addr_key(candidate_text)
    num_n = _norm_addr_key(num)
    pref_n = _norm_addr_key(prefix)
    if not hay or not num_n or not pref_n:
        return False
    idx = hay.find(num_n)
    if idx < 0:
        return False
    window = hay[idx : idx + len(num_n) + 32]
    return pref_n in window


def _address_matches_hint(address_hint: str, candidate_text: str, *, q12: Optional[str] = None) -> bool:
    """Match Q1.2 street fragment against autocomplete label or table row text."""
    num, prefix = parse_q12_number_and_street_prefix(q12) if q12 else ("", "")
    if not num and address_hint:
        m = re.match(r"^(\d+[A-Za-z]?)\s*(.*)$", (address_hint or "").strip(), re.I)
        if m:
            num = m.group(1).upper()
            wm = re.search(r"[A-Za-z]{3,}", m.group(2) or "")
            if wm:
                prefix = wm.group(0)[:3].upper()
    if num and prefix and _address_matches_number_and_prefix(num, prefix, candidate_text):
        return True

    want = _norm_addr_key(address_hint)
    hay = _norm_addr_key(candidate_text)
    if not want or not hay:
        return False
    if want in hay or hay in want:
        return True
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", want)]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in hay)
    return hits >= min(2, len(tokens)) or (len(tokens) == 1 and hits == 1)


def _prefer_search_result(matches: List[tuple]) -> tuple:
    """Prefer BCC Response Repairs / Routine Repairs when several rows match the address."""
    if len(matches) == 1:
        return matches[0]
    for pair in matches:
        low = pair[1].lower()
        if "response repairs" in low or "routine repairs" in low:
            return pair
    return matches[0]


async def _click_autocomplete_by_address(
    page: Page,
    address_hint: str,
    timeout_ms: int,
    *,
    q12: Optional[str] = None,
) -> bool:
    """
    Pick from jQuery UI autocomplete, e.g.
    ``36/1486: Routine Repairs: 701 Bishport Avenue, BS13 9EH`` when Q1.2 contains 701 BISHPORT.
    """
    want = (address_hint or "").strip()
    if not want and not (q12 or "").strip():
        return False

    items = page.locator(
        ".ui-autocomplete:visible li.ui-menu-item .ui-menu-item-wrapper, "
        ".ui-autocomplete:visible li.ui-menu-item"
    )
    try:
        await items.first.wait_for(state="visible", timeout=min(timeout_ms, 12_000))
    except Exception:
        return False

    n = await items.count()
    matches: List[tuple] = []
    for i in range(n):
        el = items.nth(i)
        try:
            label = (await el.inner_text() or "").strip()
        except Exception:
            continue
        if not label:
            continue
        if _address_matches_hint(want, label, q12=q12):
            matches.append((el, label))

    if not matches:
        num, pref = parse_q12_number_and_street_prefix(q12)
        log.warning(
            "search_dwelling: %s autocomplete item(s), none matched Q1.2 address %r (num=%r prefix=%r)",
            n,
            want or q12,
            num,
            pref,
        )
        return False

    chosen_el, chosen_label = _prefer_search_result(matches)
    await chosen_el.click()
    num, pref = parse_q12_number_and_street_prefix(q12)
    log.info(
        "search_dwelling: selected autocomplete by Q1.2 %r (num=%r prefix=%r) — %r (%s/%s)",
        want or q12,
        num,
        pref,
        chosen_label,
        len(matches),
        n,
    )
    return True


async def _click_wi_row_by_address(
    page: Page,
    address_hint: str,
    timeout_ms: int,
    *,
    q12: Optional[str] = None,
) -> bool:
    """Select a ``tr.wi_row`` when search returns multiple works orders for the same ref."""
    want = (address_hint or "").strip()
    if not want and not (q12 or "").strip():
        return False

    rows = page.locator("tr.wi_row")
    try:
        await rows.first.wait_for(state="visible", timeout=min(timeout_ms, 12_000))
    except Exception:
        return False

    n = await rows.count()
    matches: List[Any] = []
    for i in range(n):
        row = rows.nth(i)
        try:
            if await row.evaluate("el => el.classList.contains('disabled')"):
                continue
        except Exception:
            pass
        try:
            row_text = await row.inner_text()
        except Exception:
            continue
        if _address_matches_hint(want, row_text, q12=q12):
            matches.append((row, row_text))

    if not matches:
        return False

    chosen_el, chosen_label = _prefer_search_result(matches)
    await chosen_el.click()
    log.info(
        "search_dwelling: selected wi_row by address hint %r — %r (%s match(es))",
        address_hint,
        chosen_label,
        len(matches),
    )
    return True


async def _fill_search_dwelling(
    page: Page,
    text: str,
    *,
    address_hint: Optional[str] = None,
    q12: Optional[str] = None,
    timeout_ms: int = 12_000,
) -> None:
    field = page.locator("#search_dwelling")
    await field.click()
    await field.fill("")
    await field.type(text, delay=60)

    hint = (address_hint or "").strip() or parse_address_from_q12(q12)
    q12_raw = (q12 or "").strip() or None

    # Autocomplete dropdown (primary for Q1.3 search on planned works index)
    if hint and await _click_autocomplete_by_address(
        page, hint, timeout_ms, q12=q12_raw
    ):
        return

    try:
        items = page.locator(".ui-autocomplete:visible li.ui-menu-item")
        n_items = await items.count()
        if n_items == 1 and not hint:
            await items.first.click()
            log.info("search_dwelling: single autocomplete item clicked (no Q1.2 hint)")
            return
    except Exception:
        pass

    if hint and await _click_autocomplete_by_address(
        page, hint, timeout_ms, q12=q12_raw
    ):
        return

    # Fallback: results table (older / alternate UI)
    if hint and await _click_wi_row_by_address(page, hint, timeout_ms, q12=q12_raw):
        return

    try:
        rows = page.locator("tr.wi_row:not(.disabled)")
        if await rows.count() > 0:
            if await rows.count() == 1 and not hint:
                await rows.first.click()
                log.info("search_dwelling: single wi_row clicked (no Q1.2 hint)")
                return
    except Exception:
        pass

    if hint:
        num, pref = parse_q12_number_and_street_prefix(q12_raw)
        raise ValueError(
            f"Q1.3 search {text!r}: no result matched Q1.2 address {hint!r} "
            f"(house number {num!r}, street prefix {pref!r}). "
            "Check Q1.2 matches the property in the dropdown."
        )

    try:
        item = page.locator(".ui-autocomplete:visible li.ui-menu-item")
        await item.first.wait_for(state="visible", timeout=min(8_000, timeout_ms))
        await item.first.click()
        log.warning("search_dwelling: fell back to first autocomplete item (no Q1.2 provided)")
    except Exception:
        log.warning("search_dwelling: no autocomplete/wi_row match (value may still be in field)")


async def _click_pre_item_dialog_for_contract(
    page: Page,
    contract_id: str,
    timeout_ms: int,
    *,
    pre_item_item_id: Optional[str] = None,
    pre_item_works_id: Optional[str] = None,
    pre_item_item_type: Optional[str] = None,
) -> None:
    """
    Open the correct pw_works_show_pre_item_dialog row by parsing data-item-args JSON.

    When several links share the same contract_id (e.g. Global WO vs JIS / wi_pw), pass pre_item_item_id
    (EasyBOP ``item_id`` inside data-item-args), optionally pre_item_works_id and pre_item_item_type.
    """
    cid_raw = str(contract_id).strip()
    cid_norm = str(int(cid_raw)) if cid_raw.isdigit() else cid_raw
    want_item = str(pre_item_item_id).strip() if pre_item_item_id else None
    want_works = str(pre_item_works_id).strip() if pre_item_works_id else None

    links = page.locator('a[data-action="pw_works_show_pre_item_dialog"]')
    await links.first.wait_for(state="attached", timeout=timeout_ms)
    n = await links.count()

    async def gather(type_filter: Optional[str]) -> List[tuple]:
        wt = str(type_filter).strip().lower() if type_filter else None
        matched: List[tuple] = []
        for i in range(n):
            el = links.nth(i)
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                continue
            raw = await el.get_attribute("data-item-args")
            obj = _parse_data_item_args(raw)
            if not obj:
                continue
            if str(obj.get("contract_id", "")).strip() != cid_norm:
                continue
            if wt and str(obj.get("item_type", "")).lower() != wt:
                continue
            if want_works and str(obj.get("works_id", "")).strip() != want_works:
                continue
            if want_item and str(obj.get("item_id", "")).strip() != want_item:
                continue
            matched.append((el, obj))
        return matched

    candidates = await gather(pre_item_item_type)
    if not candidates and pre_item_item_type:
        candidates = await gather(None)

    if want_item:
        if len(candidates) == 1:
            chosen = candidates[0][0]
        elif len(candidates) == 0:
            raise ValueError(
                f"No visible pre_item link matches contract_id={cid_norm} and item_id={want_item} "
                f"(and optional works_id/type filters)."
            )
        else:
            raise ValueError(f"Multiple pre_item links matched item_id={want_item} (unexpected).")
    else:
        if len(candidates) == 1:
            chosen = candidates[0][0]
        elif len(candidates) > 1:
            ids = sorted({str(c[1].get("item_id")) for c in candidates})
            raise ValueError(
                f"Several 'Click on me' rows match contract_id={cid_norm} (item_ids={ids}). "
                "Set pre_item_item_id in the API body, JIS_PRE_ITEM_ITEM_ID in .env, "
                "or add an **item_id** column on the sheet (e.g. 27059 for JIS vs 24819 for Global WO)."
            )
        else:
            raise ValueError(
                f"No matching pre_item link for contract_id={cid_norm}. "
                "Check item_type / works_id filters or set pre_item_item_id."
            )

    await chosen.click()
    log.info("jis: clicked pw_works_show_pre_item_dialog (item_id=%s)", candidates[0][1].get("item_id"))


async def _wait_first_dialog_visible(page: Page, timeout_ms: int) -> None:
    dlg = page.locator(_DIALOG_LOCATOR)
    await dlg.first.wait_for(state="visible", timeout=timeout_ms)
    log.info("jis: first popup dialog visible")


async def _click_job_information_smart_form(page: Page, smart_form_id: str, timeout_ms: int) -> None:
    """Open Job information sheet (works_ajax_openSmartForm). Content often loads via AJAX after the first dialog."""
    sfid = (smart_form_id or "17799").strip()
    phase_ms = _jis_phase_ms(timeout_ms, floor=10_000, cap=18_000)
    deadline = time.monotonic() + phase_ms / 1000.0
    last_err: Optional[Exception] = None
    jis_name = re.compile(r"Job\s+information\s+sheet", re.I)

    while time.monotonic() < deadline:
        dlg = page.locator(_DIALOG_LOCATOR).last

        loc = dlg.locator(
            f'[data-action="{_SMART_FORM_ACTION}"][data-smart-form-id="{sfid}"]'
        ).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                log.info("jis: clicked Job information sheet (smart-form-id=%s)", sfid)
                return
        except Exception as e:
            last_err = e

        loc2 = dlg.locator(f'[data-action="{_SMART_FORM_ACTION}"]').filter(has_text=jis_name).first
        try:
            if await loc2.count() > 0 and await loc2.is_visible():
                await loc2.click()
                log.info("jis: clicked Job information sheet (matched text in dialog)")
                return
        except Exception as e:
            last_err = e

        for role in ("link", "button"):
            try:
                r = dlg.get_by_role(role, name=jis_name)
                if await r.count() > 0:
                    first = r.first
                    if await first.is_visible():
                        await first.click()
                        log.info("jis: clicked Job information sheet (role=%s, in dialog)", role)
                        return
            except Exception as e:
                last_err = e

        try:
            tloc = dlg.get_by_text("Job information sheet", exact=False).first
            if await tloc.count() > 0 and await tloc.is_visible():
                await tloc.click()
                log.info("jis: clicked Job information sheet (get_by_text in dialog)")
                return
        except Exception as e:
            last_err = e

        glob = page.locator(
            f'[data-action="{_SMART_FORM_ACTION}"][data-smart-form-id="{sfid}"]'
        ).first
        try:
            if await glob.count() > 0 and await glob.is_visible():
                await glob.click()
                log.info("jis: clicked Job information sheet (global, id=%s)", sfid)
                return
        except Exception as e:
            last_err = e

        await asyncio.sleep(_POLL_FAST_S)

    msg = (
        f"Job information sheet control not found within {phase_ms}ms "
        f"(data-smart-form-id={sfid!r}). Last error: {last_err!r}"
    )
    log.error("jis: %s", msg)
    raise ValueError(msg)


async def _wait_nested_smart_form_popup(
    page: Page,
    context: BrowserContext,
    timeout_ms: int,
    *,
    ignore_urls: Optional[set] = None,
) -> None:
    """
    After clicking Job information sheet: exit as soon as SmartForm tab, iframe, or extra dialog appears.
    """
    cap_ms = _jis_phase_ms(timeout_ms, floor=3_000, cap=8_000)
    deadline = time.monotonic() + cap_ms / 1000.0
    prev = await page.locator(_DIALOG_LOCATOR).count()
    while time.monotonic() < deadline:
        found = await _probe_smart_form_target(context, page, ignore_urls=ignore_urls)
        if found is not None:
            log.info("jis: SmartForm signal during post-click wait (%s)", found[1])
            return
        cur = await page.locator(_DIALOG_LOCATOR).count()
        if cur > prev:
            log.info("jis: extra dialog layer detected (count %s -> %s)", prev, cur)
            return
        await asyncio.sleep(_POLL_FAST_S)
    log.info(
        "jis: no SmartForm tab / extra dialog within %sms — continuing to SmartForm detection",
        cap_ms,
    )


def _absolute_easybop_url(src: str) -> str:
    """Turn iframe or relative SmartForm href into full https URL."""
    s = (src or "").strip()
    if not s:
        return s
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("/"):
        return "https://easybop.co.uk" + s
    return s


async def _probe_smart_form_target(
    context: BrowserContext,
    opener: Page,
    *,
    ignore_urls: Optional[set] = None,
) -> Optional[Tuple[Page, str]]:
    """Return (page, canonical_url) when SmartForm is already present — no waiting."""
    skip = {u.rstrip("/").lower() for u in (ignore_urls or set())}

    for p in list(context.pages):
        try:
            u = (p.url or "").split("#")[0]
            canon = u.split("?")[0].rstrip("/")
            if "/SmartForms/" in u and "login" not in u.lower() and canon.lower() not in skip:
                return p, canon
        except Exception:
            continue

    iframe = opener.locator(
        'iframe[src*="SmartForms"], iframe[src*="/SmartForms/"], .ui-dialog iframe'
    )
    if await iframe.count() > 0:
        src = await iframe.first.get_attribute("src") or ""
        if not src:
            try:
                await iframe.first.wait_for(state="attached", timeout=1_500)
                for _ in range(40):
                    src = await iframe.first.get_attribute("src") or ""
                    if src:
                        break
                    await asyncio.sleep(_POLL_FAST_S)
            except Exception:
                src = ""
        if src and "SmartForms" in src:
            full = _absolute_easybop_url(src)
            canon = full.split("?")[0].rstrip("/")
            if canon.lower() not in skip:
                return opener, canon

    u = (opener.url or "").split("#")[0]
    canon = u.split("?")[0].rstrip("/")
    if "/SmartForms/" in u and "login" not in u.lower() and canon.lower() not in skip:
        return opener, canon
    return None


async def _ensure_smart_form_fill_page(
    context: BrowserContext,
    opener: Page,
    timeout_ms: int,
    *,
    ignore_urls: Optional[set] = None,
) -> Tuple[Page, str]:
    """
    After Job information sheet opens, locate EasyBOP SmartForm (tab, iframe src, or inline URL).
    Returns (page_to_use, canonical_url_without_query) for filling.
    """
    phase_ms = _jis_phase_ms(timeout_ms, floor=10_000, cap=22_000)

    async def _materialize(found: Tuple[Page, str]) -> Tuple[Page, str]:
        page, canon = found
        if page is opener and "/smartforms/" not in (opener.url or "").lower():
            iframe = opener.locator('iframe[src*="SmartForms"], iframe[src*="/SmartForms/"]')
            if await iframe.count() > 0:
                src = await iframe.first.get_attribute("src") or ""
                full = _absolute_easybop_url(src)
                if full:
                    log.info("jis: SmartForm iframe -> goto %s", full)
                    await opener.goto(full, wait_until="domcontentloaded")
                    try:
                        await opener.wait_for_load_state("load", timeout=min(6_000, phase_ms))
                    except Exception:
                        pass
                    canon = opener.url.split("?")[0].rstrip("/")
                    page = opener
        else:
            log.info("jis: SmartForm browser tab: %s", canon)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(4_000, phase_ms))
        except Exception:
            pass
        return page, canon

    found = await _probe_smart_form_target(context, opener, ignore_urls=ignore_urls)
    if found is not None:
        return await _materialize(found)

    deadline = time.monotonic() + phase_ms / 1000.0
    while time.monotonic() < deadline:
        found = await _probe_smart_form_target(context, opener, ignore_urls=ignore_urls)
        if found is not None:
            return await _materialize(found)
        await asyncio.sleep(_POLL_FAST_S)

    raise ValueError(
        "Could not detect EasyBOP SmartForm (/SmartForms/...). "
        "Wait longer or check that Job information sheet opens the form."
    )


async def _snapshot_smart_form_urls(context: BrowserContext) -> set:
    urls: set = set()
    for p in list(context.pages):
        try:
            u = (p.url or "").split("#")[0].split("?")[0].rstrip("/")
            if "/SmartForms/" in u:
                urls.add(u)
        except Exception:
            pass
    return urls


async def _open_job_sheet_and_smart_form(
    page: Page,
    *,
    contract_id: str,
    smart_form_job_info_id: str,
    timeout_ms: int,
    pre_item_item_id: Optional[str],
    pre_item_works_id: Optional[str],
    pre_item_item_type: Optional[str],
) -> Tuple[Page, str]:
    """Open Job information sheet and wait for a *new* SmartForm URL (retry once after dialog reset)."""
    last_err: Optional[ValueError] = None
    t_sf = _jis_phase_ms(timeout_ms, floor=10_000, cap=18_000)
    for attempt in range(2):
        before_urls = await _snapshot_smart_form_urls(page.context)
        if attempt == 0:
            await open_work_instruction_job_information_sheet(
                page,
                contract_id,
                smart_form_job_info_id,
                timeout_ms,
                pre_item_item_id=pre_item_item_id,
                pre_item_works_id=pre_item_works_id,
                pre_item_item_type=pre_item_item_type,
                smart_form_urls_before=before_urls,
            )
        else:
            log.warning("jis: SmartForm open retry — dismiss dialogs and re-click Job information sheet")
            await _dismiss_stale_index_dialogs(page)
            if await page.locator(_DIALOG_LOCATOR).count() > 0:
                await _click_job_information_smart_form(page, smart_form_job_info_id, t_sf)
                await _wait_nested_smart_form_popup(
                    page, page.context, t_sf, ignore_urls=before_urls
                )
            else:
                await open_work_instruction_job_information_sheet(
                    page,
                    contract_id,
                    smart_form_job_info_id,
                    timeout_ms,
                    pre_item_item_id=pre_item_item_id,
                    pre_item_works_id=pre_item_works_id,
                    pre_item_item_type=pre_item_item_type,
                    smart_form_urls_before=before_urls,
                )
        t_detect = _jis_phase_ms(timeout_ms, floor=10_000, cap=22_000)
        try:
            return await _ensure_smart_form_fill_page(
                page.context, page, t_detect, ignore_urls=before_urls
            )
        except ValueError as e:
            last_err = e
            if attempt == 0:
                continue
            raise
    if last_err:
        raise last_err
    raise ValueError("Could not open SmartForm for Job information sheet.")


async def _wait_until_easybop_ready_to_expand_all(page: Page, until_mono: float) -> bool:
    """
    Wait until the sheet is actually interactive: form present, first question row visible,
    toolbar **All** visible and enabled (Ionic hydrated). Uses two consecutive OK polls to avoid racing.
    """
    stable = 0
    need = 2
    step = _POLL_FAST_S
    while time.monotonic() < until_mono:
        form = page.locator("form#smartForm-form")
        if await form.count() == 0:
            stable = 0
            await asyncio.sleep(step)
            continue
        q_ready = False
        try:
            if await form.locator('div[id^="dcl-question"]').count() > 0:
                q_ready = await form.locator('div[id^="dcl-question"]').first.is_visible()
            elif await form.locator("ion-label").count() > 0:
                q_ready = await form.locator("ion-label").first.is_visible()
        except Exception:
            q_ready = False
        if not q_ready:
            stable = 0
            await asyncio.sleep(step)
            continue

        btn = page.locator("ion-toolbar ion-button").filter(
            has_text=re.compile(r"^\s*all\s*$", re.I)
        )
        if await btn.count() == 0:
            btn = page.locator("ion-toolbar").get_by_role(
                "button",
                name=re.compile(r"^\s*all\s*$", re.I),
            )
        if await btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile(r"^\s*all\s*$", re.I))
        if await btn.count() == 0:
            stable = 0
            await asyncio.sleep(step)
            continue

        b = btn.first
        try:
            if not await b.is_visible():
                stable = 0
                await asyncio.sleep(step)
                continue
            if not await b.is_enabled():
                stable = 0
                await asyncio.sleep(step)
                continue
        except Exception:
            stable = 0
            await asyncio.sleep(step)
            continue

        stable += 1
        if stable >= need:
            log.info("jis: SmartForm sheet ready (questions + All actionable)")
            return True
        await asyncio.sleep(step)
    return False


async def _prepare_easybop_smart_form_for_fill(page: Page, timeout_ms: int) -> None:
    """
    1) Wait until the form sheet is ready (first question + **All** visible and enabled).
    2) Click **All** once to load every section.
    3) Exit as soon as labels are visible — no fixed sleeps.
    """
    phase_ms = _jis_phase_ms(timeout_ms, floor=6_000, cap=15_000)
    budget = phase_ms / 1000.0
    deadline = time.monotonic() + budget

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(4_000, phase_ms))
    except Exception:
        pass

    ready_limit = time.monotonic() + min(budget * 0.65, 10.0)
    if not await _wait_until_easybop_ready_to_expand_all(page, ready_limit):
        log.warning(
            "jis: SmartForm readiness timeout (question + All) — will still try **All**; fills may miss rows"
        )

    try:
        n_before = await page.locator("form#smartForm-form ion-label").count()
    except Exception:
        n_before = 0

    all_phase_deadline = time.monotonic() + min(8.0, max(1.5, deadline - time.monotonic()))
    all_clicked = False
    while time.monotonic() < all_phase_deadline and not all_clicked:
        btn = page.locator("ion-toolbar ion-button").filter(
            has_text=re.compile(r"^\s*all\s*$", re.I)
        )
        if await btn.count() == 0:
            btn = page.locator("ion-toolbar").get_by_role(
                "button",
                name=re.compile(r"^\s*all\s*$", re.I),
            )
        if await btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile(r"^\s*all\s*$", re.I))
        if await btn.count() == 0:
            await asyncio.sleep(_POLL_FAST_S)
            continue

        b = btn.first
        try:
            await b.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        for force in (False, True):
            try:
                await b.click(timeout=4_000, force=force)
                all_clicked = True
                log.info("jis: SmartForm — clicked toolbar 'All' after ready (force=%s)", force)
                break
            except Exception as e:
                log.debug("jis: All click (force=%s): %s", force, e)
        if all_clicked:
            break
        await asyncio.sleep(_POLL_FAST_S)

    if not all_clicked:
        log.warning(
            "jis: SmartForm **All** not clicked — form may stay paginated; later Q fields can fail"
        )
    elif n_before >= 0:
        expand_deadline = time.monotonic() + 3.0
        while time.monotonic() < expand_deadline:
            try:
                n_after = await page.locator("form#smartForm-form ion-label").count()
            except Exception:
                n_after = 0
            if n_after > n_before:
                log.debug("jis: SmartForm ion-label count %s -> %s after All", n_before, n_after)
                break
            await asyncio.sleep(_POLL_FAST_S)

    label_deadline = time.monotonic() + min(4.0, max(0.5, deadline - time.monotonic()))
    while time.monotonic() < label_deadline:
        labs = page.locator("form#smartForm-form ion-label")
        if await labs.count() > 0:
            try:
                if await labs.first.is_visible():
                    return
            except Exception:
                pass
        await asyncio.sleep(_POLL_FAST_S)

    log.warning(
        "jis: SmartForm prep timeout (%ss) — proceeding with fill attempts anyway",
        round(budget, 1),
    )


async def _click_easybop_smart_form_show_all(page: Page, timeout_ms: int) -> None:
    """Kept for compatibility; same as full prep (form + All + labels)."""
    await _prepare_easybop_smart_form_for_fill(page, timeout_ms)


def _map_q11_start_within_days_to_radio_label(val: str) -> str:
    """
    Map sheet / scope_days numbers to EasyBOP JIS Smart Form radios:
    urgent, 1-2 day, 3-5 day, 1 week, 2 week, 3 week.
    """
    v = val.strip().lower()
    labels = ("urgent", "1-2 day", "3-5 day", "1 week", "2 week", "3 week")
    for lab in labels:
        if lab in v:
            return lab
    try:
        num = re.sub(r"[^\d.]", "", v)
        d = int(float(num)) if num else -1
    except ValueError:
        return val.strip()
    if d < 0:
        return val.strip()
    if d == 0:
        return "urgent"
    if d <= 2:
        return "1-2 day"
    if d <= 5:
        return "3-5 day"
    if d <= 8:
        return "1 week"
    if d <= 14:
        return "2 week"
    return "3 week"


def _normalize_smart_form_field_value(q_key: str, raw: str) -> str:
    s = re.sub(r",\s*\(derived\)\s*$", "", raw.strip(), flags=re.IGNORECASE).strip()
    if not s:
        return raw.strip()
    if q_key == "Q1.1":
        return _map_q11_start_within_days_to_radio_label(s)
    return s


def _rich_text_html_from_plain(val: str) -> str:
    parts = val.replace("\r\n", "\n").split("\n")
    return "<br>".join(html.escape(p) for p in parts)


def _norm_option_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _split_multi_values(raw: str) -> List[str]:
    """
    Supports single and multi-value prop-multiple answers.
    Example: "Semi- detached, Front" -> ["semi- detached", "front"].
    """
    txt = (raw or "").strip()
    if not txt:
        return []
    parts = [p.strip() for p in re.split(r"[,\n;|]+", txt) if p.strip()]
    return parts or [txt]


async def _ion_item_is_selected(it: Any) -> bool:
    try:
        icon = it.locator("prop-icon").first
        if await icon.count() == 0:
            return False
        cls = (await icon.get_attribute("class") or "").lower()
        return "icon-primary" in cls
    except Exception:
        return False


async def _easybop_smart_form_next_page(ctx: Any) -> bool:
    """Ionic footer: advance multi-page smart forms (e.g. '1 OF 11')."""
    footer = ctx.locator("ion-footer")
    if await footer.count() == 0:
        return False
    for pattern in (
        re.compile(r"^\s*next\s*$", re.I),
        re.compile(r"^\s*forward\s*$", re.I),
    ):
        btn = footer.get_by_role("button", name=pattern)
        if await btn.count() > 0:
            try:
                b = btn.first
                if await b.is_visible():
                    await b.click()
                    try:
                        await ctx.wait_for_load_state("domcontentloaded", timeout=2_500)
                    except Exception:
                        await asyncio.sleep(_POLL_FAST_S)
                    return True
            except Exception:
                pass
        alt = footer.locator("ion-button").filter(has_text=pattern)
        if await alt.count() > 0:
            try:
                b = alt.first
                if await b.is_visible():
                    await b.click()
                    try:
                        await ctx.wait_for_load_state("domcontentloaded", timeout=2_500)
                    except Exception:
                        await asyncio.sleep(_POLL_FAST_S)
                    return True
            except Exception:
                pass
    return False


async def _fill_easybop_ionic_smart_form(ctx: Any, q_key: str, val: str, errors: List[str]) -> bool:
    """
    EasyBOP Smart Forms (Angular + Ionic): prop-multiple uses ion-item[role=listitem];
    rich-text uses contenteditable divs — no native radios / text inputs in the dump.
    """
    form = ctx.locator("form#smartForm-form")
    if await form.count() == 0:
        return False
    form = form.first
    pat = re.compile(re.escape(q_key), re.I)
    wanted_values = [_norm_option_text(v) for v in _split_multi_values(val)]
    vl_norm = wanted_values[0] if wanted_values else _norm_option_text(val)

    for page_i in range(14):
        labels = form.locator("ion-label").filter(has_text=pat)
        if await labels.count() == 0:
            if not await _easybop_smart_form_next_page(ctx):
                log.debug("easybop ionic: %s not found after %s page(s)", q_key, page_i)
                return False
            continue

        lab = labels.first
        try:
            await lab.scroll_into_view_if_needed(timeout=8_000)
        except Exception:
            pass

        block = lab.locator('xpath=ancestor::div[starts-with(@id, "dcl-question")][1]')
        if await block.count() == 0:
            errors.append(f"[SmartForm {q_key}] could not find dcl-question wrapper")
            return False
        block = block.first

        items = block.locator('ion-item[role="listitem"]')
        ni = await items.count()
        if ni > 0:
            clicked_any = False
            all_wanted_satisfied = True
            for want in wanted_values or [vl_norm]:
                matched_item = None

                # Pass 1: exact normalized match
                for j in range(ni):
                    it = items.nth(j)
                    try:
                        txt_norm = _norm_option_text(await it.inner_text())
                    except Exception:
                        continue
                    if want == txt_norm:
                        matched_item = it
                        break

                # Pass 2: fuzzy contains fallback
                if matched_item is None:
                    for j in range(ni):
                        it = items.nth(j)
                        try:
                            txt_norm = _norm_option_text(await it.inner_text())
                        except Exception:
                            continue
                        if want in txt_norm or txt_norm in want:
                            matched_item = it
                            break

                if matched_item is None:
                    errors.append(f"[SmartForm {q_key}] no list item matching {want!r} (prop-multiple)")
                    all_wanted_satisfied = False
                    continue

                # Avoid re-clicking selected item (prevents toggle/unselect behavior).
                if await _ion_item_is_selected(matched_item):
                    continue

                await matched_item.scroll_into_view_if_needed(timeout=5_000)
                try:
                    await matched_item.click(timeout=5_000)
                except Exception:
                    await matched_item.click(timeout=5_000, force=True)
                clicked_any = True
                try:
                    await block.locator("ion-item[role='listitem']").first.wait_for(
                        state="visible", timeout=1_500
                    )
                except Exception:
                    await asyncio.sleep(_POLL_FAST_S)

            return clicked_any or all_wanted_satisfied

        rich = block.locator("rich-text div[contenteditable='true']").first
        if await rich.count() > 0:
            try:
                await rich.scroll_into_view_if_needed(timeout=8_000)
                await rich.click(timeout=5_000)
                try:
                    await rich.fill(val)
                except Exception:
                    h = _rich_text_html_from_plain(val)
                    await rich.evaluate(
                        """(el, html) => {
                          el.innerHTML = html;
                          el.dispatchEvent(new Event('input', { bubbles: true }));
                          el.dispatchEvent(new Event('change', { bubbles: true }));
                          el.blur();
                        }""",
                        h,
                    )
                return True
            except Exception as exc:
                errors.append(f"[SmartForm {q_key}] rich-text: {exc}")
                return False

        errors.append(f"[SmartForm {q_key}] unknown control under EasyBOP question block")
        return False

    return False


async def _fill_legacy_smart_form_blocks(ctx: Any, q_key: str, value: str, errors: List[str]) -> bool:
    """Generic Smart Form: native radios, inputs, selects inside labeled blocks."""
    val = str(value).strip()
    if not val:
        return False
    pat = re.compile(re.escape(q_key), re.I)
    blocks = ctx.locator("div, section, fieldset, tr, li, article, main, form").filter(has_text=pat)
    nb = await blocks.count()
    for i in range(min(nb, 24)):
        block = blocks.nth(i)
        try:
            if not await block.is_visible():
                continue
        except Exception:
            continue

        radios = block.get_by_role("radio")
        rc = await radios.count()
        if rc > 0:
            for j in range(rc):
                r = radios.nth(j)
                try:
                    av = (await r.get_attribute("value") or "").strip()
                    txt = await r.evaluate(
                        """e => {
                          const l = e.labels && e.labels[0];
                          return (l && l.innerText) ? l.innerText.trim() : '';
                        }"""
                    )
                except Exception:
                    txt = ""
                hay = f"{txt} {av}".strip().lower()
                vl = val.lower()
                if vl == txt.lower() or vl == av.lower() or vl == hay:
                    await r.check()
                    return True
            for j in range(rc):
                r = radios.nth(j)
                try:
                    txt = await r.evaluate(
                        "e => (e.labels&&e.labels[0]) ? e.labels[0].innerText.trim() : ''"
                    )
                except Exception:
                    txt = ""
                tl, vl = txt.lower(), val.lower()
                if vl in tl or tl in vl:
                    await r.check()
                    return True
            continue

        inp = block.locator("textarea, input[type='text'], input[type='search']").first
        if await inp.count() == 0:
            inp = block.locator("input:not([type='hidden'])").first
        if await inp.count() > 0:
            try:
                typ = (await inp.get_attribute("type") or "").lower()
                if typ in ("radio", "checkbox", "submit", "button", "image", "hidden"):
                    continue
            except Exception:
                pass
            await inp.click()
            await inp.fill(val)
            return True

        sel = block.locator("select").first
        if await sel.count() > 0:
            try:
                await sel.select_option(label=val, timeout=8_000)
            except Exception:
                try:
                    await sel.select_option(value=val)
                except Exception as exc:
                    errors.append(f"[SmartForm {q_key}] select: {exc}")
                    continue
            return True

    return False


async def _fill_smart_form_on_context(ctx: Any, q_key: str, value: str, errors: List[str]) -> bool:
    """
    ctx: Page or Frame — EasyBOP Smart Form may embed fields in iframes.
    """
    val = str(value).strip()
    if not val:
        return False
    if await _fill_easybop_ionic_smart_form(ctx, q_key, val, errors):
        return True
    return await _fill_legacy_smart_form_blocks(ctx, q_key, val, errors)


async def _fill_smart_form_section(page: Page, q_key: str, value: str, errors: List[str]) -> bool:
    """Try main page then child frames (Smart Forms sometimes nest)."""
    ctxs: List[Any] = [page]
    try:
        for fr in page.frames:
            if fr != page.main_frame:
                ctxs.append(fr)
    except Exception:
        pass
    for ctx in ctxs:
        try:
            if await _fill_smart_form_on_context(ctx, q_key, value, errors):
                return True
        except Exception as e:
            log.debug("SmartForm fill try on %s: %s", type(ctx).__name__, e)
    return False


async def open_work_instruction_job_information_sheet(
    page: Page,
    contract_id: str,
    smart_form_id: str,
    timeout_ms: int,
    *,
    pre_item_item_id: Optional[str] = None,
    pre_item_works_id: Optional[str] = None,
    pre_item_item_type: Optional[str] = None,
    smart_form_urls_before: Optional[set] = None,
) -> None:
    """
    After Q1.3 / dwelling search: open work-instruction pre-item dialog, then Job information sheet smart form.
    Async sequence only; further fields in the inner popup are intentionally left for a follow-up step.
    """
    await _dismiss_stale_index_dialogs(page)
    t = _jis_phase_ms(timeout_ms, floor=10_000, cap=20_000)
    t_sf = _jis_phase_ms(timeout_ms, floor=10_000, cap=18_000)
    await _click_pre_item_dialog_for_contract(
        page,
        contract_id,
        t,
        pre_item_item_id=pre_item_item_id,
        pre_item_works_id=pre_item_works_id,
        pre_item_item_type=pre_item_item_type,
    )
    await _wait_first_dialog_visible(page, t)
    await _click_job_information_smart_form(page, smart_form_id, t_sf)
    await _wait_nested_smart_form_popup(
        page, page.context, t_sf, ignore_urls=smart_form_urls_before
    )


async def _fill_jis_labeled_row(
    page: Page,
    label_re: re.Pattern,
    value: str,
    errors: List[str],
) -> bool:
    """Single-row variant: first works_table row whose label matches."""
    rows = page.locator("table.works_table tr").filter(has=page.locator("label").filter(has_text=label_re))
    if await rows.count() == 0:
        return False
    inp = rows.first.locator(
        "select.input, input.input, textarea.input, select, input[type='text'], textarea"
    ).first
    if await inp.count() == 0:
        return False
    try:
        tag = await inp.evaluate("e=>e.tagName.toLowerCase()")
        if tag == "select":
            try:
                await inp.select_option(label=value, timeout=8_000)
            except Exception:
                await inp.select_option(value=value)
        else:
            await inp.fill(value)
        return True
    except Exception as exc:
        errors.append(f"{label_re.pattern!r}: {exc}")
        log.warning("jis row fill error: %s", exc)
        return False


async def _try_click_locator(
    loc: Any,
    *,
    label: str,
    timeout_ms: int = 15_000,
) -> bool:
    try:
        if await loc.count() == 0:
            log.info("jis: %s — not found", label)
            return False
        btn = loc.first
        visible = await btn.is_visible()
        enabled = await btn.is_enabled()
        log.info("jis: %s — visible=%s enabled=%s", label, visible, enabled)
        if not visible:
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
                log.info("jis: clicked %s (%s)", label, click_mode)
                return True
            except Exception as click_exc:
                log.warning("jis: %s click (%s) failed: %s", label, click_mode, click_exc)
    except Exception as exc:
        log.warning("jis: %s failed: %s", label, exc)
    return False


async def _select_jis_complete_option(page: Page, *, timeout_ms: int = 10_000) -> bool:
    """After first Save, pick 'Complete' from the Ionic action sheet / modal."""
    complete_re = re.compile(r"^\s*complete\s*$", re.I)
    deadline = time.monotonic() + timeout_ms / 1000.0
    candidates_fn = lambda: [
        (
            "ion-action-sheet button Complete",
            page.locator(
                "ion-action-sheet button.action-sheet-button, "
                "ion-action-sheet .action-sheet-button"
            ).filter(has_text=complete_re),
        ),
        (
            "ion-action-sheet ion-label Complete",
            page.locator("ion-action-sheet ion-label").filter(has_text=complete_re),
        ),
        (
            "ion-alert Complete",
            page.locator("ion-alert .alert-radio-label, ion-alert ion-label").filter(has_text=complete_re),
        ),
        (
            "ion-modal ion-item Complete",
            page.locator("ion-modal ion-item").filter(has_text=complete_re),
        ),
        (
            "ion-modal ion-label Complete",
            page.locator("ion-modal ion-label").filter(has_text=complete_re),
        ),
        (
            "ion-item Complete",
            page.locator("ion-item").filter(has_text=complete_re),
        ),
        (
            "ion-label Complete",
            page.locator("ion-label").filter(has_text=complete_re),
        ),
        (
            "role=radio Complete",
            page.get_by_role("radio", name=complete_re),
        ),
        (
            "text Complete",
            page.get_by_text(complete_re),
        ),
    ]

    log.info("jis: looking for Complete option…")
    while time.monotonic() < deadline:
        for name, loc in candidates_fn():
            if await _try_click_locator(loc, label=name, timeout_ms=2_500):
                return True
        await asyncio.sleep(_POLL_FAST_S)

    log.error("jis: Complete option not found after first Save")
    return False


_GLOBAL_JIS_ROW_RE = re.compile(r"Global:\s*Job\s+information\s+sheet", re.I)


async def _wait_for_global_job_info_sheet_complete(
    index_page: Page,
    *,
    timeout_ms: int,
    pre_item_item_id: Optional[str] = None,
) -> bool:
    """
    After SmartForm block Save: wait for the pre-item dialog row
    'Global: Job information sheet' — last cell gains pw_complete_color when saved complete.
    """
    phase_ms = _jis_phase_ms(timeout_ms, floor=10_000, cap=30_000)
    deadline = time.monotonic() + phase_ms / 1000.0
    want_item = str(pre_item_item_id).strip() if pre_item_item_id else None

    try:
        await index_page.bring_to_front()
    except Exception:
        pass

    log.info("jis: waiting for Global Job information sheet row (pw_complete_color)…")
    while time.monotonic() < deadline:
        scopes: List[Any] = []
        dlg = index_page.locator(".ui-dialog:visible")
        if await dlg.count() > 0:
            scopes.append(dlg.last)
        scopes.append(index_page)

        for scope in scopes:
            rows = scope.locator("tr").filter(
                has=scope.locator("td").filter(has_text=_GLOBAL_JIS_ROW_RE)
            )
            n = await rows.count()
            for i in range(n):
                row = rows.nth(i)
                if want_item:
                    link = row.locator('a[data-action="pw_works_show_pre_item_dialog"]')
                    if await link.count() > 0:
                        args = await link.first.get_attribute("data-item-args") or ""
                        obj = _parse_data_item_args(args)
                        if obj and str(obj.get("item_id", "")).strip() != want_item:
                            continue
                status = row.locator("td.pw_complete_color").last
                if await status.count() == 0:
                    status = row.locator("td.b_last").last
                if await status.count() == 0:
                    status = row.locator("td").last
                try:
                    cls = (await status.get_attribute("class") or "").lower()
                    if "pw_complete_color" in cls:
                        log.info(
                            "jis: Global Job information sheet complete (pw_complete_color visible)"
                        )
                        return True
                except Exception:
                    pass

        await asyncio.sleep(_POLL_FAST_S)

    log.warning(
        "jis: timed out (%sms) waiting for Global Job information sheet pw_complete_color",
        phase_ms,
    )
    return False


async def _submit_jis_smart_form(page: Page, *, timeout_ms: int = 20_000) -> Tuple[bool, str]:
    """
    EasyBOP JIS save flow:
    1. Toolbar Save (ion-button.in-toolbar)
    2. Select Complete
    3. Block Save (ion-button expand=block)
    """
    save_re = re.compile(r"^\s*save\s*$", re.I)
    log.info("jis: submit — step 1/3: toolbar Save")

    step1_locators = [
        page.locator("ion-button.in-toolbar").filter(has_text=save_re),
        page.locator("ion-toolbar ion-button[color='success']").filter(has_text=save_re),
        page.locator("ion-toolbar ion-button").filter(has_text=save_re),
    ]
    step1_ok = False
    step1_via = ""
    for i, loc in enumerate(step1_locators):
        if await _try_click_locator(loc, label=f"toolbar Save [{i}]", timeout_ms=timeout_ms):
            step1_ok = True
            step1_via = f"toolbar Save [{i}]"
            break
    if not step1_ok:
        return False, "step 1 toolbar Save not clicked"

    overlay_deadline = time.monotonic() + 5.0
    while time.monotonic() < overlay_deadline:
        if await page.locator("ion-action-sheet, ion-alert, ion-modal").count() > 0:
            break
        await asyncio.sleep(_POLL_FAST_S)

    log.info("jis: submit — step 2/3: select Complete")
    if not await _select_jis_complete_option(page, timeout_ms=min(10_000, timeout_ms)):
        return False, "step 2 Complete not selected"

    save_block_deadline = time.monotonic() + 5.0
    block_save_re = save_re
    while time.monotonic() < save_block_deadline:
        if await page.locator("ion-button[expand='block']").filter(has_text=block_save_re).count() > 0:
            break
        await asyncio.sleep(_POLL_FAST_S)

    log.info("jis: submit — step 3/3: block Save")
    step3_locators = [
        page.locator("ion-button[expand='block']").filter(has_text=save_re),
        page.locator("ion-button.button-block").filter(has_text=save_re),
        page.locator("ion-button.ion-color-success:not(.in-toolbar)").filter(has_text=save_re),
        page.locator("ion-button:not(.in-toolbar)").filter(has_text=save_re),
        page.locator("ion-footer ion-button").filter(has_text=save_re),
        page.locator("ion-button").filter(has_text=save_re),
    ]
    for i, loc in enumerate(step3_locators):
        if await _try_click_locator(loc, label=f"block Save [{i}]", timeout_ms=timeout_ms):
            try:
                await page.wait_for_load_state("load", timeout=min(15_000, timeout_ms))
            except Exception as e:
                log.warning("jis: block Save post-click load wait: %s", e)
            log.info("jis: submit complete (%s → Complete → block Save [%s])", step1_via, i)
            return True, f"{step1_via} → Complete → block Save [{i}]"

    return False, "step 3 block Save not clicked"


async def _click_jis_save_button(page: Page, *, timeout_ms: int = 15_000) -> Tuple[bool, str]:
    """Legacy single-step save — prefer _submit_jis_smart_form for SmartForm JIS."""
    return await _submit_jis_smart_form(page, timeout_ms=timeout_ms)


async def fill_jis_planned_works(
    page: Page,
    q_values: Dict[str, Optional[str]],
    *,
    contract_id: str,
    smart_form_job_info_id: str = "17799",
    open_job_information_sheet: bool = True,
    pre_item_item_id: Optional[str] = None,
    pre_item_works_id: Optional[str] = None,
    pre_item_item_type: Optional[str] = None,
    works_id: Optional[str] = None,
    submit: bool = False,
    timeout_ms: int = 20_000,
    relogin_username: Optional[str] = None,
    relogin_password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    q_fill_order: Optional[List[str]] = None,
    return_to_index_after: bool = False,
    fill_appointments: bool = False,
    appointment_row: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[List[str], int, int, Optional[str], Page]:
    """
    q_values: keys Q1.1 .. Q5.2. Uses Q1.3 for #search_dwelling; opens Job information sheet;
    detects SmartForm URL; fills Q on SmartForm (or legacy works_table). Returns (errors, filled, smart_form_url, page_used_for_fill).
    """
    order = q_fill_order or [
        "Q1.1",
        "Q1.2",
        "Q1.3",
        "Q1.4",
        "Q1.5",
        "Q1.6",
        "Q2.1",
        "Q2.2",
        "Q3.1",
        "Q3.2",
        "Q4.1",
        "Q4.2",
        "Q5.1",
        "Q5.2",
    ]
    errors: List[str] = []
    filled = 0
    appointments_filled = 0
    smart_form_url: Optional[str] = None
    fill_page: Page = page

    search_text = (q_values.get("Q1.3") or "").strip()
    if not search_text:
        raise ValueError("Q1.3 is required for dwelling search but is empty for this Example row.")

    q12_raw = (q_values.get("Q1.2") or "").strip()
    address_hint = parse_address_from_q12(q12_raw)

    direct_wid = str(works_id or pre_item_works_id or "").strip() or None
    if direct_wid:
        await navigate_to_works_wi_page(
            page,
            direct_wid,
            contract_id,
            timeout_ms,
            relogin_username=relogin_username,
            relogin_password=relogin_password,
            after_relogin=after_relogin,
        )
        pre_item_works_id = direct_wid
    else:
        await ensure_jis_index_ready(
            page,
            contract_id,
            relogin_username,
            relogin_password,
            timeout_ms,
            after_relogin=after_relogin,
        )

        await _fill_search_dwelling(
            page,
            search_text,
            address_hint=address_hint or None,
            q12=q12_raw or None,
            timeout_ms=timeout_ms,
        )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(5_000, timeout_ms))
        except Exception as e:
            log.warning("post-dwelling-select load wait: %s", e)

    smart_form_prepared = False
    if open_job_information_sheet:
        try:
            fill_page, smart_form_url = await _open_job_sheet_and_smart_form(
                page,
                contract_id=contract_id,
                smart_form_job_info_id=smart_form_job_info_id,
                timeout_ms=timeout_ms,
                pre_item_item_id=pre_item_item_id,
                pre_item_works_id=pre_item_works_id,
                pre_item_item_type=pre_item_item_type,
            )
            try:
                await fill_page.bring_to_front()
            except Exception:
                pass
            log.info("jis: SmartForm ready for Q fill — %s", smart_form_url)
            await _prepare_easybop_smart_form_for_fill(fill_page, _jis_phase_ms(timeout_ms, cap=15_000))
            smart_form_prepared = True
        except ValueError as e:
            log.warning("%s", e)
            errors.append(str(e))
    else:
        try:
            await page.wait_for_selector("table.works_table", state="attached", timeout=min(12_000, timeout_ms))
        except Exception:
            log.warning("works_table not found after dwelling search — Q fields may not fill")

    for qkey in order:
        val = q_values.get(qkey)
        if not val or not str(val).strip():
            continue
        v = str(val).strip()
        v = _normalize_smart_form_field_value(qkey, v)
        label_re = re.compile(rf"\b{re.escape(qkey)}\b", re.I)
        done = False
        if smart_form_url:
            done = await _fill_smart_form_section(fill_page, qkey, v, errors)
        if done:
            filled += 1
            log.info("jis: filled SmartForm %s", qkey)
            continue
        if await _fill_jis_labeled_row(fill_page, label_re, v, errors):
            filled += 1
            log.info("jis: filled works_table %s", qkey)
        else:
            msg = f"No matching row/field for {qkey}"
            errors.append(msg)
            log.warning(msg)

    if fill_appointments and appointment_row and smart_form_url:
        if not smart_form_prepared:
            await _prepare_easybop_smart_form_for_fill(
                fill_page, _jis_phase_ms(timeout_ms, cap=15_000)
            )
        fill_appointments_on_smart_form = _get_appointment_automation().fill_appointments_on_smart_form
        appt_errors, appt_filled = await fill_appointments_on_smart_form(
            fill_page,
            appointment_row,
            timeout_ms=_jis_phase_ms(timeout_ms, floor=12_000, cap=20_000),
        )
        errors.extend(appt_errors)
        appointments_filled = appt_filled
        if appt_filled:
            log.info("jis: filled %s appointment slot(s)", appt_filled)

    if submit and smart_form_url:
        log.info("jis: submit=true — running Save → Complete → Save")
        clicked, via = await _submit_jis_smart_form(
            fill_page, timeout_ms=_jis_phase_ms(timeout_ms, floor=12_000, cap=18_000)
        )
        if not clicked:
            errors.append(f"submit failed ({via})")
        else:
            log.info("jis: submit finished via %s", via)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            complete_ok = await _wait_for_global_job_info_sheet_complete(
                page,
                timeout_ms=_jis_phase_ms(timeout_ms, floor=10_000, cap=30_000),
                pre_item_item_id=pre_item_item_id,
            )
            if not complete_ok:
                errors.append(
                    "Global Job information sheet row did not show complete (pw_complete_color)"
                )

    if fill_page is not page and smart_form_url:
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await fill_page.close()
        except Exception:
            pass
        await _dismiss_stale_index_dialogs(page)

    if return_to_index_after:
        await return_to_planned_works_index(
            page,
            contract_id,
            timeout_ms,
            relogin_username=relogin_username,
            relogin_password=relogin_password,
            after_relogin=after_relogin,
        )
        fill_page = page

    return errors, filled, appointments_filled, smart_form_url, fill_page
