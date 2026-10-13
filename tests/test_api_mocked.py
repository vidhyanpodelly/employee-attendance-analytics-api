"""Contract and endpoint test suite using FastAPI TestClient with mocked MongoDB operations."""
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.main import app, get_db

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
def test_health_success():
    mock_db = MagicMock()
    mock_db.client.admin.command.return_value = {"ok": 1}
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_failure():
    mock_db = MagicMock()
    mock_db.client.admin.command.side_effect = Exception("Connection refused")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "MongoDB is unreachable"


# --------------------------------------------------------------------------- #
# /employees
# --------------------------------------------------------------------------- #
def test_create_employee_success():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
    app.dependency_overrides[get_db] = lambda: mock_db

    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "emp_code already exists"


def test_create_employee_duplicate_key_error():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = None
    mock_db.employees.insert_one.side_effect = DuplicateKeyError("E11000 duplicate")
    app.dependency_overrides[get_db] = lambda: mock_db

    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 409


def test_create_employee_invalid_shifts():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 422


def test_list_employees():
    sample_doc = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
    }
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.skip.return_value = mock_cursor
    mock_cursor.limit.return_value = [sample_doc]

    mock_db = MagicMock()
    mock_db.employees.count_documents.return_value = 1
    mock_db.employees.find.return_value = mock_cursor
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.get("/employees?department=Engineering&page=1&page_size=20")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert isinstance(data["items"][0]["created_at"], int)


# --------------------------------------------------------------------------- #
# /attendance/punch-in & punch-out
# --------------------------------------------------------------------------- #
def test_punch_in_unknown_employee():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert resp.status_code == 404


def test_punch_in_duplicate():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = {"emp_code": "EMP0001", "date": "2026-07-06"}
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert resp.status_code == 409


def test_punch_out_unknown_employee():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert resp.status_code == 404


def test_punch_out_no_punch_in():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert resp.status_code == 404


def test_punch_out_already_punched_out():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "mock_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc),
    }
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert resp.status_code == 409


def test_punch_out_timestamp_before_punch_in():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "mock_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc),
        "punch_out": None,
    }
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec
    app.dependency_overrides[get_db] = lambda: mock_db

    ms_early = int(datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_early})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /attendance listing
# --------------------------------------------------------------------------- #
def test_list_attendance_date_validation():
    resp = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date}
# --------------------------------------------------------------------------- #
def test_regularize_absent_with_punch_times():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {"_id": "mock_id", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"}
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": 1783312500000,
            "reason": "was on leave",
            "regularized_by": "manager",
        }
    )
    assert resp.status_code == 422


def test_regularize_no_changes():
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "mock_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc),
        "work_hours": 9.12,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "PRESENT",
            "reason": "nothing changed here",
            "regularized_by": "hr.admin",
        }
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /analytics
# --------------------------------------------------------------------------- #
def test_employee_monthly_unknown():
    mock_db = MagicMock()
    mock_db.employees.find_one.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert resp.status_code == 404


def test_department_trend_invalid_dates():
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert resp.status_code == 422


def test_department_trend_range_too_long():
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert resp.status_code == 422


def test_department_trend_unknown_dept():
    mock_db = MagicMock()
    mock_db.employees.count_documents.return_value = 0
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.get("/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-10")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# /admin/explain
# --------------------------------------------------------------------------- #
def test_admin_explain_missing_params():
    resp = client.get("/admin/explain/employee_monthly")
    assert resp.status_code == 422
