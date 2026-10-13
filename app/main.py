"""Employee Attendance & Analytics API.

FastAPI backend conforming strictly to openapi.yaml, DATA_MODEL.md,
and business rules R1 through R10.
"""
from __future__ import annotations

import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def get_db() -> Any:
    """Dependency provider for MongoDB database."""
    return db


# --------------------------------------------------------------------------- #
# Indexes and Startup
# --------------------------------------------------------------------------- #
def create_indexes(target_db: Any = None) -> None:
    """Create all required indexes idempotently at startup."""
    if target_db is None:
        target_db = db
    try:
        # Unique index on employees.emp_code
        target_db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        # Department lookups and employee filtering
        target_db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)],
            name="idx_emp_dept_code"
        )
        target_db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            name="idx_emp_dept_joined"
        )
        target_db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

        # Unique index on attendance_logs (emp_code, date)
        target_db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="idx_att_emp_date_unique"
        )
        # Attendance listing sort: date DESC, emp_code ASC
        target_db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_att_date_desc_emp_asc"
        )
        # Punch-out search: latest punch_in for employee
        target_db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="idx_att_emp_punch_in"
        )
        # Analytics date queries and leaderboard
        target_db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", DESCENDING)],
            name="idx_att_date_late"
        )
        target_db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
            name="idx_att_emp_date_status"
        )
    except Exception:
        # Gracefully handle cold boot when MongoDB is unreachable
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Domain Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before calculations."""
    return dt.replace(microsecond=0)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC-aware datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt_utc = dt.astimezone(UTC)
    return int(dt_utc.timestamp()) * 1000


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def round_half_up(value: Union[float, Decimal], decimals: int = 2) -> float:
    """R4, R8: Rounding half-up (never banker's rounding)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Determine attendance date (YYYY-MM-DD) in IST.

    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the
    previous calendar day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < dt_time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def get_shift_datetimes(att_date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """Calculate UTC start and end instants for the shift on the attendance date."""
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_ist = datetime.combine(att_date, dt_time(start_h, start_m), tzinfo=IST)
    if shift_end <= shift_start:
        end_ist = datetime.combine(att_date + timedelta(days=1), dt_time(end_h, end_m), tzinfo=IST)
    else:
        end_ist = datetime.combine(att_date, dt_time(end_h, end_m), tzinfo=IST)
    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


def compute_late_minutes(punch_in_dt: datetime, shift_start_utc: datetime) -> int:
    """R2: Grace period 10 minutes.

    Late only if punch-in is strictly > 10:00 (600s) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    elapsed_secs = int((punch_in_dt - shift_start_utc).total_seconds())
    if elapsed_secs > 600:
        return max(0, elapsed_secs // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    elapsed_secs = int((punch_out_dt - punch_in_dt).total_seconds())
    hours = Decimal(elapsed_secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_dt: datetime, shift_end_utc: datetime) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, only if >= 30, else 0."""
    elapsed_secs = int((punch_out_dt - shift_end_utc).total_seconds())
    minutes = elapsed_secs // 60
    if minutes >= 30:
        return int(minutes)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when work_hours < 4.50."""
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Working days (Mon-Fri) between start_date and end_date inclusive."""
    d_start = date.fromisoformat(start_date_str)
    d_end = date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    count = 0
    cur = d_start
    while cur <= d_end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB employee document to OpenAPI Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a single history audit entry."""
    changes: Dict[str, Any] = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = dt_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = dt_to_epoch_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": dt_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to OpenAPI AttendanceRecord schema."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas Matching OpenAPI Contract Exactly
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]

EpochMillisField = Annotated[
    int,
    Field(
        ge=100000000000,
        le=4102444800000,
        description="Instant as epoch milliseconds."
    )
]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_difference(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisField] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisField] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillisField] = None
    punch_out: Optional[EpochMillisField] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health(database: Any = Depends(get_db)):
    """Readiness probe: 200 if MongoDB answers ping, else 503."""
    try:
        if hasattr(database, "client"):
            database.client.admin.command("ping")
        else:
            client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB is unreachable"
        )


# --------------------------------------------------------------------------- #
# Endpoints: Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate, database: Any = Depends(get_db)):
    """Create an employee. Enforces uniqueness via unique index."""
    if database.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    doc = body.model_dump()
    created_at_dt = truncate_to_seconds(datetime.now(UTC))
    doc["created_at"] = created_at_dt

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    """List employees paginated and sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = database.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest, database: Any = Depends(get_db)):
    """Record punch-in for employee and attendance date."""
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(UTC))

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    start_utc, _ = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, start_utc)

    # Check for existing record
    if database.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest, database: Any = Depends(get_db)):
    """Close the employee's most recent open record whose punch_in is <= punched_at."""
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(UTC))

    # Find the most recent record whose punch_in <= punch_out_dt (and punch_in is not null)
    record = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt: datetime = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out"
        )

    # Validate punch-out constraints
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly after punch_in"
        )

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    _, end_utc = get_shift_datetimes(record["date"], emp["shift_start"], emp["shift_end"])
    ot_min = compute_overtime_minutes(punch_out_dt, end_utc)
    is_half = compute_half_day(work_hrs)

    # Concurrency safe atomic update
    updated_doc = database.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record was concurrently punched out"
        )

    return format_attendance_doc(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    """List attendance records with database-side sorting and pagination."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q: Dict[str, Any] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status_filter:
        q["status"] = status_filter

    total = database.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)

    items = [format_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
    database: Any = Depends(get_db),
):
    """Manually correct one day's record and append an audit history entry."""
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Contract Rule: setting status ABSENT or LEAVE clears both punch times;
    # supplying punch_in or punch_out with it is 422.
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Punch times cannot be supplied when setting status to ABSENT or LEAVE"
            )

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in"
            )

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=UTC)

        # Validate that punch_in stays on the record's attendance date
        calc_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {calc_date}, does not match record date {date}"
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=UTC)

            if target_punch_out <= target_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in"
                )
            if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )

            start_utc, end_utc = get_shift_datetimes(date, emp["shift_start"], emp["shift_end"])
            target_late_minutes = compute_late_minutes(target_punch_in, start_utc)
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime_minutes(target_punch_out, end_utc)
            target_half_day = compute_half_day(target_work_hours)
        else:
            start_utc, _ = get_shift_datetimes(date, emp["shift_start"], emp["shift_end"])
            target_late_minutes = compute_late_minutes(target_punch_in, start_utc)
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Determine changes
    changes: Dict[str, Any] = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any, is_instant: bool = False):
        if is_instant:
            old_ms = dt_to_epoch_ms(old_val) if isinstance(old_val, datetime) else old_val
            new_ms = dt_to_epoch_ms(new_val) if isinstance(new_val, datetime) else new_val
            if old_ms != new_ms:
                changes[field_name] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), target_status)
    check_diff("punch_in", record.get("punch_in"), target_punch_in, is_instant=True)
    check_diff("punch_out", record.get("punch_out"), target_punch_out, is_instant=True)
    check_diff("work_hours", record.get("work_hours"), target_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), target_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), target_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), target_half_day)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request made no changes to the attendance record"
        )

    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe: verify history length has not changed
    cur_history = record.get("history", [])
    history_query: Dict[str, Any] = {"_id": record["_id"]}
    if "history" in record:
        history_query["history"] = {"$size": len(cur_history)}
    else:
        history_query["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]

    updated = database.attendance_logs.find_one_and_update(
        history_query,
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on this record"
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Pipelines: Analytics Helpers
# --------------------------------------------------------------------------- #
def get_month_boundaries(month: str) -> Tuple[str, str, int]:
    """Returns (first_day_str, last_day_str, total_days_in_month)."""
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{num_days:02d}", num_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_boundaries(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        }
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_boundaries(month)
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "raw_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$raw_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$l.half_day", True]},
                                            0.5,
                                            1.0
                                        ]
                                    },
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_boundaries(month)
    stages: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        stages.append({"$match": {"emp.department": department}})

    stages.extend([
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        }
    ])
    return stages


def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str
) -> List[Dict[str, Any]]:
    """Build pure MongoDB aggregation pipeline for daily department trend.

    Uses $densify for gap filling and $setWindowFields for 7-day moving average.
    """
    from_dt = datetime.fromisoformat(from_date_str).replace(tzinfo=UTC)
    to_dt = datetime.fromisoformat(to_date_str).replace(tzinfo=UTC)
    bounds_end_dt = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", from_date_str]},
                                    {"$lte": ["$date", to_date_str]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "all_employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on"
                    }
                },
                "raw_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "all_employees": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$raw_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {"$unwind": {"path": "$flat_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "$cond": [
                        {"$ne": ["$flat_logs", None]},
                        "$flat_logs.date",
                        from_date_str
                    ]
                },
                "department": {"$first": "$department"},
                "all_employees": {"$first": "$all_employees"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$flat_logs", None]},
                                    {"$in": ["$flat_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$flat_logs.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$flat_logs", None]},
                                    {"$gt": [{"$ifNull": ["$flat_logs.late_minutes", 0]}, 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "department": 1,
                "all_employees": 1,
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Gap filling via $densify across requested date bounds
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bounds_end_dt]
                }
            }
        },
        # Look up all department employees so every row (original & densified) has the complete roster
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}}
                ],
                "as": "all_employees"
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "date_dt": 1,
                "all_employees": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$all_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date_dt": ASCENDING}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints: Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    database: Any = Depends(get_db),
):
    """Monthly attendance summary for one employee."""
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    first_day, last_day, _ = get_month_boundaries(month)
    start_counting_date = max(first_day, emp["joined_on"])
    working_days = count_working_days(start_counting_date, last_day)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(database.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    database: Any = Depends(get_db),
):
    """Department summary for a month via cross-collection aggregation."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(database.employees.aggregate(pipeline))

    items = []
    for row in results:
        headcount = int(row.get("headcount", 0))
        if headcount == 0:
            continue
        raw_avg = row.get("avg_work_hours")
        avg_hours = round_half_up(raw_avg, 2) if raw_avg is not None else None

        items.append({
            "department": row["department"],
            "headcount": headcount,
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": avg_hours,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
    database: Any = Depends(get_db),
):
    """Late-comers leaderboard with standard competition ranking."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(database.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    database: Any = Depends(get_db),
):
    """Daily attendance trend with gap filling and 7-day moving average."""
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to date must be greater than or equal to from date"
        )

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date range cannot exceed 92 days"
        )

    dept_count = database.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(database.employees.aggregate(pipeline))

    items = []
    for r in results:
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Endpoints: Admin Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    """MongoDB explain with executionStats verbosity for an endpoint's main query."""
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                dq: Dict[str, Any] = {}
                if date_from:
                    dq["$gte"] = date_from
                if date_to:
                    dq["$lte"] = date_to
                q["date"] = dq
            if status_filter:
                q["status"] = status_filter

            cmd_target = database if hasattr(database, "command") else db
            raw_explain = cmd_target.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                verbosity="executionStats"
            )
            collection_name = "attendance_logs"

        elif endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required emp_code")
            if not month:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required month")
            pipeline = build_employee_monthly_pipeline(emp_code, month)
            cmd_target = database if hasattr(database, "command") else db
            raw_explain = cmd_target.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats"
            )
            collection_name = "attendance_logs"

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required month")
            pipeline = build_department_summary_pipeline(month, department)
            cmd_target = database if hasattr(database, "command") else db
            raw_explain = cmd_target.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats"
            )
            collection_name = "employees"

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required month")
            pipeline = build_late_leaderboard_pipeline(month, limit, department)
            cmd_target = database if hasattr(database, "command") else db
            raw_explain = cmd_target.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats"
            )
            collection_name = "attendance_logs"

        elif endpoint == "department_trend":
            if not department:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required department")
            if not from_date or not to_date:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required from/to dates")
            if to_date < from_date:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to < from")
            pipeline = build_department_trend_pipeline(department, from_date, to_date)
            cmd_target = database if hasattr(database, "command") else db
            raw_explain = cmd_target.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats"
            )
            collection_name = "employees"
        else:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint")

        explain_json = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": collection_name,
            "explain": explain_json,
        }
    except PyMongoError as pe:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"MongoDB explain error: {str(pe)}"
        )
