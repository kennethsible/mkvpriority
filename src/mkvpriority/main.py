import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import tomllib

UNSUPPORTED_FORMATS = ['.mp4', '.m4v', '.mov', '.avi', '.webm']

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvmerge_logger = logging.getLogger('mkvmerge')


def setup_logging():
    if logging.getLogger().hasHandlers():
        return
    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )


@dataclass
class Track:
    score: int
    uid: int
    type: str
    name: str
    language: str
    codec: str
    channels: int
    default: bool
    enabled: bool
    forced: bool


@dataclass
class Config:
    toml_path: str
    audio_mode: list[str]
    audio_languages: dict[str, int]
    audio_codecs: dict[str, int]
    audio_channels: dict[str, int]
    audio_filters: dict[str, int]
    subtitle_mode: list[str]
    subtitle_languages: dict[str, int]
    subtitle_codecs: dict[str, int]
    subtitle_filters: dict[str, int]

    @classmethod
    def from_file(cls, toml_path: str) -> 'Config':
        with open(toml_path, 'rb') as f:
            toml_file = tomllib.load(f)
        if 'track_filters' in toml_file and 'subtitle_filters' not in toml_file:
            mkvpriority_logger.warning(
                f"'{toml_path}' [track_filters] is deprecated; use [subtitle_filters] instead"
            )
            toml_file['subtitle_filters'] = toml_file['track_filters']
        return cls(
            toml_path=toml_path,
            audio_mode=toml_file.get('audio_mode', []),
            audio_languages=toml_file.get('audio_languages', {}),
            audio_codecs=toml_file.get('audio_codecs', {}),
            audio_channels=toml_file.get('audio_channels', {}),
            audio_filters=toml_file.get('audio_filters', {}),
            subtitle_mode=toml_file.get('subtitle_mode', []),
            subtitle_languages=toml_file.get('subtitle_languages', {}),
            subtitle_codecs=toml_file.get('subtitle_codecs', {}),
            subtitle_filters=toml_file.get('subtitle_filters', {}),
        )


class Database:
    SCHEMA_VERSION = 1

    def __init__(self, db_path: str, dry_run: bool = False):
        self.con = sqlite3.connect(db_path)
        self.cur = self.con.cursor()
        self.cur.execute('PRAGMA foreign_keys = ON')
        self.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS archive (
                file_path TEXT PRIMARY KEY,
                file_mtime INTEGER,
                schema_version INTEGER
            )
        """
        )
        self.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                file_path TEXT,
                track_uid TEXT,
                default_flag INTEGER,
                forced_flag INTEGER,
                enabled_flag INTEGER,
                PRIMARY KEY (file_path, track_uid),
                FOREIGN KEY(file_path) REFERENCES archive(file_path) ON DELETE CASCADE
            )
        """
        )
        self.migrate(db_path)
        self.db_path = db_path
        self.dry_run = dry_run

    def insert(self, file_path: str, tracks: list[Track]):
        dry_run = '[DRY RUN] ' if self.dry_run else ''
        if self.contains(file_path):
            mkvpriority_logger.info(dry_run + f"updating database '{self.db_path}'")
        else:
            mkvpriority_logger.info(dry_run + f"inserting into database '{self.db_path}'")
        if self.dry_run:
            return

        file_mtime = int(os.path.getmtime(file_path))
        self.cur.execute(
            """
            INSERT INTO archive (
                file_path,
                file_mtime,
                schema_version
            )
            VALUES (?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                file_mtime = excluded.file_mtime,
                schema_version = excluded.schema_version
            """,
            (file_path, file_mtime, self.SCHEMA_VERSION),
        )
        for track in tracks:
            self.cur.execute(
                """
                INSERT INTO metadata (
                    file_path,
                    track_uid,
                    default_flag,
                    forced_flag,
                    enabled_flag
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_path, track_uid) DO NOTHING
                """,
                (
                    file_path,
                    str(track.uid),
                    int(track.default),
                    int(track.forced),
                    int(track.enabled),
                ),
            )
        self.con.commit()

    def delete(self, file_path: str, print_entry: bool = False):
        dry_run = '[DRY RUN] ' if self.dry_run else ''
        if print_entry:
            mkvpriority_logger.info(
                dry_run + f"deleting from database '{self.db_path}': '{file_path}'"
            )
        else:
            mkvpriority_logger.info(dry_run + f"deleting from database '{self.db_path}'")
        if not self.dry_run:
            self.cur.execute('DELETE FROM archive WHERE file_path = ?', (file_path,))
            self.con.commit()

    def contains(self, file_path: str, file_mtime: int | None = None) -> bool:
        if file_mtime is None:
            self.cur.execute('SELECT 1 FROM archive WHERE file_path = ?', (file_path,))
        else:
            self.cur.execute(
                'SELECT 1 FROM archive WHERE file_path = ? AND file_mtime = ?',
                (file_path, file_mtime),
            )
        return self.cur.fetchone() is not None

    def restore(self, file_path: str, track: Track) -> bool:
        self.cur.execute(
            'SELECT default_flag, forced_flag, enabled_flag FROM metadata WHERE file_path = ? AND track_uid = ?',
            (file_path, str(track.uid)),
        )
        result = self.cur.fetchone()
        if result:
            track.default, track.forced, track.enabled = map(bool, result)
        return result is not None

    def prune(self):
        self.cur.execute('SELECT file_path FROM archive')
        for row in self.cur.fetchall():
            file_path = row[0]
            if file_path is None or os.path.exists(file_path):
                continue
            self.delete(file_path, print_entry=True)

    def migrate(self, db_path: str):
        def column_exists(table: str, column: str) -> bool:
            self.cur.execute(f'PRAGMA table_info({table})')
            return column in [row[1] for row in self.cur.fetchall()]

        if not column_exists('archive', 'schema_version'):
            self.cur.execute('ALTER TABLE archive ADD COLUMN schema_version INTEGER')

        self.cur.execute('SELECT schema_version FROM archive ORDER BY schema_version DESC LIMIT 1')
        row = self.cur.fetchone()
        schema_version = row[0] if row and row[0] is not None else 0
        if schema_version < self.SCHEMA_VERSION:
            mkvpriority_logger.info(
                f"migrating schema for '{db_path}' to version {self.SCHEMA_VERSION}"
            )

        if schema_version < 1:
            if not column_exists('archive', 'file_mtime'):
                self.cur.execute('ALTER TABLE archive ADD COLUMN file_mtime INTEGER')
            self.cur.execute('INSERT INTO archive (schema_version) VALUES (1)')
        self.con.commit()


def identify_tracks(file_path: str):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(['--identification-format', 'json', '--identify', file_path], temp_file)
        temp_file.flush()
        result = subprocess.run(
            ['mkvmerge', f'@{temp_file.name}'],
            capture_output=True,
            encoding='utf-8',
            check=True,
            text=True,
        )
        mkvmerge_logger.debug(result.stdout.rstrip())
        return json.loads(result.stdout)


def modify_tracks(mkv_args: list[str]):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        result = subprocess.run(
            ['mkvpropedit', f'@{temp_file.name}'],
            capture_output=True,
            check=True,
            text=True,
        )
        mkvpropedit_logger.debug(result.stdout.rstrip())


def extract_tracks(
    file_path: str,
    config: Config | None = None,
    database: Database | None = None,
    restore: bool = False,
) -> tuple[list[Track], list[Track], list[Track]]:
    video_tracks: list[Track] = []
    audio_tracks: list[Track] = []
    subtitle_tracks: list[Track] = []

    try:
        json_object = identify_tracks(file_path)
    except subprocess.CalledProcessError as e:
        json_object = json.loads(e.stdout)
        for warning in json_object.get('warnings', []):
            mkvmerge_logger.warning(warning)
        for error in json_object.get('errors', []):
            mkvmerge_logger.error(error)
        return video_tracks, audio_tracks, subtitle_tracks
    else:
        for warning in json_object.get('warnings', []):
            mkvmerge_logger.warning(warning)

    for metadata in json_object.get('tracks', {}):
        properties = metadata.get('properties', {})

        track = Track(
            score=0,
            uid=properties.get('uid'),
            type=metadata.get('type'),
            name=properties.get('track_name'),
            language=properties.get('language', 'und'),
            codec=properties.get('codec_id'),
            channels=properties.get('audio_channels', 0),
            default=properties.get('default_track', False),
            enabled=properties.get('enabled_track', False),
            forced=properties.get('forced_track', False),
        )
        if track.uid is None:
            continue

        if config and restore:
            if database is None:
                mkvpriority_logger.error('cannot restore without a database')
                return [], [], []
            mkvpriority_logger.debug(track)
            if track.type == 'audio':
                if database.restore(file_path, track):
                    audio_tracks.append(track)
            elif track.type == 'subtitles':
                if database.restore(file_path, track):
                    subtitle_tracks.append(track)
            continue

        if track.type == 'video':
            video_tracks.append(track)

        elif track.type == 'audio':
            if config:
                track.score += config.audio_languages.get(track.language, 0)
                track.score += config.audio_codecs.get(track.codec, 0)
                track.score += config.audio_channels.get(str(track.channels), 0)
                if track.name:
                    for key, value in config.audio_filters.items():
                        if key in track.name.lower():
                            track.score += value
            audio_tracks.append(track)
            mkvpriority_logger.debug(track)

        elif track.type == 'subtitles':
            if config:
                track.score += config.subtitle_languages.get(track.language, 0)
                track.score += config.subtitle_codecs.get(track.codec, 0)
                if track.name:
                    for key, value in config.subtitle_filters.items():
                        if key in track.name.lower():
                            track.score += value
            subtitle_tracks.append(track)
            mkvpriority_logger.debug(track)

    return video_tracks, audio_tracks, subtitle_tracks


def mkvpropedit(
    file_path: str,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    config: Config,
    database: Database | None = None,
    restore: bool = False,
    dry_run: bool = False,
):
    if restore:
        if database is None:
            mkvpriority_logger.error('cannot restore without a database')
            return
        mkv_args = [file_path]
        for track in [*audio_tracks, *subtitle_tracks]:
            mkv_args += [
                '--edit',
                f'track:={track.uid}',
                '--set',
                f'flag-default={int(track.default)}',
                '--set',
                f'flag-forced={int(track.forced)}',
                '--set',
                f'flag-enabled={int(track.enabled)}',
            ]
        if len(mkv_args) > 1:
            if dry_run:
                mkvpropedit_logger.info('[DRY RUN] ' + ' '.join(mkv_args[1:]))
            else:
                mkvpropedit_logger.info(' '.join(mkv_args[1:]))
                try:
                    modify_tracks(mkv_args)
                except subprocess.CalledProcessError as e:
                    mkvpropedit_logger.error(e.stdout.rstrip())
                    return
        database.delete(file_path)
        return

    archive_tracks: list[Track] = []
    mkv_args = [file_path]

    def process_tracks(tracks: list[Track], track_modes: list[str]):
        nonlocal mkv_args
        default_mode, forced_mode = 'default' in track_modes, 'forced' in track_modes
        disabled_mode, enabled_mode = 'disabled' in track_modes, 'enabled' in track_modes
        mkv_flags: dict[int, list[str]] = {track.uid: [] for track in tracks}

        if tracks[0].score > 0:
            if default_mode and not tracks[0].default:
                mkv_flags[tracks[0].uid].append('flag-default=1')
            if forced_mode and not tracks[0].forced:
                mkv_flags[tracks[0].uid].append('flag-forced=1')
            if (disabled_mode or enabled_mode) and not tracks[0].enabled:
                mkv_flags[tracks[0].uid].append('flag-enabled=1')
            unwanted_tracks = tracks[1:]
        else:
            unwanted_tracks = tracks

        for track in unwanted_tracks:
            if default_mode and track.default:
                mkv_flags[track.uid].append('flag-default=0')
            if forced_mode and track.forced:
                mkv_flags[track.uid].append('flag-forced=0')
            if disabled_mode and track.enabled:
                mkv_flags[track.uid].append('flag-enabled=0')
            if enabled_mode and not track.enabled:
                mkv_flags[track.uid].append('flag-enabled=1')

        for track in tracks:
            if mkv_flags[track.uid]:
                mkv_args += ['--edit', f'track:={track.uid}']
                for flag in mkv_flags[track.uid]:
                    mkv_args += ['--set', flag]
                archive_tracks.append(track)

    if audio_tracks:
        process_tracks(audio_tracks, config.audio_mode)
    if subtitle_tracks:
        process_tracks(subtitle_tracks, config.subtitle_mode)

    if len(mkv_args) > 1:
        if dry_run:
            mkvpropedit_logger.info('[DRY RUN] ' + ' '.join(mkv_args[1:]))
        else:
            mkvpropedit_logger.info(' '.join(mkv_args[1:]))
            try:
                modify_tracks(mkv_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error(e.stdout.rstrip())
                return
    if database is not None:
        database.insert(file_path, archive_tracks)


def process_file(
    file_path: str,
    config: Config,
    database: Database | None = None,
    restore: bool = False,
    dry_run: bool = False,
) -> tuple[list[Track], list[Track]]:
    _, audio_tracks, subtitle_tracks = extract_tracks(file_path, config, database, restore)
    for tracks in (audio_tracks, subtitle_tracks):
        tracks.sort(reverse=True, key=lambda track: track.score)
    mkvpropedit(file_path, audio_tracks, subtitle_tracks, config, database, restore, dry_run)
    return audio_tracks, subtitle_tracks


def main(argv: list[str] | None = None, orig_lang: str | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='append', metavar='TOML_PATH[::TAG]')
    parser.add_argument('-a', '--archive', metavar='DB_PATH')
    parser.add_argument('-v', '--verbose', action='store_true', help='print track information')
    parser.add_argument('-x', '--debug', action='store_true', help='show mkvtoolnix results')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress logging output')
    parser.add_argument('-p', '--prune', action='store_true', help='prune database entries')
    parser.add_argument('-r', '--restore', action='store_true', help='restore original flags')
    parser.add_argument('-n', '--dry-run', action='store_true', help='leave tracks unchanged')
    parser.add_argument(
        'input_paths', nargs='*', metavar='INPUT_PATH[::TAG]', help='files or directories'
    )
    args = parser.parse_args(argv)

    setup_logging()
    mkvpriority_logger.setLevel(
        logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    )
    log_level = logging.ERROR if args.quiet else logging.DEBUG if args.debug else logging.INFO
    mkvpropedit_logger.setLevel(log_level)
    mkvmerge_logger.setLevel(log_level)

    configs: dict[str, Config] = {}
    for toml_path in args.config:
        tag = 'default'
        if '::' in toml_path:
            toml_path, tag = toml_path.rsplit('::', 1)
        try:
            config = Config.from_file(toml_path)
        except (FileNotFoundError, tomllib.TOMLDecodeError):
            mkvpriority_logger.exception(f"error occurred while loading config: '{toml_path}'")
            raise
        if orig_lang and 'org' in config.audio_languages:
            config.audio_languages[orig_lang] = config.audio_languages['org']
        if orig_lang and 'org' in config.subtitle_languages:
            config.subtitle_languages[orig_lang] = config.subtitle_languages['org']
        configs[tag] = config

    database = None
    if args.archive:
        try:
            database = Database(args.archive, args.dry_run)
        except (FileNotFoundError, sqlite3.OperationalError):
            mkvpriority_logger.exception(f"error occurred while loading database: '{args.archive}'")
            raise
    if args.prune:
        if database is None:
            parser.error('cannot use --prune without --archive')
        else:
            database.prune()
    if args.restore and database is None:
        parser.error('cannot use --restore without --archive')

    dry_run = '[DRY RUN] ' if args.dry_run else ''
    for input_path in args.input_paths:
        tag = 'default'
        if '::' in input_path:
            input_path, tag = input_path.rsplit('::', 1)
        config = configs.get(tag, configs['default'])

        file_paths: list[str] = []
        if os.path.isdir(input_path):
            mkvpriority_logger.info(dry_run + f"scanning '{input_path}'")
            for root, _, files in os.walk(input_path):
                file_paths.extend(os.path.join(root, f) for f in files)
        elif os.path.isfile(input_path):
            file_paths.append(input_path)
        else:
            mkvpriority_logger.warning(dry_run + f"skipping (not found) '{input_path}'")

        for file_path in file_paths:
            suffix = Path(file_path).suffix.lower()
            if suffix != '.mkv':
                if suffix in UNSUPPORTED_FORMATS:
                    mkvpriority_logger.warning(
                        dry_run + f"skipping (unsupported format) '{file_path}'"
                    )
                continue
            if database is not None:
                file_mtime = int(os.path.getmtime(file_path))
                is_archived = database.contains(file_path, file_mtime)
                if not args.restore and is_archived:
                    mkvpriority_logger.info(dry_run + f"skipping (archived) '{file_path}'")
                    continue
                if args.restore and not is_archived:
                    mkvpriority_logger.info(dry_run + f"skipping (unarchived) '{file_path}'")
                    continue

            operation = 'restoring' if args.restore else 'processing'
            mkvpriority_logger.info(dry_run + f"{operation} '{file_path}'")
            mkvpriority_logger.info(dry_run + f"using config '{config.toml_path}' ({tag})")
            process_file(file_path, config, database, args.restore, args.dry_run)


if __name__ == '__main__':
    main()
