import subprocess
import sys
import tempfile
from pathlib import Path

import main


def create_tracks(temp_dir: Path) -> dict[str, Path]:
    # 1. Black Screen Video MP4
    video_path = temp_dir / 'video.mp4'
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-f',
            'lavfi',
            '-i',
            'color=size=128x128:duration=1:rate=1:color=black',
            '-c:v',
            'libx264',
            '-t',
            '1',
            str(video_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # 2. Japanese Audio FLAC
    audio1_path = temp_dir / 'japanese_surround.flac'
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-f',
            'lavfi',
            '-i',
            'anullsrc=r=48000:cl=5.1',
            '-t',
            '1',
            '-c:a',
            'flac',
            str(audio1_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # 3. Japanese Audio AAC
    audio2_path = temp_dir / 'japanese_stereo.aac'
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-f',
            'lavfi',
            '-i',
            'anullsrc=r=48000:cl=stereo',
            '-t',
            '1',
            '-c:a',
            'aac',
            str(audio2_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # 4. English Audio AAC
    audio3_path = temp_dir / 'english_stereo.aac'
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-f',
            'lavfi',
            '-i',
            'anullsrc=r=48000:cl=stereo',
            '-t',
            '1',
            '-c:a',
            'aac',
            str(audio3_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # 5. English Subtitles ASS/SRT
    sub1_path = temp_dir / 'full_subs.ass'
    sub2_path = temp_dir / 'signs_songs.ass'
    sub3_path = temp_dir / 'dialogue.srt'

    ass_template = '''
[Script Info]
Title: Dummy Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1.5,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''

    srt_template = '''1
00:00:00,000 --> 00:00:01,000
Dummy Subtitle
'''

    sub1_path.write_text(ass_template.strip())
    sub2_path.write_text(ass_template.strip())
    sub3_path.write_text(srt_template.strip())

    return {
        'video': video_path,
        'audio1': audio1_path,
        'audio2': audio2_path,
        'audio3': audio3_path,
        'subs1': sub1_path,
        'subs2': sub2_path,
        'subs3': sub3_path,
    }


def multiplex_tracks(output_path: Path, tracks: dict[str, Path]):
    subprocess.run(
        [
            'mkvmerge',
            '-o',
            str(output_path),
            # Black Screen Video MP4
            '--track-name',
            '0:Dummy Video',
            '--default-track',
            '0:no',
            str(tracks['video']),
            # Japanese Audio FLAC
            '--language',
            '0:jpn',
            '--track-name',
            '0:5.1 FLAC (Japanese)',
            '--default-track',
            '0:no',
            str(tracks['audio1']),
            # Japanese Audio AAC
            '--language',
            '0:jpn',
            '--track-name',
            '0:Stereo AAC (Japanese)',
            '--default-track',
            '0:no',
            str(tracks['audio2']),
            # English Audio AAC
            '--language',
            '0:eng',
            '--track-name',
            '0:Stereo AAC (English)',
            '--default-track',
            '0:yes',
            str(tracks['audio3']),
            # English Subtitles ASS
            '--language',
            '0:eng',
            '--track-name',
            '0:Full Subtitles [FanSub]',
            '--default-track',
            '0:no',
            str(tracks['subs1']),
            # English Subtitles ASS
            '--language',
            '0:eng',
            '--track-name',
            '0:Signs & Songs [FanSub]',
            '--default-track',
            '0:no',
            '--forced-track',
            '0:yes',
            str(tracks['subs2']),
            # English Subtitles SRT
            '--language',
            '0:eng',
            '--track-name',
            '0:Dialogue [Blu-ray]',
            '--default-track',
            '0:no',
            str(tracks['subs3']),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def extract_tracks(file_path: Path) -> list[main.Track]:
    tracks = []
    for metadata in main.identify(str(file_path))['tracks']:
        properties = metadata['properties']
        tracks.append(
            main.Track(
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
        )
    return tracks


def test_mkvpropedit():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        tracks = create_tracks(temp_path)
        multiplex_tracks(file_path, tracks)

        for track in extract_tracks(file_path):
            match track.track_name:
                case 'Stereo AAC (English)':
                    assert track.default
                case 'Signs & Songs [FanSub]':
                    assert track.forced
                case _:
                    assert not track.default and not track.forced

        sys.argv = ['main.py', '-q', temp_dir]
        main.main()

        for track in extract_tracks(file_path):
            match track.track_name:
                case '5.1 FLAC (Japanese)':
                    assert track.default
                case 'Full Subtitles [FanSub]':
                    assert track.default and track.forced
                case _:
                    assert not track.default and not track.forced


def test_mkvmerge():
    pass


def test_restore():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        tracks = create_tracks(temp_path)
        multiplex_tracks(file_path, tracks)

        with tempfile.NamedTemporaryFile() as archive_file:
            sys.argv = ['main.py', '-q', '-a', archive_file.name, temp_dir]
            main.main()

            for track in extract_tracks(file_path):
                match track.track_name:
                    case '5.1 FLAC (Japanese)':
                        assert track.default
                    case 'Full Subtitles [FanSub]':
                        assert track.default and track.forced
                    case _:
                        assert not track.default and not track.forced

            sys.argv = ['main.py', '-q', '-a', archive_file.name, temp_dir, '--restore']
            main.main()

            for track in extract_tracks(file_path):
                match track.track_name:
                    case 'Stereo AAC (English)':
                        assert track.default
                    case 'Signs & Songs [FanSub]':
                        assert track.forced
                    case _:
                        assert not track.default and not track.forced
