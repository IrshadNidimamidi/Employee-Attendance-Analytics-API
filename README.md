# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for employee attendance tracking with punch-in/out, manual corrections with audit trail, and analytics endpoints.

## Stack
- Python 3.11+
- FastAPI
- MongoDB 6.0+ (tested on Atlas M0 / MongoDB 7)
- PyMongo 4.x
- Pydantic v2

## Local Setup

```bash
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate        # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables (create a .env file)
# MONGO_URI=mongodb://localhost:27017   (local)
# MONGO_URI=mongodb+srv://...           (Atlas)
# MONGO_DB=attendance_db

# 4. Seed sample data
python sample_seed.py

# 5. Start the server
uvicorn app.main:app --port 8000 --reload
```

## API Docs
Once running, visit: http://localhost:8000/docs

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /health | MongoDB ping check |
| POST | /employees | Create employee |
| GET | /employees | List employees (paginated) |
| POST | /attendance/punch-in | Record punch-in |
| POST | /attendance/punch-out | Record punch-out |
| GET | /attendance | List attendance records |
| PATCH | /attendance/{emp_code}/{date} | Regularize a record |
| GET | /analytics/employees/{emp_code}/monthly | Monthly summary |
| GET | /analytics/departments/summary | Department summary |
| GET | /analytics/leaderboard/late | Late-comers leaderboard |
| GET | /analytics/departments/{department}/trend | Daily trend |
| GET | /admin/explain/{endpoint} | MongoDB explain output |

## What's implemented
- All 12 endpoints from the OpenAPI spec
- Business rules R1–R10 (IST timezone, late grace period, overtime, half-day, working days, pagination)
- Audit trail on manual corrections (history array)
- Startup indexes for all query patterns
- Concurrent punch-in/out safety via unique indexes and atomic updates

## What's missing
Nothing — full implementation submitted.
