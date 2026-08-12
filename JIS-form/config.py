from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

_jis_dir = Path(__file__).resolve().parent
_repo_root = _jis_dir.parent
_env_path = _repo_root / ".env"
_env = {k: (v or "").strip() for k, v in dotenv_values(_env_path).items()}

_filler_session = _repo_root / "filler" / "easybop_session.json"
_default_session = str(_filler_session if _filler_session.exists() else _jis_dir / "easybop_session.json")


class Settings:
    username: str = _env.get("username", "")
    password: str = _env.get("password", "")
    headless: bool = _env.get("headless", "false").lower() == "true"
    slow_mo_ms: int = int(_env.get("EASYBOP_SLOW_MO_MS", "0") or "0")
    session_file: str = (_env.get("session_file") or _default_session).strip() or _default_session
    request_timeout_ms: int = int(_env.get("request_timeout_ms", "20000"))
    keep_page_open_after_fill: bool = _env.get("EASYBOP_KEEP_PAGE_OPEN", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    reload_on_change: bool = _env.get("EASYBOP_RELOAD", "false").lower() in ("1", "true", "yes")
    # Default JIS planned-works list URL uses this contract_id query param
    jis_contract_id: str = (_env.get("JIS_CONTRACT_ID") or "321129").strip()
    # Optional default path to SOR-Codes-Template workbook (xlsx)
    jis_excel_path: str = (_env.get("JIS_EXCEL_PATH") or "").strip()
    # SOR-Codes-Template Google Sheet (CSV export — use "Anyone with the link can view")
    _default_jis_google = (
        "https://docs.google.com/spreadsheets/d/1PRUtOeUec0uMhrj4k1sFEAdo5qvXE9cDOeyAQ67hIDI/edit?gid=1611853392#gid=1611853392"
    )
    _default_appt_google = (
        "https://docs.google.com/spreadsheets/d/1PRUtOeUec0uMhrj4k1sFEAdo5qvXE9cDOeyAQ67hIDI/edit?gid=1360938164#gid=1360938164"
    )
    _raw_google = _env.get("JIS_GOOGLE_SHEET_URL")
    if _raw_google is None:
        jis_google_sheet_url: str = _default_jis_google.strip()
    else:
        jis_google_sheet_url = _raw_google.strip()
    _raw_appt_google = _env.get("JIS_APPOINTMENT_GOOGLE_SHEET_URL")
    if _raw_appt_google is None:
        jis_appointment_google_sheet_url: str = _default_appt_google.strip()
    else:
        jis_appointment_google_sheet_url = _raw_appt_google.strip()
    # Port when running `python main.py` (override with env JIS_FORM_PORT)
    jis_form_port: int = int(_env.get("JIS_FORM_PORT", "8001") or "8001")
    # EasyBOP "Job information sheet" smart form (data-smart-form-id)
    jis_smart_form_job_info_id: str = (_env.get("JIS_SMART_FORM_JOB_INFO_ID") or "17799").strip()
    # Disambiguate "Click on me" when both Global WO and JIS exist (data-item-args item_id)
    jis_pre_item_item_id: str = (_env.get("JIS_PRE_ITEM_ITEM_ID") or "").strip()
    jis_pre_item_works_id: str = (_env.get("JIS_PRE_ITEM_WORKS_ID") or "").strip()
    _raw_item_type = _env.get("JIS_PRE_ITEM_ITEM_TYPE")
    if _raw_item_type is None:
        jis_pre_item_item_type: Optional[str] = "wi_pw"
    else:
        jis_pre_item_item_type = _raw_item_type.strip() or None


def resolve_session_path() -> Path:
    p = Path(settings.session_file)
    if not p.is_absolute():
        p = _jis_dir / p
    return p.resolve()


settings = Settings()
