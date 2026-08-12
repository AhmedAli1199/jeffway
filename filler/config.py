from pathlib import Path
from dotenv import dotenv_values

# Load .env explicitly so Windows system env vars (USERNAME, etc.) don't override
_env_path = Path(__file__).parent.parent / ".env"
_env = {k: (v or "").strip() for k, v in dotenv_values(_env_path).items()}


class Settings:
    username: str           = _env.get("username", "")
    password: str           = _env.get("password", "")
    easybop_base_url: str   = _env.get("easybop_base_url", "https://easybop.co.uk")
    # false = real browser window on the machine running FastAPI (what you want for demos)
    headless: bool          = _env.get("headless", "false").lower() == "true"
    # Slow down Playwright actions (ms) so you can follow the fill — 0 = off
    slow_mo_ms: int         = int(_env.get("EASYBOP_SLOW_MO_MS", "0") or "0")
    session_file: str       = _env.get("session_file", "easybop_session.json")
    # Max wait for #works_form (attached) and other Playwright waits — override in .env if needed
    request_timeout_ms: int = int(_env.get("request_timeout_ms", "20000"))
    # After /fill-work-order, leave the tab open so you can inspect the browser (headed mode)
    keep_page_open_after_fill: bool = _env.get("EASYBOP_KEEP_PAGE_OPEN", "true").lower() in ("1", "true", "yes")
    # When true, `python main.py` runs uvicorn with --reload (on Windows use `python run_dev.py` instead)
    reload_on_change: bool = _env.get("EASYBOP_RELOAD", "false").lower() in ("1", "true", "yes")
    # HTML id of the "Asbestos Status" UDF (EasyBOP uses udf_pw_c_works_instructions_XXXX).
    # Keep empty by default so we do not accidentally target the wrong UDF on contracts with different layouts.
    asbestos_status_field_id: str = (_env.get("EASYBOP_ASBESTOS_FIELD_ID") or "").strip()
    # Stage 1 WI create: max parallel Playwright browsers (1–3)
    wi_create_max_concurrent: int = max(
        1, min(3, int(_env.get("WI_CREATE_MAX_CONCURRENT", "3") or "3"))
    )
    # Comma-separated client IPs allowed to call the API. Use * to disable (not recommended on public servers).
    api_allowed_ips: str = _env.get(
        "API_ALLOWED_IPS",
        "127.0.0.1,::1,144.126.195.119",
    )
    api_port: int = int(_env.get("API_PORT", "8010") or "8010")


settings = Settings()

