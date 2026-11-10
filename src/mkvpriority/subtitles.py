import glob
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from .types import Track, resolve_language

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvextract_logger = logging.getLogger('mkvextract')

POSITION_PATTERN = re.compile(r'\\(?:pos|move|org|i?clip|fade?|t)\s*\(|\\an[13-79]', re.IGNORECASE)
ROTATION_PATTERN = re.compile(r'\\(fr[xyz]?|fa[xy])-?\d+\.?\d*', re.IGNORECASE)
KARAOKE_PATTERN = re.compile(r'\\k[fo]?\d+\.?\d*', re.IGNORECASE)
DRAWING_PATTERN = re.compile(r'\\p[1-9]\d*', re.IGNORECASE)
OVERRIDE_PATTERN = re.compile(r'\{[^}]*\}')

SUBTITLE_EXTENSIONS = {'.ass': 'S_TEXT/ASS', '.ssa': 'S_TEXT/SSA', '.srt': 'S_TEXT/UTF8'}


def extract_header_title(subtitle_path: Path) -> str | None:
    with open(subtitle_path, encoding='utf-8-sig') as subtitle_file:
        for line in subtitle_file:
            if line.startswith('Title:'):
                return line.split(':', 1)[1].strip()
            if line.startswith('[V4'):
                break
    return None


def parse_sidecar_track(subtitle_path: Path, file_stem: str, virtual_index: int) -> Track | None:
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
    if remaining_segments:
        track_name = ' '.join(remaining_segments)
    elif file_suffix in ('.ass', '.ssa'):
        track_name = extract_header_title(subtitle_path) or ''

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


def find_sidecar_tracks(file_path: Path) -> list[Track]:
    parent_dir = file_path.parent
    if not parent_dir.is_dir():
        return []

    subtitle_tracks: list[Track] = []
    file_stem = file_path.stem

    virtual_id = -1
    for sidecar_path in sorted(parent_dir.glob(f'{glob.escape(file_stem)}*')):
        if not sidecar_path.is_file():
            continue
        if subtitle_track := parse_sidecar_track(sidecar_path, file_stem, virtual_id):
            subtitle_tracks.append(subtitle_track)
            virtual_id -= 1

    if subtitle_tracks:
        sidecar_suffixes = ', '.join(
            f"'{subtitle_track.file_path.name[len(file_stem) :]}'"
            for subtitle_track in subtitle_tracks
            if subtitle_track.file_path
        )
        mkvpriority_logger.info(f'detected external subtitles: {sidecar_suffixes}')

    return subtitle_tracks


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


def compute_subtitle_sizes(file_path: Path, tracks: list[Track]) -> dict[int, int]:
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

    def codec_ext(codec: str) -> str:
        return 'ass' if codec in ('S_TEXT/ASS', 'S_TEXT/SSA') else 'srt'

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        track_paths = [
            (track, temp_dir_path / f'{track.index}.{codec_ext(track.codec)}')
            for track in internal_tracks
        ]
        path_args = [f'{track.index}:{temp_path}' for track, temp_path in track_paths]
        arguments = ['tracks', str(file_path), *path_args]
        mkvextract_logger.debug(' '.join(arguments))

        with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(arguments, temp_file)
            temp_file_path = Path(temp_file.name)

        try:
            result = subprocess.run(
                ['mkvextract', f'@{temp_file_path}'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                check=True,
            )
            if result.stdout.strip():
                mkvextract_logger.debug(result.stdout.strip())
        except (subprocess.SubprocessError, OSError) as e:
            mkvextract_logger.error(str(e).strip())
            return dialogue_counts
        finally:
            temp_file_path.unlink(missing_ok=True)

        for track, temp_path in track_paths:
            if not temp_path.exists():
                continue
            dialogue_counts[track.index] = count_unique_dialogue(
                temp_path, is_srt=track.codec == 'S_TEXT/UTF8'
            )

    return dialogue_counts
