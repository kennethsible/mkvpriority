import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from string.templatelib import Template
from tempfile import NamedTemporaryFile
from typing import Any

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt', 'WEBVTT': 'vtt'}

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvextract_logger = logging.getLogger('mkvextract')
mkvmerge_logger = logging.getLogger('mkvmerge')


class StreamFilter(logging.Filter):
    def __init__(self, stream_level: int = logging.INFO):
        super().__init__()
        self.stream_level = stream_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == 'mkvpriority':
            return record.levelno >= self.stream_level
        return True


stream_filter = StreamFilter()


def setup_logging(log_path: str | None = None, max_bytes: int = 0, max_files: int = 1) -> None:
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.addFilter(stream_filter)

    handlers: list[logging.Handler] = [stream_handler]
    if log_path:
        file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=max_files)
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)

    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s', handlers=handlers
    )


@dataclass
class Track:
    index: int
    kind: str
    score: int
    name: str
    language: str
    codec: str
    channels: int
    default: bool
    enabled: bool
    forced: bool
    uid: int


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
    penalize_unscored_languages: bool

    @classmethod
    def from_file(cls, toml_path: str) -> 'Config':
        with open(toml_path, 'rb') as f:
            toml_file = tomllib.load(f)
        if 'track_filters' in toml_file and 'subtitle_filters' not in toml_file:
            mkvpriority_logger.warning(
                f"'{toml_path}' [track_filters] is deprecated; use [subtitle_filters] instead"
            )
            toml_file['subtitle_filters'] = toml_file.pop('track_filters')
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
            penalize_unscored_languages=toml_file.get('penalize_unscored_languages', False),
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

    def insert(self, file_path: Path, tracks: list[Track]) -> None:
        dry_run = '[DRY RUN] ' if self.dry_run else ''
        if self.contains(file_path):
            mkvpriority_logger.info(dry_run + f"updating database '{self.db_path}'")
        else:
            mkvpriority_logger.info(dry_run + f"inserting into database '{self.db_path}'")
        if self.dry_run:
            return

        file_mtime = file_path.stat().st_mtime
        self.execute_t(
            t"""
            INSERT INTO archive (
                file_path,
                file_mtime,
                schema_version
            )
            VALUES ({str(file_path)}, {int(file_mtime)}, {self.SCHEMA_VERSION})
            ON CONFLICT(file_path) DO UPDATE SET
                file_mtime = excluded.file_mtime,
                schema_version = excluded.schema_version
            """
        )
        for track in tracks:
            self.execute_t(
                t"""
                INSERT INTO metadata (
                    file_path,
                    track_uid,
                    default_flag,
                    forced_flag,
                    enabled_flag
                )
                VALUES ({str(file_path)}, {str(track.uid)}, {int(track.default)}, {int(track.forced)}, {int(track.enabled)})
                ON CONFLICT(file_path, track_uid) DO NOTHING
                """
            )
        self.con.commit()

    def delete(self, file_path: Path, print_entry: bool = False) -> None:
        dry_run = '[DRY RUN] ' if self.dry_run else ''
        if print_entry:
            mkvpriority_logger.info(
                dry_run + f"deleting from database '{self.db_path}': '{file_path}'"
            )
        else:
            mkvpriority_logger.info(dry_run + f"deleting from database '{self.db_path}'")
        if not self.dry_run:
            self.execute_t(t'DELETE FROM archive WHERE file_path = {str(file_path)}')
            self.con.commit()

    def contains(self, file_path: Path, file_mtime: float | None = None) -> bool:
        if file_mtime is None:
            self.execute_t(t'SELECT 1 FROM archive WHERE file_path = {str(file_path)}')
        else:
            self.execute_t(
                t'SELECT 1 FROM archive WHERE file_path = {str(file_path)} AND file_mtime = {int(file_mtime)}'
            )
        return self.cur.fetchone() is not None

    def restore(self, file_path: Path, track: Track) -> bool:
        self.execute_t(
            t'SELECT default_flag, forced_flag, enabled_flag FROM metadata WHERE file_path = {str(file_path)} AND track_uid = {str(track.uid)}'
        )
        result = self.cur.fetchone()
        if result:
            track.default, track.forced, track.enabled = map(bool, result)
        return result is not None

    def prune(self) -> None:
        self.cur.execute('SELECT file_path FROM archive')
        for row in self.cur.fetchall():
            file_path = row[0]
            if file_path is None or Path(file_path).is_file():
                continue
            self.delete(file_path, print_entry=True)

    def migrate(self, db_path: str) -> None:
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

    def execute_t(self, template: Template):
        query = '?'.join(template.strings)
        params = tuple(interp.value for interp in template.interpolations)
        return self.cur.execute(query, params)


def extract_subtitles(file_path: Path, subtitle_track: Track) -> Path | None:
    if not subtitle_track.codec.startswith('S_TEXT/'):
        return None
    subtitle_format = subtitle_track.codec.split('/')[-1]
    if subtitle_format not in SUBTITLE_EXTENSIONS:
        return None
    extension = SUBTITLE_EXTENSIONS[subtitle_format]

    subtitle_suffix = f'.{subtitle_track.language}'
    if subtitle_track.default:
        subtitle_suffix += '.default'
    if subtitle_track.forced:
        subtitle_suffix += '.forced'
    subtitle_path = Path(file_path).with_suffix(f'{subtitle_suffix}.{extension}')
    if subtitle_path.is_file():
        return None
    mkvextract_logger.info(f"extracting subtitles to '{subtitle_path.parent}'")

    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(['tracks', str(file_path), f'{subtitle_track.index}:{subtitle_path}'], temp_file)
        temp_file.flush()
        result = subprocess.run(
            ['mkvextract', f'@{temp_file.name}'],
            capture_output=True,
            encoding='utf-8',
            check=True,
            text=True,
        )
        mkvextract_logger.debug(result.stdout.strip())

    return subtitle_path


def identify_tracks(file_path: Path) -> Any:
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(['--identification-format', 'json', '--identify', str(file_path)], temp_file)
        temp_file.flush()
        result = subprocess.run(
            ['mkvmerge', f'@{temp_file.name}'],
            capture_output=True,
            encoding='utf-8',
            check=True,
            text=True,
        )
        mkvmerge_logger.debug(result.stdout.strip())
        return json.loads(result.stdout)


def modify_tracks(arguments: list[str]) -> None:
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(arguments, temp_file)
        temp_file.flush()
        result = subprocess.run(
            ['mkvpropedit', f'@{temp_file.name}'],
            capture_output=True,
            check=True,
            text=True,
        )
        mkvpropedit_logger.debug(result.stdout.strip())


def extract_tracks(
    file_path: Path, scorer: Config | Database | None = None
) -> tuple[list[Track], list[Track], list[Track]]:
    video_tracks: list[Track] = []
    audio_tracks: list[Track] = []
    subtitle_tracks: list[Track] = []

    try:
        track_data = identify_tracks(file_path)
    except subprocess.CalledProcessError as e:
        try:
            track_data = json.loads(e.stdout)
        except json.JSONDecodeError:
            mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
            return video_tracks, audio_tracks, subtitle_tracks
        for warning in track_data.get('warnings', []):
            mkvmerge_logger.warning(warning)
        for error in track_data.get('errors', []):
            mkvmerge_logger.error(error)
        return video_tracks, audio_tracks, subtitle_tracks
    else:
        for warning in track_data.get('warnings', []):
            mkvmerge_logger.warning(warning)

    for metadata in track_data.get('tracks', []):
        properties = metadata.get('properties', {})

        track = Track(
            index=metadata.get('id'),
            kind=metadata.get('type'),
            score=0,
            name=properties.get('track_name'),
            language=properties.get('language', 'und'),
            codec=properties.get('codec_id'),
            channels=properties.get('audio_channels', 0),
            default=properties.get('default_track', False),
            enabled=properties.get('enabled_track', False),
            forced=properties.get('forced_track', False),
            uid=properties.get('uid'),
        )
        if track.uid is None:
            continue

        if isinstance(scorer, Database):
            if track.kind == 'audio':
                if scorer.restore(file_path, track):
                    audio_tracks.append(track)
            elif track.kind == 'subtitles':
                if scorer.restore(file_path, track):
                    subtitle_tracks.append(track)
            mkvpriority_logger.debug(track)
            continue

        match track.kind:
            case 'video':
                video_tracks.append(track)
            case 'audio':
                if scorer:
                    track.score += scorer.audio_languages.get(track.language, 0)
                    track.score += scorer.audio_codecs.get(track.codec, 0)
                    track.score += scorer.audio_channels.get(str(track.channels), 0)
                    if track.name:
                        for key, value in scorer.audio_filters.items():
                            if key in track.name.lower():
                                track.score += value
                audio_tracks.append(track)
                mkvpriority_logger.debug(track)
            case 'subtitles':
                if scorer:
                    default_language_score = -10000 if scorer.penalize_unscored_languages else 0
                    track.score += scorer.subtitle_languages.get(
                        track.language, default_language_score
                    )
                    track.score += scorer.subtitle_codecs.get(track.codec, 0)
                    if track.name:
                        for key, value in scorer.subtitle_filters.items():
                            if key in track.name.lower():
                                track.score += value
                subtitle_tracks.append(track)
                mkvpriority_logger.debug(track)

    return video_tracks, audio_tracks, subtitle_tracks


def restore_tracks(
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    database: Database,
    dry_run: bool = False,
) -> None:
    modify_args = [str(file_path)]
    logger_args: list[str] = []

    def apply_flags(track: Track, use_index: bool = False) -> list[str]:
        track_id = track.index if use_index else track.uid
        return [
            '--edit',
            f'track:={track_id}',
            '--set',
            f'flag-default={int(track.default)}',
            '--set',
            f'flag-forced={int(track.forced)}',
            '--set',
            f'flag-enabled={int(track.enabled)}',
        ]

    for track in [*audio_tracks, *subtitle_tracks]:
        modify_args += apply_flags(track, use_index=False)
        logger_args += apply_flags(track, use_index=True)

    if len(modify_args) > 1:
        mkvpropedit_logger.info(('[DRY RUN] ' if dry_run else '') + ' '.join(logger_args))
        if not dry_run:
            try:
                modify_tracks(modify_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    database.delete(file_path)


def process_tracks(
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    config: Config,
    database: Database | None = None,
    extract: bool = False,
    dry_run: bool = False,
) -> None:
    archive_tracks: list[Track] = []
    embedded_subtitles: Track | None = None
    modify_args = [str(file_path)]
    logger_args: list[str] = []

    def apply_flags(tracks: list[Track], track_modes: list[str]) -> None:
        nonlocal embedded_subtitles, modify_args, logger_args
        default_mode, forced_mode = 'default' in track_modes, 'forced' in track_modes
        disabled_mode, enabled_mode = 'disabled' in track_modes, 'enabled' in track_modes
        mkv_flags: dict[int, list[str]] = {track.uid: [] for track in tracks}

        if tracks[0].score > 0:
            if default_mode:
                if not tracks[0].default:
                    mkv_flags[tracks[0].uid].append('flag-default=1')
                if tracks[0].kind == 'subtitles':
                    if embedded_subtitles is None:
                        embedded_subtitles = replace(tracks[0])
                    embedded_subtitles.default = True
            if forced_mode:
                if not tracks[0].forced:
                    mkv_flags[tracks[0].uid].append('flag-forced=1')
                if tracks[0].kind == 'subtitles':
                    if embedded_subtitles is None:
                        embedded_subtitles = replace(tracks[0])
                    embedded_subtitles.forced = True
            if disabled_mode or enabled_mode:
                if not tracks[0].enabled:
                    mkv_flags[tracks[0].uid].append('flag-enabled=1')
            unwanted_tracks = tracks[1:]
        else:
            unwanted_tracks = tracks

        for track in unwanted_tracks:
            if track.score == 0:
                continue
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
                modify_args += ['--edit', f'track:={track.uid}']
                logger_args += ['--edit', f'track:={track.index}']
                for flag in mkv_flags[track.uid]:
                    modify_args += ['--set', flag]
                    logger_args += ['--set', flag]
                archive_tracks.append(track)

    if audio_tracks:
        apply_flags(audio_tracks, config.audio_mode)
    if subtitle_tracks:
        apply_flags(subtitle_tracks, config.subtitle_mode)

    if len(modify_args) > 1:
        mkvpropedit_logger.info(('[DRY RUN] ' if dry_run else '') + ' '.join(logger_args))
        if not dry_run:
            try:
                modify_tracks(modify_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    if extract and embedded_subtitles:
        extract_subtitles(file_path, embedded_subtitles)
    if database is not None:
        database.insert(file_path, archive_tracks)


def restore_file(
    file_path: Path, database: Database, dry_run: bool = False
) -> tuple[list[Track], list[Track]]:
    _, audio_tracks, subtitle_tracks = extract_tracks(file_path, database)
    for tracks in (audio_tracks, subtitle_tracks):
        tracks.sort(reverse=True, key=lambda track: track.score)
    restore_tracks(file_path, audio_tracks, subtitle_tracks, database, dry_run)
    return audio_tracks, subtitle_tracks


def process_file(
    file_path: Path,
    config: Config,
    database: Database | None = None,
    extract: bool = False,
    dry_run: bool = False,
) -> tuple[list[Track], list[Track]]:
    _, audio_tracks, subtitle_tracks = extract_tracks(file_path, config)
    for tracks in (audio_tracks, subtitle_tracks):
        tracks.sort(reverse=True, key=lambda track: track.score)
    process_tracks(file_path, audio_tracks, subtitle_tracks, config, database, extract, dry_run)
    return audio_tracks, subtitle_tracks


def main(argv: list[str] | None = None, orig_lang: str | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='append', default=[], metavar='TOML_PATH[::TAG]')
    parser.add_argument('-a', '--archive', metavar='DB_PATH')
    parser.add_argument('-v', '--verbose', action='store_true', help='inspect track metadata')
    parser.add_argument('-x', '--debug', action='store_true', help='show mkvtoolnix output')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress logging output')
    parser.add_argument('-p', '--prune', action='store_true', help='prune database entries')
    parser.add_argument('-n', '--dry-run', action='store_true', help='simulate track changes')
    parser.add_argument('-r', '--restore', action='store_true', help='restore original tracks')
    parser.add_argument('-e', '--extract', action='store_true', help='extract embedded subtitles')
    parser.add_argument(
        'input_paths', nargs='*', metavar='INPUT_PATH[::TAG]', help='files or directories'
    )
    args = parser.parse_args(argv)

    main_level = logging.DEBUG if args.verbose else logging.INFO
    tool_level = logging.DEBUG if args.debug else logging.INFO
    if args.quiet:
        main_level = tool_level = logging.ERROR
    setup_logging()

    stream_filter.stream_level = main_level
    mkvpriority_logger.setLevel(logging.DEBUG)
    for logger in (mkvpropedit_logger, mkvextract_logger, mkvmerge_logger):
        logger.setLevel(tool_level)

    configs: dict[str, Config] = {}
    for toml_path in args.config:
        if '::' in toml_path:
            toml_path, tag = toml_path.rsplit('::', 1)
        else:
            tag = 'untagged'
        config = Config.from_file(toml_path)
        if orig_lang and 'org' in config.audio_languages:
            config.audio_languages[orig_lang] = config.audio_languages['org']
        if orig_lang and 'org' in config.subtitle_languages:
            config.subtitle_languages[orig_lang] = config.subtitle_languages['org']
        if tag in configs:
            raise ValueError(f"duplicate ::{tag}: '{configs[tag]}' and '{config}'")
        configs[tag] = config

    database = None
    if args.archive:
        database = Database(args.archive, args.dry_run)
    if args.prune:
        if database is None:
            parser.error('cannot use --prune without --archive')
        else:
            database.prune()
    if args.restore and database is None:
        parser.error('cannot use --restore without --archive')

    dry_run = '[DRY RUN] ' if args.dry_run else ''
    for input_path in args.input_paths:
        if '::' in input_path:
            input_path, tag = input_path.rsplit('::', 1)
            if not (config := configs.get(tag)):
                mkvpriority_logger.warning(f"skipping (undefined ::{tag}) '{input_path}'")
                continue
        elif not (config := configs.get(tag)):
            parser.error('cannot process file(s) without --config')
        input_path = Path(input_path)

        if input_path.is_dir():
            mkvpriority_logger.info(dry_run + f"scanning '{input_path}'")
            file_paths = list(input_path.rglob('*.mkv'))
        elif input_path.is_file():
            file_paths = [input_path]
        else:
            mkvpriority_logger.warning(dry_run + f"skipping (not found) '{input_path}'")
            continue

        for file_path in file_paths:
            if database is not None:
                file_mtime = file_path.stat().st_mtime
                is_archived = database.contains(file_path, file_mtime)
                if not args.restore and is_archived:
                    mkvpriority_logger.info(dry_run + f"skipping (archived) '{file_path}'")
                    continue
                if args.restore and not is_archived:
                    mkvpriority_logger.info(dry_run + f"skipping (not archived) '{file_path}'")
                    continue

            if args.restore:
                mkvpriority_logger.info(dry_run + f"restoring '{file_path}'")
                restore_file(file_path, database, args.dry_run)
            else:
                mkvpriority_logger.info(dry_run + f"processing '{file_path}'")
                mkvpriority_logger.info(dry_run + f"using config '{config.toml_path}' ({tag})")
                process_file(file_path, config, database, args.extract, args.dry_run)


if __name__ == '__main__':
    main()
