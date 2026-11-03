"""Fire-and-forget background tasks that cannot be garbage-collected or crash a request."""
import asyncio
import logging

log = logging.getLogger("coffee")
_tasks: set = set()


async def _safe(coro):
    try:
        await coro
    except Exception as exc:
        log.error("background task failed: %s", type(exc).__name__)


def spawn(coro) -> None:
    task = asyncio.ensure_future(_safe(coro))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
