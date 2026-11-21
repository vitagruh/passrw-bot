from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_db.py"


def _run(*args: str, source: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--dest", str(dest), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _count_rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()


def test_backup_copies_wal_database_and_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    dest = tmp_path / "backup.db"

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        writer.executemany("INSERT INTO items(name) VALUES (?)", [("a",), ("b",)])
        writer.commit()
        assert Path(f"{source}-wal").exists()

        first = _run(source=source, dest=dest)
        assert first.returncode == 0, first.stderr
        assert str(dest) in first.stdout
        assert "байт" in first.stdout
        assert _count_rows(dest) == 2

        copy = sqlite3.connect(dest)
        try:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            copy.close()

        writer.execute("INSERT INTO items(name) VALUES ('c')")
        writer.commit()

        refused = _run(source=source, dest=dest)
        assert refused.returncode != 0
        assert "существует" in refused.stderr
        assert _count_rows(dest) == 2

        forced = _run("--force", source=source, dest=dest)
        assert forced.returncode == 0, forced.stderr
        assert _count_rows(dest) == 3
    finally:
        writer.close()


def test_backup_fails_for_missing_source(tmp_path: Path) -> None:
    result = _run(source=tmp_path / "missing.db", dest=tmp_path / "backup.db")
    assert result.returncode == 1
    assert "не найдена" in result.stderr
    assert not (tmp_path / "backup.db").exists()
