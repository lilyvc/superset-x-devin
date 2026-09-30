"""Periodic workflow engine driver."""

import asyncio
import logging

logger = logging.getLogger("poller")


async def run_poller(engine, settings) -> None:
    while True:
        try:
            await engine.tick()
        except Exception:
            logger.exception("poll iteration failed")
        await asyncio.sleep(settings.github_poll_interval_seconds)
