"""Focused unit tests for complex business rules, competition ranking ties,
overnight shifts, zero-attendance employees, and decimal half-up rounding.
"""
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.main import (
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    count_working_days,
    format_history_entry,
    get_attendance_date,
    get_shift_datetimes,
    round_half_up,
)


def test_standard_competition_ranking_logic():
    """Verify standard competition ranking (1, 2, 2, 4) semantics."""
    # Suppose 4 employees have late minutes: 120, 80, 80, 50
    # In standard competition ranking:
    # 120 -> Rank 1
    # 80  -> Rank 2
    # 80  -> Rank 2 (tie)
    # 50  -> Rank 4 (rank 3 is skipped)
    scores = [120, 80, 80, 50]
    ranks = []
    current_rank = 1
    for i, score in enumerate(scores):
        if i > 0 and score < scores[i - 1]:
            current_rank = i + 1
        ranks.append(current_rank)

    assert ranks == [1, 2, 2, 4]

    # If limit is 2, items with rank <= 2 must be returned (including BOTH tied at rank 2)
    filtered = [(score, r) for score, r in zip(scores, ranks) if r <= 2]
    assert len(filtered) == 3
    assert filtered == [(120, 1), (80, 2), (80, 2)]


def test_overnight_shift_date_resolution_and_overtime():
    """Verify overnight shift R1 and R3 rules."""
    shift_start = "22:00"
    shift_end = "06:00"

    # Case 1: Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    p1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(p1, shift_start, shift_end) == "2026-07-06"

    # Case 2: Punch in at 01:30 IST on 2026-07-07 (20:00 UTC on July 6)
    # Since 01:30 < 06:00, belongs to the shift that started on 2026-07-06!
    p2 = datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc)
    assert get_attendance_date(p2, shift_start, shift_end) == "2026-07-06"

    # Shift start and end UTC instants for attendance date 2026-07-06
    start_utc, end_utc = get_shift_datetimes("2026-07-06", shift_start, shift_end)
    # Shift start: 2026-07-06 22:00 IST -> 16:30 UTC
    assert start_utc == datetime(2026, 7, 6, 16, 30, 0, tzinfo=timezone.utc)
    # Shift end: 2026-07-07 06:00 IST -> 00:30 UTC on July 7
    assert end_utc == datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)

    # Overtime Case A: Punch out at 06:40 IST on July 7 (01:10 UTC) -> 40 min overtime (>= 30)
    p_out_ot = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(p_out_ot, end_utc) == 40

    # Overtime Case B: Punch out at 06:25 IST on July 7 (00:55 UTC) -> 25 min (< 30) -> 0
    p_out_no_ot = datetime(2026, 7, 7, 0, 55, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(p_out_no_ot, end_utc) == 0


def test_half_up_rounding_boundaries():
    """Verify Decimal half-up rounding (R4, R8)."""
    # 2 decimals
    assert round_half_up(4.495, 2) == 4.50
    assert round_half_up(4.494, 2) == 4.49
    assert round_half_up(9.116, 2) == 9.12
    assert round_half_up(2.505, 2) == 2.51

    # 4 decimals (for rates)
    assert round_half_up(0.33335, 4) == 0.3334
    assert round_half_up(0.33334, 4) == 0.3333


def test_half_day_presence_boundary():
    """Verify half-day rule R5 (< 4.50 hours)."""
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(5.00) is False
    assert compute_half_day(None) is False


def test_format_history_entry_converts_nested_dates():
    """Verify format_history_entry transforms BSON dates to integer epoch ms."""
    dt_at = datetime(2026, 7, 8, 5, 0, 0, tzinfo=timezone.utc)
    dt_from = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    dt_to = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)

    raw_entry = {
        "at": dt_at,
        "by": "hr.admin",
        "reason": "biometric glitch",
        "changes": {
            "punch_in": {"from": dt_from, "to": dt_to},
            "late_minutes": {"from": 35, "to": 0},
            "work_hours": {"from": 8.42, "to": 9.03},
        },
    }

    formatted = format_history_entry(raw_entry)
    assert formatted["by"] == "hr.admin"
    assert formatted["reason"] == "biometric glitch"
    assert formatted["at"] == int(dt_at.timestamp() * 1000)
    assert formatted["changes"]["punch_in"]["from"] == int(dt_from.timestamp() * 1000)
    assert formatted["changes"]["punch_in"]["to"] == int(dt_to.timestamp() * 1000)
    assert formatted["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert formatted["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
