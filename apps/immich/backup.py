#!/usr/bin/env python3
"""
Behavior:
- Loads .env from the Immich project root by default (override with IMMICH_ENV_FILE).
- Dumps the Immich Postgres database from Docker to IMMICH_BACKUP_LOCATION/db.
- Archives config files (.env, docker-compose.yml, and scripts/ if present) to IMMICH_BACKUP_LOCATION/config.
- Removes backup files older than 5 days.
- Verifies the SQL dump size and header.
- Performs a restore test into a temporary database to catch obvious corruption.

Usage:
  chmod +x immich-db-backup.py
  ./immich-db-backup.py
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


def run_cmd(cmd, /, check: bool = True, capture_output: bool = False, stdout=None, stdin=None):
    print(f"Running: {shlex.join(cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        stdout=stdout,
        stdin=stdin,
        text=False,
    )


def find_project_root(start: Path) -> Path:
    """Find the Immich project root by walking upward.

    We prefer a directory containing both docker-compose.yml and .env.
    Fallbacks keep the script working if you move it around.
    """

    candidates = [start, *start.parents]
    for d in candidates:
        if (d / "docker-compose.yml").exists() and (d / ".env").exists():
            return d
    for d in candidates:
        if (d / "docker-compose.yml").exists():
            return d
    for d in candidates:
        if (d / ".env").exists():
            return d
    return start.parent


def archive_paths(target: Path, paths: list[Path], project_root: Path):
    print(f"Archiving {len(paths)} paths to {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as tar:
        for p in paths:
            if not p.exists():
                print(f"Warning: {p} does not exist, skipping")
                continue
            try:
                arcname = p.relative_to(project_root)
            except ValueError:
                arcname = p.name
            tar.add(p, arcname=str(arcname))


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


def ensure_writable_path(path: Path) -> bool:
    if path.exists():
        if not os.access(path, os.W_OK):
            print(f"ERROR: Path exists but is not writable: {path}")
            return False
        return True

    parent = path.parent
    if not parent.exists():
        print(f"ERROR: Backup path parent does not exist: {parent}")
        return False
    if not os.access(parent, os.W_OK):
        print(f"ERROR: Backup path parent is not writable: {parent}")
        return False
    return True


def save_config(project_root: Path, backup_root: Path, date_str: str) -> bool:
    cfg_dir = backup_root / "config"
    cfg_file = cfg_dir / f"immich-config-{date_str}.tar.gz"
    try:
        print("Backing up config...")
        cfg_dir.mkdir(parents=True, exist_ok=True)

        paths = [
            project_root / ".env",
            project_root / "docker-compose.yml",
        ]

        archive_paths(cfg_file, paths, project_root)
        print(f"Config backup saved to {cfg_file}")
        return True
    except PermissionError as e:
        print(f"Permission denied while creating config backup directory {cfg_dir}: {e}")
        return False
    except Exception as e:
        print(f"Failed to backup config {cfg_file}: {e}")
        return False
    finally:
        prune_old_files(cfg_dir, "immich-config-*.tar.gz", days=5)
        print()


def save_database_dump(backup_root: Path, date_str: str) -> bool:
    db_dir = backup_root / "db"
    db_file = db_dir / f"immich-db-{date_str}.sql"
    try:
        print("Backing up database...")

        db_dir.mkdir(parents=True, exist_ok=True)

        pg_user = os.environ.get("DB_USERNAME")
        pg_db = os.environ.get("DB_DATABASE_NAME")
        db_password = os.environ.get("DB_PASSWORD")
        db_container = os.environ.get("DB_CONTAINER", "immich_postgres")

        if not pg_user or not pg_db or not db_container:
            print("ERROR: DB_USERNAME, DB_DATABASE_NAME, and DB_CONTAINER must be defined")
            return False

        exec_cmd = ["docker", "exec"]
        if db_password:
            exec_cmd += ["-e", f"PGPASSWORD={db_password}"]
        exec_cmd += [db_container, "pg_dump", "-U", pg_user, pg_db]

        with db_file.open("wb") as out:
            run_cmd(exec_cmd, stdout=out)

        print(f"Database dump saved to {db_file}")
    except PermissionError as e:
        print(f"Permission denied while creating database backup directory {db_dir}: {e}")
        return False
    except subprocess.CalledProcessError:
        print("Database dump failed.")
        return False

    if not verify_database_dump(db_file):
        return False

    if not restore_test_database(db_file):
        return False

    prune_old_files(db_dir, "immich-db-*.sql", days=5)
    return True


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

    try:
        with db_file.open() as f:
            header = "\n".join([next(f) for _ in range(5)])
    except Exception as e:
        print(f"Failed to read backup header: {e}")
        return False

    if "PostgreSQL database dump" not in header:
        print("Invalid dump header!")
        return False

    print("Header verification successful.")
    return True


def restore_test_database(backup_file: Path) -> bool:
    print(f"Performing restore test using {backup_file}")

    pg_user = os.environ.get("DB_USERNAME")
    pg_db = os.environ.get("DB_DATABASE_NAME")
    db_password = os.environ.get("DB_PASSWORD")
    db_container = os.environ.get("DB_CONTAINER", "immich_postgres")
    restore_db = f"restore_test_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    exec_env = []
    if db_password:
        exec_env = ["-e", f"PGPASSWORD={db_password}"]

    try:
        run_cmd(
            [
                "docker",
                "exec",
                *exec_env,
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
                    *exec_env,
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
                    *exec_env,
                    db_container,
                    "psql",
                    "-U",
                    pg_user,
                    "-d",
                    pg_db,
                    "-c",
                    f"DROP DATABASE {restore_db};",
                ],
                stdout=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            print(f"Warning: failed to drop restore database {restore_db}.")

    print("Restore test successful.")
    return True


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    project_root = find_project_root(script_dir)

    env_file = Path(os.environ.get("IMMICH_ENV_FILE", project_root / ".env"))
    if not env_file.is_absolute():
        env_file = (project_root / env_file).resolve()

    if not env_file.exists():
        print(f"ERROR: Environment file not found: {env_file}")
        print("Set IMMICH_ENV_FILE to the path of your .env and retry.")
        return 1

    load_dotenv(env_file)

    backup_root_value = os.environ.get("IMMICH_BACKUP_LOCATION") or os.environ.get("BACKUP_ROOT")
    if not backup_root_value:
        print("ERROR: IMMICH_BACKUP_LOCATION (or BACKUP_ROOT) must be defined in .env")
        return 1

    backup_root = Path(backup_root_value).expanduser()
    date_str = datetime.now().strftime("%F-%H-%M-%S")

    if not ensure_writable_path(backup_root):
        if os.geteuid() == 0:
            print("Note: Running as root against an NFS-mounted backup path may still fail if root_squash is enabled.")
            print("Run this script as your normal user instead.")
        return 1

    print("=============================================")
    print(date_str)
    print()

    if not save_config(project_root, backup_root, date_str):
        return 1
    if not save_database_dump(backup_root, date_str):
        return 1

    print("=============================================")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(2)
