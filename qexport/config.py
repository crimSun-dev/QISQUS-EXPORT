from datetime import date
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QISCUS_", env_file=".env", extra="ignore")

    app_code: str                       # e.g. "enckp-1dh7o4rkopdtmh5"
    admin_token: SecretStr              # qismo-data.user.authentication_token
    sdk_user_id: str                    # qismo-data.details.sdk_user.id
    sdk_token: SecretStr                # qismo-data.details.sdk_user.token
    role: str = "admin"                 # qismo-data.user.type_as_string; used in broadcast paths
    omni_base_url: str = "https://multichannel-api.qiscus.com"
    sdk_base_url: str = "https://api3.qiscus.com"
    rooms_rps: float = 1.2              # observed-safe: <=1.4 req/s
    messages_rps: float = 3.0           # observed-safe: 4 req/s with 0x429
    broadcast_rps: float = 1.2          # same host as rooms; 0x429 at 1.25 req/s observed
    page_limit: int = 100
    max_room_attempts: int = 5
    max_retry_attempts: int = 8
    request_timeout_s: float = 30.0
    display_tz: str = "Asia/Jakarta"
    out_dir: Path = Path("exports")
    broadcasts_since: date | None = None   # only export broadcast jobs created on/after this date
    broadcast_refresh_days: int = 14       # --since-last-run re-pulls logs of jobs this recent
    attribution_window_days: int = 7       # last-touch window for linking replies to a broadcast


def load_settings(env_file: Path | None = None, **overrides) -> Settings:
    """Raises pydantic.ValidationError listing every missing/invalid variable."""
    if env_file is not None:
        return Settings(_env_file=env_file, **overrides)
    return Settings(**overrides)
