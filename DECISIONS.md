# DECISIONS.md

1. **Indexes.**
   I created five indexes at startup (idempotently via `create_index`).
   - `employees.emp_code` (unique): enforces the uniqueness constraint at the database level so concurrent duplicate creates yield exactly one 201 and the rest 409 without any application-level lock.
   - `employees.(department, emp_code)`: serves the filtered, sorted `GET /employees` query and the department-scoped analytics lookups.
   - `employees.joined_on`: supports the headcount filter (`joined_on <= last_day`) used by department summary and trend.
   - `attendance_logs.(emp_code, date)` (unique): the natural key; enforces one record per (employee, day) and is the primary lookup path for punch-in, punch-out, and regularize.
   - `attendance_logs.(date DESC, emp_code ASC)`: serves the `GET /attendance` list which sorts by date descending then emp_code.
   - `attendance_logs.(emp_code, punch_in)`: used by punch-out to find the most recent open record.
   I considered a compound index on `(emp_code, status, date)` for analytics but rejected it because the analytics queries already hit the `(emp_code, date)` index efficiently and adding a third field would increase write overhead without measurable read gain on typical monthly ranges.

2. **Punch-in race.**
   Two identical punch-in requests arrive at the same instant. Both pass the employee lookup and build the same document (same `emp_code`, same `date`). Both call `insert_one`. MongoDB's unique index on `(emp_code, date)` allows only one insert to succeed; the second raises `DuplicateKeyError`. The handler catches it and returns 409. No application-level check-then-insert exists, so there is no TOCTOU window.

3. **Ties.**
   The leaderboard uses standard competition ranking. All employees are sorted by `total_late_minutes` descending, then `emp_code` ascending. Rank is assigned as the 1-based position of the first occurrence of each distinct total. If two employees share a total that would fall at positions 2 and 3, both get rank 2 and the next employee gets rank 4. When `limit=2` is requested, both tied employees are returned because both have `rank <= 2`, giving three rows in the response.

4. **Headcount.**
   The department summary fetches all employees whose `joined_on <= last_day_of_month` directly from the `employees` collection before touching `attendance_logs`. It groups those employees by department to build the headcount. Attendance logs are then matched against that employee set and merged in Python. An employee who joined but has no logs still appears in `dept_emps` and contributes to `headcount`; they simply add zero to all other counters.

5. **One thing I would change for 100x the data.**
   I would move the `employee_monthly` and `department_summary` analytics from Python-side aggregation to full MongoDB aggregation pipelines (`$group`, `$lookup`, `$addFields`). At current scale the Python loop over one employee's month (≤31 documents) is fast, but at 100x data the per-request fan-out of fetching thousands of log documents and joining in memory would become the bottleneck. A single aggregation pipeline keeps all the data movement inside MongoDB and returns only the final summary rows.
