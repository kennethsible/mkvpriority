import argparse
import json
import os
import pprint
import subprocess
from dataclasses import dataclass
from tempfile import NamedTemporaryFile

import tomllib


@dataclass
class TrackRecord:
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


def mkv_identify(file_path: str):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(['--identification-format', 'json', '--identify', file_path], temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file.name}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print(e.stderr)
        return json.loads(result.stdout)


def mkv_modify(mkv_args: list[str], *, suppress_output: bool = False):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvpropedit', f'@{temp_file.name}'], capture_output=True, check=True
            )
            if not suppress_output:
                print(result.stdout)
        except subprocess.CalledProcessError as e:
            print(e.stderr)


def mkv_multiplex(mkv_args: list[str], *, suppress_output: bool = False):
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump(mkv_args, temp_file)
        temp_file.flush()
        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file.name}'], capture_output=True, check=True
            )
            if not suppress_output:
                print(result.stdout)
        except subprocess.CalledProcessError as e:
            print(e.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', metavar='FILE_PATH', default='config.toml')
    parser.add_argument('-d', '--dry-run', action='store_true', help='leaves tracks unchanged')
    parser.add_argument('-q', '--quiet', action='store_true', help='disables output (stdout)')
    parser.add_argument('-v', '--verbose', action='store_true', help='outputs track information')
    parser.add_argument('-r', '--reorder', action='store_true', help='reorders tracks by score')
    parser.add_argument('-s', '--strip', action='store_true', help='strips unwanted tracks')
    parser.add_argument('input_dirs', nargs='*', default=[])
    args = parser.parse_args()

    with open(args.config, 'rb') as f:
        config = tomllib.load(f)

    input_dirs = config.get('input_dirs', []) + args.input_dirs
    assert len(input_dirs) > 0
    audio_mode = config.get('audio_mode', [])
    assert 'enable' not in audio_mode or 'disable' not in audio_mode
    subtitle_mode = config.get('subtitle_mode', [])
    assert 'enable' not in subtitle_mode and 'disable' not in subtitle_mode

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

                mkv_tracks = mkv_identify(file_path)['tracks']
                audio_tracks, subtitle_tracks = [], []

                for mkv_track in mkv_tracks:
                    mkv_props = mkv_track['properties']
                    record = TrackRecord(
                        track_id=mkv_track['id'],
                        track_type=mkv_track['type'],
                        track_name=mkv_props.get('track_name'),
                        uid=mkv_props['uid'],
                        score=0,
                        language=mkv_props['language'],
                        codec=mkv_props['codec_id'],
                        channels=mkv_props.get('audio_channels', 0),
                        default=mkv_props['default_track'],
                        enabled=mkv_props['enabled_track'],
                        forced=mkv_props['forced_track'],
                    )

                    if record.track_type == 'audio':
                        audio_languages = config.get('audio_languages', {})
                        record.score += audio_languages.get(record.language, 0)

                        audio_codecs = config.get('audio_codecs', {})
                        record.score += audio_codecs.get(record.codec, 0)

                        audio_channels = config.get('audio_channels', {})
                        record.score += audio_channels.get(record.channels, 0)

                        if record.track_name:
                            track_filters = config.get('track_filters', {})
                            for key, value in track_filters.items():
                                if key in record.track_name.lower():
                                    record.score += value

                        audio_tracks.append(record)
                        if args.verbose and not args.quiet:
                            pprint.pp(record)

                    elif record.track_type == 'subtitles':
                        subtitle_languages = config.get('subtitle_languages', {})
                        record.score += subtitle_languages.get(record.language, 0)

                        subtitle_codecs = config.get('subtitle_codecs', {})
                        record.score += subtitle_codecs.get(record.codec, 0)

                        if record.track_name:
                            track_filters = config.get('track_filters', {})
                            for key, value in track_filters.items():
                                if key in record.track_name.lower():
                                    record.score += value

                        subtitle_tracks.append(record)
                        if args.verbose and not args.quiet:
                            pprint.pp(record)

                if args.reorder or args.strip:
                    audio_first = audio_tracks[0].track_id < subtitle_tracks[0].track_id
                for tracks in (audio_tracks, subtitle_tracks):
                    tracks.sort(reverse=True, key=lambda record: record.score)
                mkv_args = [file_path]

                def set_default(records: list[TrackRecord], track_mode: str):
                    nonlocal mkv_args
                    if 'default' in track_mode and len(records) > 1:
                        default_track = next((record for record in records if record.default), None)
                        if not records[0].default:
                            mkv_args += [
                                '--edit',
                                f'track:={records[0].uid}',
                                '--set',
                                'flag-default=1',
                            ]
                            if default_track:
                                mkv_args += [
                                    '--edit',
                                    f'track:={default_track.uid}',
                                    '--set',
                                    'flag-default=0',
                                ]

                def set_forced(records: list[TrackRecord], track_mode: str):
                    nonlocal mkv_args
                    if 'forced' in track_mode and len(records) > 1:
                        forced_track = next((record for record in records if record.forced), None)
                        if not records[0].forced:
                            mkv_args += [
                                '--edit',
                                f'track:={records[0].uid}',
                                '--set',
                                'flag-forced=1',
                            ]
                            if forced_track:
                                mkv_args += [
                                    '--edit',
                                    f'track:={forced_track.uid}',
                                    '--set',
                                    'flag-forced=0',
                                ]

                def set_enabled(records: list[TrackRecord], track_mode: str, languages: list[str]):
                    nonlocal mkv_args
                    if 'disable' in track_mode and len(records) > 1:
                        for record in records[1:]:
                            if record.enabled and record.language not in languages:
                                mkv_args += [
                                    '--edit',
                                    f'track:={record.uid}',
                                    '--set',
                                    'flag-enabled=0',
                                ]
                        if not records[0].enabled:
                            mkv_args += [
                                '--edit',
                                f'track:={records[0].uid}',
                                '--set',
                                'flag-enabled=1',
                            ]

                    if 'enable' in track_mode:
                        for audio_track in records:
                            if not audio_track.enabled:
                                mkv_args += [
                                    '--edit',
                                    f'track:={audio_track.uid}',
                                    '--set',
                                    'flag-enabled=1',
                                ]

                set_default(audio_tracks, audio_mode)
                set_forced(audio_tracks, audio_mode)
                set_enabled(audio_tracks, audio_mode, audio_languages)

                set_default(subtitle_tracks, subtitle_mode)
                set_forced(subtitle_tracks, subtitle_mode)

                if len(mkv_args) > 1:
                    if not args.quiet:
                        print('[mkvpropedit] ' + ' '.join(mkv_args))
                    if not args.dry_run:
                        mkv_modify(mkv_args, suppress_output=args.quiet)

                def process_records(
                    records: list[TrackRecord],
                    track_order: list[str],
                    track_strip: list[str],
                    languages: list[str],
                ):
                    for record in records:
                        if args.reorder and args.strip:
                            if record.language in languages:
                                track_order.append(f'0:{record.track_id}')
                            else:
                                track_strip.append(f'!{record.track_id}')
                        elif args.reorder:
                            track_order.append(f'0:{record.track_id}')
                        elif args.strip:
                            if record.language not in languages:
                                track_strip.append(f'!{record.track_id}')

                if args.reorder or args.strip:
                    track_order, audio_strip, subtitle_strip = [], [], []
                    if audio_first:
                        process_records(audio_tracks, track_order, audio_strip, audio_languages)
                        process_records(
                            subtitle_tracks, track_order, subtitle_strip, subtitle_languages
                        )
                    else:
                        process_records(
                            subtitle_tracks, track_order, subtitle_strip, subtitle_languages
                        )
                        process_records(audio_tracks, track_order, audio_strip, audio_languages)

                    mkv_args = ['-o', f'{file_path.split('.mkv')[0]}_multiplex.mkv']
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
                        if not args.quiet:
                            print('[mkvmerge] ' + ' '.join(mkv_args))
                        if not args.dry_run:
                            mkv_multiplex(mkv_args, suppress_output=args.quiet)


if __name__ == '__main__':
    main()
