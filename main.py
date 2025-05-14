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
        result = subprocess.run(
            ['mkvmerge', f'@{temp_file.name}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='utf-8',
            check=True,
        )
        return json.loads(result.stdout)


def mkv_modify(file_path: str, mkv_args: list[str]):
    print(' '.join(mkv_args))  # TODO remove; used for debugging
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump([file_path] + mkv_args, temp_file)
        temp_file.flush()
        subprocess.run(['mkvpropedit', f'@{temp_file.name}'], check=True)


def mkv_multiplex(file_path: str, mkv_args: list[str]):
    print(' '.join(mkv_args))  # TODO remove; used for debugging
    with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
        json.dump([file_path] + mkv_args, temp_file)
        temp_file.flush()
        subprocess.run(['mkvmerge', f'@{temp_file.name}'], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', metavar='FILE_PATH', default='config.toml')
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
                    track_record = TrackRecord(
                        track_id=mkv_track['id'],
                        track_type=mkv_track['type'],
                        track_name=mkv_props.get('track_name'),
                        uid=mkv_props['uid'],
                        score=0,
                        language=mkv_props['language'],
                        codec=mkv_track['codec'],
                        channels=mkv_props.get('audio_channels', 0),
                        default=mkv_props['default_track'],
                        enabled=mkv_props['enabled_track'],
                        forced=mkv_props['forced_track'],
                    )

                    if track_record.track_type == 'audio':
                        audio_languages = config.get('audio_languages', {})
                        track_record.score += audio_languages.get(track_record.language, 0)

                        audio_codecs = config.get('audio_codecs', {})
                        track_record.score += audio_codecs.get(track_record.codec, 0)

                        audio_channels = config.get('audio_channels', {})
                        track_record.score += audio_channels.get(track_record.channels, 0)

                        if track_record.track_name:
                            track_filters = config.get('track_filters', {})
                            for key, value in track_filters.items():
                                if key in track_record.track_name.lower():
                                    track_record.score += value

                        audio_tracks.append(track_record)
                        pprint.pp(track_record)  # TODO remove; used for debugging

                    elif track_record.track_type == 'subtitles':
                        subtitle_languages = config.get('subtitle_languages', {})
                        track_record.score += subtitle_languages.get(track_record.language, 0)

                        subtitle_codecs = config.get('subtitle_codecs', {})
                        track_record.score += subtitle_codecs.get(track_record.codec, 0)

                        if track_record.track_name:
                            track_filters = config.get('track_filters', {})
                            for key, value in track_filters.items():
                                if key in track_record.track_name.lower():
                                    track_record.score += value

                        subtitle_tracks.append(track_record)
                        pprint.pp(track_record)  # TODO remove; used for debugging

                # audio_first = audio_tracks[0].track_id < subtitle_tracks[0].track_id
                for tracks in (audio_tracks, subtitle_tracks):
                    tracks.sort(reverse=True, key=lambda record: record.score)
                mkv_args = []

                if 'default' in audio_mode and len(audio_tracks) > 1:
                    default_audio = next(
                        (record for record in audio_tracks if record.default), None
                    )
                    if not audio_tracks[0].default:
                        mkv_args += [
                            '--edit',
                            f'track:={audio_tracks[0].uid}',
                            '--set',
                            'flag-default=1',
                        ]
                        if default_audio:
                            mkv_args += [
                                '--edit',
                                f'track:={default_audio.uid}',
                                '--set',
                                'flag-default=0',
                            ]

                if 'forced' in audio_mode and len(audio_tracks) > 1:
                    forced_audio = next((record for record in audio_tracks if record.forced), None)
                    if not audio_tracks[0].forced:
                        mkv_args += [
                            '--edit',
                            f'track:={audio_tracks[0].uid}',
                            '--set',
                            'flag-forced=1',
                        ]
                        if forced_audio:
                            mkv_args += [
                                '--edit',
                                f'track:={forced_audio.uid}',
                                '--set',
                                'flag-forced=0',
                            ]

                disable = 'disable' in audio_mode
                disable_strict = 'disable_strict' in audio_mode
                if (disable or disable_strict) and len(audio_tracks) > 1:
                    for audio_track in audio_tracks[1:]:
                        if audio_track.enabled:
                            if disable and audio_track.language in audio_languages:
                                continue
                            mkv_args += [
                                '--edit',
                                f'track:={audio_track.uid}',
                                '--set',
                                'flag-enabled=0',
                            ]
                    if not audio_tracks[0].enabled:
                        mkv_args += [
                            '--edit',
                            f'track:={audio_tracks[0].uid}',
                            '--set',
                            'flag-enabled=1',
                        ]

                if 'enable' in audio_mode:
                    for audio_track in audio_tracks:
                        if not audio_track.enabled:
                            mkv_args += [
                                '--edit',
                                f'track:={audio_track.uid}',
                                '--set',
                                'flag-enabled=1',
                            ]

                if 'default' in subtitle_mode and len(subtitle_tracks) > 1:
                    default_subtitle = next(
                        (record for record in subtitle_tracks if record.default), None
                    )
                    if not subtitle_tracks[0].default:
                        mkv_args += [
                            '--edit',
                            f'track:={subtitle_tracks[0].uid}',
                            '--set',
                            'flag-default=1',
                        ]
                        if default_subtitle:
                            mkv_args += [
                                '--edit',
                                f'track:={default_subtitle.uid}',
                                '--set',
                                'flag-default=0',
                            ]

                if 'forced' in subtitle_mode and len(subtitle_tracks) > 1:
                    forced_subtitle = next(
                        (record for record in subtitle_tracks if record.forced), None
                    )
                    if not subtitle_tracks[0].forced:
                        mkv_args += [
                            '--edit',
                            f'track:={subtitle_tracks[0].uid}',
                            '--set',
                            'flag-forced=1',
                        ]
                        if forced_subtitle:
                            mkv_args += [
                                '--edit',
                                f'track:={forced_subtitle.uid}',
                                '--set',
                                'flag-forced=0',
                            ]

                if mkv_args:
                    mkv_modify(file_path, mkv_args)

                # if audio_first:
                #     track_order = []
                #     for track in audio_tracks + subtitle_tracks:
                #         track_order.append(f'0:{track.track_id}')
                # else:
                #     track_order = []
                #     for track in subtitle_tracks + audio_tracks:
                #         track_order.append(f'0:{track.track_id}')
                #     mkv_args.append(','.join(track_order))

                # if track_order:
                #     mkv_args = [
                #         '-o',
                #         f'{file_path.split('.mkv')[0]}_multiplex.mkv',
                #         '--track-order',
                #         ','.join(track_order),
                #     ]
                #     mkv_multiplex(file_path, mkv_args)


if __name__ == '__main__':
    main()
