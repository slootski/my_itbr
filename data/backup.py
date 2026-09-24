"""Rotating local backups of the SQLite store.

``db/snapshots.sqlite`` stopped being a cache the moment ``data/history.py``
started archiving trades and cash transactions: it is now the *only* copy of
anything older than IBKR's rolling 365-day Flex window, and the Flex Web Service
cannot re-serve that history. Losing the file loses the data permanently.

So each refresh drops a dated copy into the backup directory, keeping the most
recent ``keep`` of them.

Two deliberate choices:

- **``VACUUM INTO``**, not a file copy. It takes a read lock and writes a clean,
  fully-consistent database, so a backup taken while Streamlit holds the file
  open can never be a torn mid-write image. (Needs SQLite 3.27+; the stdlib
  build ships far newer.)
- **Verify, then prune.** The new copy is opened and integrity-checked before any
  old one is deleted, so a failed or truncated backup can never be the reason
  the previous good backups went away.

The destination defaults inside the project but is settable via ``BACKUP_DIR``.
Pointing it at a synced folder (iCloud/Dropbox) or an external disk is the only
thing that protects against losing the whole machine — copies sitting on the same
disk as the original protect against accidental deletion and corruption, nothing
more.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("myitbr.backup")

DEFAULT_KEEP = 14
_STEM = "snapshots-"
_NAME_RE = re.compile(rf"^{_STEM}(\d{{4}}-\d{{2}}-\d{{2}})\.sqlite$")


def _dated_path(backup_dir: Path, day: str) -> Path:
    return backup_dir / f"{_STEM}{day}.sqlite"


def existing(backup_dir: Path | str) -> list[Path]:
    """Dated backups present, oldest first. Ignores anything else in the dir."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    found = [p for p in backup_dir.iterdir() if _NAME_RE.match(p.name)]
    return sorted(found, key=lambda p: p.name)


def _verify(path: Path) -> bool:
    """A backup nobody opened is not a backup. Integrity-check the copy."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(result) and result[0] == "ok"
    except sqlite3.Error as exc:
        logger.warning("backup verify failed for %s: %s", path.name, exc)
        return False


def _prune(backup_dir: Path, keep: int) -> int:
    """Delete all but the ``keep`` newest dated backups. Returns how many went."""
    stale = existing(backup_dir)[:-keep] if keep > 0 else []
    removed = 0
    for path in stale:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("could not prune %s: %s", path.name, exc)
    return removed


def run(
    db_path: Path | str,
    backup_dir: Path | str,
    *,
    keep: int = DEFAULT_KEEP,
    today: str | None = None,
) -> Path | None:
    """Take today's backup if there isn't one yet, then prune old ones.

    Returns the backup path, or None when nothing was written (already backed up
    today, or the source database does not exist yet). Never raises: a backup
    failure must not cost the user their dashboard.
    """
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.exists():
        return None

    day = today or dt.date.today().isoformat()
    target = _dated_path(backup_dir, day)
    if target.exists():
        return None  # one per day is enough; the source only changes on refresh

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # VACUUM INTO refuses to overwrite, so stage under a temp name and swap.
        staged = target.with_suffix(".sqlite.part")
        staged.unlink(missing_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(staged),))

        if not _verify(staged):
            staged.unlink(missing_ok=True)
            logger.error("backup for %s failed verification; keeping older copies", day)
            return None

        staged.replace(target)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("backup failed: %s", exc)
        return None

    # Only ever prune once a verified new backup is on disk.
    removed = _prune(backup_dir, keep)
    logger.info(
        "backup wrote %s (%.1f KB); pruned %d, %d retained",
        target.name, target.stat().st_size / 1024, removed, len(existing(backup_dir)),
    )
    return target


def status(backup_dir: Path | str) -> dict:
    """Latest backup + count, for the sidebar. Never raises — display only."""
    try:
        found = existing(backup_dir)
    except OSError:
        found = []
    if not found:
        return {"count": 0, "latest": None, "latest_date": None}
    latest = found[-1]
    match = _NAME_RE.match(latest.name)
    return {
        "count": len(found),
        "latest": latest,
        "latest_date": match.group(1) if match else None,
    }
