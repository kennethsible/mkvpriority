import argparse
import glob
import logging
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, ConfigError
from .database import Database
from .extension import Extension, load_extension
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


class StreamFilter(logging.Filter):
    def __init__(self, stream_level: int = logging.INFO):
        super().__init__()
        self.stream_level = stream_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == 'mkvpriority':
            return record.levelno >= self.stream_level
        return True


stream_filter = StreamFilter()


def configure_logging(
    log_path_str: str | None = None, max_bytes: int = 0, max_files: int = 1
) -> None:
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.addFilter(stream_filter)

    handlers: list[logging.Handler] = [stream_handler]
    if log_path_str:
        file_handler = RotatingFileHandler(log_path_str, maxBytes=max_bytes, backupCount=max_files)
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)

    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s', handlers=handlers
    )


def collect_file_paths(path_str: str, dry_run: bool = False) -> list[Path]:
    log_prefix = '[DRY RUN] ' if dry_run else ''
    escaped_pattern = path_str.replace('[', '[[]')
    if not (matched_paths := sorted(glob.glob(escaped_pattern, recursive=True))):
        mkvpriority_logger.warning(log_prefix + f"skipping (not found) '{path_str}'")
        return []

    file_paths: list[Path] = []
    for matched_path in matched_paths:
        if (path := Path(matched_path)).is_dir():
            mkvpriority_logger.info(log_prefix + f"scanning '{path}'")
            file_paths.extend(sorted(path.rglob('*.mkv')))
        elif path.is_file() and path.suffix.lower() == '.mkv':
            file_paths.append(path)
    return list(dict.fromkeys(file_paths))


def get_archive_status(file_path: Path, database: Database, dry_run: bool = False) -> bool:
    archive_record = database.select_by_path(file_path)
    file_mtime = int(file_path.stat().st_mtime)
    if archive_record and archive_record.file_mtime == file_mtime:
        return True

    segment_uid, *_ = extract_tracks(file_path)
    if not segment_uid:
        segment_uid = ensure_segment_uid(file_path, dry_run)
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
    parser.add_argument(
        '-i',
        '--include',
        action='append',
        default=[],
        metavar='MODULE_NAME',
        help='include extension module',
    )
    parser.add_argument(
        '-o',
        '--override',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='override config settings',
    )
    parser.add_argument('-v', '--verbose', action='store_true', help='inspect track metadata')
    parser.add_argument('-x', '--debug', action='store_true', help='print mkvtoolnix output')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress normal logging')
    parser.add_argument('-p', '--prune', action='store_true', help='prune database entries')
    parser.add_argument('-n', '--dry-run', action='store_true', help='simulate track changes')
    parser.add_argument('-r', '--restore', action='store_true', help='restore original flags')
    parser.add_argument(
        'paths', nargs='*', metavar='PATH[::TAG]', help='files, directories, or patterns'
    )
    return parser


def main(argv: list[str] | None = None, orig_lang: str | None = None) -> None:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        verify_mkvtoolnix_install()
    except MissingCommandError as e:
        parser.error(str(e))

    main_level = logging.DEBUG if args.verbose else logging.INFO
    tool_level = logging.DEBUG if args.debug else logging.INFO
    if args.quiet:
        main_level = tool_level = logging.ERROR
    log_prefix = '[DRY RUN] ' if args.dry_run else ''
    configure_logging()

    stream_filter.stream_level = main_level
    mkvpriority_logger.setLevel(logging.DEBUG)
    for logger in (mkvpropedit_logger, mkvextract_logger, mkvmerge_logger):
        logger.setLevel(tool_level)

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
            database = Database(args.archive, args.dry_run)
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
                mkvpriority_logger.warning(log_prefix + f"skipping (unmatched config) '{path_str}'")
                continue

            for file_path in collect_file_paths(path_str, args.dry_run):
                if database is not None:
                    is_archived = get_archive_status(file_path, database, args.dry_run)
                    if not args.restore and is_archived and not args.override:
                        mkvpriority_logger.info(log_prefix + f"skipping (archived) '{file_path}'")
                        continue
                    if args.restore and not is_archived:
                        mkvpriority_logger.info(
                            log_prefix + f"skipping (not archived) '{file_path}'"
                        )
                        continue

                if args.restore and database is not None:
                    mkvpriority_logger.info(log_prefix + f"restoring '{file_path}'")
                    restore_file(file_path, database, args.dry_run)
                elif matched_config is not None:
                    toml_path, toml_label = matched_config.toml_path, matched_config.toml_label
                    config_tag = f'::{toml_label}' if toml_label != 'untagged' else ''
                    mkvpriority_logger.info(log_prefix + f"processing '{file_path}'")
                    mkvpriority_logger.info(log_prefix + f"using config '{toml_path}{config_tag}'")
                    process_file(file_path, matched_config, database, extensions, args.dry_run)

    finally:
        if database is not None:
            database.close()


if __name__ == '__main__':
    main()
