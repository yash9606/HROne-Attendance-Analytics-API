import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.main import (
    EmployeeIn,
    PunchInIn,
    PunchOutIn,
    RegularizeIn,
    create_employee,
    db,
    punch_in,
    punch_out,
    regularize,
    save_if_unchanged,
)
from tests.conftest import TEST_DB

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def epoch_ms(year, month, day, hour, minute, second=0, millis=0) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(moment.timestamp()) * 1000 + millis


def employee_body(code="EMP0001", **overrides):
    body = {
        "emp_code": code,
        "name": "Asha Rao",
        "email": f"{code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    body.update(overrides)
    return body


def create(api, code="EMP0001", **overrides):
    response = api.post("/employees", json=employee_body(code, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_openapi(api):
    health = api.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    schema = api.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    expected = {
        "/health",
        "/employees",
        "/attendance/punch-in",
        "/attendance/punch-out",
        "/attendance",
        "/attendance/{emp_code}/{date}",
        "/analytics/employees/{emp_code}/monthly",
        "/analytics/departments/summary",
        "/analytics/leaderboard/late",
        "/analytics/departments/{department}/trend",
        "/admin/explain/{endpoint}",
    }
    assert expected <= set(paths)
    assert paths["/employees"]["post"]["responses"]["201"]
    assert paths["/attendance/punch-in"]["post"]["responses"]["201"]
    assert paths["/attendance/punch-out"]["post"]["responses"]["200"]


def test_health_reports_database_down(api, monkeypatch):
    class DownAdmin:
        def command(self, *args, **kwargs):
            from pymongo.errors import ServerSelectionTimeoutError

            raise ServerSelectionTimeoutError("down")

    class DownClient:
        admin = DownAdmin()

    monkeypatch.setattr("app.main.client", DownClient())
    response = api.get("/health")
    assert response.status_code == 503
    assert isinstance(response.json()["detail"], str)


def test_create_and_list_employees(api):
    created = create(api, "EMP0002", department="Sales", name="Neha Iyer")
    assert set(created) == {
        "emp_code",
        "name",
        "email",
        "department",
        "shift_start",
        "shift_end",
        "joined_on",
        "created_at",
    }
    assert "_id" not in created
    assert isinstance(created["created_at"], int)
    assert created["created_at"] >= 100_000_000_000
    create(api, "EMP0001")
    create(api, "EMP0003", department="Sales")

    page = api.get("/employees", params={"page": 1, "page_size": 2})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert [item["emp_code"] for item in body["items"]] == ["EMP0001", "EMP0002"]

    second = api.get("/employees", params={"page": 2, "page_size": 2})
    assert [item["emp_code"] for item in second.json()["items"]] == ["EMP0003"]

    sales = api.get("/employees", params={"department": "Sales"})
    assert sales.json()["total"] == 2
    assert {item["emp_code"] for item in sales.json()["items"]} == {"EMP0002", "EMP0003"}

    duplicate = api.post("/employees", json=employee_body("EMP0001"))
    assert duplicate.status_code == 409
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"emp_code": "EMP12", "name": "A", "email": "a@b.co", "department": "Engineering", "joined_on": "2026-01-05"},
        {"emp_code": "EMP0001", "name": "", "email": "a@b.co", "department": "Engineering", "joined_on": "2026-01-05"},
        {"emp_code": "EMP0001", "name": "A", "email": "not-an-email", "department": "Engineering", "joined_on": "2026-01-05"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Engineering", "joined_on": "2026-7-01"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Engineering", "joined_on": "2026-02-31"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Engineering", "shift_start": "09:30", "shift_end": "09:30", "joined_on": "2026-01-05"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Engineering", "shift_start": "9:30", "joined_on": "2026-01-05"},
    ],
)
def test_employee_validation(api, payload):
    response = api.post("/employees", json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_malformed_json(api):
    response = api.post(
        "/employees",
        content=b"{",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_pagination_bounds(api):
    create(api)
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422
    assert api.get("/attendance", params={"page_size": 0}).status_code == 422


def test_parallel_employee_create(api):
    barrier = threading.Barrier(6)
    body = employee_body("EMP0099")

    def attempt():
        barrier.wait(timeout=5)
        try:
            create_employee(EmployeeIn(**body))
            return 201
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda _: attempt(), range(6)))
    assert codes.count(201) == 1
    assert codes.count(409) == 5
    assert db.employees.count_documents({"emp_code": "EMP0099"}) == 1


def test_punch_rules_and_rounding(api):
    create(api)
    # 09:40:00.900 is truncated to 09:40:00 and is inside the 10-minute grace.
    boundary = epoch_ms(2026, 7, 6, 9, 40, 0, millis=900)
    early = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": boundary},
    )
    assert early.status_code == 201, early.text
    record = early.json()
    assert set(record) == {
        "emp_code",
        "date",
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    }
    assert record["date"] == "2026-07-06"
    assert record["late_minutes"] == 0
    assert record["punch_in"] == boundary - 900
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["half_day"] is False
    assert record["history"] == []
    assert "id" not in record and "_id" not in record

    duplicate = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 50)},
    )
    assert duplicate.status_code == 409

    # A different day: 09:40:01 is 10 whole minutes late, not zero.
    late = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 7, 9, 40, 1)},
    )
    assert late.status_code == 201
    assert late.json()["late_minutes"] == 10

    later = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 8, 10, 15, 59)},
    )
    assert later.status_code == 201
    assert later.json()["late_minutes"] == 45

    # 3618 seconds is 1.005 hours. Half-up is 1.01; Python round() yields 1.0.
    short = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 40) + 3618 * 1000},
    )
    assert short.status_code == 200, short.text
    assert short.json()["work_hours"] == 1.01
    assert short.json()["half_day"] is True
    assert short.json()["overtime_minutes"] == 0

    # Exactly 4.50 hours is not a half day. 4 hours 29 minutes is.
    create(api, "EMP0002")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    full_half = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": epoch_ms(2026, 7, 6, 14, 0)},
    )
    assert full_half.json()["work_hours"] == 4.5
    assert full_half.json()["half_day"] is False

    create(api, "EMP0003")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0003", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    under = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0003", "punched_at": epoch_ms(2026, 7, 6, 13, 59)},
    )
    assert under.json()["work_hours"] == 4.48
    assert under.json()["half_day"] is True

    # 29 minutes after shift end is not overtime. 30 minutes is.
    create(api, "EMP0004")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0004", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    under_ot = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0004", "punched_at": epoch_ms(2026, 7, 6, 18, 59)},
    )
    assert under_ot.json()["overtime_minutes"] == 0

    create(api, "EMP0005")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    enough_ot = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 6, 19, 0)},
    )
    assert enough_ot.json()["overtime_minutes"] == 30
    assert enough_ot.json()["work_hours"] == 9.5

    # One second short of 30 minutes floors to 29, which is not overtime.
    create(api, "EMP0006")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0006", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    almost_ot = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0006", "punched_at": epoch_ms(2026, 7, 6, 18, 59, 59)},
    )
    assert almost_ot.json()["overtime_minutes"] == 0

    # 4:29:42 is 4.495 hours, which rounds half-up to 4.50, so R5 says not a half day.
    create(api, "EMP0007")
    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0007", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    rounded_up = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0007", "punched_at": epoch_ms(2026, 7, 6, 13, 59, 42)},
    )
    assert rounded_up.json()["work_hours"] == 4.5
    assert rounded_up.json()["half_day"] is False

    again = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 6, 19, 10)},
    )
    assert again.status_code == 409


def test_punch_errors(api):
    missing = api.post("/attendance/punch-in", json={"emp_code": "EMP4040", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    assert missing.status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP4040"}).status_code == 404

    create(api)
    no_open = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 18, 30)},
    )
    assert no_open.status_code == 404

    for payload in (
        {"emp_code": "EMP0001", "punched_at": 1_700_000_000},
        {"emp_code": "EMP0001", "punched_at": 99_999_999_999},
        {"emp_code": "EMP0001", "punched_at": 4_102_444_800_001},
        {"emp_code": "EMP0001", "punched_at": 1.5},
        {"emp_code": "EMP0001", "punched_at": 1783312500000.0},
        {"emp_code": "EMP0001", "punched_at": True},
        {"emp_code": "EMP0001", "punched_at": None},
        {"emp_code": "EMP0001", "punched_at": "1783312500000"},
        {"emp_code": "EMP0001", "status": "ABSENT"},
        {"emp_code": "EMP0001", "status": "LEAVE"},
        {"status": "PRESENT"},
    ):
        response = api.post("/attendance/punch-in", json=payload)
        assert response.status_code == 422, payload

    api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    same = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)},
    )
    assert same.status_code == 422
    # Business-rule 422s use the same list-shaped body as schema validation.
    assert isinstance(same.json()["detail"], list)
    assert same.json()["detail"][0]["msg"]
    too_late = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 7, 9, 30, 1)},
    )
    assert too_late.status_code == 422
    exact = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 7, 9, 30)},
    )
    assert exact.status_code == 200, exact.text

    # Omitting punched_at means "now"; the date is today's IST date for a day shift.
    create(api, "EMP0010", shift_start="00:00", shift_end="23:59")
    now_punch = api.post("/attendance/punch-in", json={"emp_code": "EMP0010"})
    assert now_punch.status_code == 201, now_punch.text
    assert now_punch.json()["date"] == datetime.now(IST).date().isoformat()
    assert now_punch.json()["punch_in"] % 1000 == 0


def test_overnight_punch_and_which_record_closes(api):
    create(api, "EMP0005", shift_start="22:00", shift_end="06:00", department="Support")
    early_morning = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 7, 5, 59)},
    )
    assert early_morning.status_code == 201
    assert early_morning.json()["date"] == "2026-07-06"

    conflict = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 6, 22, 10)},
    )
    assert conflict.status_code == 409

    next_shift = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 7, 6, 0)},
    )
    assert next_shift.status_code == 201
    assert next_shift.json()["date"] == "2026-07-07"

    create(api, "EMP0008", shift_start="22:00", shift_end="06:00")
    started = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0008", "punched_at": epoch_ms(2026, 7, 6, 21, 55)},
    )
    assert started.json()["date"] == "2026-07-06"
    assert started.json()["late_minutes"] == 0
    closed = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0008", "punched_at": epoch_ms(2026, 7, 7, 6, 40)},
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["date"] == "2026-07-06"
    assert body["work_hours"] == 8.75
    assert body["overtime_minutes"] == 40
    assert body["half_day"] is False

    # Two open days: punch-out closes the latest punch_in at or before that time.
    create(api, "EMP0009")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0009", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0009", "punched_at": epoch_ms(2026, 7, 7, 9, 30)})
    first = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0009", "punched_at": epoch_ms(2026, 7, 6, 18, 30)},
    )
    assert first.json()["date"] == "2026-07-06"
    still_open = db.attendance_logs.find_one({"emp_code": "EMP0009", "date": "2026-07-07"})
    assert still_open["punch_out"] is None
    second = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0009", "punched_at": epoch_ms(2026, 7, 7, 18, 0)},
    )
    assert second.json()["date"] == "2026-07-07"


def test_parallel_punch_in_and_punch_out(api):
    create(api, "EMP1001")
    stamp = epoch_ms(2026, 7, 6, 9, 31)
    barrier = threading.Barrier(8)

    def attempt_in():
        barrier.wait(timeout=5)
        try:
            punch_in(PunchInIn(emp_code="EMP1001", punched_at=stamp))
            return 201
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: attempt_in(), range(8)))
    assert codes.count(201) == 1
    assert codes.count(409) == 7
    assert db.attendance_logs.count_documents({"emp_code": "EMP1001", "date": "2026-07-06"}) == 1

    out_barrier = threading.Barrier(8)
    out_at = epoch_ms(2026, 7, 6, 18, 40)

    def attempt_out():
        out_barrier.wait(timeout=5)
        try:
            punch_out(PunchOutIn(emp_code="EMP1001", punched_at=out_at))
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        out_codes = list(pool.map(lambda _: attempt_out(), range(8)))
    assert out_codes.count(200) == 1
    assert out_codes.count(409) == 7
    saved = db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-07-06"})
    assert saved["punch_out"] is not None
    assert saved["overtime_minutes"] == 0


def test_unique_index_is_the_punch_lock(api):
    assert db.name == TEST_DB
    names = {index["name"] for index in db.attendance_logs.list_indexes()}
    assert "uq_attendance_employee_date" in names
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            db.attendance_logs.insert_one(
                {"emp_code": "EMP1002", "date": "2026-07-06", "status": "PRESENT"}
            )
            return "ok"
        except DuplicateKeyError:
            return "dup"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["dup", "ok"]


def test_list_attendance_contract(api):
    create(api, "EMP0001")
    create(api, "EMP0002")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 7, 9, 30), "status": "WFH"})

    listed = api.get("/attendance", params={"page": 1, "page_size": 10})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 3
    assert [item["date"] + item["emp_code"] for item in body["items"]] == [
        "2026-07-07EMP0001",
        "2026-07-06EMP0001",
        "2026-07-06EMP0002",
    ]
    assert "id" not in body["items"][0]
    assert isinstance(body["items"][0]["punch_in"], int)

    window = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"})
    assert window.json()["total"] == 1
    assert window.json()["items"][0]["status"] == "WFH"
    wfh = api.get("/attendance", params={"status": "WFH", "emp_code": "EMP0001"})
    assert wfh.json()["total"] == 1
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-06"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "06-07-2026"}).status_code == 422
    assert api.get("/attendance", params={"status": "SICK"}).status_code == 422

    page = api.get("/attendance", params={"page": 2, "page_size": 2})
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1


def test_regularize_history_and_recompute(api):
    create(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 18, 30)})
    original = api.get("/attendance").json()["items"][0]
    assert original["late_minutes"] == 35
    assert original["work_hours"] == 8.42

    corrected = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": epoch_ms(2026, 7, 6, 9, 28),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
            "work_hours": 1,
        },
    )
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["punch_in"] == epoch_ms(2026, 7, 6, 9, 28)
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 9.03
    assert body["work_hours"] != 1
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert set(entry) == {"at", "by", "reason", "changes"}
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    assert entry["changes"]["punch_in"]["from"] == epoch_ms(2026, 7, 6, 10, 5)
    assert entry["changes"]["punch_in"]["to"] == epoch_ms(2026, 7, 6, 9, 28)

    raw = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert isinstance(raw["history"][0]["at"], datetime)
    assert isinstance(raw["history"][0]["changes"]["punch_in"]["from"], datetime)

    noop = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_in": epoch_ms(2026, 7, 6, 9, 28), "reason": "same value", "regularized_by": "hr.admin"},
    )
    assert noop.status_code == 422

    second = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "WFH", "reason": "worked from home", "regularized_by": "hr.admin"},
    )
    assert second.status_code == 200
    assert len(second.json()["history"]) == 2
    assert set(second.json()["history"][1]["changes"]) == {"status"}
    assert second.json()["history"][0] == entry

    absent = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ABSENT", "reason": "approved absence", "regularized_by": "hr.admin"},
    )
    assert absent.status_code == 200
    assert absent.json()["punch_in"] is None
    assert absent.json()["punch_out"] is None
    assert absent.json()["work_hours"] is None
    assert absent.json()["late_minutes"] == 0
    assert absent.json()["half_day"] is False

    with_punches = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "LEAVE",
            "punch_in": epoch_ms(2026, 7, 6, 9, 30),
            "reason": "should fail",
            "regularized_by": "hr.admin",
        },
    )
    assert with_punches.status_code == 422


def test_regularize_failures(api):
    assert api.patch(
        "/attendance/EMP4040/2026-07-06",
        json={"status": "LEAVE", "reason": "missing person", "regularized_by": "hr"},
    ).status_code == 404
    create(api)
    assert api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "LEAVE", "reason": "no record yet", "regularized_by": "hr"},
    ).status_code == 404
    assert api.patch(
        "/attendance/EMP0001/2026-7-6",
        json={"status": "LEAVE", "reason": "bad date", "regularized_by": "hr"},
    ).status_code == 422
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    wrong_day = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_in": epoch_ms(2026, 7, 7, 10, 0), "reason": "wrong day", "regularized_by": "hr"},
    )
    assert wrong_day.status_code == 422
    bad_gap = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_out": epoch_ms(2026, 7, 7, 10, 0),
            "reason": "too long",
            "regularized_by": "hr",
        },
    )
    assert bad_gap.status_code == 422
    assert api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "nope", "regularized_by": "hr"},
    ).status_code == 422
    assert api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "WFH", "reason": "no", "regularized_by": "hr"},
    ).status_code == 422
    for nulls in ({"punch_out": None}, {"punch_in": None}, {"status": None}):
        response = api.patch(
            "/attendance/EMP0001/2026-07-06",
            json={**nulls, "reason": "explicit null", "regularized_by": "hr"},
        )
        assert response.status_code == 422, nulls
    noop = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "PRESENT", "reason": "same status", "regularized_by": "hr"},
    )
    assert noop.status_code == 422
    assert isinstance(noop.json()["detail"], list)

    create(api, "EMP0007", shift_start="22:00", shift_end="06:00")
    db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0007",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 16, 30, tzinfo=UTC),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    )
    moved = api.patch(
        "/attendance/EMP0007/2026-07-06",
        json={
            "punch_in": epoch_ms(2026, 7, 7, 5, 30),
            "reason": "still this shift",
            "regularized_by": "hr",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["date"] == "2026-07-06"


def test_regularize_does_not_clobber_a_concurrent_write(api, monkeypatch):
    create(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    real = save_if_unchanged

    def sneak(collection, filt, update):
        collection.update_one(
            {"_id": filt["_id"]},
            {
                "$set": {"status": "WFH"},
                "$push": {
                    "history": {
                        "at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC),
                        "by": "other",
                        "reason": "won the race",
                        "changes": {"status": {"from": "PRESENT", "to": "WFH"}},
                    }
                },
            },
        )
        return real(collection, filt, update)

    monkeypatch.setattr("app.main.save_if_unchanged", sneak)
    response = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ON_DUTY", "reason": "losing update", "regularized_by": "hr"},
    )
    assert response.status_code == 409
    saved = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert saved["status"] == "WFH"
    assert saved["history"][0]["by"] == "other"
    assert len(saved["history"]) == 1


def test_parallel_regularizations_keep_every_history_entry(api):
    create(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 30)})
    statuses = ["WFH", "ON_DUTY", "WFH", "ON_DUTY", "WFH", "ON_DUTY"]
    barrier = threading.Barrier(len(statuses))

    def attempt(status):
        barrier.wait(timeout=5)
        try:
            regularize(
                "EMP0001",
                "2026-07-06",
                RegularizeIn(status=status, reason=f"set {status}", regularized_by=status.lower()),
            )
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=len(statuses)) as pool:
        codes = list(pool.map(attempt, statuses))
    assert set(codes) <= {200, 409, 422}
    assert codes.count(200) >= 1
    saved = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    # Every 200 left exactly one entry, and the last entry matches the stored status.
    assert len(saved["history"]) == codes.count(200)
    assert saved["history"][-1]["changes"]["status"]["to"] == saved["status"]
    for before, after in zip(saved["history"], saved["history"][1:]):
        assert after["changes"]["status"]["from"] == before["changes"]["status"]["to"]


def test_health_recreates_indexes_after_a_late_start(api, monkeypatch):
    db.attendance_logs.drop_index("attendance_date_emp")
    monkeypatch.setattr("app.main.indexes_ready", False)
    assert api.get("/health").status_code == 200
    names = {index["name"] for index in db.attendance_logs.list_indexes()}
    assert "attendance_date_emp" in names


def test_legacy_record_defaults(api):
    create(api, "EMP0002", name="Vikram Shah", joined_on="2026-07-13")
    db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0002",
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
            "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
            "work_hours": 8.98,
            "late_minutes": 0,
            "overtime_minutes": 0,
        }
    )
    listed = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert listed["half_day"] is False
    assert listed["history"] == []
    assert listed["work_hours"] == 8.98
    assert "_id" not in listed
