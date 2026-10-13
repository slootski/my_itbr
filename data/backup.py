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

import contextlib
import datetime as dt
import logging
import os
import re
import secrets
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("myitbr.backup")

DEFAULT_KEEP = 14
_STEM = "snapshots-"
_NAME_RE = re.compile(rf"^{_STEM}(\d{{4}}-\d{{2}}-\d{{2}})\.sqlite$")
# The copy taken before the day's first IBKR write. Not matched by _NAME_RE, so
# existing(), status() and ``keep`` go on counting days, not files.
_PRESYNC = "-presync"
_PRESYNC_RE = re.compile(rf"^{_STEM}(\d{{4}}-\d{{2}}-\d{{2}}){_PRESYNC}\.sqlite$")
# run() and refresh() stage and swap files in the same directory.
_LOCK = threading.Lock()


def _dated_path(backup_dir: Path, day: str) -> Path:
    return backup_dir / f"{_STEM}{day}.sqlite"


def existing(backup_dir: Path | str) -> list[Path]:
    """Dated backups present, oldest first. Ignores anything else in the dir."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    found = [p for p in backup_dir.iterdir() if _NAME_RE.match(p.name)]
    return sorted(found, key=lambda p: p.name)


def _presync_path(backup_dir: Path, day: str) -> Path:
    return backup_dir / f"{_STEM}{day}{_PRESYNC}.sqlite"


def _verify(path: Path) -> bool:
    """A backup nobody opened is not a backup. Integrity-check the copy."""
    try:
        # as_uri() quotes "?", "#" and "%"; a joined string would open another file.
        uri = path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(result) and result[0] == "ok"
    except sqlite3.Error as exc:
        logger.warning("backup verify failed for %s: %s", path.name, exc)
        return False


def _prune(backup_dir: Path, keep: int) -> int:
    """Keep the ``keep`` newest days; delete the files of every older day.

    A day counts when it has a main file or a pre-sync file, so a pre-sync copy
    whose main file is missing goes with its day too. Returns how many went.
    """
    if keep <= 0 or not backup_dir.is_dir():
        return 0
    by_day: dict[str, list[Path]] = {}
    for path in backup_dir.iterdir():
        match = _NAME_RE.match(path.name) or _PRESYNC_RE.match(path.name)
        if match:
            by_day.setdefault(match.group(1), []).append(path)
    removed = 0
    for day in sorted(by_day)[:-keep]:
        for path in by_day[day]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("could not prune %s: %s", path.name, exc)
    return removed


def _sweep_parts(*, backup_dir: Path) -> None:
    """Remove staging files a crashed run left. Only this module's own names:
    the directory may be a shared or synced folder."""
    if not backup_dir.is_dir():
        return
    for path in backup_dir.glob(f"{_STEM}*.part"):
        path.unlink(missing_ok=True)


def _write_copy(*, db_path: Path, target: Path) -> bool:
    """Stage a verified copy of the DB and swap it into ``target``.

    VACUUM INTO refuses to overwrite and cannot run inside a transaction, so the
    copy is written under a unique staging name on its own connection, checked,
    and only then moved over the target. The caller holds ``_LOCK``.
    """
    staged = target.with_name(f"{target.name}.{secrets.token_hex(4)}.part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute("VACUUM INTO ?", (str(staged),))
        if not _verify(staged):
            logger.error("backup %s failed verification; keeping older copies", target.name)
            return False
        os.replace(staged, target)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("backup failed: %s", exc)
        return False
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass


def _report(*, backup_dir: Path, target: Path, removed: int) -> None:
    try:
        size = target.stat().st_size / 1024
    except OSError:
        size = 0.0
    logger.info("backup wrote %s (%.1f KB); pruned %d, %d retained",
                target.name, size, removed, len(existing(backup_dir)))


def run(
    db_path: Path | str,
    backup_dir: Path | str,
    *,
    keep: int = DEFAULT_KEEP,
    today: str | None = None,
) -> Path | None:
    """Take today's backup if there isn't one yet, then prune old ones.

    Called before an IBKR write. It also keeps a separate pre-sync copy of the
    day (once), because ``refresh`` replaces the day's main file after every
    watchlist change and would otherwise overwrite the only pre-sync state.

    Returns the main backup's path, or None when no main file was written
    (already backed up today, or the source database does not exist yet). Never
    raises: a backup failure must not cost the user their dashboard.
    """
    try:
        db_path, backup_dir = Path(db_path), Path(backup_dir)
        if not db_path.exists():
            return None
        day = today or dt.date.today().isoformat()
        target = _dated_path(backup_dir, day)
        with _LOCK:
            _sweep_parts(backup_dir=backup_dir)
            presync = _presync_path(backup_dir, day)
            if not presync.exists():
                _write_copy(db_path=db_path, target=presync)
            if target.exists():
                return None  # the day's main file is kept fresh by refresh()
            if not _write_copy(db_path=db_path, target=target):
                return None
            # Only ever prune once a verified new backup is on disk.
            removed = _prune(backup_dir, keep)
        _report(backup_dir=backup_dir, target=target, removed=removed)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning("backup failed: %s", exc)
        return None


def refresh(*, db_path: Path | str, backup_dir: Path | str, keep: int, today: str) -> Path | None:
    """Write today's backup even when one exists, then prune old days.

    Called after every watchlist change: lists and targets are typed by the user
    and cannot be pulled from IBKR again. Returns the backup's path, or None when
    nothing was written. Never raises.
    """
    try:
        db_path, backup_dir = Path(db_path), Path(backup_dir)
        if not db_path.exists():
            return None
        target = _dated_path(backup_dir, today)
        with _LOCK:
            _sweep_parts(backup_dir=backup_dir)
            if not _write_copy(db_path=db_path, target=target):
                return None
            removed = _prune(backup_dir, keep)
        _report(backup_dir=backup_dir, target=target, removed=removed)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning("backup refresh failed: %s", exc)
        return None


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
