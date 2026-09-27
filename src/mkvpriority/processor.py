import copy
import logging
import subprocess
from pathlib import Path

from .config import Config
from .database import Database
from .extension import Extension
from .mkvtoolnix import ensure_segment_uid, extract_tracks, modify_tracks
from .subtitles import compute_subtitle_sizes
from .types import (
    AudioProfileGroup,
    Profile,
    ProfileGroup,
    SubtitleProfile,
    SubtitleProfileGroup,
    Track,
)

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')


def score_tracks[T: Profile](
    file_path: Path, tracks: list[Track], group: ProfileGroup[T], dry_run: bool = False
) -> None:
    max_track_size = 0

    def compute_score(track: Track, profile: Profile) -> int:
        score = 0
        default_lang_score = -10000 if group.penalize_unscored_languages else 0
        score += group.languages.get(track.normalized_language, default_lang_score)
        score += group.codecs.get(track.codec, 0)
        if isinstance(group, AudioProfileGroup):
            score += group.channels.get(str(track.channels), 0)

        filter_matched = False
        if track.name:
            for key, value in profile.filters.items():
                if key.lower() in track.name.lower():
                    score += value
                    filter_matched = True

        within_size_ratio = False
        if (
            isinstance(profile, SubtitleProfile)
            and (profile.max_size_ratio is not None)
            and max_track_size > 0
        ):
            if track.size is not None:
                if track.size / max_track_size <= profile.max_size_ratio:
                    within_size_ratio = True
                else:
                    score -= 10000
            else:
                score -= 10000

        if profile.require_filter_match and not (filter_matched or within_size_ratio):
            return -10000
        return score

    for track in tracks:
        for profile_name, profile in group.profiles.items():
            track.scores[profile_name] = compute_score(track, profile)

    if isinstance(group, SubtitleProfileGroup) and any(
        profile.max_size_ratio is not None for profile in group.profiles.values()
    ):
        candidate_tracks = [
            track
            for track in tracks
            if any(score > 0 for score in track.scores.values())
            and track.codec in ('S_TEXT/ASS', 'S_TEXT/SSA', 'S_TEXT/UTF8')
        ]
        is_ambiguous = len(candidate_tracks) > 1 and (
            all(not track.name for track in candidate_tracks)
        )

        if not is_ambiguous and len(candidate_tracks) > 1:
            profile_winners: dict[str, Track] = {}
            for profile_name, group_profile in group.profiles.items():
                best_track = max(
                    candidate_tracks, key=lambda track: track.scores.get(profile_name, 0)
                )
                if best_track.scores.get(profile_name, 0) > 0:
                    profile_winners[profile_name] = best_track

            for profile_name, group_profile in group.profiles.items():
                if group_profile.max_size_ratio is None:
                    continue
                if (target_track := profile_winners.get(profile_name)) is None:
                    continue
                if any(
                    target_track == other_track
                    for other_name, other_track in profile_winners.items()
                    if profile_name != other_name
                ):
                    is_ambiguous = True
                    break

        if is_ambiguous:
            subtitle_sizes = compute_subtitle_sizes(file_path, candidate_tracks, dry_run)
            for subtitle_track in candidate_tracks:
                subtitle_track.size = subtitle_sizes.get(subtitle_track.index, 0)
                max_track_size = max(subtitle_track.size, max_track_size)

            if max_track_size > 0:
                for track in tracks:
                    for profile_name, group_profile in group.profiles.items():
                        track.scores[profile_name] = compute_score(track, group_profile)


def restore_tracks(
    segment_uid: str,
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    database: Database,
    dry_run: bool = False,
) -> None:
    uid_args = [str(file_path)]
    idx_args: list[str] = []

    def apply_track_modes(track: Track, use_index: bool = False) -> list[str]:
        track_id = track.index if use_index else track.uid
        track_name = f' ({track.name})' if use_index and track.name else ''
        return [
            '--edit',
            f'track:={track_id}{track_name}',
            '--set',
            f'flag-default={int(track.default)}',
            '--set',
            f'flag-forced={int(track.forced)}',
            '--set',
            f'flag-enabled={int(track.enabled)}',
        ]

    for track in [*audio_tracks, *subtitle_tracks]:
        if track.is_external:
            continue
        uid_args += apply_track_modes(track, use_index=False)
        idx_args += apply_track_modes(track, use_index=True)

    if len(uid_args) > 1:
        log_prefix = '[DRY RUN] ' if dry_run else ''
        mkvpropedit_logger.info(log_prefix + ' '.join(idx_args))
        if not dry_run:
            try:
                modify_tracks(uid_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    database.delete(segment_uid)


def restore_file(file_path: Path, database: Database, dry_run: bool = False) -> None:
    segment_uid, _, audio_tracks, subtitle_tracks = extract_tracks(file_path, database=database)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, dry_run)
    restore_tracks(segment_uid, file_path, audio_tracks, subtitle_tracks, database, dry_run)


def process_tracks(
    segment_uid: str,
    file_path: Path,
    audio_tracks: list[Track],
    subtitle_tracks: list[Track],
    config: Config,
    database: Database | None = None,
    dry_run: bool = False,
) -> None:
    orig_tracks: dict[int, Track] = {}
    uid_args = [str(file_path)]
    idx_args: list[str] = []

    def snapshot_track(track: Track) -> None:
        if track.is_external:
            return
        if track.uid not in orig_tracks:
            orig_tracks[track.uid] = copy.copy(track)

    def apply_profiles[T: Profile](
        tracks: list[Track], group: ProfileGroup[T], suppress_default: bool = False
    ) -> Track | None:
        if not tracks or not group.profiles:
            return None

        default_track: Track | None = None
        track_flags: dict[int, dict[str, str]] = {track.uid: {} for track in tracks}

        for profile_name, profile in group.profiles.items():
            track_modes = profile.mode
            default_mode = 'default' in track_modes
            forced_mode = 'forced' in track_modes
            disabled_mode = 'disabled' in track_modes
            enabled_mode = 'enabled' in track_modes

            sorted_tracks = sorted(
                tracks, key=lambda track: track.scores.get(profile_name, 0), reverse=True
            )
            best_track = sorted_tracks[0]
            best_score = best_track.scores.get(profile_name, 0)

            if default_mode and best_score > 0:
                default_track = best_track

            if best_score > 0:
                if default_mode and not suppress_default and not best_track.default:
                    track_flags[best_track.uid]['flag-default'] = '1'
                    snapshot_track(best_track)
                    best_track.default = True
                if forced_mode and not best_track.forced:
                    track_flags[best_track.uid]['flag-forced'] = '1'
                    snapshot_track(best_track)
                    best_track.forced = True
                if (disabled_mode or enabled_mode) and not best_track.enabled:
                    track_flags[best_track.uid]['flag-enabled'] = '1'
                    snapshot_track(best_track)
                    best_track.enabled = True
                unwanted_tracks = sorted_tracks[1:]
            else:
                unwanted_tracks = sorted_tracks

            for track in unwanted_tracks:
                if not track.scores.get(profile_name, 0):
                    continue
                if default_mode and not suppress_default and track.default:
                    track_flags[track.uid]['flag-default'] = '0'
                    snapshot_track(track)
                    track.default = False
                if forced_mode and track.forced:
                    track_flags[track.uid]['flag-forced'] = '0'
                    snapshot_track(track)
                    track.forced = False
                if disabled_mode and track.enabled:
                    track_flags[track.uid]['flag-enabled'] = '0'
                    snapshot_track(track)
                    track.enabled = False
                if enabled_mode and not track.enabled:
                    track_flags[track.uid]['flag-enabled'] = '1'
                    snapshot_track(track)
                    track.enabled = True

        for track in tracks:
            if track.default and suppress_default:
                track_flags[track.uid]['flag-default'] = '0'
                snapshot_track(track)
                track.default = False
            if track.default and track.forced:
                snapshot_track(track)
                track.forced = False
                if track.uid in orig_tracks and orig_tracks[track.uid].forced:
                    track_flags[track.uid]['flag-forced'] = '0'
                else:
                    track_flags[track.uid].pop('flag-forced', None)

        for track in tracks:
            mkvpriority_logger.debug(track)
            if track_flags[track.uid]:
                track_name = f' ({track.name})' if track.name else ''
                if not track.is_external:
                    uid_args.extend(['--edit', f'track:={track.uid}'])
                idx_args.extend(['--edit', f'track:={track.index}{track_name}'])
                for flag, value in track_flags[track.uid].items():
                    if not track.is_external:
                        uid_args.extend(['--set', f'{flag}={value}'])
                    idx_args.extend(['--set', f'{flag}={value}'])

        return default_track or tracks[0]

    score_tracks(file_path, audio_tracks, config.audio_group, dry_run)
    default_audio_track = apply_profiles(audio_tracks, config.audio_group)

    suppress_default = (
        default_audio_track is not None
        and default_audio_track.language in config.subtitle_group.native_languages
    )

    score_tracks(file_path, subtitle_tracks, config.subtitle_group, dry_run)
    apply_profiles(subtitle_tracks, config.subtitle_group, suppress_default=suppress_default)

    if len(uid_args) > 1:
        log_prefix = '[DRY RUN] ' if dry_run else ''
        mkvpropedit_logger.info(log_prefix + ' '.join(idx_args))
        if not dry_run:
            try:
                modify_tracks(uid_args)
            except subprocess.CalledProcessError as e:
                mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
                return
    if database is not None:
        database.insert(segment_uid, file_path, list(orig_tracks.values()))


def process_file(
    file_path: Path,
    config: Config,
    database: Database | None = None,
    extensions: list[Extension] | None = None,
    dry_run: bool = False,
) -> None:
    segment_uid, video_tracks, audio_tracks, subtitle_tracks = extract_tracks(file_path, config)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, dry_run)
    process_tracks(segment_uid, file_path, audio_tracks, subtitle_tracks, config, database, dry_run)
    if extensions is not None:
        for extension in extensions:
            extension.process_file(
                file_path, video_tracks, audio_tracks, subtitle_tracks, config, database, dry_run
            )
