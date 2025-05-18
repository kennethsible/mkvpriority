import argparse
import json
import logging
import os
import pprint
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from tempfile import NamedTemporaryFile

import tomllib

logger = logging.getLogger(__name__)


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


def identify(file_path: str):
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
            logger.error(e.stdout.rstrip())
            raise
        return json.loads(result.stdout)


def modify(mkv_args: list[str]):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvpropedit', f'@{temp_file.name}'], capture_output=True, check=True, text=True
            )
            logger.debug(result.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(e.stdout.rstrip())
            raise


def multiplex(mkv_args: list[str]):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file.name}'], capture_output=True, check=True, text=True
            )
            logger.debug(result.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(e.stdout.rstrip())
            raise


def extract_tracks(
    file_path: str, config: Config, database: Database, restore_mode: bool
) -> tuple[list[Track], list[Track], list[Track]]:
    video_tracks, audio_tracks, subtitle_tracks = [], [], []

    for metadata in identify(file_path)['tracks']:
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

        if restore_mode:
            if logger.level == logging.DEBUG:
                pprint.pp(track)
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
            track.score += config.audio_languages.get(track.language, 0)
            track.score += config.audio_codecs.get(track.codec, 0)
            track.score += config.audio_channels.get(track.channels, 0)
            if track.track_name:
                for key, value in config.track_filters.items():
                    if key in track.track_name.lower():
                        track.score += value
            audio_tracks.append(track)
            if logger.level == logging.DEBUG:
                pprint.pp(track)

        elif track.track_type == 'subtitles':
            track.score += config.subtitle_languages.get(track.language, 0)
            track.score += config.subtitle_codecs.get(track.codec, 0)
            if track.track_name:
                for key, value in config.track_filters.items():
                    if key in track.track_name.lower():
                        track.score += value
            subtitle_tracks.append(track)
            if logger.level == logging.DEBUG:
                pprint.pp(track)

    return video_tracks, audio_tracks, subtitle_tracks


def mkvpropedit(
    file_path: str,
    config: Config,
    database: Database,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    archive_mode: bool,
    restore_mode: bool,
    dry_run: bool,
):
    if restore_mode:
        mkv_args = [file_path]
        for track in audio_tracks + subtitle_tracks:
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
        logger.info('[mkvpropedit] ' + ' '.join(mkv_args))
        if not dry_run:
            modify(mkv_args)
            database.delete(file_path)
            logger.info(f'[mkvpriority] \'{file_path}\' restored; removed from archive')
        return

    archive_tracks: list[Track] = []
    mkv_args = [file_path]

    def process_tracks(tracks: list[Track], track_modes: list[str], track_langs: dict[str, int]):
        nonlocal mkv_args
        default_mode, forced_mode = 'default' in track_modes, 'forced' in track_modes
        disabled_mode, enabled_mode = 'disabled' in track_modes, 'enabled' in track_modes
        mkv_flags: dict[int, list[str]] = {track.uid: [] for track in tracks}

        if default_mode and not tracks[0].default:
            mkv_flags[tracks[0].uid].append('flag-default=1')
        if forced_mode and not tracks[0].forced:
            mkv_flags[tracks[0].uid].append('flag-forced=1')
        if disabled_mode and not tracks[0].enabled:
            mkv_flags[tracks[0].uid].append('flag-enabled=1')
        if enabled_mode and not tracks[0].enabled:
            mkv_flags[tracks[0].uid].append('flag-enabled=1')

        for track in tracks[1:]:
            if default_mode and track.default:
                mkv_flags[track.uid].append('flag-default=0')
            if forced_mode and track.forced:
                mkv_flags[track.uid].append('flag-forced=0')
            if disabled_mode and track.enabled and track.language not in track_langs:
                mkv_flags[track.uid].append('flag-enabled=0')
            if enabled_mode and not track.enabled:
                mkv_flags[track.uid].append('flag-enabled=1')

        for track in tracks:
            if mkv_flags[track.uid]:
                mkv_args += ['--edit', f'track:={track.uid}']
                for flag in mkv_flags[track.uid]:
                    mkv_args += ['--set', flag]
                archive_tracks.append(track)

    process_tracks(audio_tracks, config.audio_mode, config.audio_languages)
    process_tracks(subtitle_tracks, config.subtitle_mode, config.subtitle_languages)

    if len(mkv_args) > 1:
        logger.info('[mkvpropedit] ' + ' '.join(mkv_args))
        if not dry_run:
            modify(mkv_args)
            if archive_mode:
                database.insert(file_path, archive_tracks)


def mkvmerge(
    file_path: str,
    config: Config,
    database: Database,
    video_tracks: list[Track],
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    archive_mode: bool,
    reorder_mode: bool,
    strip_mode: bool,
    dry_run: bool,
):
    track_order: list[str] = []
    audio_strip: list[str] = []
    subtitle_strip: list[str] = []

    def process_tracks(
        tracks: list[Track],
        track_order: list[str],
        track_strip: list[str],
        track_langs: dict[str, int],
    ):
        for track in tracks:
            if reorder_mode and strip_mode:
                if track.language in track_langs:
                    track_order.append(f'0:{track.track_id}')
                else:
                    track_strip.append(f'!{track.track_id}')
            elif reorder_mode:
                track_order.append(f'0:{track.track_id}')
            elif strip_mode:
                if track.language not in track_langs:
                    track_strip.append(f'!{track.track_id}')

    for track in video_tracks:
        track_order.append(f'0:{track.track_id}')
    process_tracks(audio_tracks, track_order, audio_strip, config.audio_languages)
    process_tracks(subtitle_tracks, track_order, subtitle_strip, config.subtitle_languages)

    mkv_args = ['-o', f'{os.path.splitext(file_path)[0]}_remux.mkv']
    if audio_strip:
        mkv_args += ['--audio-tracks', ','.join(audio_strip)]
    if subtitle_strip:
        mkv_args += ['--subtitle-tracks', ','.join(subtitle_strip)]
    mkv_args += [file_path]
    if track_order and any(
        int(id_a.split(':')[1]) > int(id_b.split(':')[1])
        for id_a, id_b in zip(track_order, track_order[1:])
    ):
        mkv_args += ['--track-order', ','.join(track_order)]

    if len(mkv_args) > 3:
        logger.info('[mkvmerge] ' + ' '.join(mkv_args))
        if not dry_run:
            multiplex(mkv_args)
            if archive_mode:
                database.insert(mkv_args[1], [])


def process_file(
    file_path: str,
    config: Config,
    database: Database,
    archive_mode: bool,
    restore_mode: bool,
    reorder_mode: bool,
    strip_mode: bool,
    dry_run: bool,
):
    video_tracks, audio_tracks, subtitle_tracks = extract_tracks(
        file_path, config, database, restore_mode
    )
    for tracks in (audio_tracks, subtitle_tracks):
        tracks.sort(reverse=True, key=lambda track: track.score)

    mkvpropedit(
        file_path,
        config,
        database,
        audio_tracks,
        subtitle_tracks,
        archive_mode,
        restore_mode,
        dry_run,
    )
    if reorder_mode or strip_mode:
        mkvmerge(
            file_path,
            config,
            database,
            video_tracks,
            audio_tracks,
            subtitle_tracks,
            archive_mode,
            reorder_mode,
            strip_mode,
            dry_run,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', metavar='FILE_PATH', default='config.toml')
    parser.add_argument('-a', '--archive', metavar='FILE_PATH', default='archive.db')
    parser.add_argument('-n', '--dry-run', action='store_true', help='leave tracks unchanged')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress standard output')
    parser.add_argument('-v', '--verbose', action='store_true', help='print track information')
    parser.add_argument('-r', '--reorder', action='store_true', help='reorder tracks by score')
    parser.add_argument('-s', '--strip', action='store_true', help='remove unwanted tracks')
    parser.add_argument('--restore', action='store_true', help='restore original flags')
    parser.add_argument('input_dirs', nargs='*', default=[])
    args = parser.parse_args()

    logger.setLevel(
        logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    )
    logger.addHandler(logging.StreamHandler(sys.stdout))

    if not os.path.isfile(args.config):
        logger.info(f'[mkvpriority] \'{args.config}\' not found; using default')
        args.config = 'config.toml'
    with open(args.config, 'rb') as f:
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

    input_dirs = toml_file.get('input_dirs', []) + args.input_dirs
    if len(input_dirs) == 0:
        parser.error('at least one input_dirs must be provided')
    if args.restore and (args.reorder or args.strip):
        parser.error('--restore is incompatible with --reorder and --strip')
    if 'enable' in config.audio_mode and 'disable' in config.audio_mode:
        parser.error('--enable and --disable cannot be used simultaneously')
    if 'enable' in config.subtitle_mode or 'disable' in config.subtitle_mode:
        parser.error('--enable and --disable are unsupported for subtitle_mode')

    archive_mode = os.path.isfile(args.archive)
    restore_mode = archive_mode and args.restore
    if archive_mode:
        database = Database(args.archive)
    else:
        logger.info(f'[mkvpriority] \'{args.archive}\' not found; disabling database')
        if args.restore:
            parser.error('cannot use --restore without --archive (database required)')
        database = None

    for input_dir in input_dirs:
        if os.path.isfile(input_dir):
            file_path = input_dir
            filename = os.path.basename(file_path)
            if not filename.lower().endswith('.mkv'):
                continue
            if archive_mode:
                is_archived = database.contains(file_path)
                if not restore_mode and is_archived:
                    logger.info(f'[mkvpriority] \'{file_path}\' archived; skipping file')
                    continue
                if restore_mode and not is_archived:
                    logger.info(f'[mkvpriority] \'{file_path}\' not archived; cannot restore')
                    continue

            process_file(
                file_path,
                config,
                database,
                archive_mode,
                restore_mode,
                args.reorder,
                args.strip,
                args.dry_run,
            )

            continue

        if not os.path.isdir(input_dir):
            continue

        for root_path, _, filenames in os.walk(input_dir):
            for filename in filenames:
                if not filename.lower().endswith('.mkv'):
                    continue
                file_path = os.path.join(root_path, filename)
                if not os.path.isfile(file_path):
                    continue
                if archive_mode:
                    is_archived = database.contains(file_path)
                    if not restore_mode and is_archived:
                        logger.info(f'[mkvpriority] \'{file_path}\' archived; skipping file')
                        continue
                    if restore_mode and not is_archived:
                        logger.info(f'[mkvpriority] \'{file_path}\' not archived; cannot restore')
                        continue

                process_file(
                    file_path,
                    config,
                    database,
                    archive_mode,
                    restore_mode,
                    args.reorder,
                    args.strip,
                    args.dry_run,
                )


if __name__ == '__main__':
    main()
