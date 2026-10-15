"""
Employee Attendance & Analytics API

FastAPI application for employee management, attendance lifecycle (punch-in, punch-out, regularization),
monthly analytics, department summaries, latecomer leaderboard, department trends, and explain plans.

All code lives in app/main.py.
Run: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import RedirectResponse
from starlette import status as http_status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Environment variables (environment takes precedence over .env)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in"
        )
        db.attendance_logs.create_index(
            [("date", 1), ("late_minutes", -1)], name="idx_att_date_late"
        )
    except Exception as e:
        # If DB is not available yet during test import, will retry on startup/health
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def root():
    """Redirect root path to interactive Swagger documentation."""
    return RedirectResponse(url="/docs")


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Truncate to whole seconds and convert epoch milliseconds to UTC datetime."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_utc_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON UTC datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def round_half_up_2(val: Optional[float | Decimal]) -> Optional[float]:
    """Round numeric values to 2 decimal places using half-up rounding (R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float | Decimal]) -> Optional[float]:
    """Round rates to 4 decimal places using half-up rounding (R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Determine attendance date (YYYY-MM-DD) in IST.
    For overnight shifts (shift_end <= shift_start), if punch-in IST time is earlier than shift_end,
    it belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    cal_date = punch_in_ist.date()

    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(end_h, end_m)
        if punch_in_ist.time() < shift_end_time:
            return (cal_date - timedelta(days=1)).isoformat()

    return cal_date.isoformat()


def compute_late_minutes(
    punch_in_utc: datetime, attendance_date_str: str, shift_start_str: str
) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 (600s)
    after shift_start. When late, late_minutes = floor(minutes elapsed since shift_start).
    """
    att_date = date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start_str.split(":"))
    shift_start_ist = datetime(
        att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST
    )
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    d = Decimal(str(int(diff_sec))) / Decimal("3600")
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_utc: datetime, attendance_date_str: str, shift_start_str: str, shift_end_str: str
) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    Overnight shifts have shift_end on the next calendar day.
    """
    att_date = date.fromisoformat(attendance_date_str)
    end_h, end_m = map(int, shift_end_str.split(":"))
    if shift_end_str <= shift_start_str:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST
    )
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def get_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    """
    R7: Count Monday-Friday working days in month.
    For employees who joined mid-month, working days start from max(month_start, joined_on).
    """
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = date(year, m, 1)
    month_end = date(year, m, num_days)
    joined_on = date.fromisoformat(joined_on_str)

    start_date = max(month_start, joined_on)
    if start_date > month_end:
        return 0

    count = 0
    cur = start_date
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to OpenAPI AttendanceRecord response schema."""
    out = dict(doc)
    out.pop("_id", None)
    out.pop("id", None)

    out["punch_in"] = datetime_utc_to_epoch_ms(out.get("punch_in"))
    out["punch_out"] = datetime_utc_to_epoch_ms(out.get("punch_out"))
    out["half_day"] = bool(out.get("half_day", False))
    out["late_minutes"] = int(out.get("late_minutes", 0))
    out["overtime_minutes"] = int(out.get("overtime_minutes", 0))

    history_list = []
    for h in out.get("history", []):
        h_entry = dict(h)
        h_entry["at"] = datetime_utc_to_epoch_ms(h_entry.get("at"))
        changes = dict(h_entry.get("changes", {}))
        for k, v in changes.items():
            if k in ("punch_in", "punch_out"):
                v_copy = dict(v)
                if isinstance(v_copy.get("from"), datetime):
                    v_copy["from"] = datetime_utc_to_epoch_ms(v_copy["from"])
                if isinstance(v_copy.get("to"), datetime):
                    v_copy["to"] = datetime_utc_to_epoch_ms(v_copy["to"])
                changes[k] = v_copy
        h_entry["changes"] = changes
        history_list.append(h_entry)

    out["history"] = history_list
    return out


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
VALID_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
MIN_EPOCH = 100000000000
MAX_EPOCH = 4102444800000


def validate_epoch_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Instant must be an integer count of epoch milliseconds")
    if v < MIN_EPOCH or v > MAX_EPOCH:
        raise ValueError(f"Epoch milliseconds out of range [{MIN_EPOCH}, {MAX_EPOCH}]")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"Invalid status: {v}. Must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {v}. Must be one of {VALID_STATUSES}")
        return v

    @field_validator("punch_in", mode="before")
    @classmethod
    def check_punch_in(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("punch_out", mode="before")
    @classmethod
    def check_punch_out(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=http_status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee. emp_code must be unique, enforced by database unique index.
    Concurrent duplicate creates yield exactly one 201 and the rest 409.
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="emp_code already exists")

    doc["created_at"] = int(now_utc.timestamp()) * 1000
    doc.pop("_id", None)
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total counts the filtered set."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = []
    for emp in cursor:
        emp["created_at"] = datetime_utc_to_epoch_ms(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=http_status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record (R1, R2).
    Ensures race-safe insert via unique index (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_in_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=http_status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Atomic update ensures concurrent punch-outs yield exactly one 200 and subsequent 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_out_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find the most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not rec:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="No matching punch-in found"
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="Record is already punched out"
        )

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime(
        punch_out_utc, rec["date"], emp["shift_start"], emp["shift_end"]
    )
    half_day = work_hours < 4.50

    # Atomic conditional update on punch_out is None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="Record is already punched out"
        )

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date desc, then emp_code asc.
    Uses index scan and database-level pagination.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to"
        )
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid status: {status}"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize one day's attendance record. Addressable by natural key (emp_code, date).
    Recomputes derived fields and appends one history entry containing only fields that actually changed.
    Uses optimistic concurrency control to prevent lost history updates under concurrency.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )

    # Validation rules
    cur_status = rec["status"]
    cur_punch_in = rec.get("punch_in")
    cur_punch_out = rec.get("punch_out")
    cur_work_hours = rec.get("work_hours")
    cur_late_minutes = rec.get("late_minutes", 0)
    cur_overtime_minutes = rec.get("overtime_minutes", 0)
    cur_half_day = rec.get("half_day", False)

    new_status = body.status if body.status is not None else cur_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch times when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            new_punch_in = cur_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in instant",
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in instant must correspond to attendance date {date}, got {calc_date}",
            )

        new_late_minutes = compute_late_minutes(new_punch_in, date, emp["shift_start"])

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            new_punch_out = cur_punch_out

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, date, emp["shift_start"], emp["shift_end"]
            )
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Collect changes
    changes_mongo: Dict[str, Any] = {}
    changes_api: Dict[str, Any] = {}

    if new_status != cur_status:
        changes_mongo["status"] = {"from": cur_status, "to": new_status}
        changes_api["status"] = {"from": cur_status, "to": new_status}

    if new_punch_in != cur_punch_in:
        changes_mongo["punch_in"] = {"from": cur_punch_in, "to": new_punch_in}
        changes_api["punch_in"] = {
            "from": datetime_utc_to_epoch_ms(cur_punch_in),
            "to": datetime_utc_to_epoch_ms(new_punch_in),
        }

    if new_punch_out != cur_punch_out:
        changes_mongo["punch_out"] = {"from": cur_punch_out, "to": new_punch_out}
        changes_api["punch_out"] = {
            "from": datetime_utc_to_epoch_ms(cur_punch_out),
            "to": datetime_utc_to_epoch_ms(new_punch_out),
        }

    if new_work_hours != cur_work_hours:
        changes_mongo["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}
        changes_api["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}

    if new_late_minutes != cur_late_minutes:
        changes_mongo["late_minutes"] = {"from": cur_late_minutes, "to": new_late_minutes}
        changes_api["late_minutes"] = {"from": cur_late_minutes, "to": new_late_minutes}

    if new_overtime_minutes != cur_overtime_minutes:
        changes_mongo["overtime_minutes"] = {
            "from": cur_overtime_minutes,
            "to": new_overtime_minutes,
        }
        changes_api["overtime_minutes"] = {
            "from": cur_overtime_minutes,
            "to": new_overtime_minutes,
        }

    if new_half_day != cur_half_day:
        changes_mongo["half_day"] = {"from": cur_half_day, "to": new_half_day}
        changes_api["half_day"] = {"from": cur_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes detected in request"
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    # Concurrency control: match on history length so concurrent updates don't clobber history
    current_history_len = len(rec.get("history", []))
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not update_result:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict; please retry",
        )

    return format_attendance_doc(update_result)


# --------------------------------------------------------------------------- #
# Aggregation Analytics Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}-"},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation pipeline.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = get_working_days_in_month(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        data = agg_res[0]
        present_days = round_half_up_2(data["present_days"]) or 0.0
        leave_days = int(data["leave_days"])
        late_count = int(data["late_count"])
        total_late_minutes = int(data["total_late_minutes"])
        total_overtime_minutes = int(data["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up_2((present_days / working_days) * 100)

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


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start_str = f"{month}-01"
    month_end_str = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$gt": ["$$this.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]},
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$eq": ["$$this.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$eq": ["$$this.status", "ON_DUTY"]},
                        }
                    }
                },
                "work_hours_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        "$$this.work_hours",
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$this.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department monthly summary cross-collection aggregation.
    Includes employees with zero attendance logs (R9).
    """
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        wh_count = doc["work_hours_count"]
        wh_sum = doc["work_hours_sum"]
        avg_work_hours = (
            round_half_up_2(Decimal(str(wh_sum)) / Decimal(str(wh_count)))
            if wh_count > 0
            else None
        )

        items.append(
            {
                "department": doc["_id"],
                "headcount": doc["headcount"],
                "present_days": round_half_up_2(doc["present_days"]) or 0.0,
                "avg_work_hours": avg_work_hours,
                "late_count": doc["late_count"],
                "total_late_minutes": doc["total_late_minutes"],
                "leave_count": doc["leave_count"],
                "on_duty_count": doc["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


def build_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$regex": f"^{month}-"},
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard using standard competition ranking with $setWindowFields $rank.
    Limit applies after ranking, returning all rows where rank <= limit.
    """
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {"_id": from_date, "present_count": 0.0, "late_count": 0},
                            {"_id": to_date, "present_count": 0.0, "late_count": 0},
                        ]
                    }
                ]
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": "full",
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "cond": {"$lte": ["$$this.joined_on", "$date"]},
                        }
                    }
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average.
    """
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Department not found"
        )

    dt_from = date.fromisoformat(from_date)
    dt_to = date.fromisoformat(to_date)
    if dt_to < dt_from:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from"
        )
    if (dt_to - dt_from).days > 92:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Range cannot exceed 92 days"
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for doc in cursor:
        items.append(
            {
                "date": doc["date"],
                "is_working_day": bool(doc["is_working_day"]),
                "headcount": int(doc["headcount"]),
                "present_count": round_half_up_2(doc["present_count"]) or 0.0,
                "late_count": int(doc["late_count"]),
                "attendance_rate": round_half_up_4(doc["attendance_rate"]),
                "moving_avg_7d": round_half_up_4(doc["moving_avg_7d"]),
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
VALID_EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain with executionStats verbosity for endpoint queries and aggregations.
    """
    if endpoint not in VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unsupported endpoint: {endpoint}"
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to"
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        find_cmd: Dict[str, Any] = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "limit": page_size,
        }
        if skip > 0:
            find_cmd["skip"] = skip
        raw_explain = db.command("explain", find_cmd, verbosity="executionStats")
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        raw_explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required"
            )
        pipeline = build_department_summary_pipeline(month, department)
        raw_explain = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required"
            )
        pipeline = build_leaderboard_pipeline(month, limit, department)
        raw_explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        raw_explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"

    explain_clean = json.loads(json_util.dumps(raw_explain))
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_clean,
    }
