"""Employee Attendance & Analytics API.

Instants cross the HTTP boundary as epoch milliseconds and are stored as UTC
datetimes. Calendar dates and shift times are IST (UTC+05:30), kept as strings.
Derived attendance fields are stored when a record is written; analytics read
those stored values and do not recompute them from punch times.
"""

from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# Real environment variables win. A local .env is only a fallback.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
WEEKDAYS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: Monday=2 ... Friday=6

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000
GRACE_SECONDS = 10 * 60
OVERTIME_MINIMUM_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_PUNCH_GAP = timedelta(hours=24)
MAX_TREND_DAYS = 92

TRACKED_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5000,
)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Time, rounding, and shift rules (R1-R5, R8)
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    value = as_utc(value).replace(microsecond=0)
    return int(value.timestamp()) * 1000


def unprocessable(loc: list, message: str) -> HTTPException:
    """422 with FastAPI's validation-error body; the contract's 422 schema is a list."""
    return HTTPException(
        status_code=422,
        detail=[{"type": "value_error", "loc": loc, "msg": message}],
    )


def epoch_ms_to_utc(value: int) -> datetime:
    """Truncate to whole seconds before any attendance calculation (R1)."""
    if type(value) is not int or not EPOCH_MS_MIN <= value <= EPOCH_MS_MAX:
        raise unprocessable(["body"], "timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value // 1000, UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)


def as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def round_half_up(value: Any, places: int) -> float:
    return float(quantize(as_decimal(value), places))


def parse_day(value: str, loc: list) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise unprocessable(loc, "date must be YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise unprocessable(loc, "date must be YYYY-MM-DD")
    return value


def parse_month(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise unprocessable(["query", "month"], "month must be YYYY-MM")
    year, month_number = map(int, month.split("-"))
    last = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def elapsed_seconds(start: datetime, end: datetime) -> int:
    return int(as_utc(end).timestamp()) - int(as_utc(start).timestamp())


def shift_bounds(employee: dict, day: str) -> tuple[datetime, datetime]:
    """IST shift window for an attendance date. Overnight shifts end the next day."""
    day_value = date.fromisoformat(day)
    start_time = parse_hhmm(employee["shift_start"])
    end_time = parse_hhmm(employee["shift_end"])
    start = datetime.combine(day_value, start_time, IST)
    end = datetime.combine(day_value, end_time, IST)
    if end_time <= start_time:
        end += timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def attendance_date_for(punch: datetime, employee: dict) -> str:
    """R1. Overnight: a punch earlier than shift_end belongs to the previous day."""
    local = as_utc(punch).astimezone(IST)
    start_time = parse_hhmm(employee["shift_start"])
    end_time = parse_hhmm(employee["shift_end"])
    day = local.date()
    local_time = local.time().replace(tzinfo=None)
    if end_time <= start_time and local_time < end_time:
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch_in: datetime | None, employee: dict, day: str) -> int:
    """R2. Late only after a strict 10-minute grace; minutes are since shift start."""
    if punch_in is None:
        return 0
    start, _ = shift_bounds(employee, day)
    elapsed = elapsed_seconds(start, punch_in)
    if elapsed <= GRACE_SECONDS:
        return 0
    return elapsed // 60


def work_hours_between(punch_in: datetime, punch_out: datetime) -> float:
    """R4. Decimal half-up, not binary-float or banker's rounding."""
    seconds = elapsed_seconds(punch_in, punch_out)
    return float(quantize(Decimal(seconds) / Decimal(3600), 2))


def overtime_minutes(punch_out: datetime | None, employee: dict, day: str) -> int:
    """R3. Whole minutes after shift end, counted only when at least 30."""
    if punch_out is None:
        return 0
    _, end = shift_bounds(employee, day)
    minutes = elapsed_seconds(end, punch_out) // 60
    if minutes >= OVERTIME_MINIMUM_MINUTES:
        return minutes
    return 0


def derived_fields(
    punch_in: datetime | None,
    punch_out: datetime | None,
    employee: dict,
    day: str,
) -> dict:
    late = late_minutes(punch_in, employee, day)
    if punch_in is None or punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    hours = work_hours_between(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(punch_out, employee, day),
        "half_day": as_decimal(hours) < HALF_DAY_HOURS,
    }


def working_days(start: date, end: date) -> int:
    """R7. Monday-Friday inclusive. No holiday calendar."""
    if start > end:
        return 0
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def date_filter(date_from: str | None, date_to: str | None) -> dict:
    start = parse_day(date_from, ["query", "date_from"]) if date_from is not None else None
    end = parse_day(date_to, ["query", "date_to"]) if date_to is not None else None
    if start and end and start > end:
        raise unprocessable(["query", "date_from"], "date_from must be on or before date_to")
    clause: dict[str, str] = {}
    if start:
        clause["$gte"] = start
    if end:
        clause["$lte"] = end
    return {"date": clause} if clause else {}


def parse_trend_range(start: str, end: str) -> tuple[str, str]:
    start = parse_day(start, ["query", "from"])
    end = parse_day(end, ["query", "to"])
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise unprocessable(["query", "to"], "to must be on or after from")
    if (end_day - start_day).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], "range cannot exceed 92 days")
    return start, end


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
indexes_ready = False


def ensure_indexes() -> None:
    """Create the indexes the queries need. Safe to run on every startup."""
    global indexes_ready
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_emp",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employees_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_emp",
    )
    indexes_ready = True


def save_if_unchanged(collection, filt: dict, update: dict):
    """Update one document only when `filt` still matches.

    MongoDB applies this atomically. A miss means a concurrent request won.
    """
    return collection.find_one_and_update(
        filt, update, return_document=ReturnDocument.AFTER
    )


def snapshot_filter(doc: dict) -> dict:
    filt: dict[str, Any] = {"_id": doc["_id"]}
    for field in (*TRACKED_FIELDS, "history"):
        if field in doc:
            filt[field] = doc[field]
        else:
            filt[field] = {"$exists": False}
    return filt


def require_employee(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return employee


def normalize_state(doc: dict, fields: dict | None = None) -> dict:
    source = doc if fields is None else {**doc, **fields}
    punch_in = source.get("punch_in")
    punch_out = source.get("punch_out")
    hours = source.get("work_hours")
    return {
        "status": source.get("status"),
        "punch_in": None if punch_in is None else as_utc(punch_in).replace(microsecond=0),
        "punch_out": None if punch_out is None else as_utc(punch_out).replace(microsecond=0),
        "work_hours": None if hours is None else round_half_up(hours, 2),
        "late_minutes": int(source.get("late_minutes") or 0),
        "overtime_minutes": int(source.get("overtime_minutes") or 0),
        "half_day": bool(source.get("half_day")),
    }


def changes_between(old: dict, new: dict) -> dict:
    changes = {}
    for field in TRACKED_FIELDS:
        if old[field] != new[field]:
            changes[field] = {"from": old[field], "to": new[field]}
    return changes


def serialize_change_value(field: str, value: Any) -> Any:
    if field in ("punch_in", "punch_out"):
        return to_epoch_ms(value) if isinstance(value, datetime) else value
    return value


def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_epoch_ms(created) if isinstance(created, datetime) else created,
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": serialize_change_value(field, change.get("from")),
                "to": serialize_change_value(field, change.get("to")),
            }
        history.append(
            {
                "at": to_epoch_ms(entry.get("at")) if isinstance(entry.get("at"), datetime) else entry.get("at"),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    hours = doc.get("work_hours")
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else None,
        "punch_out": to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else None,
        "work_hours": None if hours is None else hours,
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def aggregate(collection_name: str, pipeline: list, hint: str | None = None) -> list:
    options: dict[str, Any] = {"allowDiskUse": True}
    if hint:
        options["hint"] = hint
    return list(db[collection_name].aggregate(pipeline, **options))


def explain_command(command: dict) -> dict:
    raw = db.command("explain", command, verbosity="executionStats")
    # Extended JSON keeps the plan text and drops BSON-only types.
    return json.loads(json_util.dumps(raw))


# --------------------------------------------------------------------------- #
# Aggregation building blocks. Analytics read stored derived fields.
# --------------------------------------------------------------------------- #
def weekday_expr(date_field: str) -> dict:
    # Missing log dates (an employee with no attendance) are not weekdays.
    return {
        "$cond": [
            {"$eq": [{"$type": date_field}, "string"]},
            {
                "$in": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}},
                    WEEKDAYS,
                ]
            },
            False,
        ]
    }


def present_days_expr(status_field: str, half_field: str, date_field: str) -> dict:
    """R5-R7. Presence on Monday-Friday; a stored half day counts as 0.5."""
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [status_field, PRESENCE]},
                    weekday_expr(date_field),
                ]
            },
            {"$cond": [{"$eq": [{"$ifNull": [half_field, False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def present_count_expr(status_field: str, half_field: str) -> dict:
    """Presence on any calendar day. Weekends still count here."""
    return {
        "$cond": [
            {"$in": [status_field, PRESENCE]},
            {"$cond": [{"$eq": [{"$ifNull": [half_field, False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def late_count_expr(field: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [field, 0]}, 0]}, 1, 0]}


def has_present_hours(status_field: str, hours_field: str) -> dict:
    return {
        "$and": [
            {"$in": [status_field, PRESENCE]},
            {"$ne": [{"$ifNull": [hours_field, None]}, None]},
        ]
    }


def half_up_expr(expr: dict, places: int) -> dict:
    """Half-up for a non-negative value, kept as Decimal128. $round is half-even."""
    factor = str(10**places)
    return {
        "$divide": [
            {
                "$trunc": {
                    "$add": [
                        {"$multiply": [{"$toDecimal": expr}, {"$toDecimal": factor}]},
                        {"$toDecimal": "0.5"},
                    ]
                }
            },
            {"$toDecimal": factor},
        ]
    }


def employee_month_pipeline(emp_code: str, month: str) -> list:
    start, end = parse_month(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": present_days_expr("$status", "$half_day", "$date")
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": late_count_expr("$late_minutes")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None) -> tuple[list, str]:
    start, end = parse_month(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    hint = "employees_joined_on"
    if department is not None:
        match["department"] = department
        hint = "department_joined"
    hours_ready = has_present_hours("$logs.status", "$logs.work_hours")
    pipeline = [
        {"$match": match},
        {
            # Equality join plus a plain range match: served by uq_attendance_employee_date.
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                            "date": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": present_days_expr("$logs.status", "$logs.half_day", "$logs.date")
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [hours_ready, {"$toDecimal": "$logs.work_hours"}, 0]
                    }
                },
                "hours_count": {"$sum": {"$cond": [hours_ready, 1, 0]}},
                "late_count": {"$sum": late_count_expr("$logs.late_minutes")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "hours_sum": 1,
                "hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline, hint


def format_department_row(row: dict) -> dict:
    count = int(row.get("hours_count") or 0)
    if count:
        average = quantize(as_decimal(row["hours_sum"]) / Decimal(count), 2)
        avg_work_hours: float | None = float(average)
    else:
        avg_work_hours = None
    return {
        "department": row["department"],
        "headcount": int(row["headcount"]),
        "present_days": round_half_up(row.get("present_days") or 0, 2),
        "avg_work_hours": avg_work_hours,
        "late_count": int(row.get("late_count") or 0),
        "total_late_minutes": int(row.get("total_late_minutes") or 0),
        "leave_count": int(row.get("leave_count") or 0),
        "on_duty_count": int(row.get("on_duty_count") or 0),
    }


def leaderboard_pipeline(
    month: str, limit: int, department: str | None
) -> tuple[list, str]:
    start, end = parse_month(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": start, "$lte": end},
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
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    # Rank on late minutes only. emp_code must not break ties or $rank becomes unique.
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
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
            },
        ]
    )
    return pipeline, "attendance_date_emp"


def format_leaderboard_row(row: dict) -> dict:
    return {
        "rank": int(row["rank"]),
        "emp_code": row["emp_code"],
        "name": row.get("name"),
        "department": row.get("department"),
        "total_late_minutes": int(row["total_late_minutes"]),
        "late_count": int(row["late_count"]),
    }


def department_codes(department: str) -> list[str]:
    """The department's emp_codes; the trend matches logs by these, not by a per-log join."""
    cursor = db.employees.find(
        {"department": department}, {"_id": 0, "emp_code": 1}
    ).hint("department_emp")
    return [doc["emp_code"] for doc in cursor]


def trend_pipeline(
    department: str, start: str, end: str, emp_codes: list[str]
) -> tuple[list, str]:
    start_dt = datetime.combine(date.fromisoformat(start), time.min, UTC)
    # The upper $densify bound is exclusive, so it is the midnight after `end`.
    end_dt = datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, UTC)
    pipeline = [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": present_count_expr("$status", "$half_day")},
                "late_count": {"$sum": late_count_expr("$late_minutes")},
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
            }
        },
        {"$match": {"day": {"$gte": start_dt, "$lt": end_dt}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up_expr(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        # $avg ignores null rates, so weekends do not dilute the 7-day window.
        # Rates are Decimal128, so the mean is exact before the final half-up.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline, "uq_attendance_employee_date"


def format_trend_row(row: dict) -> dict:
    rate = row.get("attendance_rate")
    moving = row.get("moving_avg_7d")
    return {
        "date": row["date"],
        "is_working_day": bool(row["is_working_day"]),
        "headcount": int(row["headcount"]),
        "present_count": round_half_up(row.get("present_count") or 0, 2),
        "late_count": int(row.get("late_count") or 0),
        "attendance_rate": None if rate is None else round_half_up(rate, 4),
        "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
    }


def attendance_list_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[dict, str, int]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    query.update(date_filter(date_from, date_to))
    if status is not None:
        query["status"] = status
    if emp_code is not None:
        hint = "uq_attendance_employee_date"
    elif status is not None:
        hint = "attendance_status_date_emp"
    else:
        hint = "attendance_date_emp"
    return query, hint, (page - 1) * page_size


def find_command(query: dict, hint: str, skip: int, limit: int) -> dict:
    return {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": skip,
        "limit": limit,
        "hint": hint,
    }


def aggregate_command(collection_name: str, pipeline: list, hint: str) -> dict:
    return {
        "aggregate": collection_name,
        "pipeline": pipeline,
        "cursor": {},
        "allowDiskUse": True,
        "hint": hint,
    }


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except ConnectionFailure:
        # MongoDB is not reachable yet. /health retries before it reports ready.
        pass
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ConnectionFailure)
async def mongo_unavailable(_request, _exc):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD") from None
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def check_epoch_ms(value: int | None) -> int:
    """Omitted fields keep their default and skip this check; an explicit null does not."""
    if value is None:
        raise ValueError("must be epoch milliseconds, not null")
    if not EPOCH_MS_MIN <= value <= EPOCH_MS_MAX:
        raise ValueError("must be epoch milliseconds")
    return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    _punched_at = field_validator("punched_at")(check_epoch_ms)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    _punched_at = field_validator("punched_at")(check_epoch_ms)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    _punches = field_validator("punch_in", "punch_out")(check_epoch_ms)

    @field_validator("status")
    @classmethod
    def status_not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("status cannot be null")
        return value


# --------------------------------------------------------------------------- #
# Employees and attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    stored = body.model_dump()
    stored["created_at"] = now_utc()
    try:
        db.employees.insert_one(stored)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists") from None
    return serialize_employee(stored)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    hint = "department_emp" if department is not None else "uq_emp_code"
    total = db.employees.count_documents(query, hint=hint)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = require_employee(body.emp_code)
    punched = now_utc() if body.punched_at is None else epoch_ms_to_utc(body.punched_at)
    day = attendance_date_for(punched, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(punched, employee, day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index is the lock. Both racers may insert.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from None
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = require_employee(body.emp_code)
    punched = now_utc() if body.punched_at is None else epoch_ms_to_utc(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    punch_in = as_utc(record["punch_in"])
    if punched <= punch_in or punched > punch_in + MAX_PUNCH_GAP:
        raise unprocessable(
            ["body", "punched_at"],
            "punch-out must be after punch-in and within 24 hours",
        )
    fields = derived_fields(punch_in, punched, employee, record["date"])
    updated = save_if_unchanged(
        db.attendance_logs,
        {
            "_id": record["_id"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": {"punch_out": punched, **fields}},
    )
    if updated is None:
        current = db.attendance_logs.find_one({"_id": record["_id"]})
        if current is not None and current.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="already punched out")
        raise HTTPException(status_code=409, detail="record changed concurrently; retry")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, hint, skip = attendance_list_query(
        emp_code, date_from, date_to, status, page, page_size
    )
    total = db.attendance_logs.count_documents(query, hint=hint)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_day(date, ["path", "date"])
    employee = require_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    status = body.status if body.status is not None else record.get("status")
    punch_in = (
        epoch_ms_to_utc(body.punch_in)
        if body.punch_in is not None
        else record.get("punch_in")
    )
    punch_out = (
        epoch_ms_to_utc(body.punch_out)
        if body.punch_out is not None
        else record.get("punch_out")
    )
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(["body", "status"], "ABSENT/LEAVE cannot have punch times")
        punch_in = None
        punch_out = None
    elif punch_in is None:
        raise unprocessable(["body", "punch_in"], "presence status requires punch_in")
    else:
        punch_in = as_utc(punch_in).replace(microsecond=0)
        if punch_out is not None:
            punch_out = as_utc(punch_out).replace(microsecond=0)
        if attendance_date_for(punch_in, employee) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must belong to the record date")
        if punch_out is not None and (
            punch_out <= punch_in or punch_out > punch_in + MAX_PUNCH_GAP
        ):
            raise unprocessable(
                ["body", "punch_out"], "punch_out must be after punch_in and within 24 hours"
            )

    fields = derived_fields(punch_in, punch_out, employee, day)
    new_values = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **fields,
    }
    old_state = normalize_state(record)
    new_state = normalize_state(record, new_values)
    changes = changes_between(old_state, new_state)
    if not changes:
        raise unprocessable(["body"], "request changes nothing")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = save_if_unchanged(
        db.attendance_logs,
        snapshot_filter(record),
        {"$set": new_values, "$push": {"history": entry}},
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    start, end = parse_month(month)
    first = max(date.fromisoformat(start), date.fromisoformat(employee["joined_on"]))
    days = working_days(first, date.fromisoformat(end))
    rows = aggregate(
        "attendance_logs",
        employee_month_pipeline(emp_code, month),
        hint="uq_attendance_employee_date",
    )
    row = rows[0] if rows else {}
    present = quantize(as_decimal(row.get("present_days") or 0), 2)
    if days == 0:
        pct = None
    else:
        # A percentage is a reported number under R8 (2 places). The 4-place rule
        # is for the 0-1 rates in the trend; 2 places of a percentage is the same precision.
        pct = float(quantize(present / Decimal(days) * Decimal(100), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": float(present),
        "leave_days": int(row.get("leave_days") or 0),
        "late_count": int(row.get("late_count") or 0),
        "total_late_minutes": int(row.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes") or 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline, hint = department_summary_pipeline(month, department)
    items = [format_department_row(row) for row in aggregate("employees", pipeline, hint)]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline, hint = leaderboard_pipeline(month, limit, department)
    items = [
        format_leaderboard_row(row)
        for row in aggregate("attendance_logs", pipeline, hint)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(from_date, to_date)
    emp_codes = department_codes(department)
    if not emp_codes:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline, hint = trend_pipeline(department, start, end, emp_codes)
    items = [format_trend_row(row) for row in aggregate("attendance_logs", pipeline, hint)]
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if month is not None:
        parse_month(month)
    if endpoint == "attendance_list":
        query, hint, skip = attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        command = find_command(query, hint, skip, page_size)
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or month is None:
            raise unprocessable(["query"], "emp_code and month are required")
        command = aggregate_command(
            "attendance_logs",
            employee_month_pipeline(emp_code, month),
            "uq_attendance_employee_date",
        )
        collection_name = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            raise unprocessable(["query", "month"], "month is required")
        pipeline, hint = department_summary_pipeline(month, department)
        command = aggregate_command("employees", pipeline, hint)
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise unprocessable(["query", "month"], "month is required")
        pipeline, hint = leaderboard_pipeline(month, limit, department)
        command = aggregate_command("attendance_logs", pipeline, hint)
        collection_name = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise unprocessable(["query"], "department, from and to are required")
        start, end = parse_trend_range(from_date, to_date)
        pipeline, hint = trend_pipeline(
            department, start, end, department_codes(department)
        )
        command = aggregate_command("attendance_logs", pipeline, hint)
        collection_name = "attendance_logs"
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_command(command),
    }
