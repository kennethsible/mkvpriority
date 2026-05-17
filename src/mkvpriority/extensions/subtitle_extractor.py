import json
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from mkvpriority import Config, Extension, Track

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt', 'WEBVTT': 'vtt'}


class SubtitleExtractor(Extension):
    def __init__(self):
        super().__init__('subtitle_extractor')

    def process_file(
        self,
        file_path: Path,
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        if not subtitle_tracks:
            return
        embedded_subtitles = max(subtitle_tracks, key=lambda track: track.score)
        self.extract_subtitles(file_path, embedded_subtitles)

    def extract_subtitles(self, file_path: Path, subtitle_track: Track) -> Path | None:
        if not subtitle_track.codec.startswith('S_TEXT/'):
            return None
        subtitle_format = subtitle_track.codec.split('/')[-1]
        if subtitle_format not in SUBTITLE_EXTENSIONS:
            return None
        extension = SUBTITLE_EXTENSIONS[subtitle_format]

        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        subtitle_path = Path(file_path).with_suffix(f'{subtitle_suffix}.{extension}')
        if subtitle_path.is_file():
            return None
        self.extension_logger.info(f"extracting subtitles to '{subtitle_path.parent}'")

        with NamedTemporaryFile('w+', suffix='.json', delete=False, encoding='utf-8') as temp_file:
            json.dump(
                ['tracks', str(file_path), f'{subtitle_track.index}:{subtitle_path}'], temp_file
            )
            temp_file.flush()
            result = subprocess.run(
                ['mkvextract', f'@{temp_file.name}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
                text=True,
            )
            self.extension_logger.debug(result.stdout.strip())

        return subtitle_path
