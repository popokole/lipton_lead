"""Точка входа процесса воркера."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.runtime import Runtime
from app.workers.worker import Worker

logger = get_logger(__name__)


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings)

    worker = Worker(Runtime(settings))
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows не даёт SIGTERM
            loop.add_signal_handler(sig, worker.request_stop)

    await worker.run()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")


if __name__ == "__main__":
    main()
