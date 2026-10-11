"""Environment-only dashboard configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DashboardSettings:
    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    discord_guild_id: str
    moderator_role_ids: frozenset[str]
    session_secret: str
    database_path: Path
    institution_id: str
    cookie_secure: bool = True
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 30
    session_max_age_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "DashboardSettings":
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        required = {
            "discord_client_id": "MCORP_DISCORD_CLIENT_ID",
            "discord_client_secret": "MCORP_DISCORD_CLIENT_SECRET",
            "discord_redirect_uri": "MCORP_DISCORD_REDIRECT_URI",
            "discord_guild_id": "MCORP_DISCORD_GUILD_ID",
            "session_secret": "MCORP_SESSION_SECRET",
            "database_path": "MCORP_DATABASE_PATH",
        }
        values: dict[str, str] = {}
        missing: list[str] = []
        for field, variable in required.items():
            value = os.environ.get(variable, "").strip()
            if not value:
                missing.append(variable)
            values[field] = value
        if missing:
            raise ConfigurationError("Variables manquantes : " + ", ".join(missing))
        if len(values["session_secret"]) < 32:
            raise ConfigurationError("MCORP_SESSION_SECRET doit contenir au moins 32 caractères")
        role_ids = frozenset(
            value.strip() for value in os.environ.get("MCORP_MODERATOR_ROLE_IDS", "").split(",")
            if value.strip()
        )
        return cls(
            discord_client_id=values["discord_client_id"],
            discord_client_secret=values["discord_client_secret"],
            discord_redirect_uri=values["discord_redirect_uri"],
            discord_guild_id=values["discord_guild_id"],
            moderator_role_ids=role_ids,
            session_secret=values["session_secret"],
            database_path=Path(values["database_path"]).expanduser().resolve(),
            institution_id=os.environ.get("MCORP_INSTITUTION_ID", "mcorp").strip() or "mcorp",
            cookie_secure=_boolean("MCORP_COOKIE_SECURE", True),
            scheduler_enabled=_boolean("MCORP_SCHEDULER_ENABLED", True),
            scheduler_interval_seconds=_positive_int("MCORP_SCHEDULER_INTERVAL_SECONDS", 30),
            session_max_age_seconds=_positive_int("MCORP_SESSION_MAX_AGE_SECONDS", 3600),
        )


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.casefold() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} doit être un entier") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} doit être positif")
    return value
