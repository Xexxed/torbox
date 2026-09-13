"""Serialization for browser-mutating operations.

Every structure scrape and link generation runs through one Playwright
worker. That worker serializes individual commands, but it does not protect
the page's *logical* state: two callers can interleave navigations and
corrupt each other's view (one expands a torrent while another expects the
dashboard, and both fail).

So every browser-mutating operation acquires one shared lock. The lock is
held only for that single operation -- never for the duration of a whole
download -- which means the Stream page can still resolve a link between a
download's per-file link generations instead of being blocked for the entire
transfer.
"""

import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_OP_TIMEOUT_S = 60.0


class BrowserBusyError(RuntimeError):
    """The browser lock could not be acquired within the timeout."""


@contextmanager
def browser_op(lock, timeout=DEFAULT_BROWSER_OP_TIMEOUT_S, what="use the browser"):
    """Hold `lock` for one atomic browser operation.

    `lock=None` is a no-op, so CLI paths that own the browser exclusively
    keep working without a shared lock.
    """
    if lock is None:
        yield
        return

    if timeout and timeout > 0:
        acquired = lock.acquire(timeout=timeout)
    else:
        acquired = lock.acquire()
    if not acquired:
        raise BrowserBusyError(
            f"Browser is busy: timed out after {timeout:g}s waiting to {what}."
        )
    try:
        yield
    finally:
        lock.release()
