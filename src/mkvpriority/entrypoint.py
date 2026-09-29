import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
from pathlib import Path

import cron_descriptor
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from cron_descriptor import FormatError

import mkvpriority.main
from mkvpriority import __version__

from .types import resolve_language

entrypoint_logger = logging.getLogger('entrypoint')

type ItemPayload = tuple[str, str, str | None]
type ProcessingQueue = asyncio.Queue[ItemPayload]

MKVPRIORITY_ARGS = ['-c', '/config/config.toml'] + shlex.split(os.getenv('MKVPRIORITY_ARGS', ''))
LOG_MAX_BYTES, LOG_MAX_FILES = os.getenv('LOG_MAX_BYTES'), os.getenv('LOG_MAX_FILES')

WEBHOOK_PORT_STR = os.getenv('WEBHOOK_PORT')
WEBHOOK_PORT = int(WEBHOOK_PORT_STR) if WEBHOOK_PORT_STR else None

CRON_MACROS = {
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *',
    '@weekly': '0 0 * * 0',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@hourly': '0 * * * *',
}
CRON_TIMEZONE = os.getenv('TZ', 'UTC')
CRON_SCHEDULE = os.getenv('CRON_SCHEDULE')
CRON_TARGET_PATHS = shlex.split(os.getenv('CRON_TARGET_PATHS', ''))


async def process_item(file_path: str, item_tags: str, orig_lang: str | None) -> None:
    if item_tags:
        file_path += f'::{re.split(r"[,;|]", item_tags)[0]}'
    try:
        argv = [*MKVPRIORITY_ARGS, file_path]
        await asyncio.to_thread(mkvpriority.main.main, argv, orig_lang)
    except Exception:
        entrypoint_logger.exception(f"error occurred while processing '{file_path}'")


async def queue_worker(queue: ProcessingQueue) -> None:
    while True:
        file_path, item_tags, item_id = await queue.get()
        try:
            await process_item(file_path, item_tags, item_id)
        finally:
            queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return web.json_response({'error': 'expected JSON object'}, status=400)
    except (json.JSONDecodeError, web.HTTPBadRequest):
        return web.json_response({'error': 'invalid JSON payload'}, status=400)

    file_path = payload.get('file_path')
    if not file_path:
        return web.json_response({'error': "missing 'file_path'"}, status=400)

    item_tags = payload.get('item_tags', '')
    if orig_lang := payload.get('orig_lang'):
        orig_lang = resolve_language(orig_lang, target_format='alpha_3')

    queue: ProcessingQueue = request.app['processing_queue']
    await queue.put((file_path, item_tags, orig_lang))
    return web.json_response({'message': f"received '{file_path}'"})


async def create_runner(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    queue: ProcessingQueue = asyncio.Queue()
    app['processing_queue'] = queue
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application) -> None:
        app['worker'] = asyncio.create_task(queue_worker(queue))

    async def on_cleanup(app: web.Application) -> None:
        app['worker'].cancel()
        try:
            await app['worker']
        except asyncio.CancelledError:
            pass

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


async def create_scheduler(expr: str, timezone: str | None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger.from_crontab(expr, timezone)
    cron_argv = MKVPRIORITY_ARGS + CRON_TARGET_PATHS
    scheduler.add_job(
        lambda: asyncio.create_task(asyncio.to_thread(mkvpriority.main.main, cron_argv)), trigger
    )
    scheduler.start()
    return scheduler


def migrate_database(config_file: Path) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-c', '--config')
    parser.add_argument('-a', '--archive')
    parser.add_argument('-x', '--debug', action='store_true')
    parser.add_argument('-n', '--dry-run', action='store_true')
    args, _ = parser.parse_known_args(MKVPRIORITY_ARGS)

    if args.archive:
        arguments = ['-a', args.archive]
        if args.config:
            arguments.extend(['-c', args.config])
        else:
            arguments.extend(['-c', str(config_file)])
        if args.debug:
            arguments.append('-x')
        if args.dry_run:
            arguments.append('-n')

        mkvpriority.main.main(arguments)


async def run_daemon() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_signal(sig_name: str) -> None:
        entrypoint_logger.info(f'received {sig_name} signal')
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal('SIGTERM'))
    loop.add_signal_handler(signal.SIGINT, lambda: handle_signal('SIGINT'))

    runner: web.AppRunner | None = None
    scheduler: AsyncIOScheduler | None = None
    try:
        if WEBHOOK_PORT:
            runner = await create_runner('0.0.0.0', WEBHOOK_PORT)
            entrypoint_logger.info(f'webhook listener started on 0.0.0.0:{WEBHOOK_PORT}')

        if cron_expr := CRON_SCHEDULE:
            if cron_expr.startswith('@'):
                try:
                    cron_expr = CRON_MACROS[cron_expr]
                except KeyError as e:
                    e.add_note(f"unsupported cron macro '{cron_expr}'")
                    raise
            try:
                expr_desc = cron_descriptor.get_description(cron_expr)
                expr_desc = expr_desc[0].lower() + expr_desc[1:]
                scheduler = await create_scheduler(cron_expr, CRON_TIMEZONE)
            except (FormatError, ValueError) as e:
                e.add_note(f"unsupported cron expression '{cron_expr}'")
                raise
            entrypoint_logger.info(f'scheduled task to run {expr_desc} ({CRON_TIMEZONE})')

        await stop_event.wait()

    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if runner:
            await runner.cleanup()


def main() -> None:
    config_dir = Path('/config')
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / 'config.toml'
        if not config_file.is_file():
            shutil.copy2('config.toml', config_file)

        extensions_dir = Path('/config/extensions')
        extensions_dir.mkdir(parents=True, exist_ok=True)
        init_file = extensions_dir / '__init__.py'
        init_file.touch(exist_ok=True)

        script_file = config_dir / 'mkvpriority.sh'
        if not script_file.is_file():
            shutil.copy2('mkvpriority.sh', script_file)

        database_file = config_dir / 'archive.db'
        database_file.touch(exist_ok=True)
    except PermissionError:
        entrypoint_logger.warning(f'recreate {config_dir} with correct PUID/PGID ownership')
        raise

    max_bytes = 5242880 if LOG_MAX_BYTES is None else int(LOG_MAX_BYTES)
    max_files = 3 if LOG_MAX_FILES is None else int(LOG_MAX_FILES)
    mkvpriority.main.configure_logging('/config/mkvpriority.log', max_bytes, max_files)

    entrypoint_logger.setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    entrypoint_logger.info(f'MKVPriority {__version__}')
    migrate_database(config_file)

    asyncio.run(run_daemon())


if __name__ == '__main__':
    if WEBHOOK_PORT or CRON_SCHEDULE:
        main()
    else:
        mkvpriority.main.main()
