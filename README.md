# Employee Attendance & Analytics API

FastAPI service for punch-in, punch-out, manual attendance corrections, and MongoDB analytics. Calendar dates and shifts are IST. Instants in the API are epoch milliseconds and are stored as UTC datetimes.

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (the assignment grader uses 7)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start MongoDB locally, for example:

```bash
docker run --name attendance-mongo -p 27017:27017 mongo:7
```

The app reads connection settings from the environment. A `.env` file is optional and never overrides variables that are already set. Do not commit `.env`.

| Variable | Meaning | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

```bash
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
# PowerShell: $env:MONGO_URI='mongodb://localhost:27017'; $env:MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Indexes are created at startup. If MongoDB is not reachable yet, the app still starts, `GET /health` returns 503, and the first successful health check creates the indexes before it returns 200. Interactive docs are at `http://127.0.0.1:8000/docs`.

## Endpoints

- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` appends one history entry and recomputes derived fields
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}` returns MongoDB `executionStats` for that route's query

Analytics read stored `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day`. They do not recompute those fields from punch times. Same-day punch-ins are rejected by a unique `(emp_code, date)` index. Punch-out and regularization use a conditional update so a concurrent request cannot both succeed.

## Tests

Tests talk to a real MongoDB. They set the database name to `hrone_attendance_test` and drop it. They use `TEST_MONGO_URI` when that is set, otherwise `mongodb://127.0.0.1:27017`. They do not use `attendance_db`.

```bash
pytest
```

The suite has 31 tests, including threaded races for create, punch-in, punch-out, and regularization, and one test that loads 100,000 logs and checks every explain plan for `IXSCAN` and no `COLLSCAN`. If MongoDB is not reachable, the tests are skipped with that reason.

## Design notes

See `REVIEW.md` for the starter defects and `DECISIONS.md` for indexes, races, ties, headcount, and scale.

Reported numbers use half-up rounding. MongoDB's `$round` is half-even, so the pipelines keep exact `Decimal128` values and the last rounding step uses Python `Decimal`.

Interpretations where the contract leaves room:

- `attendance_pct` follows R8's default of 2 decimals (`2/23*100` is `8.70`). The 4-decimal rule is applied to the 0-1 rates in the trend.
- `moving_avg_7d` is the mean of the reported (4-decimal) `attendance_rate` values in the window.
- Business-rule 422s (for example, punch-out before punch-in) use the same list-shaped `detail` as FastAPI's schema errors, which is what the contract's 422 schema describes. 404, 409, and 503 use a string `detail`.
- `punched_at`, `punch_in`, `punch_out`, and `status` may be omitted, but an explicit `null` is 422 because the schema does not allow null.
- The department trend fetches the department's `emp_code` list with one indexed query, then matches logs with `$in` on the unique `(emp_code, date)` index. The daily rows, gap filling, headcount, and moving average are computed in the aggregation.

## Limitations

- There is no authentication. `regularized_by` is the actor recorded on a correction.
- Writes are single-document conditional updates. This does not start a multi-document transaction, and a standalone MongoDB server would reject one.
- Deep pages use `skip`. The sort is indexed; a very large page number still walks that index.
- The department trend looks up headcount once per day in the requested range (at most 92 days).
