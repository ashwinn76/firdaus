#!/usr/bin/env python3
"""Nextcloud DB backup script (Python port of db-backup.sh).

Behavior preserved from original:
- Loads .env from two levels above this script by default (override with NEXTCLOUD_ENV_FILE).
- Dumps Postgres DB from Docker container to /mnt/nas/nextcloud/backups/db
- Archives config files to /mnt/nas/nextcloud/backups/config
- Removes backup files older than 5 days (matches original -mtime +5)
- Verifies dump size > 1MB and checks header contains "PostgreSQL database dump"

Make executable and run from anywhere. Example:
  chmod +x db-backup.py
  ./db-backup.py
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tarfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv


def run_cmd(cmd, /, check=True, capture_output=False, stdout=None, stdin=None):
    print(f"Running: {shlex.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, stdout=stdout, stdin=stdin)


def archive_paths(target: Path, paths: list[Path]):
    print(f"Archiving {len(paths)} paths to {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as tar:
        for p in paths:
            if not p.exists():
                print(f"Warning: {p} does not exist, skipping")
                continue
            # Use arcname relative to nextcloud root when possible to avoid absolute paths
            try:
                arcname = p.relative_to(p.parents[1])
            except Exception:
                arcname = p.name
            tar.add(p, arcname=arcname)


def prune_old_files(directory: Path, pattern: str, days: int = 5):
    try:
        print(f"Pruning files in {directory} matching {pattern} older than {days} days")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        files_deleted = 0
        for f in directory.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                print(f"Removing old backup: {f}")
                f.unlink()
                files_deleted += 1
        print(f"Pruning complete. {files_deleted} old files deleted.")
    except Exception as e:
        print(f"Error pruning old files: {e}")


def save_config(nextcloud_root: Path, backup_root: Path, date_str: str):
    try:
        print("Backing up config...")

        cfg_dir = backup_root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)

        paths = [
            nextcloud_root / "html" / "config",
            nextcloud_root / ".env",
        ]
        cfg_file = cfg_dir / f"nextcloud-config-{date_str}.tar.gz"

        archive_paths(cfg_file, paths)
        print(f"Config backup saved to {cfg_file}")
    except Exception as e:
        print(f"Failed to backup config {cfg_file}: {e}")

    prune_old_files(cfg_dir, "nextcloud-config-*.tar.gz", days=5)
    print()


def save_database_dump(nextcloud_root: Path, backup_root: Path, date_str: str):
    try:
        print("Backing up database...")

        db_dir = backup_root / "db"
        db_dir.mkdir(parents=True, exist_ok=True)

        db_file = db_dir / f"nextcloud-db-{date_str}.sql"
        pg_user = os.environ["POSTGRES_USER"]
        pg_db = os.environ["POSTGRES_DB"]
        db_container = os.environ["DB_CONTAINER"]
        if not pg_user or not pg_db or not db_container:
            print("ERROR: POSTGRES_USER, POSTGRES_DB, and DB_CONTAINER must be defined in .env")
            return

        with db_file.open("wb") as out:
            run_cmd(["docker", "exec", db_container, "pg_dump", "-U", pg_user, pg_db], stdout=out)

        print(f"Database dump saved to {db_file}")
    except subprocess.CalledProcessError:
        print("Database dump failed.")
        return None

    verify_database_dump(db_file)
    restore_test_database(db_file)

    prune_old_files(db_dir, "nextcloud-db-*.sql", days=5)


def verify_database_dump(db_file: Path) -> bool:
    print(f"Verifying {db_file}")

    try:
        size = db_file.stat().st_size
    except FileNotFoundError:
        print("Backup file not found for verification.")
        return False

    if size < 1_000_000:
        print("Backup too small!")
        return False

    with db_file.open() as f:
        header = "\n".join([next(f) for _ in range(5)])
    if "PostgreSQL database dump" not in header:
        print("Invalid dump header!")
        return False

    print("Header verification successful.")
    return True


def restore_test_database(backup_file: Path) -> bool:
    print(f"Performing restore test using {backup_file}")

    pg_user = os.environ["POSTGRES_USER"]
    pg_db = os.environ["POSTGRES_DB"]
    db_container = os.environ["DB_CONTAINER"]
    restore_db = f"restore_test_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    try:
        run_cmd(
            [
                "docker",
                "exec",
                db_container,
                "psql",
                "-U",
                pg_user,
                "-d",
                pg_db,
                "-c",
                f"CREATE DATABASE {restore_db};",
            ],
            stdout=subprocess.DEVNULL,
        )
        
    except subprocess.CalledProcessError:
        print("Failed to create restore test database.")
        return False

    try:
        with backup_file.open("rb") as f:
            run_cmd(
                [
                    "docker",
                    "exec",
                    "-i",
                    db_container,
                    "psql",
                    "-U",
                    pg_user,
                    "-d",
                    restore_db,
                ],
                stdin=f,
                stdout=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError:
        print("Restore into restore test database failed.")
        return False
    finally:
        try:
            run_cmd(
                [
                    "docker",
                    "exec",
                    db_container,
                    "psql",
                    "-U",
                    pg_user,
                    "-d",
                    pg_db,
                    "-c",
                    f"DROP DATABASE {restore_db};",
                ],
                stdout=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            print(f"Warning: failed to drop restore database {restore_db}.")

    print("Restore test successful.")
    return True


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    # nextcloud root is one level up from scripts/
    nextcloud_root = script_dir.parent

    env_file = Path(os.environ.get("NEXTCLOUD_ENV_FILE", script_dir / ".env")).resolve()
    # normalize: if relative path given, resolve relative to script dir
    if not env_file.is_absolute():
        env_file = (script_dir / env_file).resolve()

    try:
        load_dotenv(env_file)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        print("Set NEXTCLOUD_ENV_FILE to the path of your .env and retry.")
        return 1

    DATE = datetime.now().strftime("%F-%H-%M-%S")
    BACKUP_ROOT = Path(os.environ["BACKUP_ROOT"])

    print("=============================================")
    print(DATE)
    print()

    save_config(nextcloud_root, BACKUP_ROOT, DATE)
    save_database_dump(nextcloud_root, BACKUP_ROOT, DATE)

    print("=============================================")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(2)
