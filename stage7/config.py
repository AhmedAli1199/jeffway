from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

_repo_root = Path(__file__).resolve().parent.parent
_env_path = _repo_root / ".env"
_env = {k: (v or "").strip() for k, v in dotenv_values(_env_path).items()}

# Prefer session file used by the main filler, fall back to JIS-form one
_filler_session = _repo_root / "filler" / "easybop_session.json"
_jis_session = _repo_root / "JIS-form" / "easybop_session.json"
_default_session = str(_filler_session if _filler_session.exists() else _jis_session)


class Settings:
    username: str = _env.get("username", "")
    password: str = _env.get("password", "")
    request_timeout_ms: int = int(_env.get("request_timeout_ms", "30000") or "30000")
    session_file: str = (_env.get("session_file") or _default_session).strip() or _default_session
    keep_page_open_after_fill: bool = _env.get("EASYBOP_KEEP_PAGE_OPEN", "true").lower() in (
        "1",
        "true",
        "yes",
    )


def resolve_session_path() -> Path:
    p = Path(settings.session_file)
    if not p.is_absolute():
        p = _repo_root / p
    return p.resolve()


settings = Settings()
