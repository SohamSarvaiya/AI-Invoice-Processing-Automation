import logging
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import TransientError

T = TypeVar("T")
log = logging.getLogger(__name__)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    retry_on: tuple[type[Exception], ...] = (TransientError,),
    on_retry: Callable[[int, Exception], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn(); on a retryable error wait base_delay * 2^(n-1) and try again."""
    attempts = max(1, attempts)
    for n in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if n == attempts:
                raise
            if on_retry:
                on_retry(n, exc)
            log.warning("attempt %d/%d failed: %s", n, attempts, exc)
            sleep(base_delay * (2 ** (n - 1)))
    raise AssertionError("unreachable")
