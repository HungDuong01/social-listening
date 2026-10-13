"""Application settings.

Every value can be overridden through the environment (prefix ``SC_``) or a
``.env`` file sitting next to the project root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SC_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # --- storage ---
    data_dir: Path = PROJECT_ROOT / "data"
    sessions_dir: Path = PROJECT_ROOT / "sessions"
    exports_dir: Path = PROJECT_ROOT / "exports"
    profiles_dir: Path = PROJECT_ROOT / "profiles"
    database_url: str = ""

    # --- browser ---
    headless: bool = True
    # "chromium" runs the full browser in Chromium's new headless mode. The
    # default (empty) would use Playwright's separate headless-shell binary,
    # which is an extra download and renders less like a real browser.
    chromium_channel: str = "chromium"
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 45_000
    locale: str = "vi-VN"
    timezone: str = "Asia/Ho_Chi_Minh"
    viewport_width: int = 1440
    viewport_height: int = 900
    # Empty means "let the browser report itself".
    #
    # Overriding this is actively harmful: Playwright replaces only the
    # User-Agent *string*, while Sec-CH-UA client hints and
    # navigator.userAgentData keep reporting the real build. A pinned UA of
    # "Chrome/131" against client hints saying "Chromium 149" is a request that
    # contradicts itself, which is exactly what login challenges look for.
    user_agent: str = ""

    # Reuse one on-disk browser profile per platform instead of a fresh cookie
    # jar per run. Login challenges bind their trust token to a stable browser
    # identity; discarding it between attempts makes every retry look like a
    # brand-new machine and the challenge repeats forever.
    use_persistent_profile: bool = True

    # --- crawl defaults ---
    default_max_posts: int = 40
    # 0 = every comment the platform will serve. A cap silently
    # truncates long threads, which looks like a crawler failure.
    default_max_comments: int = 0
    max_scrolls: int = 60
    scroll_idle_rounds: int = 3
    scroll_delay_min_ms: int = 900
    scroll_delay_max_ms: int = 2_100
    post_delay_min_ms: int = 1_500
    post_delay_max_ms: int = 3_500
    job_timeout_s: int = 3_600
    login_timeout_s: int = 300

    # --- politeness ---
    rate_per_minute: int = 20
    rate_burst: int = 5

    # --- harvester limits ---
    max_payloads_per_page: int = 400
    max_body_bytes: int = 4_000_000

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def model_post_init(self, __context: object) -> None:
        for directory in (self.data_dir, self.sessions_dir, self.exports_dir, self.profiles_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            db_path = (self.data_dir / "social_crawler.sqlite3").as_posix()
            object.__setattr__(self, "database_url", f"sqlite+aiosqlite:///{db_path}")

    def storage_state_path(self, platform: str) -> Path:
        return self.sessions_dir / f"{platform}.json"

    def profile_dir(self, platform: str) -> Path:
        return self.profiles_dir / platform


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
