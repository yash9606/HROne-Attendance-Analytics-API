import json
from datetime import date, datetime, timedelta, timezone

from app.main import db
from tests.test_operations import create, epoch_ms

UTC = timezone.utc


def weekdays(start: date, end: date) -> int:
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def utc(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def log(**fields):
    base = {
        "status": "PRESENT",
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    base.update(fields)
    db.attendance_logs.insert_one(base)


def test_sample_monthly_and_department_numbers(api):
    assert weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15
    people = [
        ("EMP0001", "Asha Rao", "Engineering", "09:30", "18:30", "2026-01-05"),
        ("EMP0002", "Vikram Shah", "Engineering", "09:30", "18:30", "2026-07-13"),
        ("EMP0003", "Neha Iyer", "Sales", "09:30", "18:30", "2026-02-01"),
        ("EMP0004", "Rohit Das", "Sales", "09:30", "18:30", "2026-02-01"),
        ("EMP0005", "Meera Nair", "Support", "22:00", "06:00", "2026-03-10"),
        ("EMP0006", "Karan Bedi", "Support", "09:30", "18:30", "2026-03-10"),
    ]
    for code, name, department, start, end, joined in people:
        create(
            api,
            code,
            name=name,
            department=department,
            shift_start=start,
            shift_end=end,
            joined_on=joined,
        )

    log(emp_code="EMP0001", date="2026-07-06", punch_in=utc(2026, 7, 6, 3, 58), punch_out=utc(2026, 7, 6, 13, 5), work_hours=9.12)
    log(emp_code="EMP0003", date="2026-07-06", punch_in=utc(2026, 7, 6, 4, 35), punch_out=utc(2026, 7, 6, 13, 40), work_hours=9.08, late_minutes=35, overtime_minutes=40)
    log(emp_code="EMP0006", date="2026-07-07", punch_in=utc(2026, 7, 7, 4, 5), punch_out=None, work_hours=None)
    log(emp_code="EMP0004", date="2026-07-07", status="WFH", punch_in=utc(2026, 7, 7, 4, 0), punch_out=utc(2026, 7, 7, 7, 30), work_hours=3.5, half_day=True)
    log(emp_code="EMP0004", date="2026-07-08", status="ABSENT", punch_in=None, punch_out=None, work_hours=None)
    log(emp_code="EMP0003", date="2026-07-08", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)
    log(emp_code="EMP0005", date="2026-07-06", punch_in=utc(2026, 7, 6, 16, 25), punch_out=utc(2026, 7, 7, 1, 10), work_hours=8.75, overtime_minutes=40)
    log(
        emp_code="EMP0001",
        date="2026-07-07",
        punch_in=utc(2026, 7, 7, 3, 58),
        punch_out=utc(2026, 7, 7, 13, 0),
        work_hours=9.03,
        history=[{
            "at": utc(2026, 7, 8, 5, 0),
            "by": "hr.admin",
            "reason": "biometric glitch",
            "changes": {
                "punch_in": {"from": utc(2026, 7, 7, 4, 35), "to": utc(2026, 7, 7, 3, 58)},
                "late_minutes": {"from": 35, "to": 0},
                "work_hours": {"from": 8.42, "to": 9.03},
            },
        }],
    )
    # Legacy shape: half_day and history are omitted and must count as false / [].
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": utc(2026, 7, 14, 4, 2),
        "punch_out": utc(2026, 7, 14, 13, 1),
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    # attendance_pct is a reported number under R8: 2 places half-up (2/23*100 = 8.6956... -> 8.70).
    expected = {
        "EMP0001": dict(working_days=23, present_days=2, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=8.70),
        "EMP0002": dict(working_days=15, present_days=1, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=6.67),
        "EMP0003": dict(working_days=23, present_days=1, leave_days=1, late_count=1, total_late_minutes=35, total_overtime_minutes=40, attendance_pct=4.35),
        "EMP0004": dict(working_days=23, present_days=0.5, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=2.17),
        "EMP0005": dict(working_days=23, present_days=1, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=40, attendance_pct=4.35),
        "EMP0006": dict(working_days=23, present_days=1, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=4.35),
    }
    for code, values in expected.items():
        response = api.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["emp_code"] == code
        assert body["month"] == "2026-07"
        for key, value in values.items():
            assert body[key] == value, (code, key, body[key], value)

    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [item["department"] for item in summary["items"]] == ["Engineering", "Sales", "Support"]
    by_name = {item["department"]: item for item in summary["items"]}
    assert by_name["Engineering"] == {
        "department": "Engineering",
        "headcount": 2,
        "present_days": 3,
        "avg_work_hours": 9.04,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    assert by_name["Sales"] == {
        "department": "Sales",
        "headcount": 2,
        "present_days": 1.5,
        "avg_work_hours": 6.29,
        "late_count": 1,
        "total_late_minutes": 35,
        "leave_count": 1,
        "on_duty_count": 0,
    }
    assert by_name["Support"] == {
        "department": "Support",
        "headcount": 2,
        "present_days": 2,
        "avg_work_hours": 8.75,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    sales = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"})
    assert [item["department"] for item in sales.json()["items"]] == ["Sales"]

    board = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()
    assert board["items"] == [{
        "rank": 1,
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "department": "Sales",
        "total_late_minutes": 35,
        "late_count": 1,
    }]

    trend = api.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-08"},
    )
    assert trend.status_code == 200, trend.text
    assert trend.json()["items"] == [
        {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 1, "late_count": 0, "attendance_rate": 1, "moving_avg_7d": 1},
        {"date": "2026-07-07", "is_working_day": True, "headcount": 1, "present_count": 1, "late_count": 0, "attendance_rate": 1, "moving_avg_7d": 1},
        # (1 + 1 + 0) / 3 = 0.6666..., half-up to 4 places is 0.6667.
        {"date": "2026-07-08", "is_working_day": True, "headcount": 1, "present_count": 0, "late_count": 0, "attendance_rate": 0, "moving_avg_7d": 0.6667},
    ]
    support = api.get(
        "/analytics/departments/Support/trend",
        params={"from": "2026-07-06", "to": "2026-07-07"},
    ).json()["items"]
    # The overnight Support shift is dated July 6, so July 7 is only the open day shift.
    assert support[0]["present_count"] == 1
    assert support[0]["attendance_rate"] == 0.5
    assert support[1]["date"] == "2026-07-07"
    assert support[1]["present_count"] == 1
    assert support[1]["attendance_rate"] == 0.5
    assert support[1]["moving_avg_7d"] == 0.5


def test_empty_joiners_weekends_ties_and_stored_values(api):
    create(api, "EMP3001", department="Quiet", joined_on="2026-07-01")
    create(api, "EMP3002", department="Quiet", joined_on="2026-08-01")
    create(api, "EMP3003", department="Later", joined_on="2026-08-15")
    empty = api.get("/analytics/employees/EMP3001/monthly", params={"month": "2026-07"}).json()
    assert empty["working_days"] == 23
    assert empty["present_days"] == 0
    assert empty["attendance_pct"] == 0
    future = api.get("/analytics/employees/EMP3002/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0
    assert future["attendance_pct"] is None
    assert api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404

    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    quiet = next(item for item in summary["items"] if item["department"] == "Quiet")
    assert quiet["headcount"] == 1
    assert quiet["present_days"] == 0
    assert quiet["avg_work_hours"] is None
    assert all(item["department"] != "Later" for item in summary["items"])

    # Stored derived values win over what the punches would recompute.
    log(
        emp_code="EMP3001",
        date="2026-07-06",
        punch_in=utc(2026, 7, 6, 3, 58),
        punch_out=utc(2026, 7, 6, 13, 0),
        work_hours=1.25,
        late_minutes=99,
        half_day=True,
        overtime_minutes=7,
    )
    # Saturday late still counts toward late totals, not present_days.
    log(
        emp_code="EMP3001",
        date="2026-07-11",
        punch_in=utc(2026, 7, 11, 4, 30),
        punch_out=utc(2026, 7, 11, 12, 30),
        work_hours=8,
        late_minutes=15,
        overtime_minutes=0,
        half_day=False,
    )
    log(emp_code="EMP3001", date="2026-07-12", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)
    monthly = api.get("/analytics/employees/EMP3001/monthly", params={"month": "2026-07"}).json()
    assert monthly["present_days"] == 0.5
    assert monthly["leave_days"] == 1
    assert monthly["late_count"] == 2
    assert monthly["total_late_minutes"] == 114
    assert monthly["total_overtime_minutes"] == 7

    quiet = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Quiet"}).json()["items"][0]
    assert quiet["present_days"] == 0.5
    assert quiet["avg_work_hours"] == 4.63  # (1.25 + 8.00) / 2 = 4.625 -> 4.63
    assert quiet["late_count"] == 2
    assert quiet["total_late_minutes"] == 114
    assert quiet["leave_count"] == 1

    # Average of 9.12 and 9.13 is 9.125, which half-up rounds to 9.13 (not 9.12).
    create(api, "EMP3004", department="Math", joined_on="2026-01-01")
    create(api, "EMP3005", department="Math", joined_on="2026-01-01")
    log(emp_code="EMP3004", date="2026-07-06", punch_in=utc(2026, 7, 6, 4, 0), punch_out=utc(2026, 7, 6, 12, 0), work_hours=9.12)
    log(emp_code="EMP3005", date="2026-07-06", punch_in=utc(2026, 7, 6, 4, 0), punch_out=utc(2026, 7, 6, 12, 0), work_hours=9.13)
    math_row = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Math"}).json()["items"][0]
    assert math_row["avg_work_hours"] == 9.13
    assert math_row["headcount"] == 2

    create(api, "EMP2001", name="Cara", department="Other", joined_on="2026-01-01")
    create(api, "EMP2002", name="Asha", department="Sales", joined_on="2026-01-01")
    create(api, "EMP2003", name="Bela", department="Sales", joined_on="2026-01-01")
    create(api, "EMP2004", name="Dev", department="Sales", joined_on="2026-01-01")
    log(emp_code="EMP2001", date="2026-08-03", late_minutes=100, punch_in=utc(2026, 8, 3, 5, 0), punch_out=utc(2026, 8, 3, 12, 0), work_hours=7)
    log(emp_code="EMP2002", date="2026-08-03", late_minutes=50, punch_in=utc(2026, 8, 3, 5, 0), punch_out=utc(2026, 8, 3, 12, 0), work_hours=7)
    log(emp_code="EMP2003", date="2026-08-04", late_minutes=50, punch_in=utc(2026, 8, 4, 5, 0), punch_out=utc(2026, 8, 4, 12, 0), work_hours=7)
    log(emp_code="EMP2004", date="2026-08-03", late_minutes=40, punch_in=utc(2026, 8, 3, 5, 0), punch_out=utc(2026, 8, 3, 12, 0), work_hours=7)
    board = api.get("/analytics/leaderboard/late", params={"month": "2026-08", "limit": 2}).json()["items"]
    assert [(row["rank"], row["emp_code"], row["total_late_minutes"]) for row in board] == [
        (1, "EMP2001", 100),
        (2, "EMP2002", 50),
        (2, "EMP2003", 50),
    ]
    sales_board = api.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-08", "department": "Sales", "limit": 1},
    ).json()["items"]
    assert [(row["rank"], row["emp_code"]) for row in sales_board] == [(1, "EMP2002"), (1, "EMP2003")]
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_trend_gaps_moving_average_and_errors(api):
    create(api, "EMP4001", department="Trend", joined_on="2026-07-07")
    create(api, "EMP4002", department="Trend", joined_on="2026-07-09")
    # Monday 6 July 2026 has no rows. Tuesday has one present employee of two who had joined.
    log(
        emp_code="EMP4001",
        date="2026-07-07",
        punch_in=utc(2026, 7, 7, 4, 0),
        punch_out=utc(2026, 7, 7, 12, 0),
        work_hours=8,
        late_minutes=12,
    )
    response = api.get(
        "/analytics/departments/Trend/trend",
        params={"from": "2026-07-06", "to": "2026-07-12"},
    )
    assert response.status_code == 200, response.text
    rows = {row["date"]: row for row in response.json()["items"]}
    assert list(rows) == [
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]
    assert rows["2026-07-06"]["is_working_day"] is True
    assert rows["2026-07-06"]["headcount"] == 0
    assert rows["2026-07-06"]["attendance_rate"] is None
    assert rows["2026-07-06"]["moving_avg_7d"] is None
    assert rows["2026-07-07"]["headcount"] == 1
    assert rows["2026-07-07"]["present_count"] == 1
    assert rows["2026-07-07"]["late_count"] == 1
    assert rows["2026-07-07"]["attendance_rate"] == 1
    assert rows["2026-07-07"]["moving_avg_7d"] == 1
    assert rows["2026-07-08"]["headcount"] == 1
    assert rows["2026-07-08"]["present_count"] == 0
    assert rows["2026-07-08"]["attendance_rate"] == 0
    assert rows["2026-07-08"]["moving_avg_7d"] == 0.5
    assert rows["2026-07-09"]["headcount"] == 2
    assert rows["2026-07-09"]["attendance_rate"] == 0
    assert rows["2026-07-09"]["moving_avg_7d"] == 0.3333
    assert rows["2026-07-11"]["is_working_day"] is False
    assert rows["2026-07-11"]["attendance_rate"] is None
    # Saturday's window is Mon-Sat. Null Monday is ignored; Tue 1, Wed 0, Thu 0, Fri 0.
    assert rows["2026-07-11"]["moving_avg_7d"] == 0.25
    assert rows["2026-07-12"]["is_working_day"] is False
    assert rows["2026-07-12"]["moving_avg_7d"] == 0.25

    assert api.get("/analytics/departments/Missing/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).status_code == 404
    assert api.get("/analytics/departments/Trend/trend", params={"from": "2026-07-08", "to": "2026-07-06"}).status_code == 422
    assert api.get("/analytics/departments/Trend/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422
    assert api.get("/analytics/departments/Trend/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200
    assert api.get("/analytics/departments/engineering/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).status_code == 404
    bad_range = api.get("/analytics/departments/Trend/trend", params={"from": "2026-07-08", "to": "2026-07-06"})
    assert isinstance(bad_range.json()["detail"], list)


def test_trend_rates_and_moving_average_round_half_up(api):
    for code in ("EMP5001", "EMP5002", "EMP5003"):
        create(api, code, department="Thirds", joined_on="2026-01-01")
    # Monday: one half day of three people -> 0.5 / 3 = 0.16666... -> 0.1667.
    log(
        emp_code="EMP5001",
        date="2026-07-06",
        punch_in=utc(2026, 7, 6, 4, 0),
        punch_out=utc(2026, 7, 6, 7, 0),
        work_hours=3,
        half_day=True,
    )
    rows = api.get(
        "/analytics/departments/Thirds/trend",
        params={"from": "2026-07-06", "to": "2026-07-07"},
    ).json()["items"]
    assert rows[0]["present_count"] == 0.5
    assert rows[0]["attendance_rate"] == 0.1667
    assert rows[0]["moving_avg_7d"] == 0.1667
    # Tuesday has no rows: rate 0, and (0.1667 + 0) / 2 = 0.08335 -> 0.0834 half-up.
    assert rows[1]["attendance_rate"] == 0
    assert rows[1]["moving_avg_7d"] == 0.0834


def assert_index_only(body: dict):
    text = json.dumps(body)
    assert "IXSCAN" in text, text[:500]
    assert "COLLSCAN" not in text


def test_explain_contract_and_index_use(api):
    create(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(2026, 7, 6, 9, 45)})
    cases = [
        ("/admin/explain/attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 20}, "attendance_logs"),
        ("/admin/explain/employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "attendance_logs"),
        ("/admin/explain/department_summary", {"month": "2026-07"}, "employees"),
        ("/admin/explain/department_summary", {"month": "2026-07", "department": "Engineering"}, "employees"),
        ("/admin/explain/late_leaderboard", {"month": "2026-07", "limit": 5}, "attendance_logs"),
        ("/admin/explain/department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}, "attendance_logs"),
    ]
    for path, params, collection in cases:
        response = api.get(path, params=params)
        assert response.status_code == 200, (path, params, response.text)
        body = response.json()
        assert set(body) == {"endpoint", "collection", "explain"}
        assert body["collection"] == collection
        assert isinstance(body["explain"], dict)
        assert_index_only(body["explain"])

    assert api.get("/admin/explain/not_a_query").status_code == 422
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/department_summary").status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Engineering"}).status_code == 422
    assert api.get(
        "/admin/explain/attendance_list",
        params={"date_from": "2026-07-31", "date_to": "2026-07-01"},
    ).status_code == 422


def test_explain_on_a_large_collection(api):
    day0 = date(2024, 1, 1)
    employees = []
    for number in range(1, 401):
        employees.append({
            "emp_code": f"EMP{number:04d}",
            "name": f"Person {number}",
            "email": f"p{number}@example.com",
            "department": f"Dept{number % 4}",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2023-01-01",
            "created_at": utc(2023, 1, 1, 0, 0),
        })
    db.employees.insert_many(employees)
    batch = []
    total = 100_000
    for index in range(total):
        batch.append({
            "emp_code": f"EMP{(index % 400) + 1:04d}",
            "date": (day0 + timedelta(days=index // 400)).isoformat(),
            "status": "ON_DUTY" if index % 23 == 0 else "PRESENT",
            "punch_in": utc(2024, 1, 1, 4, 0),
            "punch_out": utc(2024, 1, 1, 12, 0),
            "work_hours": 8,
            "late_minutes": 20 if index % 17 == 0 else 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })
        if len(batch) == 5000:
            db.attendance_logs.insert_many(batch, ordered=False)
            batch = []
    if batch:
        db.attendance_logs.insert_many(batch, ordered=False)

    listed = api.get("/attendance", params={"date_from": "2024-03-01", "date_to": "2024-03-31", "page_size": 50})
    assert listed.status_code == 200
    assert listed.json()["total"] > 0
    summary = api.get("/analytics/departments/summary", params={"month": "2024-03"})
    assert summary.status_code == 200, summary.text
    assert summary.json()["items"]
    board = api.get("/analytics/leaderboard/late", params={"month": "2024-03", "limit": 10})
    assert board.status_code == 200
    assert board.json()["items"]
    trend = api.get("/analytics/departments/Dept0/trend", params={"from": "2024-03-01", "to": "2024-03-14"})
    assert trend.status_code == 200, trend.text
    assert len(trend.json()["items"]) == 14

    for path, params in (
        ("/admin/explain/attendance_list", {"date_from": "2024-03-01", "date_to": "2024-03-31", "status": "PRESENT"}),
        ("/admin/explain/employee_monthly", {"emp_code": "EMP0001", "month": "2024-03"}),
        ("/admin/explain/department_summary", {"month": "2024-03"}),
        ("/admin/explain/late_leaderboard", {"month": "2024-03", "department": "Dept0"}),
        ("/admin/explain/department_trend", {"department": "Dept1", "from": "2024-03-01", "to": "2024-03-31"}),
    ):
        response = api.get(path, params=params)
        assert response.status_code == 200, (path, response.text[:500])
        assert_index_only(response.json()["explain"])

    # The summary's $lookup into attendance_logs must also be index-backed.
    summary_plan = json.dumps(
        api.get("/admin/explain/department_summary", params={"month": "2024-03"}).json()["explain"]
    )
    assert "uq_attendance_employee_date" in summary_plan
