import argparse
import glob
import logging
import sqlite3
from pathlib import Path

from mkvpriority import __version__

from .config import Config, ConfigError
from .database import Database
from .extension import Extension, load_extension
from .logger import VERBOSE, configure_logging, set_log_levels
from .mkvtoolnix import (
    MissingCommandError,
    ensure_segment_uid,
    extract_tracks,
    verify_mkvtoolnix_install,
)
from .processor import process_file, restore_file

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvextract_logger = logging.getLogger('mkvextract')
mkvmerge_logger = logging.getLogger('mkvmerge')


def collect_file_paths(path_str: str) -> list[Path]:
    escaped_pattern = path_str.replace('[', '[[]')
    if not (matched_paths := sorted(glob.glob(escaped_pattern, recursive=True))):
        return []

    file_paths: list[Path] = []
    for matched_path in matched_paths:
        if (path := Path(matched_path)).is_dir():
            mkvpriority_logger.info(f"scanning '{path}'")
            file_paths.extend(sorted(path.rglob('*.mkv')))
        elif path.is_file() and path.suffix.lower() == '.mkv':
            file_paths.append(path)
    return list(dict.fromkeys(file_paths))


def get_archive_status(file_path: Path, database: Database, *, simulate: bool = False) -> bool:
    archive_record = database.select_by_path(file_path)
    file_mtime = int(file_path.stat().st_mtime)
    if archive_record and archive_record.file_mtime == file_mtime:
        return True

    segment_uid, *_ = extract_tracks(file_path)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, simulate=simulate)
    if archive_record and archive_record.segment_uid != segment_uid:
        database.delete(archive_record.segment_uid)

    archive_record = database.select_by_uid(segment_uid)
    if archive_record and archive_record.file_mtime == file_mtime:
        database.insert(segment_uid, file_path, [])
        return True
    return False


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='append', default=[], metavar='TOML_PATH[::TAG]')
    parser.add_argument('-a', '--archive', metavar='DB_PATH')
    parser.add_argument('-i', '--include', action='append', default=[], metavar='EXTENSION')
    parser.add_argument('-o', '--override', action='append', default=[], metavar='KEY=VALUE')
    parser.add_argument(
        '-p', '--prune', action='store_true', help='prune database entries for missing files'
    )
    parser.add_argument(
        '-r', '--restore', action='store_true', help='restore original track flags from database'
    )
    parser.add_argument(
        '-s', '--simulate', action='store_true', help='process files without modifying containers'
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true', help='simulate execution without writing to disk'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true', help='display track metadata and computed scores'
    )
    parser.add_argument(
        '-d', '--debug', action='store_true', help='print mkvtoolnix output and debug messages'
    )
    parser.add_argument(
        '-q', '--quiet', action='store_true', help='suppress all log messages to the console'
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}',
        help='show program version and exit',
    )
    parser.add_argument(
        'paths', nargs='*', metavar='PATH[::TAG]', help='files, directories, or glob patterns'
    )
    return parser


def main(argv: list[str] | None = None, orig_lang: str | None = None) -> None:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        verify_mkvtoolnix_install()
    except MissingCommandError as e:
        parser.error(str(e))

    configure_logging()
    dry_run = args.dry_run
    simulate = args.simulate or dry_run
    if args.quiet:
        main_level = tool_level = logging.ERROR
    else:
        main_level = VERBOSE if args.verbose else logging.INFO
        tool_level = logging.DEBUG if args.debug else logging.INFO
    set_log_levels(main_level, tool_level, simulate=simulate, dry_run=dry_run)

    configs: dict[str, Config] = {}
    for toml_path in args.config:
        label = 'untagged'
        if '::' in toml_path:
            toml_path, label = toml_path.rsplit('::', 1)
        try:
            config = Config.from_file(Path(toml_path), label, args.override)
        except (ConfigError, ValueError) as e:
            parser.error(str(e))
        if orig_lang and 'org' in config.audio_group.languages:
            config.audio_group.languages[orig_lang] = config.audio_group.languages['org']
        if orig_lang and 'org' in config.subtitle_group.languages:
            config.subtitle_group.languages[orig_lang] = config.subtitle_group.languages['org']
        configs[label] = config
    if not configs and not (args.prune or args.restore):
        parser.error('cannot process file(s) without --config')

    database: Database | None = None
    if args.archive:
        try:
            database = Database(args.archive, simulate=simulate)
        except (sqlite3.IntegrityError, RuntimeError):
            mkvpriority_logger.error(f"error occurred while migrating '{args.archive}'")
            raise

    extensions: list[Extension] = []
    for module_name in args.include:
        if extension := load_extension(module_name):
            extension.extension_logger.setLevel(tool_level)
            extensions.append(extension)

    try:
        if args.prune:
            if database is None:
                parser.error('cannot use --prune without --archive')
            else:
                database.prune()
        if args.restore and database is None:
            parser.error('cannot use --restore without --archive')

        for path_str in args.paths:
            toml_label = 'untagged'
            if '::' in path_str:
                path_str, toml_label = path_str.rsplit('::', 1)
            matched_config = configs.get(toml_label) or configs.get('untagged')
            if not args.restore and matched_config is None:
                tag_suffix = f' [{toml_label}]' if toml_label != 'untagged' else ''
                mkvpriority_logger.warning(f"skipping (missing config) '{path_str}'{tag_suffix}")
                continue

            for file_path in collect_file_paths(path_str):
                if database is not None:
                    is_archived = get_archive_status(file_path, database, simulate=simulate)
                    if not args.restore and is_archived and not args.override:
                        mkvpriority_logger.info(f"skipping (archived) '{file_path}'")
                        continue
                    if args.restore and not is_archived:
                        continue

                if args.restore and database is not None:
                    mkvpriority_logger.info(f"restoring '{file_path}'")
                    restore_file(file_path, database, simulate=simulate)
                elif matched_config is not None:
                    toml_label = matched_config.toml_label
                    tag_suffix = f' [{toml_label}]' if toml_label != 'untagged' else ''
                    mkvpriority_logger.info(f"processing '{file_path}'{tag_suffix}")
                    process_file(
                        file_path,
                        matched_config,
                        database,
                        extensions,
                        simulate=simulate,
                        dry_run=dry_run,
                    )

    finally:
        if database is not None:
            database.close()


if __name__ == '__main__':
    main()
