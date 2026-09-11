from __future__ import annotations

import argparse
import copy
import glob
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Self, TypeVar

POSITION_PATTERN = re.compile(r'\\(?:pos|move|org|i?clip|fade?|t)\s*\(|\\an[13-79]', re.IGNORECASE)
ROTATION_PATTERN = re.compile(r'\\(fr[xyz]?|fa[xy])-?\d+\.?\d*', re.IGNORECASE)
KARAOKE_PATTERN = re.compile(r'\\k[fo]?\d+\.?\d*', re.IGNORECASE)
DRAWING_PATTERN = re.compile(r'\\p[1-9]\d*', re.IGNORECASE)
OVERRIDE_PATTERN = re.compile(r'\{[^}]*\}')


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


class Extension(ABC):
    def __init__(self, extension_name: str | None = None):
        name = extension_name or self.__class__.__name__
        self.extension_logger = logging.getLogger(name)

    @abstractmethod
    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        raise NotImplementedError


def setup_extension_paths() -> None:
    ext_dirs: list[Path] = []
    if env_dir := os.environ.get('MKVPRIORITY_EXT_DIR'):
        ext_dirs.append(Path(env_dir))
    ext_dirs.append(Path.home() / '.config' / 'mkvpriority' / 'extensions')
    ext_dirs.append(Path.cwd())
    for ext_dir in ext_dirs:
        if ext_dir.is_dir() and str(ext_dir) not in sys.path:
            sys.path.insert(0, str(ext_dir))


def load_extension(module_name: str) -> Extension | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        try:
            module = importlib.import_module(f'mkvpriority.extensions.{module_name}')
        except ImportError:
            mkvpriority_logger.error(f"could not locate extension '{module_name}'")
            return None

    for name, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, Extension)
            and obj is not Extension
            and obj.__module__ == module.__name__
        ):
            if not callable(obj):
                mkvpriority_logger.error(f"'{name}' in '{module_name}' is not callable")
                return None
            try:
                return obj()
            except TypeError:
                mkvpriority_logger.error(f"could not instantiate '{name}'")
                return None

    mkvpriority_logger.error(f"no valid extension subclass found in '{module_name}'")
    return None


@dataclass
class Track:
    index: int
    category: str
    name: str
    language: str
    codec: str
    channels: int
    default: bool
    forced: bool
    enabled: bool
    uid: int
    size: int | None = None
    scores: dict[str, int] = field(default_factory=dict)


P = TypeVar('P', bound='Profile')


@dataclass
class Profile:
    name: str
    mode: list[str] = field(default_factory=list)
    filters: dict[str, int] = field(default_factory=dict)
    require_filter_match: bool = False


@dataclass
class AudioProfile(Profile):
    pass


@dataclass
class SubtitleProfile(Profile):
    max_size_ratio: float | None = None


@dataclass
class ProfileGroup[P: Profile]:
    languages: dict[str, int] = field(default_factory=dict)
    codecs: dict[str, int] = field(default_factory=dict)
    profiles: dict[str, P] = field(default_factory=dict)
    penalize_unscored_languages: bool = False


@dataclass
class AudioProfileGroup(ProfileGroup[AudioProfile]):
    channels: dict[str, int] = field(default_factory=dict)


@dataclass
class SubtitleProfileGroup(ProfileGroup[SubtitleProfile]):
    native_languages: list[str] = field(default_factory=list)


class ConfigError(Exception):
    pass


def validate_config_schema(toml_dict: dict[str, Any], toml_path: Path) -> None:
    profile_sections = ('audio_profiles', 'subtitle_profiles')
    if missing_sections := [section for section in profile_sections if section not in toml_dict]:
        missing_sections_str = ' and '.join(f'[{section}]' for section in missing_sections)
        raise ConfigError(f"missing {missing_sections_str} in '{toml_path}'")

    for section_name in profile_sections:
        section = toml_dict[section_name]
        if not isinstance(section, dict):
            raise ConfigError(f"'[{section_name}]' in '{toml_path}' must be a table")

        global_section = section.get('global')
        if not isinstance(global_section, dict):
            raise ConfigError(f"missing '[{section_name}.global]' table in '{toml_path}'")

        global_keys = ['languages', 'codecs']
        if section_name == 'audio_profiles':
            global_keys.append('channels')
        for key in global_keys:
            if key in global_section and not isinstance(global_section[key], dict):
                raise ConfigError(
                    f"'[{section_name}.global.{key}]' in '{toml_path}' must be a table"
                )

        if (
            section_name == 'subtitle_profiles'
            and 'native_languages' in global_section
            and not isinstance(global_section['native_languages'], list)
        ):
            raise ConfigError(f"'native_languages' in '[{section_name}.global]' must be a list")

        profiles = {key: value for key, value in section.items() if key != 'global'}
        if not profiles:
            raise ConfigError(f"missing profiles for '[{section_name}]' in '{toml_path}'")

        mode_name = 'audio_mode' if section_name == 'audio_profiles' else 'subtitle_mode'
        for profile_name, profile in profiles.items():
            if not isinstance(profile, dict):
                raise ConfigError(
                    f"'[{section_name}.{profile_name}]' in '{toml_path}' must be a table"
                )

            profile_mode = profile.get(mode_name)
            if not isinstance(profile_mode, list):
                raise ConfigError(
                    f"'{mode_name}' in '[{section_name}.{profile_name}]' must be a list"
                )
            if not profile_mode:
                raise ConfigError(
                    f"empty {mode_name} for '[{section_name}.{profile_name}]' in '{toml_path}'"
                )

            if 'filters' in profile and not isinstance(profile['filters'], dict):
                raise ConfigError(f"'filters' in '[{section_name}.{profile_name}]' must be a table")


def apply_override(toml_dict: dict[str, Any], override: str) -> None:
    if '=' not in override:
        raise ValueError(f"invalid override format '{override}'")

    path, value_str = override.split('=', 1)
    keys = [key.strip() for key in path.strip().split('.')]
    try:
        value = json.loads(value_str.strip())
    except json.JSONDecodeError:
        value = value_str.strip()

    target = toml_dict
    for key in keys[:-1]:
        if key not in target or not isinstance(target[key], dict):
            raise ConfigError(f"unknown section '{key}' in override '{override}'")
        target = target[key]
    target[keys[-1]] = value


@dataclass
class Config:
    toml_path: str
    toml_label: str
    audio_group: AudioProfileGroup = field(default_factory=AudioProfileGroup)
    subtitle_group: SubtitleProfileGroup = field(default_factory=SubtitleProfileGroup)

    @classmethod
    def from_file(
        cls, toml_path: Path, toml_label: str = 'untagged', overrides: list[str] | None = None
    ) -> Config:
        with open(toml_path, 'rb') as f:
            toml_dict = tomllib.load(f)
        if overrides is not None:
            for override in overrides:
                apply_override(toml_dict, override)
        validate_config_schema(toml_dict, toml_path)

        audio_section = toml_dict.get('audio_profiles', {})
        audio_global = audio_section.get('global', {})
        audio_profiles = {
            key: AudioProfile(
                name=key,
                mode=value.get('audio_mode', []),
                filters=value.get('filters', {}),
                require_filter_match=value.get('require_filter_match', False),
            )
            for key, value in audio_section.items()
            if key != 'global'
        }
        audio_group = AudioProfileGroup(
            languages=audio_global.get('languages', {}),
            codecs=audio_global.get('codecs', {}),
            profiles=audio_profiles,
            penalize_unscored_languages=audio_global.get('penalize_unscored_languages', False),
            channels=audio_global.get('channels', {}),
        )

        subtitle_section = toml_dict.get('subtitle_profiles', {})
        subtitle_global = subtitle_section.get('global', {})
        subtitle_profiles = {
            key: SubtitleProfile(
                name=key,
                mode=value.get('subtitle_mode', []),
                filters=value.get('filters', {}),
                require_filter_match=value.get('require_filter_match', False),
                max_size_ratio=value.get('max_size_ratio'),
            )
            for key, value in subtitle_section.items()
            if key != 'global'
        }
        subtitle_group = SubtitleProfileGroup(
            languages=subtitle_global.get('languages', {}),
            codecs=subtitle_global.get('codecs', {}),
            profiles=subtitle_profiles,
            penalize_unscored_languages=subtitle_global.get('penalize_unscored_languages', False),
            native_languages=subtitle_global.get('native_languages', []),
        )

        return cls(
            toml_path=str(toml_path),
            toml_label=toml_label,
            audio_group=audio_group,
            subtitle_group=subtitle_group,
        )


@dataclass(frozen=True)
class ArchiveRecord:
    segment_uid: str
    file_path: Path
    file_mtime: int


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
        if not segment_uid:
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


class MissingCommandError(Exception):
    pass


def verify_mkvtoolnix_install() -> None:
    for command in ('mkvpropedit', 'mkvextract', 'mkvmerge'):
        if shutil.which(command) is None:
            raise MissingCommandError(f"'{command}' not found in PATH")


def normalize_segment_uid(segment_uid: str | None) -> str:
    if not segment_uid:
        return ''
    segment_uid = segment_uid.strip().lower().replace('-', '')
    return segment_uid.removeprefix('0x')


def ensure_segment_uid(file_path: Path, dry_run: bool = False) -> str:
    segment_uid = uuid.uuid4().hex
    mkvpriority_logger.info(f"generating segment_uid for '{file_path}'")
    arguments = [str(file_path), '--edit', 'info', '--set', f'segment-uid={segment_uid}']
    if not dry_run:
        try:
            modify_tracks(arguments)
        except subprocess.CalledProcessError as e:
            mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
    return segment_uid


def count_unique_dialogue(
    file_path: Path, tracks: list[Track], dry_run: bool = False
) -> dict[int, int]:
    ambiguous_tracks = [
        f'Track {track.index} ({track.name})' if track.name else f'Track {track.index}'
        for track in tracks
    ]
    log_prefix = '[DRY RUN] ' if dry_run else ''
    mkvextract_logger.info(log_prefix + f'analyzing subtitle sizes for {ambiguous_tracks}')

    dialogue_counts: dict[int, int] = {track.index: 0 for track in tracks}
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        arguments = ['mkvextract', 'tracks', str(file_path)]

        def codec_ext(codec: str) -> str:
            return 'ass' if codec in ('S_TEXT/ASS', 'S_TEXT/SSA') else 'srt'

        temp_paths = {
            track.index: temp_dir_path / f'{track.index}.{codec_ext(track.codec)}'
            for track in tracks
        }
        arguments.extend(f'{index}:{path}' for index, path in temp_paths.items())

        try:
            result = subprocess.run(arguments, capture_output=True, text=True, check=True)
            if result.stdout.strip():
                mkvextract_logger.debug(result.stdout.strip())
        except (subprocess.SubprocessError, OSError) as e:
            mkvextract_logger.error(str(e).strip())
            return dialogue_counts

        for index, temp_path in temp_paths.items():
            if not temp_path.exists():
                continue
            unique_dialogue: set[str] = set()
            with temp_path.open(encoding='utf-8', errors='replace') as temp_file:
                is_srt = any(
                    track.codec == 'S_TEXT/UTF8' for track in tracks if track.index == index
                )
                for line in temp_file:
                    if not (stripped_line := line.strip()):
                        continue
                    if is_srt:
                        if stripped_line.isdigit() or '-->' in stripped_line:
                            continue
                        dialogue = stripped_line
                    else:
                        if not stripped_line.startswith('Dialogue:'):
                            continue
                        dialogue = stripped_line.split(',', 9)[-1]
                    if any(
                        pattern.search(dialogue)
                        for pattern in (
                            DRAWING_PATTERN,
                            POSITION_PATTERN,
                            ROTATION_PATTERN,
                            KARAOKE_PATTERN,
                        )
                    ):
                        continue
                    stripped_dialogue = (
                        OVERRIDE_PATTERN.sub('', dialogue).strip()
                        if '{' in dialogue
                        else dialogue.strip()
                    )
                    if stripped_dialogue:
                        unique_dialogue.add(stripped_dialogue)
            dialogue_counts[index] = len(unique_dialogue)

    return dialogue_counts


def identify_tracks(file_path: Path) -> Any:
    with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
        json.dump(['--identification-format', 'json', '--identify', str(file_path)], temp_file)
        temp_file_path = Path(temp_file.name)

    try:
        result = subprocess.run(
            ['mkvmerge', f'@{temp_file_path}'],
            capture_output=True,
            encoding='utf-8',
            check=True,
            text=True,
        )
        mkvmerge_logger.debug(result.stdout.strip())
        return json.loads(result.stdout)
    finally:
        temp_file_path.unlink(missing_ok=True)


def modify_tracks(arguments: list[str]) -> None:
    with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
        json.dump(arguments, temp_file)
        temp_file_path = Path(temp_file.name)

    try:
        result = subprocess.run(
            ['mkvpropedit', f'@{temp_file_path}'],
            capture_output=True,
            check=True,
            text=True,
        )
        mkvpropedit_logger.debug(result.stdout.strip())
    finally:
        temp_file_path.unlink(missing_ok=True)


def extract_tracks(
    file_path: Path, database: Database | None = None
) -> tuple[str | None, list[Track], list[Track], list[Track]]:
    try:
        track_data = identify_tracks(file_path)
    except subprocess.CalledProcessError as e:
        try:
            track_data = json.loads(e.stdout)
        except json.JSONDecodeError:
            mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
        else:
            for warning in track_data.get('warnings', []):
                mkvmerge_logger.warning(warning)
            for error in track_data.get('errors', []):
                mkvmerge_logger.error(error)
        return None, [], [], []
    else:
        for warning in track_data.get('warnings', []):
            mkvmerge_logger.warning(warning)

    container_metadata = track_data.get('container', {})
    container_properties = container_metadata.get('properties', {})
    if segment_uid := container_properties.get('segment_uid'):
        segment_uid = normalize_segment_uid(segment_uid)

    video_tracks: list[Track] = []
    audio_tracks: list[Track] = []
    subtitle_tracks: list[Track] = []

    for metadata in track_data.get('tracks', []):
        properties = metadata.get('properties', {})

        track = Track(
            index=metadata.get('id'),
            category=metadata.get('type'),
            name=properties.get('track_name', ''),
            language=properties.get('language', 'und'),
            codec=properties.get('codec_id', ''),
            channels=properties.get('audio_channels', 0),
            default=properties.get('default_track', False),
            forced=properties.get('forced_track', False),
            enabled=properties.get('enabled_track', False),
            uid=properties.get('uid'),
        )
        if track.uid is None:
            continue

        if database is not None:
            if database.restore(segment_uid, track):
                match track.category:
                    case 'audio':
                        audio_tracks.append(track)
                    case 'subtitles':
                        subtitle_tracks.append(track)
            continue

        match track.category:
            case 'video':
                video_tracks.append(track)
            case 'audio':
                audio_tracks.append(track)
            case 'subtitles':
                subtitle_tracks.append(track)

    return segment_uid, video_tracks, audio_tracks, subtitle_tracks


def score_tracks[T: Profile](
    file_path: Path, tracks: list[Track], group: ProfileGroup[T], dry_run: bool = False
) -> None:
    max_track_size = 0

    def compute_score(track: Track, profile: Profile) -> int:
        score = 0
        default_lang_score = -10000 if group.penalize_unscored_languages else 0
        score += group.languages.get(track.language, default_lang_score)
        score += group.codecs.get(track.codec, 0)
        if isinstance(group, AudioProfileGroup):
            score += group.channels.get(str(track.channels), 0)

        filter_matched = False
        if track.name:
            for key, value in profile.filters.items():
                if key.lower() in track.name.lower():
                    score += value
                    filter_matched = True

        within_size_ratio = False
        if (
            isinstance(profile, SubtitleProfile)
            and (profile.max_size_ratio is not None)
            and max_track_size > 0
        ):
            if track.size is not None:
                if track.size / max_track_size <= profile.max_size_ratio:
                    within_size_ratio = True
                else:
                    score -= 10000
            else:
                score -= 10000

        if profile.require_filter_match and not (filter_matched or within_size_ratio):
            return -10000
        return score

    for track in tracks:
        for profile_name, profile in group.profiles.items():
            track.scores[profile_name] = compute_score(track, profile)

    if isinstance(group, SubtitleProfileGroup) and any(
        profile.max_size_ratio is not None for profile in group.profiles.values()
    ):
        candidate_tracks = [
            track
            for track in tracks
            if any(score > 0 for score in track.scores.values())
            and track.codec in ('S_TEXT/ASS', 'S_TEXT/SSA', 'S_TEXT/UTF8')
        ]
        is_ambiguous = len(candidate_tracks) > 1 and (
            all(not track.name for track in candidate_tracks)
        )

        if not is_ambiguous and len(candidate_tracks) > 1:
            profile_winners: dict[str, Track] = {}
            for profile_name, group_profile in group.profiles.items():
                best_track = max(
                    candidate_tracks, key=lambda track: track.scores.get(profile_name, 0)
                )
                if best_track.scores.get(profile_name, 0) > 0:
                    profile_winners[profile_name] = best_track

            for profile_name, group_profile in group.profiles.items():
                if group_profile.max_size_ratio is None:
                    continue
                if (target_track := profile_winners.get(profile_name)) is None:
                    continue
                if any(
                    target_track == other_track
                    for other_name, other_track in profile_winners.items()
                    if profile_name != other_name
                ):
                    is_ambiguous = True
                    break

        if is_ambiguous:
            dialogue_counts = count_unique_dialogue(file_path, candidate_tracks, dry_run)
            for subtitle_track in candidate_tracks:
                subtitle_track.size = dialogue_counts.get(subtitle_track.index, 0)
                max_track_size = max(subtitle_track.size, max_track_size)

            if max_track_size > 0:
                for track in tracks:
                    for profile_name, group_profile in group.profiles.items():
                        track.scores[profile_name] = compute_score(track, group_profile)


def restore_tracks(
    segment_uid: str,
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    database: Database,
    dry_run: bool = False,
) -> None:
    uid_args = [str(file_path)]
    idx_args: list[str] = []

    def apply_track_modes(track: Track, use_index: bool = False) -> list[str]:
        track_id = track.index if use_index else track.uid
        track_name = f' ({track.name})' if use_index and track.name else ''
        return [
            '--edit',
            f'track:={track_id}{track_name}',
            '--set',
            f'flag-default={int(track.default)}',
            '--set',
            f'flag-forced={int(track.forced)}',
            '--set',
            f'flag-enabled={int(track.enabled)}',
        ]

    for track in [*audio_tracks, *subtitle_tracks]:
        uid_args += apply_track_modes(track, use_index=False)
        idx_args += apply_track_modes(track, use_index=True)

    if len(uid_args) > 1:
        log_prefix = '[DRY RUN] ' if dry_run else ''
        mkvpropedit_logger.info(log_prefix + ' '.join(idx_args))
        if not dry_run:
            try:
                modify_tracks(uid_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    database.delete(segment_uid)


def restore_file(file_path: Path, database: Database, dry_run: bool = False) -> None:
    segment_uid, _, audio_tracks, subtitle_tracks = extract_tracks(file_path, database)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, dry_run)
    restore_tracks(segment_uid, file_path, audio_tracks, subtitle_tracks, database, dry_run)


def process_tracks(
    segment_uid: str,
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    config: Config,
    database: Database | None = None,
    dry_run: bool = False,
) -> None:
    orig_tracks: dict[int, Track] = {}
    uid_args = [str(file_path)]
    idx_args: list[str] = []

    def snapshot_track(track: Track) -> None:
        if track.uid not in orig_tracks:
            orig_tracks[track.uid] = copy.copy(track)

    def apply_profiles[T: Profile](
        tracks: list[Track], group: ProfileGroup[T], suppress_default: bool = False
    ) -> Track | None:
        if not tracks or not group.profiles:
            return None

        default_track: Track | None = None
        track_flags: dict[int, dict[str, str]] = {track.uid: {} for track in tracks}

        for profile_name, profile in group.profiles.items():
            track_modes = profile.mode
            default_mode = 'default' in track_modes
            forced_mode = 'forced' in track_modes
            disabled_mode = 'disabled' in track_modes
            enabled_mode = 'enabled' in track_modes

            sorted_tracks = sorted(
                tracks, key=lambda track: track.scores.get(profile_name, 0), reverse=True
            )
            best_track = sorted_tracks[0]
            best_score = best_track.scores.get(profile_name, 0)

            if default_mode and best_score > 0:
                default_track = best_track

            if best_score > 0:
                if default_mode and not suppress_default and not best_track.default:
                    track_flags[best_track.uid]['flag-default'] = '1'
                    snapshot_track(best_track)
                    best_track.default = True
                if forced_mode and not best_track.forced:
                    track_flags[best_track.uid]['flag-forced'] = '1'
                    snapshot_track(best_track)
                    best_track.forced = True
                if (disabled_mode or enabled_mode) and not best_track.enabled:
                    track_flags[best_track.uid]['flag-enabled'] = '1'
                    snapshot_track(best_track)
                    best_track.enabled = True
                unwanted_tracks = sorted_tracks[1:]
            else:
                unwanted_tracks = sorted_tracks

            for track in unwanted_tracks:
                if not track.scores.get(profile_name, 0):
                    continue
                if default_mode and not suppress_default and track.default:
                    track_flags[track.uid]['flag-default'] = '0'
                    snapshot_track(track)
                    track.default = False
                if forced_mode and track.forced:
                    track_flags[track.uid]['flag-forced'] = '0'
                    snapshot_track(track)
                    track.forced = False
                if disabled_mode and track.enabled:
                    track_flags[track.uid]['flag-enabled'] = '0'
                    snapshot_track(track)
                    track.enabled = False
                if enabled_mode and not track.enabled:
                    track_flags[track.uid]['flag-enabled'] = '1'
                    snapshot_track(track)
                    track.enabled = True

        for track in tracks:
            if track.default and suppress_default:
                track_flags[track.uid]['flag-default'] = '0'
                snapshot_track(track)
                track.default = False
            if track.default and track.forced:
                snapshot_track(track)
                track.forced = False
                if track.uid in orig_tracks and orig_tracks[track.uid].forced:
                    track_flags[track.uid]['flag-forced'] = '0'
                else:
                    track_flags[track.uid].pop('flag-forced', None)

        for track in tracks:
            mkvpriority_logger.debug(track)
            if track_flags[track.uid]:
                track_name = f' ({track.name})' if track.name else ''
                uid_args.extend(['--edit', f'track:={track.uid}'])
                idx_args.extend(['--edit', f'track:={track.index}{track_name}'])
                for flag, value in track_flags[track.uid].items():
                    uid_args.extend(['--set', f'{flag}={value}'])
                    idx_args.extend(['--set', f'{flag}={value}'])

        return default_track or tracks[0]

    score_tracks(file_path, audio_tracks, config.audio_group, dry_run)
    default_audio_track = apply_profiles(audio_tracks, config.audio_group)

    suppress_default = (
        default_audio_track is not None
        and default_audio_track.language in config.subtitle_group.native_languages
    )

    score_tracks(file_path, subtitle_tracks, config.subtitle_group, dry_run)
    apply_profiles(subtitle_tracks, config.subtitle_group, suppress_default=suppress_default)

    if len(uid_args) > 1:
        log_prefix = '[DRY RUN] ' if dry_run else ''
        mkvpropedit_logger.info(log_prefix + ' '.join(idx_args))
        if not dry_run:
            try:
                modify_tracks(uid_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    if database is not None:
        database.insert(segment_uid, file_path, list(orig_tracks.values()))


def process_file(
    file_path: Path,
    config: Config,
    database: Database | None = None,
    extensions: list[Extension] | None = None,
    dry_run: bool = False,
) -> None:
    segment_uid, video_tracks, audio_tracks, subtitle_tracks = extract_tracks(file_path)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, dry_run)
    process_tracks(segment_uid, file_path, audio_tracks, subtitle_tracks, config, database, dry_run)
    if extensions is not None:
        for extension in extensions:
            extension.process_file(
                file_path, video_tracks, audio_tracks, subtitle_tracks, config, dry_run
            )


def main(argv: list[str] | None = None, orig_lang: str | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='append', default=[], metavar='TOML_PATH[::TAG]')
    parser.add_argument('-a', '--archive', metavar='DB_PATH')
    parser.add_argument(
        '-i',
        '--include',
        action='append',
        default=[],
        metavar='MODULE_NAME',
        help='include extension module',
    )
    parser.add_argument(
        '-o',
        '--override',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='override config settings',
    )
    parser.add_argument('-v', '--verbose', action='store_true', help='inspect track metadata')
    parser.add_argument('-x', '--debug', action='store_true', help='show mkvtoolnix output')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress logging output')
    parser.add_argument('-p', '--prune', action='store_true', help='prune database entries')
    parser.add_argument('-n', '--dry-run', action='store_true', help='simulate track changes')
    parser.add_argument('-r', '--restore', action='store_true', help='restore original tracks')
    parser.add_argument(
        'input_paths', nargs='*', metavar='INPUT_PATH[::TAG]', help='files or directories'
    )
    args = parser.parse_args(argv)
    try:
        verify_mkvtoolnix_install()
    except MissingCommandError as e:
        parser.error(str(e))

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
        label = 'untagged'
        if '::' in toml_path:
            toml_path, label = toml_path.rsplit('::', 1)
        try:
            config = Config.from_file(Path(toml_path), label, args.override)
        except (ConfigError, ValueError) as e:
            parser.error(str(e))
        if orig_lang and 'org' in config.audio_group.languages:
            config.audio_group.languages[orig_lang] = config.audio_group.languages['org']
        if orig_lang and 'org' in config.subtitle_group.languages:
            config.subtitle_group.languages[orig_lang] = config.subtitle_group.languages['org']
        configs[label] = config
    if not configs and not (args.prune or args.restore):
        parser.error('cannot process file(s) without --config')

    database = None
    if args.archive:
        try:
            database = Database(args.archive, args.dry_run)
        except (sqlite3.IntegrityError, RuntimeError) as e:
            parser.error(str(e))
    if args.prune:
        if database is None:
            parser.error('cannot use --prune without --archive')
        else:
            database.prune()
    if args.restore and database is None:
        parser.error('cannot use --restore without --archive')

    extensions: list[Extension] = []
    if args.include:
        setup_extension_paths()
    for module_name in args.include:
        if extension := load_extension(module_name):
            extension.extension_logger.setLevel(tool_level)
            extensions.append(extension)

    log_prefix = '[DRY RUN] ' if args.dry_run else ''
    for input_path in args.input_paths:
        toml_label = 'untagged'
        if '::' in input_path:
            input_path, toml_label = input_path.rsplit('::', 1)
        if not (active_config := configs.get(toml_label) or configs.get('untagged')):
            mkvpriority_logger.warning(log_prefix + f"skipping (no config) '{input_path}'")
            continue
        escaped_pattern = input_path.replace('[', '[[]')
        if not (matched_paths := sorted(glob.glob(escaped_pattern, recursive=True))):
            mkvpriority_logger.warning(log_prefix + f"skipping (not found) '{input_path}'")
            continue

        file_paths: list[Path] = []
        for matched_path in matched_paths:
            file_path = Path(matched_path)
            if file_path.is_dir():
                mkvpriority_logger.info(log_prefix + f"scanning '{file_path}'")
                file_paths.extend(sorted(file_path.rglob('*.mkv')))
            elif file_path.is_file():
                if file_path.suffix.lower() == '.mkv':
                    file_paths.append(file_path)
        file_paths = list(dict.fromkeys(file_paths))

        for file_path in file_paths:
            if database is not None:
                is_archived = False
                file_mtime = int(file_path.stat().st_mtime)
                archive_record = database.select_by_path(file_path)
                if archive_record and archive_record.file_mtime == file_mtime:
                    is_archived = True
                if not is_archived:
                    segment_uid, *_ = extract_tracks(file_path)
                    if not segment_uid:
                        segment_uid = ensure_segment_uid(file_path, args.dry_run)
                    if archive_record and archive_record.segment_uid != segment_uid:
                        database.delete(archive_record.segment_uid)
                    uid_record = database.select_by_uid(segment_uid)
                    if uid_record and uid_record.file_mtime == file_mtime:
                        database.insert(segment_uid, file_path, [])
                        is_archived = True

                if not args.restore and is_archived:
                    mkvpriority_logger.info(log_prefix + f"skipping (archived) '{file_path}'")
                    continue
                if args.restore and not is_archived:
                    mkvpriority_logger.info(log_prefix + f"skipping (not archived) '{file_path}'")
                    continue

            if args.restore:
                assert database is not None
                mkvpriority_logger.info(log_prefix + f"restoring '{file_path}'")
                restore_file(file_path, database, args.dry_run)
            else:
                toml_path, toml_label = active_config.toml_path, active_config.toml_label
                config_tag = f'::{toml_label}' if toml_label != 'untagged' else ''
                mkvpriority_logger.info(log_prefix + f"processing '{file_path}'")
                mkvpriority_logger.info(log_prefix + f"using config '{toml_path}{config_tag}'")
                process_file(file_path, active_config, database, extensions, args.dry_run)

    if database is not None:
        database.close()


if __name__ == '__main__':
    main()
