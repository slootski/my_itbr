"""Configuration loading and validation.

Reads secrets/settings from a local .env file (never committed). All access to
environment-backed settings goes through this module so the rest of the app does
not touch os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root once at import time.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

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
        placeholders = {"", "your_flex_token_here", "your_flex_query_id_here"}
        return self.flex_token not in placeholders and self.flex_query_id not in placeholders


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
