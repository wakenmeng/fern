from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FERN_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "aws"] = "local"
    database_url: str = "postgresql://fern:fern@localhost:54329/fern"
    session_secret: str = "local-only-session-secret-change-me"
    # "/apps/fern/api" behind the Cloudflare Worker in AWS; empty locally.
    root_path: str = ""
    # Demo clock: how long pulled-in money stays unavailable after it settles (DESIGN §1).
    pull_hold_seconds: int = 600
    # Which bank backs the demo: the in-process simulator or the Column sandbox.
    bank: Literal["sim", "column"] = "sim"
    # Live demo: visitors only get pre-funded spaces from the pool (fern.pool). When it is empty,
    # POST /spaces returns 503 instead of opening a space whose payroll takes minutes to land.
    pool_only: bool = False
    column_api_key: str = ""
    column_base_url: str = "https://api.column.com"
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "sandbox"


@lru_cache
def get_settings() -> Settings:
    return Settings()
