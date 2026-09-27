import logging
import sqlite3
from pathlib import Path
from typing import Self

from .mkvtoolnix import ensure_segment_uid, extract_tracks, normalize_segment_uid
from .types import ArchiveRecord, Track

mkvpriority_logger = logging.getLogger('mkvpriority')


class Database:
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
            REFERENCES {archive_table}(segment_uid) 
            ON DELETE CASCADE 
            ON UPDATE CASCADE
    """

    def __init__(self, db_path: str, dry_run: bool = False):
        self.con = sqlite3.connect(db_path)
        self.cur = self.con.cursor()
        self.db_path = db_path
        self.dry_run = dry_run

        self._migrate(db_path)
        self._initialize()

        self.cur.execute('PRAGMA foreign_keys = ON')

        self.cur.execute('SELECT COUNT(*) FROM schema_info')
        if self.cur.fetchone()[0] == 0:
            self.cur.execute(
                'INSERT INTO schema_info (version) VALUES (?)', (int(self.SCHEMA_VERSION),)
            )
            self.con.commit()

    def insert(self, segment_uid: str | None, file_path: Path, tracks: list[Track]) -> None:
        file_path = file_path.resolve()
        if not segment_uid:
            segment_uid = ensure_segment_uid(file_path, self.dry_run)
        segment_uid = normalize_segment_uid(segment_uid)
        self.cur.execute('SELECT 1 FROM archive WHERE segment_uid = ?', (str(segment_uid),))
        is_update = self.cur.fetchone() is not None

        log_prefix = '[DRY RUN] ' if self.dry_run else ''
        if is_update:
            mkvpriority_logger.info(log_prefix + f"updating database '{self.db_path}'")
        else:
            mkvpriority_logger.info(log_prefix + f"inserting into database '{self.db_path}'")
        if self.dry_run:
            return

        self.cur.execute(
            'DELETE FROM archive WHERE file_path = ? AND segment_uid != ?',
            (str(file_path), str(segment_uid)),
        )

        file_mtime = file_path.stat().st_mtime
        self.cur.execute(
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

        for track in tracks:
            if track.is_external:
                continue
            self.cur.execute(
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
                (
                    str(segment_uid),
                    str(track.uid),
                    int(track.default),
                    int(track.forced),
                    int(track.enabled),
                ),
            )
        self.con.commit()

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
            self.cur.execute('DELETE FROM archive WHERE segment_uid = ?', (str(segment_uid),))
            self.con.commit()

    def select_by_path(self, file_path: Path) -> ArchiveRecord | None:
        file_path = file_path.resolve()
        self.cur.execute(
            """
            SELECT segment_uid, file_mtime 
            FROM archive 
            WHERE file_path = ?
            """,
            (str(file_path),),
        )
        row = self.cur.fetchone()
        if row is not None:
            return ArchiveRecord(
                segment_uid=str(row[0]), file_path=file_path, file_mtime=int(row[1])
            )
        return None

    def select_by_uid(self, segment_uid: str) -> ArchiveRecord | None:
        segment_uid = normalize_segment_uid(segment_uid)
        self.cur.execute(
            """
            SELECT file_path, file_mtime 
            FROM archive 
            WHERE segment_uid = ?
            """,
            (str(segment_uid),),
        )
        row = self.cur.fetchone()
        if row is not None:
            return ArchiveRecord(
                segment_uid=segment_uid, file_path=Path(row[0]), file_mtime=int(row[1])
            )
        return None

    def restore(self, segment_uid: str | None, track: Track) -> bool:
        if not segment_uid or track.is_external:
            return False
        segment_uid = normalize_segment_uid(segment_uid)
        self.cur.execute(
            """
            SELECT default_flag, forced_flag, enabled_flag 
            FROM metadata
            WHERE segment_uid = ? 
              AND track_uid = ?
            """,
            (str(segment_uid), str(track.uid)),
        )
        result = self.cur.fetchone()
        if result:
            track.default, track.forced, track.enabled = map(bool, result)
        return result is not None

    def prune(self) -> None:
        self.cur.execute('SELECT segment_uid, file_path FROM archive')
        for row in self.cur.fetchall():
            segment_uid, file_path = row[0], Path(row[1])
            if not file_path.is_file():
                self.delete(segment_uid, file_path)

    def _initialize(self, prefix: str = '') -> None:
        archive_table, metadata_table = f'{prefix}archive', f'{prefix}metadata'
        metadata_schema = self.METADATA_SCHEMA.format(archive_table=archive_table)
        self.cur.execute(f'CREATE TABLE IF NOT EXISTS {archive_table} ({self.ARCHIVE_SCHEMA})')
        self.cur.execute(f'CREATE TABLE IF NOT EXISTS {metadata_table} ({metadata_schema})')
        self.cur.execute('CREATE TABLE IF NOT EXISTS schema_info (version INTEGER)')

    def _migrate(self, db_path: str) -> None:
        def table_exists(table: str) -> bool:
            self.cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            return self.cur.fetchone() is not None

        def column_exists(table: str, column: str) -> bool:
            self.cur.execute('SELECT name FROM pragma_table_info(?)', (table,))
            return column in [row[0] for row in self.cur.fetchall()]

        if not table_exists('archive'):
            return

        schema_version = 0
        if table_exists('schema_info'):
            self.cur.execute('SELECT version FROM schema_info')
            row = self.cur.fetchone()
            schema_version = row[0] if row else 0
        else:
            if not column_exists('archive', 'schema_version'):
                self.cur.execute('ALTER TABLE archive ADD COLUMN schema_version INTEGER')
            self.cur.execute(
                'SELECT schema_version FROM archive ORDER BY schema_version DESC LIMIT 1'
            )
            row = self.cur.fetchone()
            schema_version = row[0] if row and row[0] is not None else 0

        if schema_version < self.SCHEMA_VERSION:
            if self.dry_run:
                raise RuntimeError(
                    f'cannot perform migration to schema version {self.SCHEMA_VERSION} during DRY RUN'
                )
            mkvpriority_logger.info(
                f"migrating '{db_path}' to schema version {self.SCHEMA_VERSION}"
            )

        if schema_version < 1:
            if not column_exists('archive', 'file_mtime'):
                self.cur.execute('ALTER TABLE archive ADD COLUMN file_mtime INTEGER')
            self.cur.execute('UPDATE archive SET schema_version = 1')
            schema_version = 1

        if schema_version < 2:
            self.cur.execute('SELECT file_path FROM archive')
            for row in self.cur.fetchall():
                file_path = row[0]
                if file_path is None or Path(file_path).is_file():
                    continue
                self.cur.execute('DELETE FROM archive WHERE file_path = ?', (str(file_path),))

            self._initialize(prefix='_')
            self.cur.execute('SELECT COUNT(*) FROM archive WHERE file_path IS NOT NULL')
            total_files = self.cur.fetchone()[0]

            def display_progress(current: int) -> None:
                if total_files <= 0:
                    return
                step = max(1, round(total_files * 0.1))
                if current % step == 0 or current == total_files:
                    percent = round((current / total_files) * 100)
                    mkvpriority_logger.info(
                        f'migrating database {current}/{total_files} ({percent}%)'
                    )

            self.cur.execute('SELECT file_path FROM archive WHERE file_path IS NOT NULL')
            for i, row in enumerate(self.cur.fetchall(), start=1):
                display_progress(i)
                file_path = Path(row[0])

                segment_uid, *_ = extract_tracks(file_path)
                if not segment_uid:
                    segment_uid = ensure_segment_uid(file_path, self.dry_run)

                file_mtime = file_path.stat().st_mtime
                self.cur.execute(
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
                self.cur.execute(
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

            self.cur.execute('DROP TABLE IF EXISTS metadata')
            self.cur.execute('DROP TABLE IF EXISTS archive')
            self.cur.execute('ALTER TABLE _archive RENAME TO archive')
            self.cur.execute('ALTER TABLE _metadata RENAME TO metadata')

            self.cur.execute('PRAGMA foreign_key_check')
            if errors := self.cur.fetchall():
                raise sqlite3.IntegrityError(
                    f'orphaned metadata records detected during migration: {errors}'
                )

            self.cur.execute('INSERT INTO schema_info (version) VALUES (2)')

        self.con.commit()

    def close(self) -> None:
        if hasattr(self, 'con') and self.con:
            self.con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
