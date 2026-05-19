import json
import subprocess
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from mkvpriority import Config, Extension, Track
from mkvpriority.main import mkvmerge_logger


class Multiplexer(Extension):
    def __init__(self) -> None:
        super().__init__('multiplexer')
        self.parameters: dict[str, Any] = {}

    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        if config.toml_path in self.parameters:
            parameters = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            parameters = toml_file.get('multiplexer', {})
            self.parameters[config.toml_path] = parameters
        self.strip: bool = parameters.get('strip_tracks', False)
        self.reorder: bool = parameters.get('reorder_tracks', False)
        self.filter_tracks(file_path, video_tracks, audio_tracks, subtitle_tracks, config, dry_run)

    def multiplex_tracks(self, arguments: list[str]) -> None:
        with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(arguments, temp_file)
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
        finally:
            temp_file_path.unlink(missing_ok=True)

    def filter_tracks(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        track_order: list[str] = []
        audio_strip: list[str] = []
        subtitle_strip: list[str] = []

        def process_tracks(
            tracks: list[Track],
            track_order: list[str],
            track_strip: list[str],
            track_langs: dict[str, int],
        ) -> None:
            for track in tracks:
                if self.strip and self.reorder:
                    if track.language in track_langs:
                        track_order.append(f'0:{track.index}')
                    else:
                        track_strip.append(f'!{track.index}')
                elif self.strip:
                    if track.language not in track_langs:
                        track_strip.append(f'!{track.index}')
                elif self.reorder:
                    track_order.append(f'0:{track.index}')

        for track in video_tracks:
            track_order.append(f'0:{track.index}')
        process_tracks(audio_tracks, track_order, audio_strip, config.audio_languages)
        process_tracks(subtitle_tracks, track_order, subtitle_strip, config.subtitle_languages)

        temp_output_path = file_path.with_name(f'{file_path.stem}_temp.mkv')
        mkv_args = ['-o', str(temp_output_path)]
        if audio_strip:
            mkv_args += ['--audio-tracks', ','.join(audio_strip)]
        if subtitle_strip:
            mkv_args += ['--subtitle-tracks', ','.join(subtitle_strip)]
        mkv_args += [str(file_path)]
        if track_order and any(
            int(id_a.split(':')[1]) > int(id_b.split(':')[1])
            for id_a, id_b in zip(track_order, track_order[1:])
        ):
            mkv_args += ['--track-order', ','.join(track_order)]

        if len(mkv_args) > 3:
            self.extension_logger.info(' '.join(mkv_args))
            if not dry_run:
                try:
                    self.multiplex_tracks(mkv_args)
                    temp_output_path.replace(file_path)
                except subprocess.CalledProcessError as e:
                    mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
                    temp_output_path.unlink(missing_ok=True)
                    raise
