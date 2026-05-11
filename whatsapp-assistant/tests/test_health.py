from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient


async def test_health_returns_overall_status(client: AsyncClient) -> None:
    """The endpoint always returns the same envelope shape, even when the DB is down."""
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"healthy", "degraded", "unhealthy"}
    assert "timestamp" in body
    assert "checks" in body
    assert "database" in body["checks"]
    assert "last_message_processed" in body["checks"]


async def test_health_timestamp_is_iso_utc(client: AsyncClient) -> None:
    response = await client.get("/health")
    timestamp = response.json()["timestamp"]

    parsed = datetime.fromisoformat(timestamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


async def test_health_reports_unhealthy_when_db_probe_fails(
    client: AsyncClient,
) -> None:
    """If SELECT 1 raises, overall status must be ``unhealthy``."""

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _stmt):
            raise RuntimeError("database connection refused")

    with patch("app.main.AsyncSessionLocal", lambda: _BrokenSession()):
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"]["status"] == "error"


async def test_health_reports_degraded_when_pipeline_stuck(
    client: AsyncClient,
) -> None:
    """DB is fine but no message processed in 6 min while unprocessed rows exist."""
    from datetime import UTC, datetime, timedelta

    last_processed = datetime.now(UTC) - timedelta(minutes=6)
    unprocessed_row = MagicMock()

    class _StubSession:
        def __init__(self) -> None:
            self._calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _stmt):
            self._calls += 1
            result = MagicMock()
            if self._calls == 1:
                # SELECT 1 health probe
                return result
            if self._calls == 2:
                # last processed timestamp
                result.scalar_one_or_none.return_value = last_processed
                return result
            # unprocessed-row probe
            result.scalar_one_or_none.return_value = unprocessed_row
            return result

    # Use a single stub across the request to keep call counting consistent.
    stub = _StubSession()
    with patch("app.main.AsyncSessionLocal", lambda: stub):
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["last_message_processed"]["status"] == "degraded"


