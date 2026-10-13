# DECISIONS.md

1. **Indexes.**
Created unique `{emp_code: 1}` on `employees` and `{emp_code: 1, date: 1}` on `attendance_logs` for integrity guarantees. Added compound indexes `{department: 1, joined_on: 1}`, `{date: -1, emp_code: 1}`, `{emp_code: 1, punch_in: -1}`, and `{date: 1, late_minutes: -1}` for department lookups, paginated scans, punch-out matching, and leaderboard sorting. Rejected a standalone index on `status` due to low cardinality and poor selectivity.

2. **Punch-in race.**
Both concurrent requests calculate the same attendance date and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` serializes insertion: exactly one write succeeds (HTTP 201), while the duplicate raises a `DuplicateKeyError`, which our exception handler intercepts and maps to HTTP 409 Conflict.

3. **Ties.**
Using `$rank` inside `$setWindowFields`, tied employees share identical standard competition ranks (1, 2, 2, 4) ordered by `emp_code`. The filter `rank <= limit` includes all tied candidates at the threshold, intentionally returning more than `limit` rows when ties occur.

4. **Headcount.**
The pipeline starts from `employees`, filtering `joined_on <= month_end`, and performs a `$lookup` into `attendance_logs`. Employees without logs receive an empty array, guaranteeing headcount includes all active staff regardless of attendance activity.

5. **One thing you would change for 100x data.**
Pre-aggregate daily summaries and monthly rollups asynchronously at punch-out or regularization time rather than executing live `$lookup` aggregations across millions of raw attendance logs.
