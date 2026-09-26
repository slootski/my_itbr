"""Configuration loading and validation.

Reads secrets/settings from a local .env file (never committed). All access to
environment-backed settings goes through this module so the rest of the app does
not touch os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Keys the in-app "Connect IBKR" dialog saves to .env. For these the file wins
# over the process environment, so a token the user just saved is used on every
# future start even if a stale FLEX_TOKEN is exported in the shell. Other keys
# (DB_PATH, BACKUP_DIR, ...) keep dotenv's default: the environment wins.
CREDENTIAL_KEYS = ("FLEX_TOKEN", "FLEX_QUERY_ID")


def load_env_file(*, env_path: Path) -> None:
    """Load ``env_path`` into os.environ; credential keys from the file override."""
    load_dotenv(env_path)
    file_values = dotenv_values(env_path)
    for key in CREDENTIAL_KEYS:
        if key in file_values and file_values[key] is not None:
            os.environ[key] = file_values[key]


# Load .env from the project root once at import time. The path is absolute, so
# it is the same file whatever the working directory (e.g. the Desktop launcher).
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
load_env_file(env_path=ENV_PATH)

DEFAULT_DB_PATH = "db/snapshots.sqlite"
DEFAULT_BACKUP_DIR = "db/backups"
DEFAULT_BACKUP_KEEP = 14


@dataclass(frozen=True)
class Settings:
    flex_token: str
    flex_query_id: str
    db_path: Path
    polygon_api_key: str = ""
    backup_dir: Path = Path(DEFAULT_BACKUP_DIR)
    backup_keep: int = DEFAULT_BACKUP_KEEP

    @property
    def is_configured(self) -> bool:
        """True when both Flex credentials are present and non-placeholder."""
        return is_real_value(self.flex_token) and is_real_value(self.flex_query_id)


# Values .env.example ships with; treated the same as "not set".
PLACEHOLDERS = frozenset({"", "your_flex_token_here", "your_flex_query_id_here"})


def is_real_value(value: str) -> bool:
    """True when ``value`` is neither empty nor one of the .env.example placeholders."""
    return value.strip() not in PLACEHOLDERS


def validate_credentials(*, token: str, query_id: str, query_id_missing: bool) -> dict[str, str]:
    """Field name -> error text for every invalid field; empty when all are valid."""
    errors = {}
    if not is_real_value(token):
        errors["token"] = "Paste the token from Flex Web Service Configuration."
    if query_id_missing and not query_id.isdigit():
        errors["query_id"] = "Use numbers only, for example 1537307."
    return errors


def save_env_values(*, env_path: Path, values: dict[str, str]) -> None:
    """Persist ``values`` to the .env file and apply them to the running process.

    The .env file is the one load_dotenv reads at every start (``ENV_PATH``), so
    saved values survive restarts. Every existing ``KEY=`` / ``export KEY=`` line
    for a saved key is rewritten: python-dotenv keeps the LAST occurrence of a
    key, so rewriting only the first would let a stale duplicate win on the next
    start. Keys not in the file are appended; all other lines are kept as is.
    The file is written owner-read/write only (it holds the token), and
    os.environ is updated so the running app uses the new values immediately.
    """
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if "=" in line and name in values:
            lines[i] = f"{name}={values[name]}"
    present = {line.split("=", 1)[0] for line in lines if "=" in line}
    lines.extend(f"{key}={value}" for key, value in values.items() if key not in present)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    os.environ.update(values)


def load_settings() -> Settings:
    """Build Settings from the current environment.

    Resolves DB_PATH relative to the project root when given as a relative path so
    the app works regardless of the current working directory.
    """
    def _resolve(raw: str) -> Path:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    # A backup on the same disk as the original only guards against accidental
    # deletion and corruption. Point BACKUP_DIR at a synced or external volume to
    # also survive losing the machine — hence expanduser() above.
    try:
        backup_keep = int(os.getenv("BACKUP_KEEP", str(DEFAULT_BACKUP_KEEP)))
    except ValueError:
        backup_keep = DEFAULT_BACKUP_KEEP

    return Settings(
        flex_token=os.getenv("FLEX_TOKEN", "").strip(),
        flex_query_id=os.getenv("FLEX_QUERY_ID", "").strip(),
        db_path=_resolve(os.getenv("DB_PATH", DEFAULT_DB_PATH)),
        polygon_api_key=os.getenv("POLYGON_API_KEY", "").strip(),
        backup_dir=_resolve(os.getenv("BACKUP_DIR", DEFAULT_BACKUP_DIR)),
        backup_keep=max(1, backup_keep),
    )
