"""Tests for domain calculations and business rules R1 through R8."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.main import (
    IST,
    UTC,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    count_working_days,
    dt_to_epoch_ms,
    epoch_ms_to_dt,
    get_attendance_date,
    get_shift_datetimes,
    round_half_up,
    truncate_to_seconds,
)


def test_truncate_to_seconds():
    dt = datetime(2026, 7, 6, 9, 40, 0, 950000, tzinfo=UTC)
    truncated = truncate_to_seconds(dt)
    assert truncated.microsecond == 0
    assert truncated.second == 0


def test_epoch_ms_conversions():
    ms = 1783312500000
    dt = epoch_ms_to_dt(ms)
    assert dt.tzinfo == UTC
    assert dt.microsecond == 0
    assert dt_to_epoch_ms(dt) == ms
    assert dt_to_epoch_ms(None) is None


def test_round_half_up():
    # Built-in round(2.675, 2) often returns 2.67 due to floating point
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(2.125, 2) == 2.13
    assert round_half_up(2.124, 2) == 2.12
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234


def test_r1_attendance_date_standard_shift():
    # Normal shift: 09:30 to 18:30 IST
    # 2026-07-06 09:28 IST is 2026-07-06 03:58 UTC
    punch_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
    att_date = get_attendance_date(punch_in, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift: 22:00 to 06:00 IST
    # Case A: Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    p1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=UTC)
    assert get_attendance_date(p1, "22:00", "06:00") == "2026-07-06"

    # Case B: Punch in at 01:30 IST on 2026-07-07 (20:00 UTC on July 6)
    # Since 01:30 < 06:00 (earlier than shift_end), belongs to shift that started previous day!
    p2 = datetime(2026, 7, 6, 20, 0, 0, tzinfo=UTC) # 01:30 IST July 7
    assert get_attendance_date(p2, "22:00", "06:00") == "2026-07-06"

    # Case C: Punch in at 05:59 IST on 2026-07-07 (< 06:00) -> belongs to July 6
    p3 = datetime(2026, 7, 7, 0, 29, 0, tzinfo=UTC) # 05:59 IST July 7
    assert get_attendance_date(p3, "22:00", "06:00") == "2026-07-06"

    # Case D: Punch in at 06:00 IST on 2026-07-07 (not earlier than shift_end) -> belongs to July 7
    p4 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC) # 06:00 IST July 7
    assert get_attendance_date(p4, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes_boundaries():
    # Shift start 09:30 IST on 2026-07-06 -> 04:00 UTC
    start_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)

    # 1. Early punch-in (09:28:00 IST = 03:58:00 UTC) -> 0
    p_early = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
    assert compute_late_minutes(p_early, start_utc) == 0

    # 2. Exactly at 10-minute grace boundary: 09:40:00 IST (= 04:10:00 UTC) -> 0
    p_exact_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC)
    assert compute_late_minutes(p_exact_grace, start_utc) == 0

    # 3. Strictly more than grace: 09:40:01 IST (= 04:10:01 UTC) -> 10 minutes
    p_late_1 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC)
    assert compute_late_minutes(p_late_1, start_utc) == 10

    # 4. 10:15:59 IST (= 04:45:59 UTC) -> 45 minutes
    p_late_45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC)
    assert compute_late_minutes(p_late_45, start_utc) == 45

    # 5. Sample 2 from spec: 10:05:00 IST (= 04:35:00 UTC) -> 35 minutes
    p_late_35 = datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC)
    assert compute_late_minutes(p_late_35, start_utc) == 35


def test_r3_overtime_minutes():
    # Shift end 18:30 IST on 2026-07-06 -> 13:00 UTC
    end_utc = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)

    # 1. Under 30 minutes: 18:35 IST (13:05 UTC) = 5 min -> 0
    p1 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC)
    assert compute_overtime_minutes(p1, end_utc) == 0

    # 2. Exactly 29 min 59 sec -> 0
    p2 = datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC)
    assert compute_overtime_minutes(p2, end_utc) == 0

    # 3. Exactly 30 minutes: 19:00 IST (13:30 UTC) -> 30
    p3 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=UTC)
    assert compute_overtime_minutes(p3, end_utc) == 30

    # 4. 40 minutes: 19:10 IST (13:40 UTC) -> 40
    p4 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=UTC)
    assert compute_overtime_minutes(p4, end_utc) == 40


def test_r4_work_hours_half_up():
    # Sample 1: 03:58:00 UTC to 13:05:00 UTC = 32820 seconds / 3600 = 9.1166... -> 9.12
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
    p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC)
    assert compute_work_hours(p_in, p_out) == 9.12

    # Sample 4: 3.5 hours exact
    p_in4 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC)
    p_out4 = datetime(2026, 7, 7, 7, 30, 0, tzinfo=UTC)
    assert compute_work_hours(p_in4, p_out4) == 3.5

    # Test half-up rounding boundary
    # 9009 seconds / 3600 = 2.5025 -> 2.50
    # 9018 seconds / 3600 = 2.505 -> 2.51
    t0 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
    t_half = t0 + timedelta(seconds=9018)
    assert compute_work_hours(t0, t_half) == 2.51


def test_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(9.12) is False
    assert compute_half_day(None) is False


def test_r7_working_days():
    # July 2026: July 1 is Wednesday, July 31 is Friday.
    # Total weekdays in July 2026: 23 weekdays.
    assert count_working_days("2026-07-01", "2026-07-31") == 23

    # Mid-month joiner: joined on 2026-07-15 (Wednesday)
    # Weekdays from July 15 to July 31: 13 weekdays.
    assert count_working_days("2026-07-15", "2026-07-31") == 13

    # Weekend only: 2026-07-04 (Sat) to 2026-07-05 (Sun) -> 0
    assert count_working_days("2026-07-04", "2026-07-05") == 0
