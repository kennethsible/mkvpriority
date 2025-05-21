import argparse
import asyncio
import logging
import os
import signal
import sys

from aiohttp import web

import mkvpriority

__version__ = 'v1.1.0'

logger = logging.getLogger('entrypoint')
processing_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

CUSTOM_SCRIPT = os.getenv('CUSTOM_SCRIPT', 'False').lower() in ('true', '1', 't')
WEBHOOK_RECEIVER = os.getenv('WEBHOOK_RECEIVER', 'False').lower() in ('true', '1', 't')
MKVPRIORITY_ARGS = os.getenv('MKVPRIORITY_ARGS', '')


async def queue_worker():
    while True:
        file_path, item_tags = await processing_queue.get()
        if item_tags:
            file_path += f'::{item_tags.split(",")[0]}'
        logger.info(file_path)
        try:
            argv = [*MKVPRIORITY_ARGS.split(), file_path]
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: mkvpriority.mkvpriority(argv)
            )
        except Exception:
            logger.exception(f"error occurred; skipping file: '{file_path}'")
        finally:
            processing_queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    args = await request.json()
    file_path = args.get('file_path')
    item_tags = args.get('item_tags', '')
    await processing_queue.put((file_path, item_tags))
    return web.json_response({'message': f"recieved '{file_path}'"})


async def init_api(host: str, port: int):
    app = web.Application()
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application):
        app['worker'] = asyncio.create_task(queue_worker())

    async def on_cleanup(app: web.Application):
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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: stop_event.set())
    loop.add_signal_handler(signal.SIGINT, lambda: stop_event.set())

    await stop_event.wait()
    await runner.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    logger.info(f'MKVPriority {__version__}')
    logger.info(f'running on http://{args.host}:{args.port}')

    asyncio.run(init_api(args.host, args.port))


if __name__ == '__main__':
    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s',
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    if WEBHOOK_RECEIVER:
        main()
    elif CUSTOM_SCRIPT:
        logger.warning('CUSTOM_SCRIPT is deprecated; use WEBHOOK_RECEIVER instead')
        main()
    else:
        mkvpriority.mkvpriority()
