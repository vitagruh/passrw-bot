"""Резервное копирование SQLite-базы passrw.

Использование:
    python scripts/backup_db.py --source data/passrw.db --dest data/backups/passrw.db

Копия создаётся через sqlite3.Connection.backup, поэтому корректно учитывает
содержимое WAL. Целостность копии проверяется через PRAGMA integrity_check.
Существующий файл назначения не перезаписывается без --force.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def backup_database(source: Path, dest: Path, *, force: bool = False) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Исходная база не найдена: {source}")
    if source.resolve() == dest.resolve():
        raise ValueError("Source и dest не могут совпадать")
    if dest.exists() and not force:
        raise FileExistsError(
            f"Файл назначения уже существует: {dest} (для перезаписи используйте --force)"
        )
    if dest.is_dir():
        raise IsADirectoryError(f"Путь назначения указывает на директорию: {dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f"{dest.name}.tmp")
    temp.unlink(missing_ok=True)

    source_conn = _connect_readonly(source)
    try:
        target_conn = sqlite3.connect(str(temp))
        try:
            source_conn.backup(target_conn)
            result = target_conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            target_conn.close()
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        source_conn.close()

    if not result or result[0] != "ok":
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Проверка целостности копии не пройдена: {result!r}")

    os.replace(temp, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Резервная копия базы passrw")
    parser.add_argument("--source", type=Path, required=True, help="исходная база SQLite")
    parser.add_argument("--dest", type=Path, required=True, help="файл резервной копии")
    parser.add_argument(
        "--force", action="store_true", help="перезаписать существующий файл назначения"
    )
    args = parser.parse_args()

    try:
        dest = backup_database(args.source.resolve(), args.dest.resolve(), force=args.force)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Ошибка резервного копирования: {exc}", file=sys.stderr)
        return 1

    size = dest.stat().st_size
    print(f"Резервная копия создана: {dest} ({size} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
