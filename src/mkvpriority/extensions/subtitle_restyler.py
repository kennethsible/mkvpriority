import re
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

from mkvpriority import Config, Extension, Track

SAFE_ATTRS = {
    'Fontname',
    'PrimaryColour',
    'SecondaryColour',
    'OutlineColour',
    'BackColour',
    'Bold',
    'Italic',
    'Underline',
    'StrikeOut',
    'ScaleX',
    'ScaleY',
    'Angle',
    'Alignment',
    'BorderStyle',
    'Encoding',
}
RES_DEP_X = {'Spacing', 'MarginL', 'MarginR'}
RES_DEP_Y = {'Fontsize', 'Outline', 'Shadow', 'MarginV'}
ASS_ATTR_MAP = {attr.lower(): attr for attr in SAFE_ATTRS | RES_DEP_X | RES_DEP_Y}


class SubtitleRestyler(Extension):
    def __init__(self, max_ratio: float = 0.15, max_allowance: int = 2):
        super().__init__('subtitle_restyler')
        self.parameters: dict[str, Any] = {}
        self.max_ratio = max_ratio
        self.max_allowance = max_allowance

    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        if not subtitle_tracks:
            return

        if config.toml_path in self.parameters:
            attributes = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            attributes = toml_file.get('subtitle_styles', {})
            self.parameters[config.toml_path] = attributes

        if attributes:
            subtitle_track = max(subtitle_tracks, key=lambda track: track.score)
            subtitle_path = self.build_subtitle_path(file_path, subtitle_track)
            if subtitle_path and subtitle_path.is_file():
                self.modify_subtitle_styles(subtitle_path, attributes)

    def build_subtitle_path(self, file_path: Path, subtitle_track: Track) -> Path | None:
        if not subtitle_track.codec.startswith('S_TEXT/'):
            return None
        if subtitle_track.codec.split('/')[-1] != 'ASS':
            return None
        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        return Path(file_path).with_suffix(f'{subtitle_suffix}.ass')

    def scale_style_attributes(
        self, input_lines: list[str], attributes: dict[str, Any]
    ) -> dict[str, str]:
        playres_x, playres_y = 1920.0, 1080.0
        for line in input_lines:
            if line.startswith('PlayResX:'):
                playres_x = float(line.split(':')[1].strip())
            elif line.startswith('PlayResY:'):
                playres_y = float(line.split(':')[1].strip())
            elif line.startswith('[Events]'):
                break

        scale_x = playres_x / 1920.0
        scale_y = playres_y / 1080.0
        scaled_attributes: dict[str, str] = {}
        for attr, val in attributes.items():
            attr = ASS_ATTR_MAP.get(attr.lower(), attr)
            if attr in SAFE_ATTRS:
                scaled_attributes[attr] = str(val)
            elif attr in RES_DEP_X:
                scaled_val = float(val) * scale_x
                scaled_attributes[attr] = str(
                    int(round(scaled_val)) if 'Margin' in attr else round(scaled_val, 2)
                )
            elif attr in RES_DEP_Y:
                scaled_val = float(val) * scale_y
                scaled_attributes[attr] = str(
                    int(round(scaled_val)) if 'Margin' in attr else round(scaled_val, 2)
                )
            else:
                self.extension_logger.warning(f"style '{attr}' is not in [V4+ Styles]")

        return scaled_attributes

    def detect_dialogue_styles(self, input_lines: list[str]) -> set[str]:
        style_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {'count_spatial': 0, 'count_karaoke': 0, 'count_drawing': 0, 'total': 0}
        )
        in_events_section = False

        position_pattern = re.compile(r'\\(pos|move|org|clip|iclip)\s*\(|\\an[1-9]', re.IGNORECASE)
        rotation_pattern = re.compile(r'\\(fr[xyz]|fa[xy])\s*-?\d', re.IGNORECASE)
        karaoke_pattern = re.compile(r'\\[kK][fo]?[0-9]+')
        drawing_pattern = re.compile(r'\\[pP][1-9]')

        for line in input_lines:
            if line.startswith('[Events]'):
                in_events_section = True
                continue
            elif line.startswith('['):
                in_events_section = False

            if in_events_section and line.startswith('Dialogue:'):
                parts = line.split(':', 1)[1].strip().split(',', 9)
                if len(parts) > 9:
                    style_name = parts[3].strip()
                    text = parts[9]
                    style_stats[style_name]['total'] += 1
                    if position_pattern.search(text) or rotation_pattern.search(text):
                        style_stats[style_name]['count_spatial'] += 1
                    if karaoke_pattern.search(text):
                        style_stats[style_name]['count_karaoke'] += 1
                    if drawing_pattern.search(text):
                        style_stats[style_name]['count_drawing'] += 1

        subtitle_styles: set[str] = set()
        for style, stats in style_stats.items():
            ratio_karaoke = stats['count_karaoke'] / stats['total']
            if stats['count_karaoke'] > 0 and ratio_karaoke > 0.1:
                continue
            if stats['count_drawing'] > 0:
                continue
            ratio_spatial = stats['count_spatial'] / stats['total']
            is_dialogue = ratio_spatial <= self.max_ratio
            if not is_dialogue and stats['count_spatial'] <= self.max_allowance:
                if ratio_spatial < 1.0:
                    is_dialogue = True
            if is_dialogue:
                subtitle_styles.add(style)

        return subtitle_styles

    def modify_subtitle_styles(self, file_path: Path, attributes: dict[str, Any]) -> None:
        with open(file_path, encoding='utf-8-sig') as f:
            input_lines = f.readlines()
        scaled_attributes = self.scale_style_attributes(input_lines, attributes)
        if not scaled_attributes:
            return
        subtitle_styles = self.detect_dialogue_styles(input_lines)
        if not subtitle_styles:
            return

        attr_indices: dict[str, int] = {}
        output_lines: list[str] = []
        in_styles_section = False
        for line in input_lines:
            if line.startswith('[V4+ Styles]'):
                in_styles_section = True
                output_lines.append(line)
                continue
            elif line.startswith('['):
                in_styles_section = False

            if in_styles_section:
                if line.startswith('Format:'):
                    format_string = line.split(':', 1)[1].strip()
                    format_parts = [p.strip() for p in format_string.split(',')]
                    for attr in scaled_attributes.keys():
                        if attr in format_parts:
                            attr_indices[attr] = format_parts.index(attr)
                elif line.startswith('Style:') and attr_indices:
                    style_parts = line.split(':', 1)[1].strip().split(',')
                    style_name = style_parts[0].strip()
                    if style_name in subtitle_styles:
                        for attr, val in scaled_attributes.items():
                            if attr in attr_indices:
                                index = attr_indices[attr]
                                style_parts[index] = val
                        line = 'Style: ' + ','.join(style_parts) + '\n'
            output_lines.append(line)

        self.extension_logger.info(f'restyling external subtitles for {sorted(subtitle_styles)}')
        with open(file_path, 'w', encoding='utf-8-sig') as f:
            f.writelines(output_lines)
