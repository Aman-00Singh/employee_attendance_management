"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from decimal import ROUND_HALF_UP, Decimal
import os
import calendar
from datetime import date, datetime, timedelta, timezone,time
import re
from typing import Optional,Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query,Path
from pydantic import BaseModel,Field,model_validator
from pymongo import MongoClient,ReturnDocument
from pymongo.errors import PyMongoError,DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        "emp_code",
        unique=True,
        name="unique_employees_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employees_department_emp_code",
    )
    db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True,
    name="unique_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
    [("date", -1), ("emp_code", 1)],
    name="ix_attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
    [("emp_code", 1), ("punch_in", -1)],
    name="ix_attendance_emp_code_punch_in",
    )
    db.employees.create_index(
    "joined_on",
    name="ix_employees_joined_on",
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    h, m = map(int, shift_start.split(":"))

    start = datetime.fromisoformat(attendance_date).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    elapsed_seconds = (punch_in - start).total_seconds()

    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)

    return 0

def datetime_to_epoch_millis(
    value: Optional[datetime],
) -> Optional[int]:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    value = value.replace(microsecond=0)

    return int(value.timestamp() * 1000)

def normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)

    return value.replace(microsecond=0)

def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    punch_in = normalize_utc_datetime(punch_in)
    punch_out = normalize_utc_datetime(punch_out)

    elapsed_seconds = int(
        (punch_out - punch_in).total_seconds()
    )

    work_hours = (
        Decimal(elapsed_seconds) / Decimal("3600")
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    return float(work_hours)

def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    shift_end_date = date.fromisoformat(attendance_date)

    if shift_end <= shift_start:
        shift_end_date += timedelta(days=1)

    end_hour, end_minute = map(
        int, shift_end.split(":")
    )

    shift_end_at = datetime.combine(
        shift_end_date,
        time(hour=end_hour, minute=end_minute),
        tzinfo=IST,
    )

    punch_out_ist = normalize_utc_datetime(
        punch_out
    ).astimezone(IST)

    overtime_seconds = int(
        (punch_out_ist - shift_end_at).total_seconds()
    )

    if overtime_seconds < 30 * 60:
        return 0

    return overtime_seconds // 60

def build_late_leaderboard_pipeline(
    month_start: date,
    month_end: date,
    limit: int,
    department: Optional[str] = None,
) -> list[dict]:
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        # Drops attendance records whose employee no longer exists.
        {"$unwind": "$employee"},
    ]

    # Filter before ranking so ranks are calculated within the department.
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$set": {
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            # Filter ranks, not row count: ties at the cutoff must remain.
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    return pipeline

def check_database() -> bool:
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
    

class EmployeeIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "emp_code": "EMP9876",
                    "name": "Riya Sharma",
                    "email": "riya.sharma@example.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-05",
                }
            ]
        }
    }

    emp_code: str = Field(
        pattern=r"^EMP\d{4,6}$",
        description="Employee code: EMP followed by 4 to 6 digits.",
    )
    name: str = Field(
        min_length=1,
        max_length=100,
        description="Employee name, 1-100 characters.",
    )
    email: str = Field(
        unique=True,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        description="Valid email format, maximum 120 characters.",
    )
    department: str = Field(
        min_length=1,
        max_length=50,
        description="Department name, 1-50 characters.",
    )
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Shift start time in 24-hour HH:MM format, IST.",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Shift end time in 24-hour HH:MM format, IST.",
    )
    joined_on: date = Field(
        description="Joining date in YYYY-MM-DD format.",
    )

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start must differ from shift_end"
            )
        return self  

class PunchInIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "emp_code": "EMP0006",
                    "status": "PRESENT",
                }
            ]
        }
    }

    emp_code: str = Field(
        description="Existing employee's emp_code.",
    )

    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description=(
            "Optional epoch timestamp in milliseconds. "
            "If omitted, the current time is used."
        ),
    )

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = Field(
        default="PRESENT",
        description="Attendance status for punch-in.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, values):
        if (
            isinstance(values, dict)
            and "punched_at" in values
            and values["punched_at"] is None
        ):
            raise ValueError(
                "punched_at must be omitted or an epoch-millisecond integer"
            )
        return values
    
class PunchOutIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "emp_code": "EMP0006",
                    # if punched at is provided, it must be in changed to seconds from the frontend while sending the request,
                    # Normally only the emp_code is required for punch-out, but if we want to specify a custom punch-out time, we can include the punched_at field, it will be handled.
                    # "punched_at": 1783429500000,
                },
            ]
        }
    }

    emp_code: str = Field(
        description="Existing employee's emp_code.",
    )

    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description=(
            "Optional epoch timestamp in milliseconds. "
            "If omitted, the current time is used."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, values):
        if (
            isinstance(values, dict)
            and "punched_at" in values
            and values["punched_at"] is None
        ):
            raise ValueError(
                "punched_at must be omitted or an epoch-millisecond integer"
            )
        return values    

class RegularizeIn(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "status": "WFH",
                    "reason": "Corrected work location",
                    "regularized_by": "hr_admin",
                }
            ]
        },
    }

    status: Optional[Literal[
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    ]] = None

    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds.",
    )
    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds.",
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_optional_fields(cls, values):
        if isinstance(values, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in values and values[field] is None:
                    raise ValueError(
                        f"{field} cannot be null; omit it to leave it unchanged"
                    )
        return values
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health",tags=["system"])
def health():
    db_healthy = check_database()
    print(f"Database health check: {db_healthy}")

    if db_healthy:
        return {"status": "ok"}

    raise HTTPException(
        status_code=503,
        detail="MongoDB unavailable",
    )


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump(mode="json")

    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        ) from exc

    doc.pop("_id", None)
    doc["created_at"] = int(created_at.timestamp() * 1000)

    return doc

@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(
        default=None,
        description="Exact, case-sensitive department match.",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}

    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)

    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []

    for employee in cursor:
        created_at = employee["created_at"]

        # PyMongo returns naive datetimes by default;
        # interpret the stored BSON datetime as UTC.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        employee["created_at"] = int(created_at.timestamp() * 1000)
        items.append(employee)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc).replace(
            microsecond=0
        )
    else:
        ts_utc = datetime.fromtimestamp(
            body.punched_at // 1000,
            tz=timezone.utc,
        )

    punch_in_ist = ts_utc.astimezone(IST)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    attendance_day = punch_in_ist.date()

    if shift_end <= shift_start:
        end_hour, end_minute = map(
            int, shift_end.split(":")
        )

        local_time = (
            punch_in_ist.hour,
            punch_in_ist.minute,
            punch_in_ist.second,
        )

        shift_end_time = (end_hour, end_minute, 0)

        if local_time < shift_end_time:
            attendance_day -= timedelta(days=1)

    attendance_date = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punch_in_ist,
            shift_start,
            attendance_date,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        ) from exc

    doc.pop("_id", None)

    doc["punch_in"] = int(ts_utc.timestamp() * 1000)

    return doc

@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one(
        {"emp_code": body.emp_code},
        {
            "_id": 0,
            "shift_start": 1,
            "shift_end": 1,
        },
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is None:
        punch_out_at = normalize_utc_datetime(
            datetime.now(timezone.utc)
        )
    else:
        punch_out_at = datetime.fromtimestamp(
            body.punched_at // 1000,
            tz=timezone.utc,
        )

    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {
                "$type": "date",
                "$lte": punch_out_at,
            },
        },
        sort=[("punch_in", -1)],
    )

    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found",
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    punch_in_at = normalize_utc_datetime(
        candidate["punch_in"]
    )

    if punch_out_at <= punch_in_at:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in",
        )

    elapsed_seconds = int(
        (punch_out_at - punch_in_at).total_seconds()
    )

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(
        punch_in_at,
        punch_out_at,
    )

    overtime_minutes = compute_overtime(
        punch_out_at,
        employee["shift_start"],
        employee["shift_end"],
        candidate["date"],
    )

    half_day = work_hours < 4.50

    updated_record = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_in": candidate["punch_in"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_in": punch_in_at,
                "punch_out": punch_out_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated_record is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out or changed",
        )

    updated_record.pop("_id", None)
    updated_record.pop("id", None)

    updated_record.setdefault("late_minutes", 0)
    updated_record.setdefault("history", [])

    updated_record["punch_in"] = datetime_to_epoch_millis(
        updated_record["punch_in"]
    )
    updated_record["punch_out"] = datetime_to_epoch_millis(
        updated_record["punch_out"]
    )

    for entry in updated_record["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = datetime_to_epoch_millis(
                entry["at"]
            )

        for change in entry.get("changes", {}).values():
            for field in ("from", "to"):
                if isinstance(change.get(field), datetime):
                    change[field] = datetime_to_epoch_millis(
                        change[field]
                    )

    return updated_record

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if (
        date_from is not None
        and date_to is not None
        and date_from > date_to
    ):
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    q = {}

    if emp_code is not None:
        q["emp_code"] = emp_code

    date_filter = {}

    if date_from is not None:
        date_filter["$gte"] = date_from.isoformat()

    if date_to is not None:
        date_filter["$lte"] = date_to.isoformat()

    if date_filter:
        q["date"] = date_filter

    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []

    for record in cursor:
        record.setdefault("punch_in", None)
        record.setdefault("punch_out", None)
        record.setdefault("work_hours", None)
        record.setdefault("late_minutes", 0)
        record.setdefault("overtime_minutes", 0)
        record.setdefault("half_day", False)
        record.setdefault("history", [])

        record["punch_in"] = datetime_to_epoch_millis(
            record["punch_in"]
        )
        record["punch_out"] = datetime_to_epoch_millis(
            record["punch_out"]
        )

        for entry in record["history"]:
            if isinstance(entry.get("at"), datetime):
                entry["at"] = datetime_to_epoch_millis(
                    entry["at"]
                )

            for change in entry.get("changes", {}).values():
                for field in ("from", "to"):
                    if isinstance(change.get(field), datetime):
                        change[field] = datetime_to_epoch_millis(
                            change[field]
                        )

        items.append(record)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.patch("/attendance/{emp_code}/{date}", status_code=200, tags=["attendance"])
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str = Path(
        description="Employee code, e.g. EMP0007"
    ),
    date: date = Path(
        description="Attendance date in YYYY-MM-DD format, e.g. 2026-07-06"
    ),
):
    date_str = date.isoformat()

    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "shift_start": 1, "shift_end": 1},
    )

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date_str}
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    # Normalize stored timestamps before using them.
    old_punch_in_raw = record.get("punch_in")
    old_punch_out_raw = record.get("punch_out")

    old_punch_in = (
        normalize_utc_datetime(old_punch_in_raw)
        if isinstance(old_punch_in_raw, datetime)
        else None
    )
    old_punch_out = (
        normalize_utc_datetime(old_punch_out_raw)
        if isinstance(old_punch_out_raw, datetime)
        else None
    )

    final_status = body.status or record["status"]

    # Omitted punch times keep their current values.
    final_punch_in = (
        normalize_utc_datetime(
            datetime.fromtimestamp(
                body.punch_in // 1000,
                tz=timezone.utc,
            )
        )
        if body.punch_in is not None
        else old_punch_in
    )

    final_punch_out = (
        normalize_utc_datetime(
            datetime.fromtimestamp(
                body.punch_out // 1000,
                tz=timezone.utc,
            )
        )
        if body.punch_out is not None
        else old_punch_out
    )

    if final_status in ("ABSENT", "LEAVE"):
        if (
            "punch_in" in body.model_fields_set
            or "punch_out" in body.model_fields_set
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT or LEAVE cannot be combined with punch times",
            )

        final_punch_in = None
        final_punch_out = None

    else:
        if final_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        punch_in_local = final_punch_in.astimezone(IST)
        actual_attendance_date = punch_in_local.date()

        if employee["shift_end"] <= employee["shift_start"]:
            end_hour, end_minute = map(int, employee["shift_end"].split(":"))
            shift_end_time = time(hour=end_hour, minute=end_minute)

            if punch_in_local.time() < shift_end_time:
                actual_attendance_date -= timedelta(days=1)

        if actual_attendance_date.isoformat() != date_str:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

        if final_punch_out is not None:
            elapsed_seconds = (
                final_punch_out - final_punch_in
            ).total_seconds()

            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if final_status in ("ABSENT", "LEAVE"):
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            final_punch_in,
            employee["shift_start"],
            date_str,
        )

        if final_punch_out is not None:
            work_hours = compute_work_hours(
                final_punch_in,
                final_punch_out,
            )
            overtime_minutes = compute_overtime(
                final_punch_out,
                employee["shift_start"],
                employee["shift_end"],
                date_str,
            )
            half_day = work_hours < 4.50
        else:
            work_hours = None
            overtime_minutes = 0
            half_day = False

    old_values = {
        "status": record["status"],
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = old_values[field]
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change the attendance record",
        )

    history_entry = {
        "at": normalize_utc_datetime(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_filter = {
        "emp_code": emp_code,
        "date": date_str,
        "status": record["status"],
        "punch_in": old_punch_in_raw,
        "punch_out": old_punch_out_raw,
    }

    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    updated_record = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated_record is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed; retry the correction",
        )

    updated_record.pop("_id", None)
    updated_record.pop("id", None)

    for field in ("punch_in", "punch_out"):
        updated_record[field] = datetime_to_epoch_millis(
            updated_record.get(field)
        )

    for entry in updated_record.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = datetime_to_epoch_millis(entry["at"])

        for change in entry.get("changes", {}).values():
            for field in ("from", "to"):
                if isinstance(change.get(field), datetime):
                    change[field] = datetime_to_epoch_millis(change[field])

    return updated_record
    
@app.get("/analytics/employees/{emp_code}/monthly",tags= ["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., description="Calendar month in YYYY-MM format, e.g. 2026-07.",),
):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
        status_code=422,
        detail="month must be in YYYY-MM format, e.g. 2026-07",
    )

    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid month") from exc

    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )

    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = 0

    start_date = max(month_start, joined_on)

    if start_date <= month_end:
        current_date = start_date
        while current_date <= month_end:
            if current_date.weekday() < 5:
                working_days += 1
            current_date += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_attendance_date": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        },
        {
            "$addFields": {
                "_day_of_week": {"$dayOfWeek": "$_attendance_date"}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$_day_of_week", 2]},
                                    {"$lte": ["$_day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
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
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    summary = result[0] if result else {}

    present_days = summary.get("present_days", 0)
    leave_days = summary.get("leave_days", 0)
    late_count = summary.get("late_count", 0)
    total_late_minutes = summary.get("total_late_minutes", 0)
    total_overtime_minutes = summary.get("total_overtime_minutes", 0)

    if working_days == 0:
        attendance_pct = None
    else:
        percentage = Decimal(str(present_days)) * Decimal("100") / Decimal(
            working_days
        )
        attendance_pct = float(
            percentage.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary",tags= ["analytics"])
def department_summary(
    month: str = Query(
        ...,
        description="Calendar month in YYYY-MM format, e.g. 2026-07.",
    ),
    department: Optional[str] = Query(
        default=None,
        description="Filter by an exact, case-sensitive department name.",
    ),
):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format, e.g. 2026-07",
        )

    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid month",
        ) from exc

    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = date(
        month_start.year,
        month_start.month,
        last_day,
    )

    employee_filter = {
        "joined_on": {"$lte": month_end.isoformat()}
    }

    if department is not None:
        employee_filter["department"] = department

    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    pipeline = [
        {
            "$match": employee_filter
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": month_start.isoformat(),
                                "$lte": month_end.isoformat(),
                            },
                            "$expr": {
                                "$eq": ["$emp_code", "$$employee_code"]
                            },
                        }
                    },
                    {
                        "$addFields": {
                            "_day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            present_statuses,
                                        ]
                                    },
                                    {"$gte": ["$attendance._day_of_week", 2]},
                                    {"$lte": ["$attendance._day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$attendance.half_day",
                                                    False,
                                                ]
                                            },
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            present_statuses,
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {
                                                "$ifNull": [
                                                    "$attendance.work_hours",
                                                    None,
                                                ]
                                            },
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$attendance.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            present_statuses,
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {
                                                "$ifNull": [
                                                    "$attendance.work_hours",
                                                    None,
                                                ]
                                            },
                                            None,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$attendance.late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$attendance.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$sort": {"_id": 1}
        },
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []

    for result in results:
        avg_work_hours = None

        if result["work_hours_count"] > 0:
            average = (
                Decimal(str(result["work_hours_sum"]))
                / Decimal(result["work_hours_count"])
            )
            avg_work_hours = float(
                average.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append(
            {
                "department": result["_id"],
                "headcount": result["headcount"],
                "present_days": result["present_days"],
                "avg_work_hours": avg_work_hours,
                "late_count": result["late_count"],
                "total_late_minutes": result["total_late_minutes"],
                "leave_count": result["leave_count"],
                "on_duty_count": result["on_duty_count"],
            }
        )

    return {
        "month": month,
        "items": items,
    }
    
@app.get(
    "/analytics/leaderboard/late",
    status_code=200,
    tags=["analytics"],
)
def late_leaderboard(
    month: str = Query(
        ...,
        description="Calendar month in YYYY-MM format, e.g. 2026-07.",
    ),
    limit: int = Query(
        default=10,
        ge=1,
        le=50,
        description="Maximum rank to include, from 1 to 50. Defaults to 10.",
    ),
    department: Optional[str] = Query(
        default=None,
        description="Optional exact, case-sensitive department name.",
    ),
):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format, e.g. 2026-07",
        )

    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid month",
        ) from exc

    last_day = calendar.monthrange(
        month_start.year,
        month_start.month,
    )[1]

    month_end = date(
        month_start.year,
        month_start.month,
        last_day,
    )

    pipeline = build_late_leaderboard_pipeline(
        month_start=month_start,
        month_end=month_end,
        limit=limit,
        department=department,
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out .
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly .
#   GET   /analytics/departments/summary .
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
