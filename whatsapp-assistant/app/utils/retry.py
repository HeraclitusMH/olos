"""Retry decorator for async functions with bounded exponential backoff.

Used by services that make outbound HTTP calls (OpenAI, Google Calendar,
WhatsApp). The decorator deliberately:

* Retries only on the exception types passed in ``exceptions=...``.
* Treats HTTP 4xx as a permanent failure — except 429 — by *not* automatically
  retrying httpx 4xx response errors. Callers that need 429 handling do it
  explicitly (read ``Retry-After`` and retry once).
* Adds jitter to each sleep to avoid synchronized retries (thundering herd).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_non_retryable_4xx(exc: BaseException) -> bool:
    """True if ``exc`` represents a permanent (non-429) 4xx HTTP failure."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if 400 <= code < 500 and code != 429:
            return True
    return False


def async_retry(
    max_retries: int = 2,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    jitter: float = 0.25,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Async retry decorator with jitter.

    Parameters
    ----------
    max_retries:
        Number of *additional* attempts after the first one. ``max_retries=2``
        means up to 3 total calls.
    delay:
        Base delay in seconds before the first retry.
    backoff:
        Multiplicative factor applied to ``delay`` each retry.
    exceptions:
        Exception types that trigger a retry. Everything else raises immediately.
    jitter:
        Fraction of the computed sleep to randomize (uniform +/-).
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            attempt = 0
            current_delay = delay
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if _is_non_retryable_4xx(exc):
                        raise
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    sleep_for = current_delay * (
                        1 + random.uniform(-jitter, jitter)
                    )
                    sleep_for = max(sleep_for, 0.0)
                    logger.warning(
                        "Retrying %s after %.2fs (attempt %d/%d) due to %s: %s",
                        func.__name__,
                        sleep_for,
                        attempt,
                        max_retries,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(sleep_for)
                    current_delay *= backoff

        return wrapper

    return decorator
