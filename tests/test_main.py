import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

import mkvpriority
from mkvpriority import entrypoint
from mkvpriority.extensions.multiplexer import Multiplexer
from mkvpriority.extensions.subtitle_converter import SubtitleConverter
from mkvpriority.extensions.subtitle_extractor import SubtitleExtractor
from mkvpriority.extensions.subtitle_restyler import SubtitleRestyler

AIOTestClient = TestClient[web.Request, web.Application]
AIOClientFixture = Callable[[web.Application], Awaitable[AIOTestClient]]


def create_dummy(temp_dir: Path) -> dict[str, Path]:
    # 1. Black Screen Video MP4
    video_path = temp_dir / 'blank_video.mp4'
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

    # 2. Japanese Audio 5.1 FLAC
    audio1_path = temp_dir / 'jpn.5.1.flac'
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

    # 3. Japanese Audio 2.0 AAC
    audio2_path = temp_dir / 'jpn.2.0.aac'
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

    # 4. English Audio 2.0 AAC
    audio3_path = temp_dir / 'eng.2.0.aac'
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
    sub3_path = temp_dir / 'dialogue.eng.srt'
    sub4_path = temp_dir / 'dialogue.ger.srt'

    ass_template = """
[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Outline, Shadow, Alignment, Encoding
Style: Default,Arial,20,&H00FFFFFF,2,1,2,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Dummy Subtitle
"""

    srt_template = """1
00:00:00,000 --> 00:00:01,000
Dummy Subtitle
"""

    sub1_path.write_text(
        (
            ass_template
            + ''.join(
                f'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Dummy Subtitle {i}\n'
                for i in range(100)
            )
        ).strip()
    )
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


def multiplex_dummy(output_path: Path, track_files: dict[str, Path]) -> None:
    result = subprocess.run(
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
            # Japanese Audio 2.0 AAC
            '--language',
            '0:jpn',
            '--track-name',
            '0:Stereo AAC (Japanese)',
            '--default-track',
            '0:no',
            str(track_files['audio2']),
            # English Audio 2.0 AAC
            '--language',
            '0:eng',
            '--track-name',
            '0:Stereo AAC (English)',
            '--default-track',
            '0:yes',
            str(track_files['audio3']),
            # Japanese Audio 5.1 FLAC
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
        check=False,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )

    result = subprocess.run(
        ['mkvpropedit', str(output_path), '--add-track-statistics-tags'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )


def test_process_file() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            'Stereo AAC (English)',
            'Signs & Songs [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == set()

        config = mkvpriority.Config.from_file(Path('config.toml'))
        mkvpriority.process_file(file_path, config)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            '5.1 FLAC (Japanese)',
            'Full Subtitles [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == {'Signs & Songs [FanSub]'}


def test_score_tracks() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        config = mkvpriority.Config.from_file(Path('config.toml'))
        config.subtitle_group.profiles['signs_songs'].max_size_ratio = None
        mkvpriority.process_file(file_path, config)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(video_tracks + audio_tracks + subtitle_tracks) == 8

        mkvpriority.score_tracks(file_path, audio_tracks, config.audio_group)
        track_scores = {
            (track.name, track.language): track.scores['default'] for track in audio_tracks
        }
        assert track_scores == {
            ('5.1 FLAC (Japanese)', 'jpn'): 256,
            ('Stereo AAC (Japanese)', 'jpn'): 222,
            ('Stereo AAC (English)', 'eng'): 122,
        }

        mkvpriority.score_tracks(file_path, subtitle_tracks, config.subtitle_group)
        track_scores = {
            (track.name, track.language): track.scores['signs_songs'] for track in subtitle_tracks
        }
        assert track_scores == {
            ('Full Subtitles [FanSub]', 'eng'): -10000,
            ('Signs & Songs [FanSub]', 'eng'): 132,
            ('Dialogue [Blu-ray]', 'eng'): -10000,
            ('Dialogue [Blu-ray]', 'ger'): -10000,
        }

        mkvpriority.score_tracks(file_path, subtitle_tracks, config.subtitle_group)
        track_scores = {
            (track.name, track.language): track.scores['dialogue'] for track in subtitle_tracks
        }
        assert track_scores == {
            ('Full Subtitles [FanSub]', 'eng'): 132,
            ('Signs & Songs [FanSub]', 'eng'): 120,
            ('Dialogue [Blu-ray]', 'eng'): 121,
            ('Dialogue [Blu-ray]', 'ger'): 21,
        }


def test_entrypoint_script() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            'Stereo AAC (English)',
            'Signs & Songs [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == set()

        mkvpriority.main.main(['-c', 'config.toml', str(file_path)])

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            '5.1 FLAC (Japanese)',
            'Full Subtitles [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == {'Signs & Songs [FanSub]'}


def test_extension_modules() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            'Stereo AAC (English)',
            'Signs & Songs [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == set()

        mkvpriority.main.main(['-c', 'config.toml', '-i', 'subtitle_extractor', str(file_path)])

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
        assert {track.name for track in tracks if track.default} == {
            '5.1 FLAC (Japanese)',
            'Full Subtitles [FanSub]',
        }
        assert {track.name for track in tracks if track.forced} == {'Signs & Songs [FanSub]'}


@pytest.mark.asyncio
async def test_webhook_listener(aiohttp_client: AIOClientFixture) -> None:
    with patch('mkvpriority.main.main') as mock_main:
        app = web.Application()
        app.router.add_post('/process', entrypoint.process_handler)
        client = await aiohttp_client(app)

        payload = {'file_path': '/movies/dummy.mkv', 'item_tags': 'anime|1080p', 'orig_lang': 'jpn'}
        response = await client.post('/process', json=payload)

        assert response.status == 200
        response_data = await response.json()
        assert response_data == {'message': "received '/movies/dummy.mkv'"}

        assert not entrypoint.processing_queue.empty()
        queued_item = await entrypoint.processing_queue.get()
        assert queued_item == ('/movies/dummy.mkv', 'anime|1080p', 'jpn')

        os.environ['MKVPRIORITY_ARGS'] = '-c config.toml'
        await entrypoint.process_item(*queued_item)
        entrypoint.processing_queue.task_done()

        expected_argv = [*entrypoint.MKVPRIORITY_ARGS, '/movies/dummy.mkv::anime']
        mock_main.assert_called_once_with(expected_argv, 'jpn')


def test_penalize_unscored() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        *_, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        mkv_args = [str(file_path)]
        for subtitle_track in subtitle_tracks:
            if subtitle_track.language != 'eng':
                track_uid = subtitle_track.uid
                mkv_args.extend(['--edit', f'track:={track_uid}', '--set', 'flag-forced=1'])
        mkvpriority.modify_tracks(mkv_args)

        config = mkvpriority.Config.from_file(Path('config.toml'))
        config.subtitle_group.languages = {'eng': 0}
        config.subtitle_group.codecs = {}
        config.subtitle_group.profiles['dialogue'].filters = {}
        config.subtitle_group.profiles['signs_songs'].filters = {}
        config.subtitle_group.profiles['signs_songs'].max_size_ratio = None
        config.subtitle_group.profiles['signs_songs'].require_filter_match = False
        config.subtitle_group.penalize_unscored_languages = True

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(video_tracks) + len(audio_tracks) + len(subtitle_tracks) == 8
        assert len(subtitle_tracks) == 4

        mkvpriority.score_tracks(file_path, subtitle_tracks, config.subtitle_group)
        for track in subtitle_tracks:
            if track.language == 'eng':
                assert track.scores['signs_songs'] == 0
            else:
                assert track.forced
                assert track.scores['signs_songs'] == -10000

        mkvpriority.process_file(file_path, config)
        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert len(video_tracks) + len(audio_tracks) + len(subtitle_tracks) == 8
        assert len(subtitle_tracks) == 4
        assert {track.name for track in subtitle_tracks if track.default} == {
            'Signs & Songs [FanSub]'
        }
        assert {track.name for track in subtitle_tracks if track.forced} == set()


def test_detect_forced() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        config = mkvpriority.Config.from_file(Path('config.toml'))
        *_, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert subtitle_tracks[0].name == 'Full Subtitles [FanSub]'
        assert subtitle_tracks[1].name == 'Signs & Songs [FanSub]'
        for subtitle_track in subtitle_tracks:
            subtitle_track.name = ''

        mkvpriority.score_tracks(file_path, subtitle_tracks, config.subtitle_group)
        assert subtitle_tracks[0].scores['signs_songs'] < 0
        assert subtitle_tracks[1].scores['signs_songs'] > 0

        config.audio_group.languages['eng'] = 300
        mkvpriority.process_file(file_path, config)

        _, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        assert {track.name for track in audio_tracks if track.default} == {'Stereo AAC (English)'}
        assert {track.name for track in subtitle_tracks if track.default} == set()
        assert {track.name for track in subtitle_tracks if track.forced} == {
            'Signs & Songs [FanSub]'
        }


def test_restore_tracks() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        with tempfile.NamedTemporaryFile() as archive_file:
            config = mkvpriority.Config.from_file(Path('config.toml'))
            database = mkvpriority.Database(archive_file.name)
            mkvpriority.process_file(file_path, config, database)

            video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
            assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
            assert {track.name for track in tracks if track.default} == {
                '5.1 FLAC (Japanese)',
                'Full Subtitles [FanSub]',
            }
            assert {track.name for track in tracks if track.forced} == {'Signs & Songs [FanSub]'}

            mkvpriority.restore_file(file_path, database)

            video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
            assert len(tracks := video_tracks + audio_tracks + subtitle_tracks) == 8
            assert {track.name for track in tracks if track.default} == {
                'Stereo AAC (English)',
                'Signs & Songs [FanSub]',
            }
            assert {track.name for track in tracks if track.forced} == set()


def test_prune_database() -> None:
    with tempfile.NamedTemporaryFile() as archive_file:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            file_path = temp_path / 'dummy.mkv'
            track_files = create_dummy(temp_path)
            multiplex_dummy(file_path, track_files)

            config = mkvpriority.Config.from_file(Path('config.toml'))
            database = mkvpriority.Database(archive_file.name)
            mkvpriority.process_file(file_path, config, database)

            assert database.contains(file_path)
            database.prune()
            assert database.contains(file_path)

        database.prune()
        assert not database.contains(file_path)


def test_extract_subtitles() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_text = toml_text.replace(
            '[subtitle_profiles.global]',
            '[subtitle_profiles.global]\nextract_embedded_subtitles = true\n',
        )
        toml_path.write_text(toml_text, encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)

        mkvpriority.process_file(file_path, config, extensions=[SubtitleExtractor()])
        assert len(list(temp_path.glob('*dummy*.ass'))) == 2
        assert len(list(temp_path.glob('*dummy*.srt'))) == 0

        subtitle_path = file_path.with_suffix('.eng.default.ass')
        assert subtitle_path.is_file()
        assert subtitle_path.stat().st_size > 0

        subtitle_path = file_path.with_suffix('.eng.forced.ass')
        assert subtitle_path.is_file()
        assert subtitle_path.stat().st_size > 0


def test_convert_subtitles() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_text = toml_text.replace(
            '[subtitle_profiles.global]',
            '[subtitle_profiles.global]\n'
            'extract_embedded_subtitles = true\n'
            'convert_external_subtitles = true\n'
            'convert_target_format = "srt"\n'
            'convert_remove_source = false\n',
        )
        toml_path.write_text(toml_text, encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)

        extensions = [SubtitleExtractor(), SubtitleConverter()]
        mkvpriority.process_file(file_path, config, extensions=extensions)
        assert len(list(temp_path.glob('*dummy*.ass'))) == 2
        assert len(list(temp_path.glob('*dummy*.srt'))) == 2

        extracted_ass = file_path.with_suffix('.eng.default.ass')
        converted_srt = file_path.with_suffix('.eng.default.srt')

        assert extracted_ass.is_file() and converted_srt.is_file()
        assert '-->' in converted_srt.read_text(encoding='utf-8')

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_text = toml_text.replace(
            '[subtitle_profiles.global]',
            '[subtitle_profiles.global]\n'
            'extract_embedded_subtitles = true\n'
            'convert_external_subtitles = true\n'
            'convert_target_format = "ass"\n'
            'convert_remove_source = true\n',
        )
        toml_path.write_text(toml_text, encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)
        config.subtitle_group.codecs['S_TEXT/ASS'] = -10000

        extensions = [SubtitleExtractor(), SubtitleConverter()]
        mkvpriority.process_file(file_path, config, extensions=extensions)
        assert len(list(temp_path.glob('*dummy*.ass'))) == 1
        assert len(list(temp_path.glob('*dummy*.srt'))) == 0

        extracted_srt = file_path.with_suffix('.eng.default.srt')
        converted_ass = file_path.with_suffix('.eng.default.ass')

        assert not extracted_srt.is_file() and converted_ass.is_file()
        assert '[Script Info]' in converted_ass.read_text(encoding='utf-8')


def test_restyle_subtitles() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_text = toml_text.replace(
            '[subtitle_profiles.global]',
            '[subtitle_styles]\nfontname = "Cabin"\nfontsize = 75\noutline = 3.6\nshadow = 1.8\n'
            '[subtitle_profiles.global]\n'
            'extract_embedded_subtitles = true\n',
        )
        toml_path.write_text(toml_text, encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)

        extensions = [SubtitleExtractor(), SubtitleRestyler()]
        mkvpriority.process_file(file_path, config, extensions=extensions)

        subtitle_path = file_path.with_suffix('.eng.default.ass')
        restyled_content = subtitle_path.read_text(encoding='utf-8-sig')
        assert 'Style: Default,Cabin,20.0,&H00FFFFFF,0.96,0.48,2,1' in restyled_content


@pytest.mark.skip  # TODO
def test_reorder_tracks() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_path.write_text(f'{toml_text}\n[multiplexer]\nreorder_tracks = true', encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        tracks = video_tracks + audio_tracks + subtitle_tracks
        second_track = next(track for track in tracks if track.index == 1)
        assert second_track.name == 'Stereo AAC (Japanese)'

        mkvpriority.process_file(file_path, config, extensions=[Multiplexer()])

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        tracks = video_tracks + audio_tracks + subtitle_tracks
        second_track = next(track for track in tracks if track.index == 1)
        assert second_track.name == '5.1 FLAC (Japanese)'


@pytest.mark.skip  # TODO
def test_strip_tracks() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / 'dummy.mkv'
        track_files = create_dummy(temp_path)
        multiplex_dummy(file_path, track_files)

        toml_path = temp_path / 'config.toml'
        toml_text = Path('config.toml').read_text(encoding='utf-8')
        toml_path.write_text(f'{toml_text}\n[multiplexer]\nstrip_tracks = true', encoding='utf-8')
        config = mkvpriority.Config.from_file(toml_path)

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        tracks = video_tracks + audio_tracks + subtitle_tracks
        assert 'ger' in {track.language for track in tracks}

        mkvpriority.process_file(file_path, config, extensions=[Multiplexer()])

        video_tracks, audio_tracks, subtitle_tracks = mkvpriority.extract_tracks(file_path)
        tracks = video_tracks + audio_tracks + subtitle_tracks
        assert 'ger' not in {track.language for track in tracks}
