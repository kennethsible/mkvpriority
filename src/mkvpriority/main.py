import argparse
import copy
import glob
import importlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from pprint import pformat
from sqlite3 import Cursor
from string.templatelib import Template
from tempfile import NamedTemporaryFile
from typing import Any, TypeVar

POSITION_PATTERN = re.compile(r'\\(?:pos|move|org|i?clip|fade?|t)\s*\(|\\an[13-79]', re.IGNORECASE)
ROTATION_PATTERN = re.compile(r'\\(fr[xyz]?|fa[xy])-?\d+\.?\d*', re.IGNORECASE)
KARAOKE_PATTERN = re.compile(r'\\k[fo]?\d+\.?\d*', re.IGNORECASE)
DRAWING_PATTERN = re.compile(r'\\p[1-9]\d*', re.IGNORECASE)
OVERRIDE_PATTERN = re.compile(r'\{[^}]*\}')


mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
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


@dataclass
class AudioProfileGroup(ProfileGroup[AudioProfile]):
    channels: dict[str, int] = field(default_factory=dict)


@dataclass
class SubtitleProfileGroup(ProfileGroup[SubtitleProfile]):
    penalize_unscored_languages: bool = False
    native_languages: list[str] = field(default_factory=list)


@dataclass
class Config:
    toml_path: str
    label: str
    audio_group: AudioProfileGroup = field(default_factory=AudioProfileGroup)
    subtitle_group: SubtitleProfileGroup = field(default_factory=SubtitleProfileGroup)

    @classmethod
    def from_file(cls, toml_path: Path, label: str = 'untagged') -> Config:
        with open(toml_path, 'rb') as f:
            toml_file = tomllib.load(f)

        audio_section = toml_file.get('audio_profiles', {})
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
            channels=audio_global.get('channels', {}),
        )

        subtitle_section = toml_file.get('subtitle_profiles', {})
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
            label=label,
            audio_group=audio_group,
            subtitle_group=subtitle_group,
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

    def execute_t(self, template: Template) -> Cursor:
        query = '?'.join(template.strings)
        params = tuple(interp.value for interp in template.interpolations)
        return self.cur.execute(query, params)


class MissingCommandError(Exception):
    pass


def verify_mkvtoolnix_install() -> None:
    for command in ('mkvpropedit', 'mkvmerge'):
        if shutil.which(command) is None:
            raise MissingCommandError(f"'{command}' not found in PATH")


def count_unique_dialogue(
    file_path: Path, track_index: int, threshold_limit: int | None = None
) -> int:
    unique_dialogue: set[str] = set()
    try:
        with subprocess.Popen(
            shlex.split(
                f'ffmpeg -nostdin -v quiet -i "{file_path}" -map 0:{track_index} -c:s ass -f ass -'
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        ) as process:
            if not process.stdout:
                return 0
            for line in process.stdout:
                if not line.startswith('Dialogue:'):
                    continue
                parts = line.split(',', 9)
                if len(parts) <= 9:
                    continue
                dialogue = parts[9]
                if '{' in dialogue:
                    if (
                        DRAWING_PATTERN.search(dialogue)
                        or POSITION_PATTERN.search(dialogue)
                        or ROTATION_PATTERN.search(dialogue)
                        or KARAOKE_PATTERN.search(dialogue)
                    ):
                        continue
                    stripped_dialogue = OVERRIDE_PATTERN.sub('', dialogue).strip()
                else:
                    stripped_dialogue = dialogue.strip()
                if stripped_dialogue:
                    unique_dialogue.add(stripped_dialogue)
                    if threshold_limit and len(unique_dialogue) >= threshold_limit:
                        process.terminate()
                        break
            process.wait()
    except (subprocess.SubprocessError, OSError) as e:
        mkvpriority_logger.error(str(e).strip())
        return 0
    return len(unique_dialogue)


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
            category=metadata.get('type'),
            name=properties.get('track_name', ''),
            language=properties.get('language', 'und'),
            codec=properties.get('codec_id', ''),
            channels=properties.get('audio_channels', 0),
            default=properties.get('default_track', False),
            enabled=properties.get('enabled_track', False),
            forced=properties.get('forced_track', False),
            uid=properties.get('uid'),
        )
        if track.uid is None:
            continue

        if database is not None:
            if database.restore(file_path, track):
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

    return video_tracks, audio_tracks, subtitle_tracks


def score_tracks[T: Profile](file_path: Path, tracks: list[Track], group: ProfileGroup[T]) -> None:
    max_track_size = 0
    if (
        isinstance(group, SubtitleProfileGroup)
        and any(profile.max_size_ratio is not None for profile in group.profiles.values())
        and any(not subtitle_track.name for subtitle_track in tracks)
    ):
        if shutil.which('ffmpeg') is None:
            mkvpriority_logger.warning('cannot apply max_size_ratio; ffmpeg not in PATH')
        else:
            for subtitle_track in tracks:
                if subtitle_track.codec not in ('S_HDMV/PGS', 'S_VOBSUB'):
                    subtitle_track.size = count_unique_dialogue(file_path, subtitle_track.index)
                    max_track_size = max(subtitle_track.size, max_track_size)

    def score_track(track: Track, profile: Profile) -> int:
        score = 0
        if isinstance(group, SubtitleProfileGroup) and group.penalize_unscored_languages:
            score += group.languages.get(track.language, -10000)
        else:
            score += group.languages.get(track.language, 0)
        score += group.codecs.get(track.codec, 0)
        if isinstance(group, AudioProfileGroup):
            score += group.channels.get(str(track.channels), 0)

        filter_matched = False
        if track.name:
            for key, value in profile.filters.items():
                if key in track.name.lower():
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
            track.scores[profile_name] = score_track(track, profile)


def restore_tracks(
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    database: Database,
    dry_run: bool = False,
) -> None:
    modify_args = [str(file_path)]
    logger_args: list[str] = []

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
        modify_args += apply_track_modes(track, use_index=False)
        logger_args += apply_track_modes(track, use_index=True)

    if len(modify_args) > 1:
        mkvpropedit_logger.info(('[DRY RUN] ' if dry_run else '') + ' '.join(logger_args))
        if not dry_run:
            try:
                modify_tracks(modify_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    database.delete(file_path)


def restore_file(file_path: Path, database: Database, dry_run: bool = False) -> None:
    _, audio_tracks, subtitle_tracks = extract_tracks(file_path, database)
    restore_tracks(file_path, audio_tracks, subtitle_tracks, database, dry_run)


def process_tracks(
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    config: Config,
    database: Database | None = None,
    dry_run: bool = False,
) -> None:
    orig_tracks: dict[int, Track] = {}
    modify_args = [str(file_path)]
    logger_args: list[str] = []

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
                track.forced = False
                track_flags[track.uid]['flag-forced'] = '0'
                snapshot_track(track)

        for track in tracks:
            mkvpriority_logger.debug(pformat(track))
            if track_flags[track.uid]:
                track_name = f' ({track.name})' if track.name else ''
                modify_args.extend(['--edit', f'track:={track.uid}'])
                logger_args.extend(['--edit', f'track:={track.index}{track_name}'])
                for flag, value in track_flags[track.uid].items():
                    modify_args.extend(['--set', f'{flag}={value}'])
                    logger_args.extend(['--set', f'{flag}={value}'])

        return default_track or tracks[0]

    score_tracks(file_path, audio_tracks, config.audio_group)
    default_audio_track = apply_profiles(audio_tracks, config.audio_group)

    suppress_default = (
        default_audio_track is not None
        and default_audio_track.language in config.subtitle_group.native_languages
    )

    score_tracks(file_path, subtitle_tracks, config.subtitle_group)
    apply_profiles(subtitle_tracks, config.subtitle_group, suppress_default=suppress_default)

    if len(modify_args) > 1:
        mkvpropedit_logger.info(('[DRY RUN] ' if dry_run else '') + ' '.join(logger_args))
        if not dry_run:
            try:
                modify_tracks(modify_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    if database is not None and orig_tracks:
        database.insert(file_path, list(orig_tracks.values()))


def process_file(
    file_path: Path,
    config: Config,
    database: Database | None = None,
    extensions: list[Extension] | None = None,
    dry_run: bool = False,
) -> None:
    video_tracks, audio_tracks, subtitle_tracks = extract_tracks(file_path)
    process_tracks(file_path, audio_tracks, subtitle_tracks, config, database, dry_run)
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
        '-i', '--include', action='append', metavar='MODULE_NAME', help='include extension module'
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
    for logger in (mkvpropedit_logger, mkvmerge_logger):
        logger.setLevel(tool_level)

    configs: dict[str, Config] = {}
    for toml_path in args.config:
        label = 'untagged'
        if '::' in toml_path:
            toml_path, label = toml_path.rsplit('::', 1)
        config = Config.from_file(Path(toml_path), label)
        if orig_lang and 'org' in config.audio_group.languages:
            config.audio_group.languages[orig_lang] = config.audio_group.languages['org']
        if orig_lang and 'org' in config.subtitle_group.languages:
            config.subtitle_group.languages[orig_lang] = config.subtitle_group.languages['org']
        configs[label] = config
    if not configs and not (args.prune or args.restore):
        parser.error('cannot process file(s) without --config')

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

    extensions: list[Extension] = []
    if args.include:
        setup_extension_paths()
        for module_name in args.include:
            if extension := load_extension(module_name):
                extension.extension_logger.setLevel(tool_level)
                extensions.append(extension)

    dry_run = '[DRY RUN] ' if args.dry_run else ''
    for input_path in args.input_paths:
        label = 'untagged'
        if '::' in input_path:
            input_path, label = input_path.rsplit('::', 1)
        if not (active_config := configs.get(label) or configs.get('untagged')):
            mkvpriority_logger.warning(dry_run + f"skipping (no config) '{input_path}'")
            continue
        if not (matched_paths := glob.glob(input_path.replace('[', '[[]'), recursive=True)):
            mkvpriority_logger.warning(dry_run + f"skipping (not found) '{input_path}'")
            continue

        file_paths: list[Path] = []
        for matched_path in matched_paths:
            file_path = Path(matched_path)
            if file_path.is_dir():
                mkvpriority_logger.info(dry_run + f"scanning '{file_path}'")
                file_paths.extend(file_path.rglob('*.mkv'))
            elif file_path.is_file():
                file_paths.append(file_path)
        file_paths = list(dict.fromkeys(file_paths))

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
                assert database is not None
                mkvpriority_logger.info(dry_run + f"restoring '{file_path}'")
                restore_file(file_path, database, args.dry_run)
            else:
                toml_path, label = active_config.toml_path, active_config.label
                config_tag = f'::{label}' if label != 'untagged' else ''
                mkvpriority_logger.info(dry_run + f"processing '{file_path}'")
                mkvpriority_logger.info(dry_run + f"using config '{toml_path}{config_tag}'")
                process_file(file_path, active_config, database, extensions, args.dry_run)


if __name__ == '__main__':
    main()
