# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB to manage employee records, attendance, punch-in/punch-out operations, and attendance analytics.

## Tech Stack

- Python and FastAPI
- MongoDB and PyMongo
- Pydantic for request validation
- Uvicorn for running the application

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create your `.env` file from the example:

   ```bash
   cp .env.example .env
   ```

   Update `MONGO_URI` and `MONGO_DB` in `.env` according to your MongoDB configuration.

4. Start the application:

   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. Open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) to explore the API documentation.

## Implemented and Fixed APIs :

- GET /health
- POST /employees
- GET /employees
- POST /attendance/punch-in
- GET /attendance
- POST /attendance/punch-out
- PATCH /attendance/{emp_code}/{date}
- GET /analytics/employees/{emp_code}/monthly
- GET /analytics/departments/summary
- GET /analytics/leaderboard/late

## APIs Not Implemented :

- GET /analytics/departments/{department}/trend
- GET /admin/explain/{endpoint}
