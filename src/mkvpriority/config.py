from __future__ import annotations

import dataclasses
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import (
    AudioProfile,
    AudioProfileGroup,
    SubtitleProfile,
    SubtitleProfileGroup,
    normalize_language_dict,
    normalize_language_list,
)


class ConfigError(Exception):
    pass


def apply_override(toml_dict: dict[str, Any], override: str) -> None:
    if '=' not in override:
        raise ValueError(f"invalid override format '{override}'")

    path, value_str = override.split('=', 1)
    keys = [key.strip() for key in path.strip().split('.')]
    try:
        value = json.loads(value_str.strip())
    except json.JSONDecodeError:
        value = value_str.strip()

    target = toml_dict
    for key in keys[:-1]:
        if key not in target or not isinstance(target[key], dict):
            raise ConfigError(f"unknown section '{key}' in override '{override}'")
        target = target[key]
    target[keys[-1]] = value


def validate_config_schema(toml_dict: dict[str, Any], toml_path: str | Path) -> None:
    profile_sections = ('audio_profiles', 'subtitle_profiles')
    if missing_sections := [section for section in profile_sections if section not in toml_dict]:
        missing_sections_str = ' and '.join(f'[{section}]' for section in missing_sections)
        raise ConfigError(f"missing {missing_sections_str} in '{toml_path}'")

    for section_name in profile_sections:
        section = toml_dict[section_name]
        if not isinstance(section, dict):
            raise ConfigError(f"'[{section_name}]' in '{toml_path}' must be a table")

        global_section = section.get('global')
        if not isinstance(global_section, dict):
            raise ConfigError(f"missing '[{section_name}.global]' table in '{toml_path}'")

        global_keys = ['languages', 'codecs']
        if section_name == 'audio_profiles':
            global_keys.append('channels')
        for key in global_keys:
            if key in global_section and not isinstance(global_section[key], dict):
                raise ConfigError(
                    f"'[{section_name}.global.{key}]' in '{toml_path}' must be a table"
                )

        if (
            section_name == 'subtitle_profiles'
            and 'native_languages' in global_section
            and not isinstance(global_section['native_languages'], list)
        ):
            raise ConfigError(f"'native_languages' in '[{section_name}.global]' must be a list")

        profiles = {key: value for key, value in section.items() if key != 'global'}
        if not profiles:
            raise ConfigError(f"missing profiles for '[{section_name}]' in '{toml_path}'")

        mode_name = 'audio_mode' if section_name == 'audio_profiles' else 'subtitle_mode'
        for profile_name, profile in profiles.items():
            if not isinstance(profile, dict):
                raise ConfigError(
                    f"'[{section_name}.{profile_name}]' in '{toml_path}' must be a table"
                )

            profile_mode = profile.get(mode_name)
            if not isinstance(profile_mode, list):
                raise ConfigError(
                    f"'{mode_name}' in '[{section_name}.{profile_name}]' must be a list"
                )
            if not profile_mode:
                raise ConfigError(
                    f"empty {mode_name} for '[{section_name}.{profile_name}]' in '{toml_path}'"
                )

            if 'filters' in profile and not isinstance(profile['filters'], dict):
                raise ConfigError(f"'filters' in '[{section_name}.{profile_name}]' must be a table")


@dataclass
class Config:
    toml_path: str
    toml_label: str
    audio_group: AudioProfileGroup = dataclasses.field(default_factory=AudioProfileGroup)
    subtitle_group: SubtitleProfileGroup = dataclasses.field(default_factory=SubtitleProfileGroup)

    @classmethod
    def from_file(
        cls, toml_path: str | Path, toml_label: str = 'untagged', overrides: list[str] | None = None
    ) -> Config:
        with open(toml_path, 'rb') as f:
            toml_dict = tomllib.load(f)
        if overrides is not None:
            for override in overrides:
                apply_override(toml_dict, override)
        validate_config_schema(toml_dict, toml_path)

        audio_section = toml_dict.get('audio_profiles', {})
        audio_global = audio_section.get('global', {})
        audio_profiles = {
            key: AudioProfile(
                name=key,
                mode=value.get('audio_mode', []),
                filters=value.get('filters', {}),
                require_filter_match=value.get('require_filter_match', False),
            )
            for key, value in audio_section.items()
            if key != 'global'
        }
        audio_group = AudioProfileGroup(
            languages=normalize_language_dict(audio_global.get('languages', {})),
            codecs=audio_global.get('codecs', {}),
            profiles=audio_profiles,
            penalize_unscored_languages=audio_global.get('penalize_unscored_languages', False),
            channels=audio_global.get('channels', {}),
        )

        subtitle_section = toml_dict.get('subtitle_profiles', {})
        subtitle_global = subtitle_section.get('global', {})
        subtitle_profiles = {
            key: SubtitleProfile(
                name=key,
                mode=value.get('subtitle_mode', []),
                filters=value.get('filters', {}),
                require_filter_match=value.get('require_filter_match', False),
                max_size_ratio=value.get('max_size_ratio'),
            )
            for key, value in subtitle_section.items()
            if key != 'global'
        }
        subtitle_group = SubtitleProfileGroup(
            languages=normalize_language_dict(subtitle_global.get('languages', {})),
            codecs=subtitle_global.get('codecs', {}),
            profiles=subtitle_profiles,
            penalize_unscored_languages=subtitle_global.get('penalize_unscored_languages', False),
            native_languages=normalize_language_list(subtitle_global.get('native_languages', [])),
            process_external_subtitles=subtitle_global.get('process_external_subtitles', False),
        )

        return cls(
            toml_path=str(toml_path),
            toml_label=toml_label,
            audio_group=audio_group,
            subtitle_group=subtitle_group,
        )
