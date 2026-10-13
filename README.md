# Employee Attendance & Analytics API

A production-minded FastAPI and MongoDB backend service implementing employee attendance lifecycle tracking, manual regularization with immutable audit trails, and advanced aggregation-based workforce analytics. Conforms strictly to OpenAPI contract v2.0.0 and business rules R1 through R10.

---

## Features

- **Employee Management:** Unique customer employee codes (`emp_code`), shift validation, and paginated department listings.
- **Attendance Punch-in & Punch-out:** Concurrency-safe state transitions, overnight shift handling (R1), strict 10-minute grace period calculations (R2), 30-minute minimum overtime eligibility (R3), and Decimal half-up work hours rounding (R4).
- **Manual Regularization & Audit:** Non-destructive corrections with an immutable audit history recording before-and-after deltas, actor, reason, and timestamp (R5, R6).
- **Workforce Analytics:**
  - *Employee Monthly Summary:* Working days (R7), present days (half day = 0.5), leave counts, and attendance rates.
  - *Department Monthly Summary:* Headcount inclusion for zero-log employees (R9), record-level average hours, and department rollups.
  - *Late Leaderboard:* Standard competition ranking (1, 2, 2, 4) with ties handled via `$rank` window functions.
  - *Department Trend:* Calendar date series with MongoDB `$densify` gap filling and 7-day moving averages (`$setWindowFields`).
- **Query Explainability:** Diagnostic endpoint providing raw execution plans (`executionStats`) verifying index usage (`IXSCAN`).

---

## Prerequisites

- **Python:** 3.11 or newer (tested on Python 3.11, 3.12, 3.14).
- **MongoDB:** Version 6.0 or newer (MongoDB 7.0 recommended, or MongoDB Atlas free tier M0).

---

## Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd attendance-api
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell)
   .\.venv\Scripts\Activate.ps1
   # Linux / macOS
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a local, Git-ignored `.env` file from the example template:
   ```bash
   cp .env.example .env
   ```
   Set your connection variables in `.env`:
   ```ini
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
   *For MongoDB Atlas clusters:*
   ```ini
   MONGO_URI=mongodb+srv://<db_user>:<db_password>@<cluster-host>.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB=attendance_db
   ```

5. **Load Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

---

## Running the API

Start the service using `uvicorn`:
```bash
uvicorn app.main:app --port 8000 --reload
```

- API Base URL: `http://localhost:8000`
- Interactive Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON Specification: `http://localhost:8000/openapi.json`
- Readiness Health Probe: `http://localhost:8000/health`

---

## Running Tests

Run the full automated test suite using `pytest`:
```bash
python -m pytest
```

Run specific test modules:
```bash
# Domain rules and boundary arithmetic tests
python -m pytest tests/test_rules_and_calculations.py

# Pydantic schema validation tests
python -m pytest tests/test_models_and_validation.py

# Mocked API endpoint tests
python -m pytest tests/test_api_mocked.py

# Integration tests against live MongoDB instance (skips if offline)
python -m pytest tests/test_real_mongo_integration.py
```

---

## Database Indexes

All indexes are created idempotently at application startup via FastAPI's `lifespan`:

### `employees` Collection
- `{"emp_code": 1}` *(unique)*: Enforces employee code uniqueness and race-free creation.
- `{"department": 1, "emp_code": 1}`: Optimizes department-filtered employee listings.
- `{"department": 1, "joined_on": 1}` & `{"joined_on": 1}`: Accelerates headcount evaluations for date-bounded analytics.

### `attendance_logs` Collection
- `{"emp_code": 1, "date": 1}` *(unique)*: Enforces at most one attendance record per employee and attendance day.
- `{"date": -1, "emp_code": 1}`: Supports index scans (`IXSCAN`) for pagination sorted by date descending and employee code ascending.
- `{"emp_code": 1, "punch_in": -1}`: Optimizes fast retrieval of the latest open punch-in during punch-out requests.
- `{"date": 1, "late_minutes": -1}`: Backs late-comer leaderboard filtering and ranking pipelines.
- `{"emp_code": 1, "date": 1, "status": 1}`: Supports employee monthly attendance aggregation.

---

## Implementation Notes & Assumptions

- **Timezone Conventions (R1):** All attendance dates and shift calculations are evaluated in IST (`UTC+05:30`). Instants stored in MongoDB are UTC BSON datetimes and converted to integer epoch milliseconds across the HTTP boundary.
- **Rounding (R4, R8):** Decimal half-up rounding (`ROUND_HALF_UP`) is used throughout the API to avoid floating-point representation and banker's rounding discrepancies.
- **Concurrency Safety:** Simultaneous punch-in and punch-out operations rely on MongoDB-level uniqueness constraints and atomic conditional updates (`find_one_and_update` with `punch_out: None`), guaranteeing zero race conditions without application-level distributed locks.
- **History Preservation:** Manual corrections append audit records atomically with length verification to prevent lost updates under concurrent modifications.
