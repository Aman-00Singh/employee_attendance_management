1. GET /health — health()

Where in the original app/main.py: health() — lines 72–74.

What was wrong/violated: The endpoint always returned {"status":"ok"} without checking MongoDB. This violated the health-check contract in openapi.yaml, lines 95–111.

How I noticed it: I set MONGO_URI to an unreachable address and checked the response. The starter code still returned 200 instead of 503. Closing the Atlas dashboard alone does not make the database unreachable.

Fixes / corrections we made: We ping MongoDB using client.admin.command("ping"). The endpoint returns {"status":"ok"} only when the ping succeeds and returns 503 on PyMongoError.

2. POST /employees — EmployeeIn and create_employee()

Where in the original app/main.py: EmployeeIn — lines 53–60; create_employee() — lines 79–85; startup index setup.

What was wrong/violated: The input model did not enforce the required emp_code pattern, string lengths, email pattern, shift-time format, valid joined_on date, or the rule that shift start and end times must differ. The duplicate check followed by insertion was also race-prone, and the starter code had no unique index on employees.emp_code. In addition, datetime.now() created a naive local datetime, and created_at was returned as a serialized datetime instead of epoch milliseconds. These issues violated the requirements in openapi.yaml, lines 13–25, 117–120, and 467–499.

How I noticed it: I tested values such as EMP123, a malformed email, 25:00, an over-length name, 2026-02-30, and equal shift times. For uniqueness, simultaneous create requests with the same emp_code could both pass the initial check. I also inspected created_at in the response and checked whether it was an integer.

Fixes / corrections we made: We added Pydantic constraints matching the OpenAPI contract, used a date type for joined_on and stored it as YYYY-MM-DD, rejected equal shift times, and kept valid overnight shifts such as 22:00–06:00. We added a unique employees.emp_code index at startup and translate the losing insert's DuplicateKeyError to 409. We generate a UTC-aware created_at timestamp, truncate it to whole seconds, store it as a BSON datetime, return it as epoch milliseconds, and keep MongoDB \_id out of the response.

3. GET /employees — list_employees()

Where in the original app/main.py: list_employees() — lines 89–96.

What was wrong/violated: The department filter used if department:, which ignores an explicitly supplied empty string. The total counted all employees instead of only those matching the filter. Pagination had no page or page-size bounds, and skip = page \* page_size skipped an extra page. The required emp_code ascending sort was missing, and created_at could be returned in the wrong format. These issues violated openapi.yaml, lines 135–145, 407–416, and 484–499, including the pagination requirements in R10.

How I noticed it: I queried with department= and filtered to Engineering while other departments existed. I also tried page=0 and page_size=101, checked page 1 to see whether it skipped the first page, and inspected result ordering and the created_at type.

Fixes / corrections we made: We apply the department filter when department is not None and calculate total with count_documents(q), using the same filter as the result query. We enforce page >= 1 and 1 <= page_size <= 100, with defaults of page=1 and page_size=20. We use (page - 1) \* page_size for the offset, sort by emp_code ascending, and convert created_at to epoch milliseconds. The original \_id projection was already correct.

4. POST /attendance/punch-in — PunchInIn, punch_in(), and compute_late_minutes()

Where in the original app/main.py: PunchInIn — lines 63–66; compute_late_minutes() — lines 33–37; punch_in() — lines 101–121; startup index setup.

What was wrong/violated: punched_at did not have strict epoch-millisecond and range validation, and status accepted arbitrary strings. An unknown employee was not handled before emp["shift_start"] was accessed, which could cause a 500 error. Timestamp conversion depended on the machine's local timezone, the instant was not consistently truncated to whole seconds, and the attendance date was calculated without the required IST and overnight-shift rules. The check-then-insert sequence was not concurrency-safe because there was no unique (emp_code, date) index. The response also added an internal id and could return punch_in as a datetime. In compute_late_minutes(), whole minutes were calculated before checking the grace period, so a punch-in at 09:40:01 for a 09:30 shift could incorrectly return 0. These issues violated openapi.yaml, lines 18–27, 160–165, and the R1 and R2 rules at lines 61–68, as well as the request and response schemas.

How I noticed it: I tested seconds-looking timestamps, strings, floats, out-of-range timestamp values, and status="ABSENT". I tried punching in for a nonexistent emp_code, tested timestamps near midnight UTC and overnight shifts before shift_end, and sent simultaneous punch-in requests for the same employee and date. I also inspected the response for id and checked whether punch_in was an integer. For lateness, I tested 09:40:00, 09:40:01, and 10:15:59 for a 09:30 shift.

Fixes / corrections we made: We validate punched_at as a strict integer in the specified range and restrict status to PRESENT, WFH, or ON_DUTY. An omitted punched_at means the current time, while an explicitly supplied null is invalid. We return 404 when the employee does not exist. We convert timestamps explicitly to UTC, truncate instants to whole seconds, derive the attendance date in IST, and assign an overnight punch-in before shift_end to the previous shift-start date. We added a unique compound index on attendance_logs(emp_code, date) and translate DuplicateKeyError to 409, so the database prevents duplicate records under concurrency. We exclude \_id and id from the response and return punch_in as epoch milliseconds. For lateness, we compare elapsed seconds with the 600-second grace period first and, only when elapsed time is strictly greater than 600 seconds, return the floored whole minutes elapsed since shift start. The shift start is built from the attendance date in IST.

5. GET /attendance — list_attendance()

Where in the original app/main.py: list_attendance() — lines 127–150.

What was wrong/violated: Date parameters were unvalidated strings, reversed date ranges were not rejected, status accepted arbitrary values, and page/page_size had no bounds. The endpoint fetched every matching document into Python before sorting and slicing, which was inefficient for the expected dataset size of approximately 100,000 records. Sorting by date alone omitted the required secondary emp_code order. The response exposed \_id as id, did not convert punch/history timestamps to epoch milliseconds, and did not apply defaults for legacy records missing history and half_day. These issues violated openapi.yaml, lines 18–25, 208–216, 407–416, and the status schema at lines 460–466, as well as the response schema and DATA_MODEL.md.

How I noticed it: I tested an impossible date such as 2026-02-30, a date_from later than date_to, status=UNKNOWN, page=0, and page_size=101. With a large dataset, the starter still fetched all matching rows before returning one page. I checked the ordering of records sharing a date and inspected records with history or legacy records missing history/half_day.

Fixes / corrections we made: We use date-typed parameters, reject reversed ranges with 422, restrict status to the five defined values, and validate page >= 1 and page_size between 1 and 100. Valid dates are converted to ISO strings for MongoDB because attendance dates are stored as strings. We use count_documents(q) for the filtered total and let MongoDB sort by date descending and emp_code ascending before applying skip and limit. We do not return \_id or an id alias. We convert punch_in, punch_out, history[].at, and datetime values inside history[].changes[].from/to to epoch milliseconds. Missing history defaults to [], missing half_day defaults to false, and other required nullable/default fields are normalized as needed.
