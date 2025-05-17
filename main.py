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
            CREATE TABLE IF NOT EXISTS records (
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

    def insert(self, file_path: str, records: list[Track]):
        self.cur.execute('REPLACE INTO archive (file_path) VALUES (?)', (file_path,))
        for record in records:
            self.cur.execute(
                '''
                REPLACE INTO records (
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
                    str(record.uid),
                    int(record.default),
                    int(record.forced),
                    int(record.enabled),
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
            'SELECT default_flag, forced_flag, enabled_flag FROM records WHERE file_path = ? AND track_uid = ?',
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
) -> tuple[list[Track], list[Track]]:
    audio_tracks, subtitle_tracks = [], []

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

        if track.track_type == 'audio':
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

    return audio_tracks, subtitle_tracks


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
        return

    archive_tracks: list[Track] = []
    mkv_args = [file_path]

    def set_default(tracks: list[Track], track_mode: list[str]):
        nonlocal mkv_args
        if 'default' in track_mode and len(tracks) > 1:
            if not tracks[0].default:
                mkv_args += [
                    '--edit',
                    f'track:={tracks[0].uid}',
                    '--set',
                    'flag-default=1',
                ]
                archive_tracks.append(tracks[0])
            for track in tracks[1:]:
                if track.default:
                    mkv_args += [
                        '--edit',
                        f'track:={track.uid}',
                        '--set',
                        'flag-default=0',
                    ]
                    archive_tracks.append(track)

    def set_forced(tracks: list[Track], track_mode: list[str]):
        nonlocal mkv_args
        if 'forced' in track_mode and len(tracks) > 1:
            if not tracks[0].forced:
                mkv_args += [
                    '--edit',
                    f'track:={tracks[0].uid}',
                    '--set',
                    'flag-forced=1',
                ]
                archive_tracks.append(tracks[0])
            for track in tracks[1:]:
                if track.forced:
                    mkv_args += [
                        '--edit',
                        f'track:={track.uid}',
                        '--set',
                        'flag-forced=0',
                    ]
                    archive_tracks.append(track)

    def set_enabled(tracks: list[Track], track_mode: list[str], track_langs: dict[str, int]):
        nonlocal mkv_args
        if 'disable' in track_mode and len(tracks) > 1:
            if not tracks[0].enabled:
                mkv_args += [
                    '--edit',
                    f'track:={tracks[0].uid}',
                    '--set',
                    'flag-enabled=1',
                ]
                archive_tracks.append(tracks[0])
            for track in tracks[1:]:
                if track.enabled and track.language not in track_langs:
                    mkv_args += [
                        '--edit',
                        f'track:={track.uid}',
                        '--set',
                        'flag-enabled=0',
                    ]
                    archive_tracks.append(track)
        if 'enable' in track_mode:
            for track in tracks:
                if not track.enabled:
                    mkv_args += [
                        '--edit',
                        f'track:={track.uid}',
                        '--set',
                        'flag-enabled=1',
                    ]
                    archive_tracks.append(track)

    set_default(audio_tracks, config.audio_mode)
    set_forced(audio_tracks, config.audio_mode)
    set_enabled(audio_tracks, config.audio_mode, config.audio_languages)
    set_default(subtitle_tracks, config.subtitle_mode)
    set_forced(subtitle_tracks, config.subtitle_mode)

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
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    audio_first: bool,
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

    if audio_first:
        process_tracks(audio_tracks, track_order, audio_strip, config.audio_languages)
        process_tracks(subtitle_tracks, track_order, subtitle_strip, config.subtitle_languages)
    else:
        process_tracks(subtitle_tracks, track_order, subtitle_strip, config.subtitle_languages)
        process_tracks(audio_tracks, track_order, audio_strip, config.audio_languages)

    mkv_args = ['-o', f'{file_path.split('.mkv')[0]}_remux.mkv']
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
    audio_tracks, subtitle_tracks = extract_tracks(file_path, config, database, restore_mode)
    audio_first = audio_tracks[0].track_id < subtitle_tracks[0].track_id
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
            audio_tracks,
            subtitle_tracks,
            audio_first,
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
        logger.info(f'\'{args.config}\' not found; using default')
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
        parser.error('at least one directory must be provided')
    if args.restore and (args.reorder or args.strip):
        parser.error('restore mode is incompatible with reorder/strip')
    if 'enable' in config.audio_mode and 'disable' in config.audio_mode:
        parser.error('audio_mode cannot contain both enable and disable')
    if 'enable' in config.subtitle_mode or 'disable' in config.subtitle_mode:
        parser.error('enable and disable are unsupported for subtitle_mode')

    archive_mode = os.path.isfile(args.archive)
    restore_mode = archive_mode and args.restore
    reorder_mode, strip_mode = args.reorder, args.strip
    if archive_mode:
        database = Database(args.archive)
    else:
        logger.info(f'\'{args.archive}\' not found; skipping')
        database = None

    for input_dir in input_dirs:
        if not os.path.isdir(input_dir):
            continue

        for root_path, _, filenames in os.walk(input_dir):
            for filename in filenames:
                if not filename.lower().endswith('.mkv'):
                    continue
                file_path = os.path.join(root_path, filename)
                if not os.path.isfile(file_path):
                    continue
                if archive_mode and not restore_mode and database.contains(file_path):
                    logger.info(f'\'{file_path}\' archived; skipping')
                    continue

                process_file(
                    file_path,
                    config,
                    database,
                    archive_mode,
                    restore_mode,
                    reorder_mode,
                    strip_mode,
                    args.dry_run,
                )


if __name__ == '__main__':
    main()
