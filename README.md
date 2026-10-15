# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee attendance tracking, regularization lifecycle management, and analytics (leaderboards, monthly summaries, and department trends).

## 1. Project Overview

This API manages the complete attendance lifecycle for organizations:
- **Employee Management**: Register employees with configurable shifts (including overnight shifts) and list employees with pagination.
- **Attendance Lifecycle**: Race-safe punch-in, punch-out with overtime, work hour, and half-day computations, and auditable regularizations (manual corrections).
- **Analytics Engine**: Native MongoDB aggregation pipelines computing monthly employee summaries, department cross-collection aggregates, latecomer competition leaderboards with window functions, and daily moving averages with gap-filling (`$densify`).
- **Explain Endpoint**: Query execution plan introspection (`executionStats`) validating index scans (`IXSCAN`) across query paths.

## 2. Prerequisites

- **Python**: 3.11+
- **MongoDB**: 6.0+ (Tested against MongoDB 7.0)

## 3. Installation Instructions

1. Clone the repository and navigate into the root directory:
   ```bash
   git clone https://github.com/karthikesh21/Employee-attendance-system.git
   cd Employee-attendance-system
   ```

2. Create and activate a Python 3.11 virtual environment:
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. Install all dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## 4. Environment Variable Configuration

The application reads configuration from environment variables (environment variables take precedence over any `.env` file):

| Variable | Default Value | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI string |
| `MONGO_DB` | `attendance_db` | MongoDB target database name |

Example for bash/zsh:
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

Example for Windows PowerShell:
```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

## 5. How to Start MongoDB

- **Docker**:
   ```bash
   docker run -d --name mongo7 -p 27017:27017 mongo:7
   ```
- **Local MongoDB**:
   ```bash
   mongod --dbpath <path_to_data_dir> --port 27017
   ```
- **MongoDB Atlas**:
   Provide your Atlas connection string via `MONGO_URI`.

## 6. Database Initialization & Indexing

The application manages database indexes automatically on startup. During application lifespan startup, `ensure_indexes()` creates all required unique constraints and compound indexes idempotently:
- `employees`: Unique index on `emp_code`, compound index on `(department, joined_on)`.
- `attendance_logs`: Unique compound index on `(emp_code, date)`, compound index on `(date desc, emp_code asc)`, index on `(emp_code, punch_in desc)`, and index on `(date, late_minutes desc)`.

No manual database initialization or migration script is required. Pointing the application to a running MongoDB instance via `MONGO_URI` and starting the server initializes all necessary indexes. Data records are created and managed directly through the REST API endpoints.

## 7. How to Run the FastAPI Application

Start the Uvicorn development server from the repository root:
```bash
uvicorn app.main:app --port 8000
```
Or with auto-reload:
```bash
uvicorn app.main:app --port 8000 --reload
```

## 8. How to Access Swagger UI

Once the application is running, access interactive OpenAPI documentation and test endpoints at:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## 9. How to Run Tests

The test suite is located in `tests/test_api.py` and exercises all API endpoints, business logic rules (R1–R8), concurrency handling, error responses, and database execution plans (`IXSCAN`).

Tests run against the MongoDB instance specified in `MONGO_URI` and `MONGO_DB` (defaulting to `attendance_db` on `mongodb://localhost:27017`). Ensure MongoDB is accessible before running tests.

Run tests using `pytest` within the activated virtual environment:
```bash
pytest -v
```
or via the Python module:
```bash
python -m pytest -v
```

## 10. Implemented Endpoints

### System & Health
- `GET /health` - Readiness probe verifying MongoDB connectivity with `ping`.

### Employees
- `POST /employees` - Register an employee with unique constraint enforcement on `emp_code`.
- `GET /employees` - Paginated and department-filtered employee list, sorted by `emp_code` ascending.

### Attendance
- `POST /attendance/punch-in` - Record punch-in, enforcing IST attendance dates (R1), late calculations (R2), and atomic concurrency control.
- `POST /attendance/punch-out` - Record punch-out, calculating work hours (R4), overtime (R3), and half-day status (R5) across midnight.
- `GET /attendance` - Query attendance history with date ranges, status filters, and index-backed sorting (`date` desc, `emp_code` asc).
- `PATCH /attendance/{emp_code}/{date}` - Regularize attendance records, recomputing derived fields and appending audit history entries.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Aggregated monthly metrics (working days, present days, late count, overtime, attendance percentage).
- `GET /analytics/departments/summary` - Cross-collection department summary counting headcount (including employees with zero logs) and record-level average work hours.
- `GET /analytics/leaderboard/late` - Latecomer rankings using standard competition ranking (`$setWindowFields` `$rank`) with post-ranking limits and tie resolution.
- `GET /analytics/departments/{department}/trend` - Daily series with gap filling (`$densify`) and 7-day moving averages (`$setWindowFields`).

### Admin
- `GET /admin/explain/{endpoint}` - Return raw `explain` execution stats for endpoint query plans, validating `IXSCAN` index usage.

## 11. Known Limitations

- In accordance with the project guidelines, authentication and authorization are out of scope (no JWT or API tokens).
- Working days are defined strictly as Monday through Friday (Rule R7); public holiday calendars are intentionally not configured.
