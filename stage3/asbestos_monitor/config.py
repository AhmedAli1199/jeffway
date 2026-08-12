from pathlib import Path

from dotenv import dotenv_values

_here = Path(__file__).resolve().parent
_repo_root = _here.parent
_env_path = _repo_root / ".env"
_env = {k: (v or "").strip() for k, v in dotenv_values(_env_path).items()}

# Reuse the shared session from filler/ or JIS-form/ — same login
_filler_session = _repo_root / "filler" / "easybop_session.json"
_jis_session = _repo_root / "JIS-form" / "easybop_session.json"

def _pick_session() -> str:
    if _filler_session.exists():
        return str(_filler_session)
    if _jis_session.exists():
        return str(_jis_session)
    return str(_here / "easybop_session.json")


class Settings:
    username: str = _env.get("username", "")
    password: str = _env.get("password", "")
    headless: bool = _env.get("headless", "false").lower() == "true"
    slow_mo_ms: int = int(_env.get("EASYBOP_SLOW_MO_MS", "0") or "0")
    session_file: str = (_env.get("session_file") or _pick_session()).strip()
    request_timeout_ms: int = int(_env.get("request_timeout_ms", "30000"))
    reload_on_change: bool = _env.get("EASYBOP_RELOAD", "false").lower() in ("1", "true", "yes")
    contract_id: str = (_env.get("ASB_CONTRACT_ID") or _env.get("JIS_CONTRACT_ID") or "321129").strip()
    port: int = int(_env.get("ASB_MONITOR_PORT", "8002") or "8002")
    keep_page_open_after_fill: bool = _env.get("EASYBOP_KEEP_PAGE_OPEN", "true").lower() == "true"


def resolve_session_path() -> Path:
    p = Path(settings.session_file)
    if not p.is_absolute():
        p = _here / p
    return p.resolve()


settings = Settings()
