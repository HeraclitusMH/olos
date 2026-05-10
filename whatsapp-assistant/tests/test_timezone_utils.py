from __future__ import annotations

import pytest

from app.utils.timezone import (
    InvalidDateTimeError,
    format_date_range,
    format_event_time,
    get_current_datetime,
    parse_iso_datetime,
)


def test_get_current_datetime_returns_zone_aware() -> None:
    now = get_current_datetime("Europe/Madrid")
    assert now.tzinfo is not None
    assert "Madrid" in str(now.tzinfo)


def test_get_current_datetime_falls_back_for_unknown_zone() -> None:
    now = get_current_datetime("Not/A_Zone")
    # Falls back to default (Europe/Madrid).
    assert now.tzinfo is not None


def test_parse_iso_accepts_offset_and_z_suffix() -> None:
    a = parse_iso_datetime("2026-05-13T16:00:00+02:00")
    b = parse_iso_datetime("2026-05-13T14:00:00Z")
    assert a.utcoffset().total_seconds() == 7200
    assert b.utcoffset().total_seconds() == 0


def test_parse_iso_rejects_garbage() -> None:
    with pytest.raises(InvalidDateTimeError):
        parse_iso_datetime("not a date")
    with pytest.raises(InvalidDateTimeError):
        parse_iso_datetime("")


def test_format_event_time_in_user_zone() -> None:
    formatted = format_event_time(
        "2026-05-13T16:00:00+02:00",
        "2026-05-13T17:00:00+02:00",
        "Europe/Madrid",
    )
    assert formatted == "Wed 13 May, 16:00-17:00"


def test_format_event_time_converts_utc_to_user_zone() -> None:
    formatted = format_event_time(
        "2026-05-13T14:00:00Z",
        "2026-05-13T15:00:00Z",
        "Europe/Madrid",
    )
    assert formatted == "Wed 13 May, 16:00-17:00"


def test_format_date_range_single_day() -> None:
    formatted = format_date_range(
        "2026-05-13T00:00:00+02:00",
        "2026-05-13T23:59:59+02:00",
        "Europe/Madrid",
    )
    assert formatted == "Wednesday 13 May"


def test_format_date_range_same_month() -> None:
    formatted = format_date_range(
        "2026-05-13T00:00:00+02:00",
        "2026-05-15T00:00:00+02:00",
        "Europe/Madrid",
    )
    assert formatted == "13-15 May"


def test_format_date_range_cross_month() -> None:
    formatted = format_date_range(
        "2026-05-30T00:00:00+02:00",
        "2026-06-02T00:00:00+02:00",
        "Europe/Madrid",
    )
    assert formatted == "30 May - 2 Jun"
