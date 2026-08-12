"""
EasyBOP Playwright automation — login + works instruction form filler.

Design notes:
- A persistent BrowserContext (with saved cookies) is shared across requests.
- The caller is responsible for opening/closing pages from that context.
- strip_derived() removes the ',(derived)' annotation before sending values.
- submit=False (default) so test runs never accidentally save a record.
"""

import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from playwright.async_api import Page

from config import settings

log = logging.getLogger("easybop.automation")

LOGIN_URL = "https://easybop.co.uk/"
FORM_URL_STATIC = "https://easybop.co.uk/a_planned_works/z_works/works_details.php?contract_id=0"

# <select> fields — use select_option() not fill()
_SELECT_FIELDS = {
    "resident_type", "occupancy_type", "n_bedrooms", "property_type",
    "vulnerability_id", "client_property_status", "year_built", "resident_title",
}

# Fields we never touch (handled separately or read-only)
_SKIP_FIELDS = {
    "resident_name",         # disabled — auto-built by page JS
    "scope_of_works_ids[]",  # checkboxes — handled in _fill_sow()
    "scope_of_work_code_human",
    "contract_name",         # display label only
    "vulnerability_codes",   # internal array
    "zone_id",               # hidden — set by autocomplete JS
    "search_property",       # autocomplete — handled in _fill_search_property()
    "asbestos_status",       # no #asbestos_status id; handled via label match
}

# EasyBOP UI section titles used in logs
SEC_SELECT_SCOPE  = "Select Scope of Work"
SEC_WORKS_INFO    = "Works Information"
SEC_PROPERTY      = "Property Details"
SEC_ADDRESS       = "Address"
SEC_RESIDENT      = "Resident Details"
SEC_ALT_ADDRESS   = "Alternative Address"
SEC_ADDITIONAL    = "Additional details"
SEC_PROPERTY_TEST = "Property test"
SEC_DISABLED      = "Disabled Adaptions"
SEC_OTHER         = "Other Information"

# Field keys per section (matches workinstruction_form.html)
KEYS_WORKS_INFORMATION: Tuple[str, ...] = ("scope_days", "contract_phase")
KEYS_PROPERTY_DETAILS: Tuple[str, ...] = (
    "property_ref", "alt_property_ref", "resident_type", "occupancy_type",
    "n_bedrooms", "property_type", "vulnerability_id", "zone_name",
    "client_order_reference", "client_estate_reference", "client_property_status", "year_built",
)
KEYS_ADDRESS: Tuple[str, ...] = (
    "address_1", "address_2", "address_3", "address_4",
    "town", "county", "postcode", "default_invoice_address",
)
KEYS_RESIDENT: Tuple[str, ...] = (
    "resident_title", "resident_f_name", "resident_l_name",
    "resident_mobile", "resident_tel", "resident_email",
)
KEYS_ALT_ADDRESS: Tuple[str, ...] = (
    "alt_address_1", "alt_address_2", "alt_address_3", "alt_address_4",
    "alt_town", "alt_county", "alt_postcode", "alt_country",
)
KEYS_OTHER_INFORMATION: Tuple[str, ...] = ("udf_pw_c_works_instructions_5065", "contract_id")

KNOWN_FORM_KEYS: Set[str] = set((
    "search_property", "scope_of_works_ids[]",
    *KEYS_WORKS_INFORMATION, *KEYS_PROPERTY_DETAILS, *KEYS_ADDRESS,
    *KEYS_RESIDENT, *KEYS_ALT_ADDRESS, *KEYS_OTHER_INFORMATION,
))

_BCC_SURVEYOR_RE  = re.compile(r"BCC\s*Surveyor",   re.IGNORECASE)
_ASBESTOS_RE      = re.compile(r"Asbestos\s*Status", re.IGNORECASE)
_SECTION_ROOT_KEYS = frozenset({
    "property_search", "scope_of_work", "works_information", "property_details",
    "address", "resident_details", "alternative_address", "additional_details",
    "property_test", "disabled_adaptations", "other_information",
})


# ─────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────

def strip_derived(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r",\(derived\)$", "", str(value)).strip()
    return cleaned or None


def clean_fields(raw: Dict[str, Any]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for k, v in raw.items():
        out[k] = None if (v is None or v is False) else ("1" if v is True else strip_derived(v))
    return out


def is_sectioned_easybop_payload(d: Dict[str, Any]) -> bool:
    return bool(d) and any(isinstance(d.get(k), dict) for k in _SECTION_ROOT_KEYS)


def sections_to_flat_form_fields(d: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten section-grouped payload (map_to_form_fileds.json) → flat HTML field names."""
    flat: Dict[str, Any] = {}

    ps = d.get("property_search") or {}
    flat["search_property"] = ps.get("search_property")

    sow = d.get("scope_of_work") or {}
    ids = sow.get("scope_of_works_ids")
    if isinstance(ids, list) and ids:
        flat["scope_of_works_ids[]"] = ids[0]
    elif isinstance(ids, str) and ids.strip():
        flat["scope_of_works_ids[]"] = ids
    flat["scope_of_work_code_human"] = sow.get("scope_of_work_code_human")

    for key in ("scope_days", "contract_phase"):
        flat[key] = (d.get("works_information") or {}).get(key)

    for section in (
        "property_details", "address", "resident_details",
        "alternative_address", "additional_details", "property_test", "disabled_adaptations",
    ):
        flat.update(d.get(section) or {})

    oi = d.get("other_information") or {}
    if "bcc_surveyor_1" in oi:
        flat["udf_pw_c_works_instructions_5065"] = oi["bcc_surveyor_1"]
    flat.update({k: v for k, v in oi.items() if k != "bcc_surveyor_1"})

    return flat


# ─────────────────────────────────────────────────────
# Login / navigation
# ─────────────────────────────────────────────────────

async def shows_login_wall(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        return await lb.count() > 0 and await lb.first.is_visible()
    except Exception:
        return False


async def page_looks_like_login_screen(page: Page) -> bool:
    try:
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


async def is_logged_in(page: Page) -> bool:
    try:
        return (
            not await shows_login_wall(page)
            and (
                await page.locator("a[href*='logout'], a[href*='log_out'], a[href*='sign_out']").count() > 0
                or await page.locator("#works_form").count() > 0
            )
        )
    except Exception:
        return False


async def login(page: Page, username: str, password: str) -> bool:
    """Navigate to EasyBOP and authenticate. EasyBOP uses randomized input ids and a JS login button."""
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


async def _wait_for_works_form(page: Page, timeout_ms: int, *, phase: str) -> None:
    log.info("%s: waiting for #works_form (attached), timeout=%sms", phase, timeout_ms)
    await page.wait_for_selector("#works_form", state="attached", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("load", timeout=min(5_000, max(1_000, timeout_ms // 4)))
    except Exception as e:
        log.warning("%s: load state wait skipped: %s", phase, e)
    log.info("%s: form present", phase)


async def ensure_form_page_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    _ = contract_id
    url = FORM_URL_STATIC
    log.info("ensure_form: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    try:
        await _wait_for_works_form(page, timeout_ms, phase="ensure_form[initial]")
        if not await page_looks_like_login_screen(page):
            log.info("ensure_form: session OK")
            return
        log.warning("ensure_form: form present but login wall visible")
    except Exception as e:
        log.warning("ensure_form: form not ready: %s", e)

    if await page_looks_like_login_screen(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        if not await login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")
        await _wait_for_works_form(page, timeout_ms, phase="ensure_form[after_login]")
        if await page_looks_like_login_screen(page):
            raise RuntimeError("Still on login wall after re-login — check credentials or URL access")
        return

    if await page.locator("#works_form").count() == 0:
        raise RuntimeError("Works form did not load. Check contract_id access and URL.")


# ─────────────────────────────────────────────────────
# Field fillers
# ─────────────────────────────────────────────────────

async def _fill_one(page: Page, field_id: str, value: str) -> Optional[str]:
    """Fill one field by id; auto-detects select vs text input. Returns error string or None."""
    try:
        el = page.locator(f"#{field_id}").first
        if await el.count() == 0:
            return f"#{field_id} not found on page"
        if field_id in _SELECT_FIELDS or await el.evaluate("e=>e.tagName.toLowerCase()") == "select":
            try:
                await el.select_option(label=value, timeout=8_000)
            except Exception:
                await el.select_option(value=value)
        else:
            await el.fill(value)
        return None
    except Exception as exc:
        return f"#{field_id} → {exc}"


async def _fill_search_property(page: Page, uprn: str) -> None:
    field = page.locator("#search_property")
    await field.click()
    await field.fill("")
    await field.type(uprn, delay=60)
    try:
        item = page.locator(".ui-autocomplete:visible li.ui-menu-item")
        await item.first.wait_for(state="visible", timeout=6_000)
        await item.first.click()
    except Exception:
        pass


async def _fill_sow(page: Page, sow_value: str) -> None:
    checked = page.locator(".sow_box:checked")
    for i in range(await checked.count()):
        await checked.nth(i).uncheck()
    target = page.locator(f"input.sow_box[value='{sow_value}']")
    if await target.count() > 0:
        await target.check()


async def _fill_rows_by_label(
    page: Page,
    label_re: re.Pattern,
    value: str,
    section: str,
    errors: List[str],
    not_found_msg: Optional[str] = None,
) -> int:
    """Fill every works_table row whose label matches `label_re`. Handles select + text inputs."""
    filled = 0
    done_ids: Set[str] = set()
    rows = page.locator("table.works_table tr").filter(
        has=page.locator("label").filter(has_text=label_re)
    )
    for i in range(await rows.count()):
        inp = rows.nth(i).locator("select.input, input.input, select, input[type='text']").first
        if await inp.count() == 0:
            continue
        iid = await inp.get_attribute("id") or ""
        if iid and iid in done_ids:
            continue
        try:
            tag = await inp.evaluate("e=>e.tagName.toLowerCase()")
            if tag == "select":
                try:
                    await inp.select_option(label=value, timeout=8_000)
                except Exception:
                    await inp.select_option(value=value)
            else:
                await inp.fill(value)
            filled += 1
            done_ids.add(iid or str(i))
            log.info("[%s] ok → #%s", section, iid or "row")
        except Exception as exc:
            errors.append(f"[{section}] {exc}")
            log.warning("[%s] row fill error: %s", section, exc)

    if filled == 0 and value and not_found_msg:
        log.warning(not_found_msg)
        errors.append(not_found_msg)
    return filled


async def _fill_mapped_keys(
    page: Page,
    fields: Dict[str, Optional[str]],
    keys: Tuple[str, ...],
    section: str,
    errors: List[str],
) -> int:
    filled = 0
    for name in keys:
        val = fields.get(name)
        if val is None or name.startswith("_") or name in _SKIP_FIELDS:
            continue
        err = await _fill_one(page, name, val)
        if err:
            log.warning("[%s] %s -> %s", section, name, err)
            errors.append(f"[{section}] {err}")
        else:
            log.info("[%s] ok %s", section, name)
            filled += 1
    return filled


def _infer_extra_section(name: str) -> str:
    low = name.lower()
    if re.search(r"property.*test|test.*property", low) or "property_test" in low:
        return SEC_PROPERTY_TEST
    if "disabled" in low or "adapt" in low:
        return SEC_DISABLED
    if name.startswith("udf_"):
        return SEC_OTHER
    return SEC_ADDITIONAL


async def _fill_unknown_fields_by_section(
    page: Page,
    fields: Dict[str, Optional[str]],
    errors: List[str],
) -> int:
    extras = sorted(
        k for k in fields
        if not k.startswith("_") and k not in _SKIP_FIELDS
        and k not in KNOWN_FORM_KEYS and fields.get(k) is not None
    )
    filled = 0

    for sec in (SEC_ADDITIONAL, SEC_PROPERTY_TEST, SEC_DISABLED):
        names = [n for n in extras if _infer_extra_section(n) == sec and not n.startswith("udf_")]
        if not names:
            continue
        log.info("—— %s ——", sec)
        for name in names:
            err = await _fill_one(page, name, fields[name])  # type: ignore[arg-type]
            if err:
                log.warning("[%s] %s -> %s", sec, name, err)
                errors.append(f"[{sec}] {err}")
            else:
                log.info("[%s] ok %s", sec, name)
                filled += 1

    log.info("—— %s —— contract_id, extra UDFs", SEC_OTHER)
    filled += await _fill_mapped_keys(page, fields, KEYS_OTHER_INFORMATION, SEC_OTHER, errors)
    for name in sorted(n for n in extras if n.startswith("udf_")):
        err = await _fill_one(page, name, fields[name])  # type: ignore[arg-type]
        if err:
            log.warning("[%s] %s -> %s", SEC_OTHER, name, err)
            errors.append(f"[{SEC_OTHER}] {err}")
        else:
            log.info("[%s] ok %s", SEC_OTHER, name)
            filled += 1
    return filled


# ─────────────────────────────────────────────────────
# Main form filler
# ─────────────────────────────────────────────────────

async def fill_form(
    page: Page,
    form_fields: Dict[str, Any],
    contract_id: str = "0",
    submit: bool = False,
    timeout_ms: int = 15_000,
    relogin_username: Optional[str] = None,
    relogin_password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> Tuple[List[str], int]:
    """
    Navigate to the works instruction form and fill every mapped field.
    Re-logs in automatically if the session has expired.
    Returns (errors, fields_filled).
    """
    raw_in: Dict[str, Any] = dict(form_fields) if form_fields else {}
    if is_sectioned_easybop_payload(raw_in):
        raw_in = sections_to_flat_form_fields(raw_in)
        log.info("fill_form: flattened sectioned payload")
    fields = clean_fields(raw_in)
    errors: List[str] = []
    filled = 0

    log.info("fill_form: start contract_id=%s submit=%s", contract_id, submit)
    await ensure_form_page_ready(page, contract_id, relogin_username, relogin_password,
                                 timeout_ms, after_relogin=after_relogin)
    log.info("fill_form: form ready — filling sections")

    # 1) Scope of Work
    log.info("—— %s ——", SEC_SELECT_SCOPE)
    sow_val = fields.get("scope_of_works_ids[]")
    if sow_val:
        await _fill_sow(page, sow_val)
        log.info("[%s] scope_of_works_ids[] = %s", SEC_SELECT_SCOPE, sow_val)
        filled += 1

    # 2) Works Information
    log.info("—— %s ——", SEC_WORKS_INFO)
    filled += await _fill_mapped_keys(page, fields, KEYS_WORKS_INFORMATION, SEC_WORKS_INFO, errors)

    # 3) Property Details + UPRN search
    log.info("—— %s ——", SEC_PROPERTY)
    uprn = fields.get("search_property") or fields.get("property_ref")
    if uprn:
        await _fill_search_property(page, uprn)
        filled += 1
    filled += await _fill_mapped_keys(page, fields, KEYS_PROPERTY_DETAILS, SEC_PROPERTY, errors)

    # 4–6) Address / Resident / Alternative Address
    for keys, sec in (
        (KEYS_ADDRESS,     SEC_ADDRESS),
        (KEYS_RESIDENT,    SEC_RESIDENT),
        (KEYS_ALT_ADDRESS, SEC_ALT_ADDRESS),
    ):
        log.info("—— %s ——", sec)
        filled += await _fill_mapped_keys(page, fields, keys, sec, errors)

    # 7) Other Information — BCC Surveyor (label-based, all rows)
    log.info("—— %s —— BCC Surveyor", SEC_OTHER)
    bcc_val = fields.get("udf_pw_c_works_instructions_5065") or fields.get("bcc_surveyor_1")
    if bcc_val:
        filled += await _fill_rows_by_label(page, _BCC_SURVEYOR_RE, bcc_val, SEC_OTHER, errors)

    # 8) Additional details — Asbestos Status (label-based, all rows)
    log.info("—— %s —— Asbestos Status", SEC_ADDITIONAL)
    av = fields.get("asbestos_status")
    if av:
        # Try configured UDF id first, then fall back to label-row matching
        fid = settings.asbestos_status_field_id
        if fid and await page.locator(f"#{fid}").count() > 0:
            err = await _fill_one(page, fid, av)
            if err:
                errors.append(f"[{SEC_ADDITIONAL}] asbestos {err}")
            else:
                log.info("[%s] ok asbestos_status → #%s", SEC_ADDITIONAL, fid)
                filled += 1
        else:
            filled += await _fill_rows_by_label(
                page, _ASBESTOS_RE, av, SEC_ADDITIONAL, errors,
                not_found_msg=(
                    "Asbestos Status: no field matched. "
                    "Set EASYBOP_ASBESTOS_FIELD_ID in .env to the input id."
                ),
            )

    # 9) Any unknown / extra UDF fields
    filled += await _fill_unknown_fields_by_section(page, fields, errors)

    if submit:
        log.info("—— Submit ——")
        btn = page.locator("#works_form [type='submit'], #works_form button[type='submit']")
        await btn.first.click()
        try:
            await page.wait_for_load_state("load", timeout=min(15_000, timeout_ms))
        except Exception as e:
            log.warning("[Submit] post-submit load wait: %s", e)

    log.info("fill_form: done filled=%s errors=%s", filled, len(errors))
    return errors, filled
