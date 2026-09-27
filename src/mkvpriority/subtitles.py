import logging
import re
import subprocess
import tempfile
from pathlib import Path

from .types import Track, resolve_language

POSITION_PATTERN = re.compile(r'\\(?:pos|move|org|i?clip|fade?|t)\s*\(|\\an[13-79]', re.IGNORECASE)
ROTATION_PATTERN = re.compile(r'\\(fr[xyz]?|fa[xy])-?\d+\.?\d*', re.IGNORECASE)
KARAOKE_PATTERN = re.compile(r'\\k[fo]?\d+\.?\d*', re.IGNORECASE)
DRAWING_PATTERN = re.compile(r'\\p[1-9]\d*', re.IGNORECASE)
OVERRIDE_PATTERN = re.compile(r'\{[^}]*\}')

SUBTITLE_EXTENSIONS = {'.ass': 'S_TEXT/ASS', '.ssa': 'S_TEXT/SSA', '.srt': 'S_TEXT/UTF8'}


mkvextract_logger = logging.getLogger('mkvextract')


def extract_header_title(subtitle_path: Path) -> str | None:
    with open(subtitle_path, encoding='utf-8-sig') as subtitle_file:
        for line in subtitle_file:
            if line.startswith('Title:'):
                return line.split(':', 1)[1].strip()
            if line.startswith('[V4'):
                break
    return None


def parse_external_subtitles(
    subtitle_path: Path, file_stem: str, virtual_index: int
) -> Track | None:
    file_suffix = subtitle_path.suffix.lower()
    if file_suffix not in SUBTITLE_EXTENSIONS:
        return None

    file_infix = subtitle_path.name[len(file_stem) : -len(file_suffix)]
    segments = [segment.strip() for segment in file_infix.split('.') if segment.strip()]

    track_lang = 'und'
    is_default = is_forced = False
    remaining_segments: list[str] = []

    for segment in segments:
        if segment.lower() == 'default':
            is_default = True
        elif segment.lower() == 'forced':
            is_forced = True
        elif track_lang == 'und' and resolve_language(segment):
            track_lang = segment
        else:
            remaining_segments.append(segment)

    track_name = ''
    if file_suffix in ('.ass', '.ssa'):
        track_name = extract_header_title(subtitle_path) or ''
    if not track_name and remaining_segments:
        track_name = ' '.join(remaining_segments)

    return Track(
        index=virtual_index,
        category='subtitles',
        name=track_name,
        language=track_lang,
        codec=SUBTITLE_EXTENSIONS[file_suffix],
        channels=0,
        default=is_default,
        forced=is_forced,
        enabled=True,
        uid=virtual_index,
        file_path=subtitle_path.resolve(),
    )


def count_unique_dialogue(temp_path: Path, is_srt: bool) -> int:
    unique_dialogue: set[str] = set()

    with temp_path.open(encoding='utf-8-sig', errors='replace') as temp_file:
        for line in temp_file:
            if not (stripped_line := line.strip()):
                continue
            if is_srt:
                if stripped_line.isdigit() or '-->' in stripped_line:
                    continue
                dialogue = stripped_line
            else:
                if not stripped_line.startswith('Dialogue:'):
                    continue
                dialogue = stripped_line.split(',', 9)[-1]
            if any(
                pattern.search(dialogue)
                for pattern in (
                    DRAWING_PATTERN,
                    POSITION_PATTERN,
                    ROTATION_PATTERN,
                    KARAOKE_PATTERN,
                )
            ):
                continue

            stripped_dialogue = (
                OVERRIDE_PATTERN.sub('', dialogue).strip() if '{' in dialogue else dialogue.strip()
            )
            if stripped_dialogue:
                unique_dialogue.add(stripped_dialogue)

    return len(unique_dialogue)


def compute_subtitle_sizes(
    file_path: Path, tracks: list[Track], dry_run: bool = False
) -> dict[int, int]:
    ambiguous_tracks = [
        f'Track {track.index} ({track.name})' if track.name else f'Track {track.index}'
        for track in tracks
    ]
    log_prefix = '[DRY RUN] ' if dry_run else ''
    mkvextract_logger.info(log_prefix + f'analyzing subtitle sizes for {ambiguous_tracks}')

    dialogue_counts: dict[int, int] = {track.index: 0 for track in tracks}

    internal_tracks = [track for track in tracks if not track.is_external]
    external_tracks = [track for track in tracks if track.is_external]

    for track in external_tracks:
        if track.file_path and track.file_path.exists():
            dialogue_counts[track.index] = count_unique_dialogue(
                track.file_path, is_srt=track.codec == 'S_TEXT/UTF8'
            )
    if not internal_tracks:
        return dialogue_counts

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        arguments = ['mkvextract', 'tracks', str(file_path)]

        def codec_ext(codec: str) -> str:
            return 'ass' if codec in ('S_TEXT/ASS', 'S_TEXT/SSA') else 'srt'

        temp_paths = {
            track.index: temp_dir_path / f'{track.index}.{codec_ext(track.codec)}'
            for track in internal_tracks
        }
        arguments.extend(f'{index}:{path}' for index, path in temp_paths.items())

        try:
            result = subprocess.run(arguments, capture_output=True, text=True, check=True)
            if result.stdout.strip():
                mkvextract_logger.debug(result.stdout.strip())
        except (subprocess.SubprocessError, OSError) as e:
            mkvextract_logger.error(str(e).strip())
            return dialogue_counts

        track_by_index = {track.index: track for track in internal_tracks}
        for index, temp_path in temp_paths.items():
            if not temp_path.exists():
                continue
            dialogue_counts[index] = count_unique_dialogue(
                temp_path, is_srt=track_by_index[index].codec == 'S_TEXT/UTF8'
            )

    return dialogue_counts
