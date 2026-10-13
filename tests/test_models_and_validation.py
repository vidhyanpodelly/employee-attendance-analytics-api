"""Tests for Pydantic models, input constraints, and validation rules."""
import pytest
from pydantic import ValidationError

from app.main import (
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
)


def test_employee_create_valid():
    emp = EmployeeCreate(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-05",
    )
    assert emp.emp_code == "EMP0001"
    assert emp.department == "Engineering"


def test_employee_create_same_shifts_rejected():
    with pytest.raises(ValidationError) as exc:
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05",
        )
    assert "shift_start must differ from shift_end" in str(exc.value)


def test_employee_create_invalid_emp_code():
    # Must match ^EMP\d{4,6}$
    for bad_code in ["EMP1", "EMP12", "EMP123", "emp0001", "EMP1234567", "ABC0001"]:
        with pytest.raises(ValidationError):
            EmployeeCreate(
                emp_code=bad_code,
                name="Asha Rao",
                email="asha@example.com",
                department="Engineering",
                joined_on="2026-01-05",
            )


def test_punch_in_request_epoch_millis_validation():
    # Valid instant in epoch ms
    req = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000)
    assert req.punched_at == 1783312500000

    # Reject seconds-looking values (< 100000000000)
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500)

    # Reject invalid status
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", status="ABSENT")  # Only PRESENT, WFH, ON_DUTY


def test_regularize_request_validation():
    # Valid regularization
    req = RegularizeRequest(
        reason="biometric glitch",
        regularized_by="hr.admin",
        punch_in=1783310280000,
        punch_out=1783344000000,
    )
    assert req.reason == "biometric glitch"
    assert req.regularized_by == "hr.admin"

    # Reason too short (< 5 chars)
    with pytest.raises(ValidationError):
        RegularizeRequest(reason="fix", regularized_by="hr.admin")

    # Regularized_by empty (< 1 char)
    with pytest.raises(ValidationError):
        RegularizeRequest(reason="biometric glitch", regularized_by="")
