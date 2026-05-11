"""Unit tests for app.utils.retry.async_retry."""

from __future__ import annotations

import httpx
import pytest

from app.utils.retry import async_retry


class _Status404(Exception):
    status_code = 404


class _Status429(Exception):
    status_code = 429


class _Status503(Exception):
    status_code = 503


async def test_async_retry_skips_4xx() -> None:
    """4xx (non-429) errors must not be retried — they're permanent."""
    calls = 0

    @async_retry(max_retries=3, delay=0.0, exceptions=(Exception,))
    async def fn():
        nonlocal calls
        calls += 1
        raise _Status404("nope")

    with pytest.raises(_Status404):
        await fn()
    assert calls == 1


async def test_async_retry_retries_429() -> None:
    """429 is retryable because the server is asking us to back off briefly."""
    calls = 0

    @async_retry(max_retries=2, delay=0.0, exceptions=(Exception,))
    async def fn():
        nonlocal calls
        calls += 1
        raise _Status429("slow down")

    with pytest.raises(_Status429):
        await fn()
    assert calls == 3  # 1 + 2 retries


async def test_async_retry_retries_5xx() -> None:
    calls = 0

    @async_retry(max_retries=1, delay=0.0, exceptions=(Exception,))
    async def fn():
        nonlocal calls
        calls += 1
        raise _Status503("oops")

    with pytest.raises(_Status503):
        await fn()
    assert calls == 2


async def test_async_retry_retries_httpx_timeout() -> None:
    calls = 0

    @async_retry(
        max_retries=1,
        delay=0.0,
        exceptions=(httpx.TimeoutException,),
    )
    async def fn():
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("timeout")

    with pytest.raises(httpx.TimeoutException):
        await fn()
    assert calls == 2


async def test_async_retry_succeeds_after_transient_failure() -> None:
    calls = 0

    @async_retry(max_retries=2, delay=0.0, exceptions=(Exception,))
    async def fn():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _Status503("retry me")
        return "ok"

    assert await fn() == "ok"
    assert calls == 2
