"""Work that must happen only after a hook's response reached the host.

A mechanism whose state records a delivery (the loop guard's redirect) passes that write to
``after_emit``. Inside a hook (``collecting``) it is queued and the host adapter runs the queue
once the response is written (``run``); when the watchdog answered first (the response was the
no-op) the queue is dropped. Outside a hook (direct calls, tests, other hosts) it runs at once.
The worst case is then a delivered response without its record, never a record of a response
the host never received.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["after_emit", "collecting", "run"]

Task = Callable[[], None]
_QUEUE: ContextVar[list[Task] | None] = ContextVar("AFTER_EMIT", default=None)


def after_emit(fn: Task) -> None:
    """Queue ``fn`` for after the response (inside ``collecting``), else run it now."""
    queue = _QUEUE.get()
    if queue is None:
        fn()
    else:
        queue.append(fn)


@contextmanager
def collecting() -> Iterator[list[Task]]:
    """Collect ``after_emit`` work of the enclosed block into the yielded list."""
    queue: list[Task] = []
    token = _QUEUE.set(queue)
    try:
        yield queue
    finally:
        _QUEUE.reset(token)


def run(queue: list[Task], on_error: Callable[[BaseException], None]) -> None:
    """Run queued work in order; one failing task is reported and does not stop the rest."""
    for fn in queue:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - each task fails open on its own
            on_error(exc)
