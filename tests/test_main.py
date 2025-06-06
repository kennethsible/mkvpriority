import subprocess
import tempfile
from itertools import chain
from pathlib import Path

import mkvpriority


def create_dummy(temp_dir: Path) -> dict[str, Path]:
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
    sub3_path = temp_dir / 'dialogue_eng.srt'
    sub4_path = temp_dir / 'dialogue_ger.srt'

    ass_template = """
[Script Info]
Title: Dummy Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1.5,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    srt_template = """1
00:00:00,000 --> 00:00:01,000
Dummy Subtitle
"""

    sub1_path.write_text(ass_template.strip())
    sub2_path.write_text(ass_template.strip())
    sub3_path.write_text(srt_template.strip())
    sub4_path.write_text(srt_template.strip())

    return {
        'video': video_path,
        'audio1': audio1_path,
        'audio2': audio2_path,
        'audio3': audio3_path,
        'subs1': sub1_path,
        'subs2': sub2_path,
        'subs3': sub3_path,
        'subs4': sub4_path,
    }


def multiplex_dummy(output_path: Path, track_files: dict[str, Path]):
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
            str(track_files['video']),
            # Japanese Audio AAC
            '--language',
            '0:jpn',
            '--track-name',
            '0:Stereo AAC (Japanese)',
            '--default-track',
            '0:no',
            str(track_files['audio2']),
            # English Audio AAC
            '--language',
            '0:eng',
            '--track-name',
            '0:Stereo AAC (English)',
            '--default-track',
            '0:yes',
            str(track_files['audio3']),
            # Japanese Audio FLAC
            '--language',
            '0:jpn',
            '--track-name',
            '0:5.1 FLAC (Japanese)',
            '--default-track',
            '0:no',
            str(track_files['audio1']),
            # English Subtitles ASS
            '--language',
            '0:eng',
            '--track-name',
            '0:Full Subtitles [FanSub]',
            '--default-track',
            '0:no',
            str(track_files['subs1']),
            # English Subtitles ASS
            '--language',
            '0:eng',
            '--track-name',
            '0:Signs & Songs [FanSub]',
            '--default-track',
            '0:no',
            '--forced-track',
            '0:yes',
            str(track_files['subs2']),
            # German Subtitles SRT
            '--language',
            '0:ger',
            '--track-name',
            '0:Dialogue [Blu-ray]',
            '--default-track',
            '0:no',
            str(track_files['subs4']),
            # English Subtitles SRT
            '--language',
            '0:eng',
            '--track-name',
            '0:Dialogue [Blu-ray]',
            '--default-track',
            '0:no',
            str(track_files['subs3']),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def test_mkvpropedit():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        track_count = total_count = 0
        tracks = mkvpriority.extract_tracks(str(file_path))
        for track in chain.from_iterable(tracks):
            match track.name:
                case 'Stereo AAC (English)':
                    track_count += 1
                    assert track.default
                case 'Signs & Songs [FanSub]':
                    track_count += 1
                    assert track.forced
                case _:
                    assert not track.default
                    assert not track.forced
            total_count += 1
        assert track_count == 2
        assert total_count == 8

        config = mkvpriority.Config.from_file('config.toml')
        mkvpriority.process_file(str(file_path), config)

        track_count = total_count = 0
        tracks = mkvpriority.extract_tracks(str(file_path))
        for track in chain.from_iterable(tracks):
            match track.name:
                case '5.1 FLAC (Japanese)':
                    track_count += 1
                    assert track.default
                case 'Full Subtitles [FanSub]':
                    track_count += 1
                    assert track.default
                    assert track.forced
                case _:
                    assert not track.default
                    assert not track.forced
            total_count += 1
        assert track_count == 2
        assert total_count == 8


def test_mkvpriority():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        config = mkvpriority.Config.from_file('config.toml')
        tracks = mkvpriority.process_file(str(file_path), config)

        for track in chain.from_iterable(tracks):
            match track.name:
                case '5.1 FLAC (Japanese)':
                    assert track.score == 118
                case 'Stereo AAC (Japanese)':
                    assert track.score == 107
                case 'Stereo AAC (English)':
                    assert track.score == 77
                case 'Full Subtitles [FanSub]':
                    assert track.score == 112
                case 'Signs & Songs [FanSub]':
                    assert track.score == 72
                case 'Dialogue [Blu-ray]':
                    if track.language == 'eng':
                        assert track.score == 108
                    else:
                        assert track.score == 8


def test_entrypoint():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        tracks = mkvpriority.extract_tracks(str(file_path))
        for track in chain.from_iterable(tracks):
            match track.name:
                case 'Stereo AAC (English)':
                    assert track.default
                case 'Signs & Songs [FanSub]':
                    assert track.forced
                case _:
                    assert not track.default
                    assert not track.forced

        mkvpriority.main.main([str(file_path)])

        tracks = mkvpriority.extract_tracks(str(file_path))
        for track in chain.from_iterable(tracks):
            match track.name:
                case '5.1 FLAC (Japanese)':
                    assert track.default
                case 'Full Subtitles [FanSub]':
                    assert track.default
                    assert track.forced
                case _:
                    assert not track.default
                    assert not track.forced


def test_restore():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        with tempfile.NamedTemporaryFile() as archive_file:
            config = mkvpriority.Config.from_file('config.toml')
            database = mkvpriority.Database(archive_file.name)
            mkvpriority.process_file(str(file_path), config, database)

            tracks = mkvpriority.extract_tracks(str(file_path))
            for track in chain.from_iterable(tracks):
                match track.name:
                    case '5.1 FLAC (Japanese)':
                        assert track.default
                    case 'Full Subtitles [FanSub]':
                        assert track.default and track.forced
                    case _:
                        assert not track.default and not track.forced

            mkvpriority.process_file(str(file_path), config, database, restore=True)

            tracks = mkvpriority.extract_tracks(str(file_path))
            for track in chain.from_iterable(tracks):
                match track.name:
                    case 'Stereo AAC (English)':
                        assert track.default
                    case 'Signs & Songs [FanSub]':
                        assert track.forced
                    case _:
                        assert not track.default and not track.forced


def test_prune():
    with tempfile.NamedTemporaryFile() as archive_file:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            file_path = temp_path / 'dummy.mkv'
            track_files = create_dummy(temp_path)
            multiplex_dummy(file_path, track_files)

            config = mkvpriority.Config.from_file('config.toml')
            database = mkvpriority.Database(archive_file.name)
            mkvpriority.process_file(str(file_path), config, database)

            assert database.contains(str(file_path))
            database.prune()
            assert database.contains(str(file_path))

        database.prune()
        assert not database.contains(str(file_path))
