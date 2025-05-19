import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from tempfile import NamedTemporaryFile

import tomllib

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvmerge_logger = logging.getLogger('mkvmerge')


@dataclass
class Track:
    track_id: int
    track_type: str
    track_name: str
    uid: int
    score: int
    language: str
    codec: str
    channels: int
    default: bool
    enabled: bool
    forced: bool


@dataclass
class Config:
    audio_mode: list[str]
    subtitle_mode: list[str]
    audio_languages: dict[str, int]
    audio_codecs: dict[str, int]
    audio_channels: dict[int, int]
    subtitle_languages: dict[str, int]
    subtitle_codecs: dict[str, int]
    track_filters: dict[str, int]


class Database:
    def __init__(self, db_path: str):
        self.con = sqlite3.connect(db_path)
        self.cur = self.con.cursor()
        self.cur.execute('PRAGMA foreign_keys = ON')
        self.cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS archive (
                file_path TEXT PRIMARY KEY
            )
        '''
        )
        self.cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS metadata (
                file_path TEXT,
                track_uid TEXT,
                default_flag INTEGER,
                forced_flag INTEGER,
                enabled_flag INTEGER,
                PRIMARY KEY (file_path, track_uid),
                FOREIGN KEY(file_path) REFERENCES archive(file_path) ON DELETE CASCADE
            )
        '''
        )

    def insert(self, file_path: str, tracks: list[Track]):
        self.cur.execute('REPLACE INTO archive (file_path) VALUES (?)', (file_path,))
        for track in tracks:
            self.cur.execute(
                '''
                REPLACE INTO metadata (
                    file_path,
                    track_uid,
                    default_flag,
                    forced_flag,
                    enabled_flag
                )
                VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    file_path,
                    str(track.uid),
                    int(track.default),
                    int(track.forced),
                    int(track.enabled),
                ),
            )
        self.con.commit()

    def delete(self, file_path: str):
        self.cur.execute('DELETE FROM archive WHERE file_path = ?', (file_path,))
        self.con.commit()

    def contains(self, file_path: str) -> bool:
        self.cur.execute('SELECT 1 FROM archive WHERE file_path = ?', (file_path,))
        return self.cur.fetchone() is not None

    def restore_track(self, file_path: str, track: Track):
        self.cur.execute(
            'SELECT default_flag, forced_flag, enabled_flag FROM metadata WHERE file_path = ? AND track_uid = ?',
            (file_path, str(track.uid)),
        )
        result = self.cur.fetchone()
        if result:
            track.default, track.forced, track.enabled = map(bool, result)


def identify_tracks(file_path: str):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(['--identification-format', 'json', '--identify', file_path], temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file.name}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            mkvmerge_logger.error(e.stdout.rstrip())
            raise
        return json.loads(result.stdout)


def modify_tracks(mkv_args: list[str]):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvpropedit', f'@{temp_file.name}'], capture_output=True, check=True, text=True
            )
            mkvpropedit_logger.debug(result.stdout)
        except subprocess.CalledProcessError as e:
            mkvpropedit_logger.error(e.stdout.rstrip())
            raise


def multiplex_tracks(mkv_args: list[str]):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file.name}'], capture_output=True, check=True, text=True
            )
            mkvmerge_logger.debug(result.stdout)
        except subprocess.CalledProcessError as e:
            mkvmerge_logger.error(e.stdout.rstrip())
            raise


def extract_tracks(
    file_path: str,
    config: Config | None = None,
    database: Database | None = None,
    restore: bool = False,
) -> tuple[list[Track], list[Track], list[Track]]:
    video_tracks, audio_tracks, subtitle_tracks = [], [], []

    for metadata in identify_tracks(file_path)['tracks']:
        properties = metadata['properties']

        track = Track(
            track_id=metadata['id'],
            track_type=metadata['type'],
            track_name=properties.get('track_name'),
            uid=properties['uid'],
            score=0,
            language=properties['language'],
            codec=properties['codec_id'],
            channels=properties.get('audio_channels', 0),
            default=properties['default_track'],
            enabled=properties['enabled_track'],
            forced=properties['forced_track'],
        )

        if config and restore:
            if database is None:
                mkvpriority_logger.error('cannot restore without archive (database)')
                return [], [], []
            mkvpriority_logger.debug(str(track))
            if track.track_type == 'audio':
                database.restore_track(file_path, track)
                audio_tracks.append(track)
            elif track.track_type == 'subtitles':
                database.restore_track(file_path, track)
                subtitle_tracks.append(track)
            continue

        if track.track_type == 'video':
            video_tracks.append(track)

        elif track.track_type == 'audio':
            if config:
                track.score += config.audio_languages.get(track.language, 0)
                track.score += config.audio_codecs.get(track.codec, 0)
                track.score += config.audio_channels.get(track.channels, 0)
                if track.track_name:
                    for key, value in config.track_filters.items():
                        if key in track.track_name.lower():
                            track.score += value
            audio_tracks.append(track)
            mkvpriority_logger.debug(str(track))

        elif track.track_type == 'subtitles':
            if config:
                track.score += config.subtitle_languages.get(track.language, 0)
                track.score += config.subtitle_codecs.get(track.codec, 0)
                if track.track_name:
                    for key, value in config.track_filters.items():
                        if key in track.track_name.lower():
                            track.score += value
            subtitle_tracks.append(track)
            mkvpriority_logger.debug(str(track))

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
            mkvpriority_logger.error('cannot restore without archive (database)')
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
        mkvpropedit_logger.info(' '.join(mkv_args))
        if not dry_run:
            modify_tracks(mkv_args)
            database.delete(file_path)
            mkvpriority_logger.info(f'file restored; removed from archive: \'{file_path}\'')
        return

    archive_tracks: list[Track] = []
    mkv_args = [file_path]

    def process_tracks(tracks: list[Track], track_modes: list[str]):
        nonlocal mkv_args
        default_mode, forced_mode = 'default' in track_modes, 'forced' in track_modes
        disabled_mode, enabled_mode = 'disabled' in track_modes, 'enabled' in track_modes
        mkv_flags: dict[int, list[str]] = {track.uid: [] for track in tracks}

        if tracks[0].score == 0:
            return
        elif tracks[0].score > 0:
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

    process_tracks(audio_tracks, config.audio_mode)
    process_tracks(subtitle_tracks, config.subtitle_mode)

    if len(mkv_args) > 1:
        mkvpropedit_logger.info(' '.join(mkv_args))
        if not dry_run:
            modify_tracks(mkv_args)
            if database is not None:
                database.insert(file_path, archive_tracks)


def process_file(
    file_path: str,
    config: Config,
    database: Database | None = None,
    restore: bool = False,
    dry_run: bool = False,
):
    _, audio_tracks, subtitle_tracks = extract_tracks(file_path, config, database, restore)
    for tracks in (audio_tracks, subtitle_tracks):
        tracks.sort(reverse=True, key=lambda track: track.score)

    mkvpropedit(file_path, audio_tracks, subtitle_tracks, config, database, restore, dry_run)


def load_config_and_database(
    toml_path: str | None = None, db_path: str | None = None
) -> tuple[Config, Database | None]:
    if toml_path is None or not os.path.isfile(toml_path):
        mkvpriority_logger.info('config not found; using default')
        toml_path = 'config.toml'
    with open(toml_path, 'rb') as f:
        toml_file = tomllib.load(f)

    config = Config(
        audio_mode=toml_file.get('audio_mode', []),
        subtitle_mode=toml_file.get('subtitle_mode', []),
        audio_languages=toml_file.get('audio_languages', {}),
        audio_codecs=toml_file.get('audio_codecs', {}),
        audio_channels=toml_file.get('audio_channels', {}),
        subtitle_languages=toml_file.get('subtitle_languages', {}),
        subtitle_codecs=toml_file.get('subtitle_codecs', {}),
        track_filters=toml_file.get('track_filters', {}),
    )

    if 'enabled' in config.audio_mode and 'disabled' in config.audio_mode:
        mkvpriority_logger.error('\'enabled\' and \'disabled\' are mutually exclusive')
    if 'enabled' in config.subtitle_mode or 'disabled' in config.subtitle_mode:
        mkvpriority_logger.error('\'enabled\' and \'disabled\' are mutually exclusive')

    if db_path and os.path.isfile(db_path):
        database = Database(db_path)
    else:
        mkvpriority_logger.info('database not found; ignoring archive')
        database = None

    return config, database


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', metavar='FILE_PATH', default='config.toml')
    parser.add_argument('-a', '--archive', metavar='FILE_PATH', default='archive.db')
    parser.add_argument('-v', '--verbose', action='store_true', help='print track information')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress standard output')
    parser.add_argument('-r', '--restore', action='store_true', help='restore original flags')
    parser.add_argument('-n', '--dry-run', action='store_true', help='leave tracks unchanged')
    parser.add_argument('input_dirs', nargs='*', default=[])
    args = parser.parse_args(argv)

    log_level = logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    mkvpriority_logger.setLevel(log_level)
    mkvpropedit_logger.setLevel(log_level)
    mkvmerge_logger.setLevel(log_level)

    with open(args.config, 'rb') as f:
        input_dirs = tomllib.load(f).get('input_dirs', []) + args.input_dirs
    config, database = load_config_and_database(args.config, args.archive)
    archive_mode, restore_mode = database is not None, args.restore

    if len(input_dirs) == 0:
        parser.error('at least one input_dirs must be provided')
    if args.restore and not archive_mode:
        parser.error('cannot use --restore without --archive (database required)')

    for input_dir in input_dirs:
        file_paths: list[str] = []
        if os.path.isdir(input_dir):
            for root, _, files in os.walk(input_dir):
                file_paths.extend(os.path.join(root, f) for f in files)
        elif os.path.isfile(input_dir):
            file_paths.append(input_dir)

        for file_path in file_paths:
            if not os.path.isfile(file_path):
                continue
            if not file_path.lower().endswith('.mkv'):
                continue
            if database is not None:
                is_archived = database.contains(file_path)
                if not restore_mode and is_archived:
                    mkvpriority_logger.info(f'already archived; skipping file: \'{file_path}\'')
                    continue
                if restore_mode and not is_archived:
                    mkvpriority_logger.info(f'file not archived; cannot restore: \'{file_path}\'')
                    continue

            process_file(file_path, config, database, args.restore, args.dry_run)


if __name__ == '__main__':
    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    main()
