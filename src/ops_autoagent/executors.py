from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Any, Callable


class CallerRunsBoundedExecutor(ThreadPoolExecutor):
    """Bounded Python equivalent of the legacy ThreadPoolExecutor + CallerRunsPolicy."""

    def __init__(self, max_workers: int, queue_size: int, thread_name_prefix: str = "ops-autoagent"):
        super().__init__(max_workers=max(1, max_workers), thread_name_prefix=thread_name_prefix)
        self._capacity = BoundedSemaphore(max(1, max_workers) + max(0, queue_size))

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        if not self._capacity.acquire(blocking=False):
            future: Future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _: self._capacity.release())
        return future
