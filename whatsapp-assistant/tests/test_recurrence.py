from __future__ import annotations

import pytest

from app.utils.recurrence import (
    RecurrenceError,
    build_recurrence_rule,
    describe_recurrence,
)

TZ = "Europe/Madrid"


def test_daily_with_count() -> None:
    assert (
        build_recurrence_rule({"frequency": "DAILY", "count": 3}, timezone=TZ)
        == "RRULE:FREQ=DAILY;COUNT=3"
    )


def test_interval_one_is_omitted() -> None:
    assert (
        build_recurrence_rule(
            {"frequency": "WEEKLY", "interval": 1, "count": 4}, timezone=TZ
        )
        == "RRULE:FREQ=WEEKLY;COUNT=4"
    )


def test_interval_greater_than_one_included() -> None:
    assert (
        build_recurrence_rule(
            {"frequency": "WEEKLY", "interval": 2, "count": 4}, timezone=TZ
        )
        == "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4"
    )


def test_by_day_normalized_and_ordered() -> None:
    rule = build_recurrence_rule(
        {"frequency": "WEEKLY", "by_day": ["fr", "mo", "we"]}, timezone=TZ
    )
    assert rule == "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_until_date_only_is_inclusive_end_of_day_utc() -> None:
    # 2026-06-30 local midnight in Madrid (UTC+2) -> end of day 23:59:59 local
    # -> 21:59:59 UTC.
    rule = build_recurrence_rule(
        {"frequency": "DAILY", "until": "2026-06-30"}, timezone=TZ
    )
    assert rule == "RRULE:FREQ=DAILY;UNTIL=20260630T215959Z"


def test_count_wins_over_until() -> None:
    rule = build_recurrence_rule(
        {"frequency": "DAILY", "count": 2, "until": "2026-12-31"}, timezone=TZ
    )
    assert rule == "RRULE:FREQ=DAILY;COUNT=2"


def test_invalid_frequency_raises() -> None:
    with pytest.raises(RecurrenceError):
        build_recurrence_rule({"frequency": "HOURLY"}, timezone=TZ)


def test_missing_frequency_raises() -> None:
    with pytest.raises(RecurrenceError):
        build_recurrence_rule({"count": 3}, timezone=TZ)


def test_count_too_large_raises() -> None:
    with pytest.raises(RecurrenceError):
        build_recurrence_rule({"frequency": "DAILY", "count": 5000}, timezone=TZ)


def test_zero_interval_raises() -> None:
    with pytest.raises(RecurrenceError):
        build_recurrence_rule(
            {"frequency": "DAILY", "interval": 0, "count": 3}, timezone=TZ
        )


def test_invalid_until_raises() -> None:
    with pytest.raises(RecurrenceError):
        build_recurrence_rule(
            {"frequency": "DAILY", "until": "next friday"}, timezone=TZ
        )


def test_describe_recurrence_daily_count() -> None:
    assert describe_recurrence({"frequency": "DAILY", "count": 3}) == "daily ×3"


def test_describe_recurrence_weekly_byday() -> None:
    assert (
        describe_recurrence({"frequency": "WEEKLY", "by_day": ["MO", "WE"]})
        == "weekly on Mon, Wed"
    )


def test_describe_recurrence_interval() -> None:
    assert (
        describe_recurrence({"frequency": "WEEKLY", "interval": 2})
        == "every 2 weeks"
    )
