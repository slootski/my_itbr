"""config.save_env_values: rewrite .env lines in place and update os.environ."""
from __future__ import annotations

import os

import pytest

from config import is_real_value, save_env_values, validate_credentials


def test_save_env_values_replaces_in_place_and_keeps_other_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEX_TOKEN", "old")
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nFLEX_TOKEN=old\nDB_PATH=db/x.sqlite\n")

    save_env_values(env_path=env_path, values={"FLEX_TOKEN": "new"})

    assert env_path.read_text() == "# comment\nFLEX_TOKEN=new\nDB_PATH=db/x.sqlite\n"
    assert os.environ["FLEX_TOKEN"] == "new"


def test_save_env_values_appends_missing_keys_and_creates_file(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEX_TOKEN", "")
    monkeypatch.setenv("FLEX_QUERY_ID", "")
    env_path = tmp_path / ".env"

    save_env_values(env_path=env_path, values={"FLEX_TOKEN": "t", "FLEX_QUERY_ID": "42"})

    assert env_path.read_text() == "FLEX_TOKEN=t\nFLEX_QUERY_ID=42\n"
    assert os.environ["FLEX_QUERY_ID"] == "42"


def test_is_real_value_rejects_empty_and_placeholders():
    assert not is_real_value("")
    assert not is_real_value("  ")
    assert not is_real_value("your_flex_token_here")
    assert not is_real_value("your_flex_query_id_here")
    assert is_real_value("123456")


@pytest.mark.parametrize(
    ("token", "query_id", "query_id_missing", "expected"),
    [
        ("123", "", False, set()),
        ("", "", False, {"token"}),
        ("your_flex_token_here", "", False, {"token"}),
        ("123", "abc", True, {"query_id"}),
        ("123", "", True, {"query_id"}),
        ("", "12a", True, {"token", "query_id"}),
        ("123", "1537307", True, set()),
    ],
)
def test_validate_credentials(token, query_id, query_id_missing, expected):
    errors = validate_credentials(token=token, query_id=query_id, query_id_missing=query_id_missing)
    assert set(errors) == expected


def test_save_env_values_rewrites_duplicates_and_export_lines(monkeypatch, tmp_path):
    """dotenv keeps the LAST occurrence of a key; every occurrence must get the
    new value or a stale duplicate would win on the next start."""
    from dotenv import dotenv_values

    monkeypatch.setenv("FLEX_TOKEN", "old")
    env_path = tmp_path / ".env"
    env_path.write_text("FLEX_TOKEN=old1\n# note\nexport FLEX_TOKEN=old2\nDB_PATH=x\n")

    save_env_values(env_path=env_path, values={"FLEX_TOKEN": "new"})

    assert env_path.read_text() == "FLEX_TOKEN=new\n# note\nFLEX_TOKEN=new\nDB_PATH=x\n"
    assert dotenv_values(env_path)["FLEX_TOKEN"] == "new"


def test_save_env_values_makes_file_owner_only(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEX_TOKEN", "")
    env_path = tmp_path / ".env"
    env_path.write_text("FLEX_TOKEN=\n")
    env_path.chmod(0o644)

    save_env_values(env_path=env_path, values={"FLEX_TOKEN": "t"})

    assert env_path.stat().st_mode & 0o777 == 0o600


def test_saved_values_load_in_a_fresh_process(monkeypatch, tmp_path):
    """Persistence across restarts: a brand-new Python process, started from a
    different working directory, loads saved credentials through config's own
    loader. Saved credentials beat a stale shell export; DB_PATH from the shell
    still beats the file (only credential keys are forced from .env)."""
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.setenv("FLEX_TOKEN", "")
    monkeypatch.setenv("FLEX_QUERY_ID", "")
    env_path = tmp_path / ".env"
    env_path.write_text("FLEX_TOKEN=your_flex_token_here\nFLEX_QUERY_ID=your_flex_query_id_here\nDB_PATH=db/file.sqlite\n")
    save_env_values(env_path=env_path, values={"FLEX_TOKEN": "9988776655", "FLEX_QUERY_ID": "1537307"})

    project = Path(__file__).resolve().parents[1]
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("FLEX_")}
    child_env["FLEX_TOKEN"] = "stale-shell-token"
    child_env["DB_PATH"] = "/from/shell.sqlite"
    code = (
        "import os, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "import config; config.load_env_file(env_path=Path(sys.argv[2])); "
        "print(os.environ['FLEX_TOKEN'], os.environ['FLEX_QUERY_ID'], os.environ['DB_PATH'])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(project), str(env_path)],
        cwd="/", env=child_env, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == ["9988776655", "1537307", "/from/shell.sqlite"]
