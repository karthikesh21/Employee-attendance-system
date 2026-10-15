# DECISIONS.md

1. **Indexes.**
We created:
- `employees`: unique `emp_code` (prevents duplicate creations) and compound `(department, joined_on)` (speeds up department summary and trend headcount filtering).
- `attendance_logs`: unique compound `(emp_code, date)` (guarantees one record per employee-day); compound `(date: -1, emp_code: 1)` (provides IXSCAN for attendance listing and sorting); `(emp_code, punch_in: -1)` (optimizes punch-out lookups); `(date, late_minutes: -1)` (serves leaderboard).
Rejected: a standalone index on `status` due to low cardinality (5 values) and poor selectivity.

2. **Punch-in race.**
Both concurrent requests validate inputs, calculate the attendance date, and execute `insert_one`. MongoDB's unique compound index `(emp_code, date)` atomically enforces uniqueness: the winning insert returns HTTP 201 Created. The competing insert trips `DuplicateKeyError`, which our exception handler intercepts and maps cleanly to HTTP 409 Conflict.

3. **Ties.**
Using MongoDB's `$setWindowFields` with `$rank` implements standard competition ranking (1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking. If two employees tie at the cutoff limit, both satisfy `rank <= limit` and are included, returning more rows than `limit`. Tied employees are sorted deterministically by `emp_code` ascending.

4. **Headcount.**
The department summary starts from `employees` filtered by `joined_on <= month_end`, preserving employees with zero logs through a `$lookup` left join. Because no inner `$unwind` occurs, every eligible employee contributes 1 to the department's headcount.

5. **One thing you would change for 100x data.**
Maintain incrementally updated monthly summary rollup collections upon punch-out or regularization, replacing expensive ad-hoc aggregations over millions of raw documents, combined with time-based collection sharding.
