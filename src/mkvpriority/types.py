import dataclasses
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path

import pycountry


@lru_cache(maxsize=128)
def resolve_language(segment: str, target_format: str | None = None) -> str | None:
    base_segment = segment.replace('_', '-').split('-')[0].lower().strip()
    if len(base_segment) <= 1:
        return None

    language = None
    if len(base_segment) == 2:
        language = pycountry.languages.get(alpha_2=base_segment)
    elif len(base_segment) == 3:
        language = pycountry.languages.get(alpha_3=base_segment)

    if language is None:
        try:
            language = pycountry.languages.lookup(base_segment)
        except LookupError:
            return None

    if target_format is None:
        return getattr(language, 'bibliographic', None) or str(language.alpha_3)

    match target_format.lower():
        case 'alpha2' | 'alpha_2' | '2-letter' | 'iso-639-1':
            return getattr(language, 'alpha_2', None) or str(language.alpha_3)
        case 'alpha3' | 'alpha_3' | '3-letter' | 'iso-639-2':
            return str(language.alpha_3)
    return None


def normalize_language_dict(languages: dict[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for language, score in languages.items():
        resolved = resolve_language(language) or language
        normalized[resolved] = score
    return normalized


def normalize_language_list(languages: list[str]) -> list[str]:
    return [resolve_language(language) or language for language in languages]


@dataclass
class Track:
    index: int
    category: str
    name: str
    language: str
    scores: dict[str, int] = dataclasses.field(default_factory=dict)
    default: bool = False
    forced: bool = False
    enabled: bool = True
    codec: str = ''
    channels: int = 0
    uid: int = 0
    size: int | None = None
    file_path: Path | None = None

    @cached_property
    def normalized_language(self) -> str:
        return resolve_language(self.language) or 'und'  # ISO 639-2/B

    @property
    def is_external(self) -> bool:
        return self.file_path is not None


@dataclass
class Profile:
    name: str
    mode: list[str] = dataclasses.field(default_factory=list)
    filters: dict[str, int] = dataclasses.field(default_factory=dict)
    require_filter_match: bool = False


@dataclass
class AudioProfile(Profile):
    pass


@dataclass
class SubtitleProfile(Profile):
    max_size_ratio: float | None = None


@dataclass
class ProfileGroup[P: Profile]:
    languages: dict[str, int] = dataclasses.field(default_factory=dict)
    codecs: dict[str, int] = dataclasses.field(default_factory=dict)
    profiles: dict[str, P] = dataclasses.field(default_factory=dict)
    penalize_unscored_languages: bool = False


@dataclass
class AudioProfileGroup(ProfileGroup[AudioProfile]):
    channels: dict[str, int] = dataclasses.field(default_factory=dict)


@dataclass
class SubtitleProfileGroup(ProfileGroup[SubtitleProfile]):
    native_languages: list[str] = dataclasses.field(default_factory=list)
    process_external_subtitles: bool = False
