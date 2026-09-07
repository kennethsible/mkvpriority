import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
from pathlib import Path
from typing import cast

import cron_descriptor
import pycountry
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from cron_descriptor import FormatError

import mkvpriority
from mkvpriority import __version__
from mkvpriority.main import setup_logging

entrypoint_logger = logging.getLogger('entrypoint')
processing_queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()


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
CRON_SCHEDULE = os.getenv('CRON_SCHEDULE')
CRON_TARGET_PATHS = shlex.split(os.getenv('CRON_TARGET_PATHS', ''))


def get_alpha_3_code(lang_name: str) -> str | None:
    try:
        lang = pycountry.languages.lookup(lang_name)
        return cast(str, lang.alpha_3)  # ISO 639-3
    except LookupError:
        return None


async def process_item(file_path: str, item_tags: str, orig_lang: str | None) -> None:
    if item_tags:
        file_path += f'::{re.split(r"[,;|]", item_tags)[0]}'
    try:
        argv = [*MKVPRIORITY_ARGS, file_path]
        await asyncio.to_thread(mkvpriority.main.main, argv, orig_lang)
    except Exception:
        entrypoint_logger.exception(f"error occurred: '{file_path}'")


async def queue_worker() -> None:
    while True:
        file_path, item_tags, item_id = await processing_queue.get()
        await process_item(file_path, item_tags, item_id)
        processing_queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    args = await request.json()
    file_path = args.get('file_path')
    item_tags = args.get('item_tags', '')
    orig_lang = get_alpha_3_code(args.get('orig_lang', ''))
    await processing_queue.put((file_path, item_tags, orig_lang))
    return web.json_response({'message': f"received '{file_path}'"})


async def create_runner(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application) -> None:
        app['worker'] = asyncio.create_task(queue_worker())

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
    scheduler.add_job(lambda: mkvpriority.main.main(cron_argv), trigger)
    scheduler.start()
    return scheduler


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
    setup_logging('/config/mkvpriority.log', max_bytes, max_files)

    entrypoint_logger.setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    async def run_all() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def handle_signal(sig_name: str) -> None:
            entrypoint_logger.info(f'received {sig_name} signal')
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal('SIGTERM'))
        loop.add_signal_handler(signal.SIGINT, lambda: handle_signal('SIGINT'))

        runner = scheduler = None
        try:
            if WEBHOOK_PORT:
                runner = await create_runner('0.0.0.0', WEBHOOK_PORT)
                entrypoint_logger.info(f'webhook listener started on 0.0.0.0:{WEBHOOK_PORT}')

            if expr := CRON_SCHEDULE:
                if expr.startswith('@'):
                    macro = expr
                    try:
                        expr = CRON_MACROS[macro]
                    except KeyError as e:
                        e.add_note(f"unsupported cron macro '{macro}'")
                        raise
                timezone = os.getenv('TZ', 'UTC')
                try:
                    expr_desc = cron_descriptor.get_description(expr)
                    expr_desc = expr_desc[0].lower() + expr_desc[1:]
                    scheduler = await create_scheduler(expr, timezone)
                except (FormatError, ValueError) as e:
                    e.add_note(f"unsupported cron expression '{expr}'")
                    raise
                entrypoint_logger.info(f'scheduled task to run {expr_desc} ({timezone})')

            await stop_event.wait()

        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            if runner:
                await runner.cleanup()

    entrypoint_logger.info(f'MKVPriority {__version__}')
    asyncio.run(run_all())


if __name__ == '__main__':
    if WEBHOOK_PORT or CRON_SCHEDULE:
        main()
    else:
        mkvpriority.main.main()
