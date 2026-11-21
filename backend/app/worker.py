"""Standalone durable worker process for multi-worker deployments."""
from __future__ import annotations

import asyncio
import logging

from . import db
from .config import validate_production_settings
from .orchestrator import WorkerManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    validate_production_settings()
    db.init_db()
    manager = WorkerManager(poll_interval=1.0)
    manager.start()
    logging.info("Started standalone durable worker %s", manager.worker_id)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
