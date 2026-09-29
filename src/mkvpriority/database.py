import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .mkvtoolnix import ensure_segment_uid, extract_tracks, normalize_segment_uid
from .types import Track

mkvpriority_logger = logging.getLogger('mkvpriority')


SCHEMA_VERSION = 2

ARCHIVE_SCHEMA = """
    segment_uid TEXT PRIMARY KEY,
    file_path TEXT UNIQUE,
    file_mtime INTEGER
"""

METADATA_SCHEMA = """
    segment_uid TEXT,
    track_uid TEXT,
    default_flag INTEGER,
    forced_flag INTEGER,
    enabled_flag INTEGER,
    PRIMARY KEY (segment_uid, track_uid),
    FOREIGN KEY(segment_uid) 
        REFERENCES archive(segment_uid) 
        ON DELETE CASCADE 
        ON UPDATE CASCADE
"""


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    segment_uid: str
    file_path: Path
    file_mtime: int


@dataclass(frozen=True, slots=True)
class MetadataRecord:
    track_uid: str
    default: bool
    forced: bool
    enabled: bool


class Database:
    def __init__(self, db_path: str | Path, dry_run: bool = False):
        self.db_path = db_path
        self.dry_run = dry_run
        self.con = sqlite3.connect(db_path)

        _perform_migrations(self.con, db_path, dry_run)

        self.con.execute('PRAGMA foreign_keys = ON')
        self._create_tables()
        self._ensure_schema_info()

    def _create_tables(self) -> None:
        self.con.execute(f'CREATE TABLE IF NOT EXISTS archive ({ARCHIVE_SCHEMA})')
        self.con.execute(f'CREATE TABLE IF NOT EXISTS metadata ({METADATA_SCHEMA})')
        self.con.execute('CREATE TABLE IF NOT EXISTS schema_info (version INTEGER)')

    def _ensure_schema_info(self) -> None:
        cur = self.con.execute('SELECT COUNT(*) FROM schema_info')
        if cur.fetchone()[0] == 0:
            with self.con:
                self.con.execute('INSERT INTO schema_info (version) VALUES (?)', (SCHEMA_VERSION,))

    def insert(self, segment_uid: str | None, file_path: Path, tracks: list[Track]) -> None:
        file_path = file_path.resolve()
        if not segment_uid:
            segment_uid = ensure_segment_uid(file_path, self.dry_run)
        segment_uid = normalize_segment_uid(segment_uid)

        cur = self.con.execute('SELECT 1 FROM archive WHERE segment_uid = ?', (str(segment_uid),))
        is_update = cur.fetchone() is not None

        log_prefix = '[DRY RUN] ' if self.dry_run else ''
        log_action = 'updating' if is_update else 'inserting into'
        mkvpriority_logger.info(f"{log_prefix}{log_action} database '{self.db_path}'")

        if self.dry_run:
            return

        file_mtime = file_path.stat().st_mtime
        metadata_records = [
            (
                str(segment_uid),
                str(track.uid),
                int(track.default),
                int(track.forced),
                int(track.enabled),
            )
            for track in tracks
            if not track.is_external
        ]

        with self.con:
            self.con.execute(
                'DELETE FROM archive WHERE file_path = ? AND segment_uid != ?',
                (str(file_path), str(segment_uid)),
            )

            self.con.execute(
                """
                INSERT INTO archive (
                    segment_uid, 
                    file_path, 
                    file_mtime
                ) VALUES (?, ?, ?) 
                ON CONFLICT(segment_uid) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_mtime = excluded.file_mtime
                """,
                (str(segment_uid), str(file_path), int(file_mtime)),
            )

            if metadata_records:
                self.con.executemany(
                    """
                    INSERT INTO metadata (
                        segment_uid, 
                        track_uid, 
                        default_flag, 
                        forced_flag, 
                        enabled_flag
                    ) VALUES (?, ?, ?, ?, ?) 
                    ON CONFLICT(segment_uid, track_uid) DO NOTHING
                    """,
                    metadata_records,
                )

    def delete(self, segment_uid: str, file_path: Path | None = None) -> None:
        segment_uid = normalize_segment_uid(segment_uid)
        log_prefix = '[DRY RUN] ' if self.dry_run else ''
        if file_path:
            file_path = file_path.resolve()
            mkvpriority_logger.info(
                log_prefix + f"deleting from database '{self.db_path}': '{file_path}'"
            )
        else:
            mkvpriority_logger.info(log_prefix + f"deleting from database '{self.db_path}'")

        if not self.dry_run:
            with self.con:
                self.con.execute('DELETE FROM archive WHERE segment_uid = ?', (str(segment_uid),))

    def prune(self) -> None:
        cur = self.con.execute('SELECT segment_uid, file_path FROM archive')
        stale_records: list[tuple[str, Path]] = []
        for row in cur.fetchall():
            segment_uid, file_path = row[0], Path(row[1])
            if not file_path.is_file():
                stale_records.append((segment_uid, file_path))

        if not stale_records:
            return

        log_prefix = '[DRY RUN] ' if self.dry_run else ''
        for _, file_path in stale_records:
            mkvpriority_logger.info(
                log_prefix + f"deleting from database '{self.db_path}': '{file_path}'"
            )

        if not self.dry_run:
            with self.con:
                self.con.executemany(
                    'DELETE FROM archive WHERE segment_uid = ?',
                    [(segment_uid,) for segment_uid, _ in stale_records],
                )

    def select_by_path(self, file_path: Path) -> ArchiveRecord | None:
        file_path = file_path.resolve()
        cur = self.con.execute(
            """
            SELECT segment_uid, file_mtime 
            FROM archive 
            WHERE file_path = ?
            """,
            (str(file_path),),
        )
        row = cur.fetchone()
        if row is not None:
            return ArchiveRecord(
                segment_uid=str(row[0]), file_path=file_path, file_mtime=int(row[1])
            )
        return None

    def select_by_uid(self, segment_uid: str) -> ArchiveRecord | None:
        segment_uid = normalize_segment_uid(segment_uid)
        cur = self.con.execute(
            """
            SELECT file_path, file_mtime 
            FROM archive 
            WHERE segment_uid = ?
            """,
            (str(segment_uid),),
        )
        row = cur.fetchone()
        if row is not None:
            return ArchiveRecord(
                segment_uid=segment_uid, file_path=Path(row[0]), file_mtime=int(row[1])
            )
        return None

    def select_metadata(self, segment_uid: str) -> dict[str, MetadataRecord]:
        segment_uid = normalize_segment_uid(segment_uid)
        cur = self.con.execute(
            """
            SELECT track_uid, default_flag, forced_flag, enabled_flag 
            FROM metadata
            WHERE segment_uid = ?
            """,
            (str(segment_uid),),
        )
        return {
            str(row[0]): MetadataRecord(
                track_uid=str(row[0]),
                default=bool(row[1]),
                forced=bool(row[2]),
                enabled=bool(row[3]),
            )
            for row in cur.fetchall()
        }

    def close(self) -> None:
        if hasattr(self, 'con') and self.con is not None:
            self.con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    cur = con.execute('SELECT name FROM pragma_table_info(?)', (table,))
    return column in [row[0] for row in cur.fetchall()]


def _perform_migrations(
    con: sqlite3.Connection, db_path: str | Path, dry_run: bool = False
) -> None:
    if not _table_exists(con, 'archive'):
        return

    schema_version = 0
    if _table_exists(con, 'schema_info'):
        cur = con.execute('SELECT version FROM schema_info')
        row = cur.fetchone()
        schema_version = row[0] if row else 0
    else:
        if not _column_exists(con, 'archive', 'schema_version'):
            con.execute('ALTER TABLE archive ADD COLUMN schema_version INTEGER')
        cur = con.execute('SELECT schema_version FROM archive ORDER BY schema_version DESC LIMIT 1')
        row = cur.fetchone()
        schema_version = row[0] if row and row[0] is not None else 0

    if schema_version >= SCHEMA_VERSION:
        return
    if dry_run:
        raise RuntimeError(
            f'cannot perform migration to schema version {SCHEMA_VERSION} during DRY RUN'
        )
    mkvpriority_logger.info(f"migrating '{db_path}' to schema version {SCHEMA_VERSION}")

    with con:
        if schema_version < 1:
            if not _column_exists(con, 'archive', 'file_mtime'):
                con.execute('ALTER TABLE archive ADD COLUMN file_mtime INTEGER')
            con.execute('UPDATE archive SET schema_version = 1')
            schema_version = 1

        if schema_version < 2:
            cur = con.execute('SELECT file_path FROM archive')
            for row in cur.fetchall():
                file_path = row[0]
                if file_path is None or Path(file_path).is_file():
                    continue
                con.execute('DELETE FROM archive WHERE file_path = ?', (str(file_path),))

            con.execute(f'CREATE TABLE _archive ({ARCHIVE_SCHEMA})')
            con.execute(f'CREATE TABLE _metadata ({METADATA_SCHEMA})')

            cur = con.execute('SELECT COUNT(*) FROM archive WHERE file_path IS NOT NULL')
            total_files = cur.fetchone()[0]

            def display_progress(current: int) -> None:
                if total_files <= 0:
                    return
                step = max(1, round(total_files * 0.1))
                if current % step == 0 or current == total_files:
                    percent = round((current / total_files) * 100)
                    mkvpriority_logger.info(
                        f'migrating database {current}/{total_files} ({percent}%)'
                    )

            cur = con.execute('SELECT file_path FROM archive WHERE file_path IS NOT NULL')
            for i, row in enumerate(cur.fetchall(), start=1):
                display_progress(i)
                file_path = Path(row[0])

                segment_uid, *_ = extract_tracks(file_path)
                if not segment_uid:
                    segment_uid = ensure_segment_uid(file_path)

                file_mtime = file_path.stat().st_mtime
                con.execute(
                    """
                        INSERT INTO _archive (
                            segment_uid, 
                            file_path, 
                            file_mtime
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(segment_uid) DO UPDATE SET
                            file_path = excluded.file_path,
                            file_mtime = excluded.file_mtime
                        """,
                    (str(segment_uid), str(file_path.resolve()), int(file_mtime)),
                )
                con.execute(
                    """
                        INSERT INTO _metadata (
                            segment_uid, 
                            track_uid, 
                            default_flag, 
                            forced_flag, 
                            enabled_flag
                        ) 
                        SELECT 
                            ?, 
                            track_uid, 
                            default_flag, 
                            forced_flag, 
                            enabled_flag
                        FROM metadata
                        WHERE file_path = ?
                        ON CONFLICT(segment_uid, track_uid) DO NOTHING
                        """,
                    (str(segment_uid), str(file_path)),
                )

            con.execute('DROP TABLE IF EXISTS metadata')
            con.execute('DROP TABLE IF EXISTS archive')
            con.execute('ALTER TABLE _archive RENAME TO archive')
            con.execute('ALTER TABLE _metadata RENAME TO metadata')

            cur = con.execute('PRAGMA foreign_key_check')
            if sqlite3_errors := cur.fetchall():
                raise sqlite3.IntegrityError(
                    f'orphaned metadata records detected during migration: {sqlite3_errors}'
                )

            con.execute('CREATE TABLE IF NOT EXISTS schema_info (version INTEGER)')
            con.execute('DELETE FROM schema_info')
            con.execute('INSERT INTO schema_info (version) VALUES (?)', (SCHEMA_VERSION,))
