"""Integration tests against genuine MongoDB instance (Local or MongoDB Atlas).

Safely loads connection credentials from a local, Git-ignored .env file.
Uses a dedicated test database (default: 'attendance_test_db') and isolated test
identifiers (prefixed with 'EMP900') to prevent deleting or altering unrelated data.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, create_indexes, get_db

# Load environment variables safely without printing credentials
load_dotenv()

TEST_MONGO_URI = os.getenv("TEST_MONGO_URI", os.getenv("MONGO_URI", "mongodb://localhost:27017"))
TEST_MONGO_DB = os.getenv("TEST_MONGO_DB", "attendance_test_db")

# Dedicated client with 5s timeout for remote Atlas clusters
test_client: MongoClient = MongoClient(TEST_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
test_db = test_client[TEST_MONGO_DB]


def is_mongo_alive() -> bool:
    try:
        test_client.admin.command("ping")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not is_mongo_alive(),
    reason="MongoDB instance is not accessible at MONGO_URI / TEST_MONGO_URI"
)

http_client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def setup_test_database():
    """Isolate tests to dedicated test database and clean test identifiers."""
    if not is_mongo_alive():
        yield
        return

    # Override dependency to use dedicated test database
    app.dependency_overrides[get_db] = lambda: test_db

    # Create all required indexes idempotently on test database
    create_indexes(test_db)

    # Clean only isolated test data
    test_db.employees.delete_many({"emp_code": {"$regex": "^EMP900"}})
    test_db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP900"}})

    yield

    # Teardown: clean only isolated test data
    test_db.employees.delete_many({"emp_code": {"$regex": "^EMP900"}})
    test_db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP900"}})
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 1. Health Probe
# --------------------------------------------------------------------------- #
def test_real_health():
    resp = http_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employee CRUD & Database Uniqueness
# --------------------------------------------------------------------------- #
def test_real_employee_lifecycle():
    emp1 = {
        "emp_code": "EMP9001",
        "name": "Integration Alice",
        "email": "alice.int@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    emp2 = {
        "emp_code": "EMP9002",
        "name": "Integration Bob",
        "email": "bob.int@example.com",
        "department": "Engineering",
        "shift_start": "22:00",
        "shift_end": "06:00",  # Overnight shift
        "joined_on": "2026-07-05",
    }
    emp3 = {
        "emp_code": "EMP9003",
        "name": "Integration Charlie",
        "email": "charlie.int@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",  # Mid-month joiner, zero logs
    }

    # Create employees
    for emp in (emp1, emp2, emp3):
        r = http_client.post("/employees", json=emp)
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == emp["emp_code"]
        assert isinstance(data["created_at"], int)
        assert "_id" not in data

    # Verify uniqueness enforced by database index
    dup_r = http_client.post("/employees", json=emp1)
    assert dup_r.status_code == 409

    # List employees with department filter and pagination
    list_r = http_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert list_r.status_code == 200
    items = list_r.json()["items"]
    codes = [e["emp_code"] for e in items]
    assert "EMP9001" in codes
    assert "EMP9002" in codes
    assert "EMP9003" in codes


# --------------------------------------------------------------------------- #
# 3. Attendance Punch-In, Overnight Shift, and Punch-Out
# --------------------------------------------------------------------------- #
def test_real_punch_in_out_and_concurrency():
    # Regular Shift: EMP9001 on 2026-07-06
    # Punch in at 09:40:01 IST (40 min 1 sec = 601s elapsed -> late 10 min per R2)
    # 2026-07-06 09:40:01 IST = 2026-07-06 04:10:01 UTC
    ms_in1 = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    r_in1 = http_client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms_in1})
    assert r_in1.status_code == 201
    rec1 = r_in1.json()
    assert rec1["date"] == "2026-07-06"
    assert rec1["late_minutes"] == 10
    assert rec1["status"] == "PRESENT"
    assert rec1["punch_out"] is None

    # Duplicate punch-in on same date rejected by database index
    dup_punch = http_client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms_in1})
    assert dup_punch.status_code == 409

    # Overnight Shift: EMP9002 (shift 22:00 to 06:00)
    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC)
    ms_in2 = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_in2 = http_client.post("/attendance/punch-in", json={"emp_code": "EMP9002", "punched_at": ms_in2})
    assert r_in2.status_code == 201
    rec2 = r_in2.json()
    assert rec2["date"] == "2026-07-06"  # Attendance date is the day shift started
    assert rec2["late_minutes"] == 0

    # Punch out EMP9001 at 19:10:00 IST (13:40 UTC) -> 40 min overtime (R3)
    # Total elapsed: 04:10:01 to 13:40:00 = 34199 seconds / 3600 = 9.4997... -> 9.50 hours (R4)
    ms_out1 = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_out1 = http_client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": ms_out1})
    assert r_out1.status_code == 200
    rec_out1 = r_out1.json()
    assert rec_out1["work_hours"] == 9.50
    assert rec_out1["overtime_minutes"] == 40
    assert rec_out1["half_day"] is False

    # Second punch out rejected with 409
    dup_out = http_client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": ms_out1})
    assert dup_out.status_code == 409

    # Punch out overnight EMP9002 on NEXT DAY at 06:40 IST (01:10 UTC July 7)
    # 40 min overtime past 06:00 IST
    ms_out2 = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_out2 = http_client.post("/attendance/punch-out", json={"emp_code": "EMP9002", "punched_at": ms_out2})
    assert r_out2.status_code == 200
    rec_out2 = r_out2.json()
    assert rec_out2["date"] == "2026-07-06"
    assert rec_out2["overtime_minutes"] == 40
    assert rec_out2["work_hours"] == 8.75


# --------------------------------------------------------------------------- #
# 4. Manual Regularization & Audit Trail
# --------------------------------------------------------------------------- #
def test_real_regularization_and_audit():
    # Correct EMP9001's punch_in on 2026-07-06 to 09:28 IST (03:58 UTC)
    # Derived fields recomputed: late_minutes becomes 0, work_hours recomputed
    ms_new_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    patch_r = http_client.patch(
        "/attendance/EMP9001/2026-07-06",
        json={
            "punch_in": ms_new_in,
            "reason": "Biometric terminal was rebooting",
            "regularized_by": "lead.admin",
        }
    )
    assert patch_r.status_code == 200
    updated = patch_r.json()
    assert updated["late_minutes"] == 0
    assert updated["work_hours"] == 9.70
    assert len(updated["history"]) == 1

    entry = updated["history"][0]
    assert entry["by"] == "lead.admin"
    assert entry["reason"] == "Biometric terminal was rebooting"
    assert "late_minutes" in entry["changes"]
    assert entry["changes"]["late_minutes"]["from"] == 10
    assert entry["changes"]["late_minutes"]["to"] == 0


# --------------------------------------------------------------------------- #
# 5. Real MongoDB Aggregation Pipelines
# --------------------------------------------------------------------------- #
def test_real_analytics_pipelines():
    # 1. Employee Monthly Summary
    r_month = http_client.get("/analytics/employees/EMP9001/monthly?month=2026-07")
    assert r_month.status_code == 200
    data_m = r_month.json()
    assert data_m["emp_code"] == "EMP9001"
    assert data_m["working_days"] == 23  # July 2026 has 23 weekdays
    assert data_m["present_days"] >= 1.0
    assert isinstance(data_m["attendance_pct"], float)

    # 2. Department Summary (includes EMP9003 who has zero logs per R9)
    r_dept = http_client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert r_dept.status_code == 200
    dept_items = r_dept.json()["items"]
    assert len(dept_items) == 1
    eng = dept_items[0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] >= 3  # Must include EMP9001, EMP9002, EMP9003
    assert eng["present_days"] >= 2.0
    assert eng["avg_work_hours"] is not None

    # 3. Late Leaderboard
    r_lead = http_client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
    assert r_lead.status_code == 200
    assert "items" in r_lead.json()

    # 4. Department Trend (gap filling & 7-day moving avg)
    r_trend = http_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert r_trend.status_code == 200
    trend_data = r_trend.json()
    assert trend_data["department"] == "Engineering"
    # Exactly 5 days from July 6 to July 10
    assert len(trend_data["items"]) == 5
    for day in trend_data["items"]:
        assert "date" in day
        assert "is_working_day" in day
        assert "headcount" in day
        assert "present_count" in day
        assert "attendance_rate" in day
        assert "moving_avg_7d" in day


# --------------------------------------------------------------------------- #
# 6. Admin Explain Endpoint & Genuine Plan Verification
# --------------------------------------------------------------------------- #
def test_real_explain_plans():
    endpoints_to_test = [
        ("attendance_list", "?emp_code=EMP9001"),
        ("employee_monthly", "?emp_code=EMP9001&month=2026-07"),
        ("department_summary", "?month=2026-07&department=Engineering"),
        ("late_leaderboard", "?month=2026-07&department=Engineering"),
        ("department_trend", "?department=Engineering&from=2026-07-06&to=2026-07-10"),
    ]

    for ep_name, qparams in endpoints_to_test:
        r_exp = http_client.get(f"/admin/explain/{ep_name}{qparams}")
        assert r_exp.status_code == 200, f"Explain failed for {ep_name}: {r_exp.text}"
        data = r_exp.json()
        assert data["endpoint"] == ep_name
        assert "collection" in data
        assert "explain" in data
        assert isinstance(data["explain"], dict)
        dump = str(data["explain"])
        assert "executionStats" in dump
        assert "IXSCAN" in dump
