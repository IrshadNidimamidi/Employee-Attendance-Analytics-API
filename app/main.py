"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env for local dev)
"""
import json
import os
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db     = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes — created idempotently at startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_empcode")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="empcode_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_empcode",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="empcode_punchin",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("status", ASCENDING), ("date", ASCENDING)],
        name="empcode_status_date",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def round_half_up(value: float, decimals: int = 2) -> float:
    """ROUND_HALF_UP — not banker's rounding (R4, R8)."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware/naive datetime → epoch milliseconds, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 — attendance date is the IST calendar date of punch_in.
    Overnight shift (shift_end <= shift_start): a punch-in whose IST time is
    strictly before shift_end belongs to the previous day's shift.
    """
    ist_dt = to_ist(punch_in_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_hm = ist_dt.hour * 60 + ist_dt.minute
        if punch_hm < (eh * 60 + em):
            return (ist_dt - timedelta(days=1)).date().isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """
    R2 — grace period 10 minutes (strict > 10:00).
    late_minutes = floor(seconds since shift_start / 60) when > 600 s, else 0.
    shift_start is anchored to att_date in IST (handles overnight shifts correctly).
    """
    sh, sm = map(int, shift_start.split(":"))
    # Anchor shift_start to the attendance date in IST
    base = datetime.fromisoformat(att_date).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    ist = to_ist(punch_in)
    elapsed = int((ist - base).total_seconds())  # truncated seconds
    if elapsed <= 600:
        return 0
    return elapsed // 60  # floor minutes from shift_start


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 — (punch_out - punch_in) seconds / 3600, rounded 2 dec half-up."""
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3 — overtime = floor minutes between shift_end and punch_out,
    only when >= 30 minutes; else 0.
    For overnight shifts shift_end falls on att_date + 1.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    base_date = datetime.fromisoformat(att_date)
    end_ist = base_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    if is_overnight:
        end_ist += timedelta(days=1)

    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    ot_seconds = (punch_out.astimezone(IST) - end_ist).total_seconds()
    if ot_seconds < 1800:
        return 0
    return int(ot_seconds // 60)


def compute_derived(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    att_date: str,
) -> dict:
    """Compute all derived fields from current punch times."""
    late_minutes = compute_late_minutes(punch_in, shift_start, att_date) if punch_in else 0
    if punch_in and punch_out:
        wh = compute_work_hours(punch_in, punch_out)
        ot = compute_overtime(punch_out, shift_start, shift_end, att_date)
        hd = wh < 4.50      # R5: rounded work_hours < 4.50
        return {"late_minutes": late_minutes, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}
    return {"late_minutes": late_minutes, "work_hours": None, "overtime_minutes": 0, "half_day": False}


def record_to_response(doc: dict) -> dict:
    """MongoDB attendance document → API response shape (datetimes → epoch ms)."""
    out = {
        "emp_code":         doc["emp_code"],
        "date":             doc["date"],
        "status":           doc["status"],
        "punch_in":         epoch_ms(doc.get("punch_in")),
        "punch_out":        epoch_ms(doc.get("punch_out")),
        "work_hours":       doc.get("work_hours"),
        "late_minutes":     doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day":         doc.get("half_day", False),
        "history":          [],
    }
    for entry in doc.get("history", []):
        h_entry = {
            "at":      epoch_ms(entry["at"]),
            "by":      entry["by"],
            "reason":  entry["reason"],
            "changes": {},
        }
        for field, chg in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                h_entry["changes"][field] = {
                    "from": epoch_ms(chg["from"]) if chg.get("from") is not None else None,
                    "to":   epoch_ms(chg["to"])   if chg.get("to")   is not None else None,
                }
            else:
                h_entry["changes"][field] = {"from": chg.get("from"), "to": chg.get("to")}
        out["history"].append(h_entry)
    return out


def employee_to_response(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = epoch_ms(doc["created_at"])
    return out


PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if len(v) < 1 or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 120:
            raise ValueError("email must be <= 120 characters")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email format")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @model_validator(mode="after")
    def shift_start_ne_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("punched_at must be epoch milliseconds (100000000000–4102444800000)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("punched_at must be epoch milliseconds")
        return v


class RegularizeIn(BaseModel):
    status:          Optional[str] = None
    punch_in:        Optional[int] = None
    punch_out:       Optional[int] = None
    reason:          str
    regularized_by:  str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("must be epoch milliseconds")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v

    @model_validator(mode="after")
    def absent_leave_no_punches(self):
        if self.status in {"ABSENT", "LEAVE"}:
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError("Cannot supply punch times with ABSENT or LEAVE status")
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return employee_to_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        item["created_at"] = epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Punch-in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    ts = (from_epoch_ms(body.punched_at) if body.punched_at is not None
          else datetime.now(tz=timezone.utc).replace(microsecond=0))

    d    = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], d)

    doc = {
        "emp_code":         body.emp_code,
        "date":             d,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    return record_to_response(doc)


# ---------------------------------------------------------------------------
# Punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    ts = (from_epoch_ms(body.punched_at) if body.punched_at is not None
          else datetime.now(tz=timezone.utc).replace(microsecond=0))

    # Most recent open record whose punch_in <= punched_at (handles overnight)
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": ts},
            "punch_out": None,
            "status":    {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No open punch-in found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff = (ts - punch_in_dt).total_seconds()
    if diff <= 0:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, detail="punched_at is more than 24 hours after punch_in")

    derived = compute_derived(punch_in_dt, ts, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic — concurrent punch-outs: loser gets None → 409
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out":        ts,
                "work_hours":       derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day":         derived["half_day"],
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Record already punched out")
    return record_to_response(result)


# ---------------------------------------------------------------------------
# List attendance
# ---------------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q: dict[str, Any] = {}
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
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [record_to_response(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Regularize (PATCH)
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    # Validate date path param
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, detail="date must be a valid YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record for that date")

    new_status = body.status if body.status is not None else record["status"]

    # Normalise stored datetimes to aware UTC
    old_pin  = record.get("punch_in")
    old_pout = record.get("punch_out")
    if old_pin  is not None and old_pin.tzinfo  is None:
        old_pin  = old_pin.replace(tzinfo=timezone.utc)
    if old_pout is not None and old_pout.tzinfo is None:
        old_pout = old_pout.replace(tzinfo=timezone.utc)

    if new_status in {"ABSENT", "LEAVE"}:
        # 422 if client is trying to supply punch times for ABSENT/LEAVE
        # (model validator handles explicit status; handle inherited status here)
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch times with ABSENT or LEAVE status")
        final_pin:  Optional[datetime] = None
        final_pout: Optional[datetime] = None
    else:
        # Resolve final punch_in
        if body.punch_in is not None:
            final_pin = from_epoch_ms(body.punch_in)
        elif old_pin is not None:
            final_pin = old_pin
        else:
            raise HTTPException(422, detail="Presence status requires a punch_in")

        # Validate punch_in stays on record's attendance date (R1)
        if attendance_date(final_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, detail="punch_in does not fall on the record's attendance date")

        # Resolve final punch_out
        if body.punch_out is not None:
            final_pout = from_epoch_ms(body.punch_out)
        elif old_pout is not None:
            final_pout = old_pout
        else:
            final_pout = None

        if final_pout is not None:
            diff = (final_pout - final_pin).total_seconds()
            if diff <= 0:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if diff > 86400:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in {"ABSENT", "LEAVE"}:
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        derived = compute_derived(final_pin, final_pout, emp["shift_start"], emp["shift_end"], date)

    # Detect which fields actually changed
    fields_to_check: dict[str, tuple] = {
        "status":           (record.get("status"),          new_status),
        "punch_in":         (old_pin,                       final_pin),
        "punch_out":        (old_pout,                      final_pout),
        "work_hours":       (record.get("work_hours"),      derived["work_hours"]),
        "late_minutes":     (record.get("late_minutes", 0), derived["late_minutes"]),
        "overtime_minutes": (record.get("overtime_minutes", 0), derived["overtime_minutes"]),
        "half_day":         (record.get("half_day", False), derived["half_day"]),
    }

    changes: dict[str, Any] = {}
    set_doc: dict[str, Any] = {}
    for field, (old_val, new_val) in fields_to_check.items():
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}
            set_doc[field] = new_val

    if not changes:
        raise HTTPException(422, detail="No fields changed")

    history_entry = {
        "at":      datetime.now(tz=timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Atomic — concurrent edits detected by matching current history size
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": len(record.get("history", []))}},
        {"$set": set_doc, "$push": {"history": history_entry}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit conflict; please retry")
    return record_to_response(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    _, last   = monthrange(year, mon)
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def _is_weekday(date_str: str) -> bool:
    return datetime.fromisoformat(date_str).weekday() < 5


def _working_days_in_month(month: str, joined_on: str) -> int:
    """Mon-Fri days in the month on or after joined_on (R7)."""
    first, last = _month_bounds(month)
    start = max(first, joined_on)
    if start > last:
        return 0
    d   = datetime.fromisoformat(start)
    end = datetime.fromisoformat(last)
    count = 0
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first, last  = _month_bounds(month)
    working_days = _working_days_in_month(month, emp["joined_on"])

    # MongoDB aggregation — spec requires this
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                # present_days: weekday presence records; half-day = 0.5, full = 1
                # We push individual records to post-process weekday check
                "records": {
                    "$push": {
                        "status":   "$status",
                        "date":     "$date",
                        "half_day": {"$ifNull": ["$half_day", False]},
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    }
                },
                "leave_days":             {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count":             {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))

    present_days          = 0.0
    leave_days            = 0
    late_count            = 0
    total_late_minutes    = 0
    total_overtime_minutes = 0

    if rows:
        row = rows[0]
        leave_days             = row["leave_days"]
        late_count             = row["late_count"]
        total_late_minutes     = row["total_late_minutes"]
        total_overtime_minutes = row["total_overtime_minutes"]
        # Count present_days — weekday only (R7), half-day = 0.5
        for rec in row["records"]:
            if rec["status"] in PRESENCE_STATUSES and _is_weekday(rec["date"]):
                present_days += 0.5 if rec["half_day"] else 1.0

    present_days  = round_half_up(present_days, 2)
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = _month_bounds(month)

    emp_q: dict = {"joined_on": {"$lte": last}}
    if department:
        emp_q["department"] = department

    all_emps = list(db.employees.find(emp_q))
    if not all_emps:
        return {"month": month, "items": []}

    emp_map   = {e["emp_code"]: e for e in all_emps}
    emp_codes = list(emp_map.keys())

    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": emp_codes},
        "date":     {"$gte": first, "$lte": last},
    }))

    dept_emps: dict = defaultdict(set)
    for e in all_emps:
        dept_emps[e["department"]].add(e["emp_code"])

    dept_stats: dict = defaultdict(lambda: {
        "present_days":   0.0,
        "work_hours_vals": [],
        "late_count":     0,
        "total_late_minutes": 0,
        "leave_count":    0,
        "on_duty_count":  0,
    })

    for doc in logs:
        ec = doc["emp_code"]
        if ec not in emp_map:
            continue
        dept   = emp_map[ec]["department"]
        s      = dept_stats[dept]
        status = doc.get("status", "")

        if status in PRESENCE_STATUSES and _is_weekday(doc["date"]):
            s["present_days"] += 0.5 if doc.get("half_day") else 1.0

        if status in PRESENCE_STATUSES and doc.get("work_hours") is not None:
            s["work_hours_vals"].append(doc["work_hours"])

        lm = doc.get("late_minutes", 0) or 0
        if lm > 0:
            s["late_count"]         += 1
            s["total_late_minutes"] += lm

        if status == "LEAVE":
            s["leave_count"]   += 1
        if status == "ON_DUTY":
            s["on_duty_count"] += 1

    items = []
    for dept, emp_set in sorted(dept_emps.items()):
        headcount = len(emp_set)
        if headcount == 0:
            continue
        s      = dept_stats[dept]
        wh     = s["work_hours_vals"]
        avg_wh = round_half_up(sum(wh) / len(wh), 2) if wh else None
        items.append({
            "department":         dept,
            "headcount":          headcount,
            "present_days":       round_half_up(s["present_days"], 2),
            "avg_work_hours":     avg_wh,
            "late_count":         s["late_count"],
            "total_late_minutes": s["total_late_minutes"],
            "leave_count":        s["leave_count"],
            "on_duty_count":      s["on_duty_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = _month_bounds(month)

    emp_q: dict = {}
    if department:
        emp_q["department"] = department
    all_emps = list(db.employees.find(emp_q, {"emp_code": 1, "name": 1, "department": 1}))
    emp_map  = {e["emp_code"]: e for e in all_emps}
    if not emp_map:
        return {"month": month, "items": []}

    pipeline = [
        {
            "$match": {
                "emp_code":     {"$in": list(emp_map.keys())},
                "date":         {"$gte": first, "$lte": last},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count":         {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4
    filtered = [r for r in agg if r["_id"] in emp_map]
    items    = []
    position = 0
    rank     = 0
    prev_total = None
    for row in filtered:
        position += 1
        total     = row["total_late_minutes"]
        if total != prev_total:
            rank = position
        prev_total = total
        if rank > limit:
            break
        e = emp_map[row["_id"]]
        items.append({
            "rank":               rank,
            "emp_code":           row["_id"],
            "name":               e["name"],
            "department":         e["department"],
            "total_late_minutes": total,
            "late_count":         row["late_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to:    str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Validate dates
    try:
        d_from = datetime.strptime(from_, "%Y-%m-%d")
        d_to   = datetime.strptime(to,    "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, detail="from/to must be valid dates in YYYY-MM-DD format")

    if to < from_:
        raise HTTPException(422, detail="'to' must be >= 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, detail="Range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # Employees joined on or before `to` (R9)
    dept_emps = list(db.employees.find({"department": department, "joined_on": {"$lte": to}}))
    emp_map   = {e["emp_code"]: e for e in dept_emps}

    # Fetch logs for the range
    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": list(emp_map.keys())},
        "date":     {"$gte": from_, "$lte": to},
    }))

    logs_by_date: dict = defaultdict(list)
    for doc in logs:
        logs_by_date[doc["date"]].append(doc)

    rows = []
    # Moving avg: window of last 7 rows (nulls included for sizing); avg non-null values
    rate_window: list[Optional[float]] = []

    current = d_from
    while current <= d_to:
        date_str   = current.date().isoformat()
        is_working = current.weekday() < 5

        headcount     = sum(1 for e in dept_emps if e["joined_on"] <= date_str)
        present_count = 0.0
        late_count    = 0

        for doc in logs_by_date.get(date_str, []):
            if doc.get("status") in PRESENCE_STATUSES:
                present_count += 0.5 if doc.get("half_day") else 1.0
            if (doc.get("late_minutes") or 0) > 0:
                late_count += 1

        # attendance_rate: only on working days with headcount > 0
        if is_working and headcount > 0:
            attendance_rate: Optional[float] = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        # Sliding window of last 7 rows (nulls included); average non-null
        rate_window.append(attendance_rate)
        if len(rate_window) > 7:
            rate_window.pop(0)
        non_null    = [r for r in rate_window if r is not None]
        moving_avg: Optional[float] = (
            round_half_up(sum(non_null) / len(non_null), 4) if non_null else None
        )

        rows.append({
            "date":            date_str,
            "is_working_day":  is_working,
            "headcount":       headcount,
            "present_count":   present_count,
            "late_count":      late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d":   moving_avg,
        })
        current += timedelta(days=1)

    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_:      Optional[str] = Query(default=None, alias="from"),
    to:         Optional[str] = None,
    page:       int           = Query(default=1, ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    collection    = "attendance_logs"
    explain_result = None

    if endpoint == "attendance_list":
        q: dict = {}
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
        explain_result = db.command(
            "explain",
            {
                "find":   "attendance_logs",
                "filter": q,
                "sort":   {"date": -1, "emp_code": 1},
                "skip":   skip,
                "limit":  page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        first, last = _month_bounds(month)
        # Explain the aggregation exactly as the endpoint runs it
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        first, last = _month_bounds(month)
        emp_q2: dict = {"joined_on": {"$lte": last}}
        if department:
            emp_q2["department"] = department
        emp_codes2 = [e["emp_code"] for e in db.employees.find(emp_q2, {"emp_code": 1})]
        # Explain the heavy attendance_logs query
        explain_result = db.command(
            "explain",
            {
                "find":   "attendance_logs",
                "filter": {
                    "emp_code": {"$in": emp_codes2},
                    "date":     {"$gte": first, "$lte": last},
                },
            },
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        first, last = _month_bounds(month)
        emp_q3: dict = {}
        if department:
            emp_q3["department"] = department
        emp_codes3 = [e["emp_code"] for e in db.employees.find(emp_q3, {"emp_code": 1})]
        pipeline = [
            {
                "$match": {
                    "emp_code":     {"$in": emp_codes3},
                    "date":         {"$gte": first, "$lte": last},
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, detail="department, from, and to are required for department_trend")
        # Get actual emp_codes for this department — same as the real endpoint
        trend_emp_codes = [
            e["emp_code"] for e in db.employees.find(
                {"department": department, "joined_on": {"$lte": to}}, {"emp_code": 1}
            )
        ]
        explain_result = db.command(
            "explain",
            {
                "find":   "attendance_logs",
                "filter": {
                    "emp_code": {"$in": trend_emp_codes},
                    "date":     {"$gte": from_, "$lte": to},
                },
                "sort": {"date": 1, "emp_code": 1},
            },
            verbosity="executionStats",
        )

    explain_clean = json.loads(json_util.dumps(explain_result))
    return {"endpoint": endpoint, "collection": collection, "explain": explain_clean}
